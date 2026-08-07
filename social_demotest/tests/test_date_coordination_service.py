import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi import HTTPException
from services.date_coordination_service import cancel_coordination_or_event, request_reschedule, withdraw_reschedule


class DateCoordinationDomainTests(unittest.TestCase):
    def _match(self, *, status="completed", revision=3):
        return {
            "_id": "match",
            "from_user": "owner",
            "to_user": "other",
            "date_coordination": {
                "coordination_id": "coord",
                "calendar_event_id": "event",
                "status": status,
                "revision": revision,
                "form": {
                    "date": "2026-08-03", "start_time": "14:00", "end_time": "15:00",
                    "activity": "喝咖啡",
                },
            },
        }

    def _event(self, *, status="confirmed", revision=3):
        return {
            "_id": "event-doc",
            "event_id": "event",
            "source_type": "date",
            "participants": ["owner", "other"],
            "status": status,
            "revision": revision,
            "title": "與對方的約會",
            "activity": "喝咖啡",
            "start_at": datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc),
            "end_at": datetime(2026, 8, 3, 7, 0, tzinfo=timezone.utc),
            "timezone": "Asia/Taipei",
        }

    def test_cancel_updates_shared_projection_and_notifies_other_once(self):
        match = self._match()
        updated_match = self._match(status="cancelled")
        updated_match["date_coordination"]["cancelled_by"] = "owner"
        calendar_write = Mock(modified_count=1)
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch("services.date_coordination_service.calendar_events_coll.find_one", return_value=self._event()), \
             patch("services.date_coordination_service.calendar_events_coll.update_one", return_value=calendar_write) as event_write, \
             patch("services.date_coordination_service.matches_coll.find_one_and_update", return_value=updated_match), \
             patch("services.date_coordination_service._sync_card") as sync, \
             patch("services.date_coordination_service.queue_mediator_event") as notify:
            result = cancel_coordination_or_event(
                "owner", "other", "coord", expected_revision=3, idempotency_key="run:1",
            )
        self.assertEqual(result["status"], "cancelled")
        event_write.assert_called_once()
        sync.assert_called_once()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "other")
        self.assertEqual(notify.call_args.args[2], "date_coordination_cancelled")

    def test_reschedule_sets_pending_change_resets_confirmations_and_notifies(self):
        match = self._match()
        updated_match = self._match(status="active", revision=4)
        updated_match["date_coordination"]["form"] = {
            "date": "2026-08-04", "start_time": "16:00", "end_time": "17:00",
            "timezone": "Asia/Taipei", "title": "", "activity": "吃晚餐",
            "location": "", "budget": "", "notes": "",
        }
        updated_match["date_coordination"]["confirmations"] = {}
        pending_event = self._event(status="pending_reconfirmation", revision=4)
        pending_event["pending_change"] = updated_match["date_coordination"]["form"]
        event_write = Mock(modified_count=1)
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch(
                 "services.date_coordination_service.calendar_events_coll.find_one",
                 side_effect=[self._event(), pending_event],
             ), \
             patch("services.date_coordination_service.calendar_events_coll.update_one", return_value=event_write) as write, \
             patch("services.date_coordination_service.matches_coll.find_one_and_update", return_value=updated_match), \
             patch("services.date_coordination_service._sync_card") as sync, \
             patch("services.date_coordination_service.queue_mediator_event") as notify:
            coordination, event = request_reschedule(
                "owner", "other", "event",
                {
                    "date": "2026-08-04", "start_time": "16:00", "end_time": "17:00",
                    "activity": "吃晚餐", "timezone": "Asia/Taipei",
                },
                expected_revision=3,
                idempotency_key="run:2",
            )
        self.assertEqual(coordination["status"], "active")
        self.assertEqual(coordination["revision"], 4)
        self.assertEqual(coordination["confirmations"], {})
        self.assertEqual(event["status"], "pending_reconfirmation")
        write.assert_called_once()
        sync.assert_called_once()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0], "other")
        self.assertEqual(notify.call_args.args[2], "date_coordination_result")

    def test_reschedule_losing_coordination_cas_never_mutates_calendar_projection(self):
        match = self._match()
        with patch(
            "services.date_coordination_service.find_accepted_match",
            side_effect=[match, match],
        ), patch(
            "services.date_coordination_service.calendar_events_coll.find_one",
            side_effect=[self._event(), self._event()],
        ), patch(
            "services.date_coordination_service.matches_coll.find_one_and_update",
            return_value=None,
        ), patch("services.date_coordination_service.calendar_events_coll.update_one") as event_write:
            with self.assertRaises(HTTPException) as raised:
                request_reschedule(
                    "owner", "other", "event",
                    {
                        "date": "2026-08-04", "start_time": "16:00",
                        "end_time": "17:00", "activity": "吃晚餐",
                    },
                    expected_revision=3,
                    idempotency_key="run:lost",
                )
        self.assertEqual(raised.exception.status_code, 409)
        event_write.assert_not_called()

    def test_cancel_rolls_back_coordination_if_calendar_projection_cannot_follow(self):
        match = self._match()
        cancelled_match = self._match(status="cancelled")
        event_write = Mock(modified_count=0)
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch(
                 "services.date_coordination_service.calendar_events_coll.find_one",
                 side_effect=[self._event(), self._event()],
             ), \
             patch("services.date_coordination_service.calendar_events_coll.update_one", return_value=event_write), \
             patch("services.date_coordination_service.matches_coll.find_one_and_update", return_value=cancelled_match), \
             patch("services.date_coordination_service.matches_coll.update_one") as rollback:
            with self.assertRaises(HTTPException) as raised:
                cancel_coordination_or_event(
                    "owner", "other", "coord",
                    expected_revision=3, idempotency_key="run:cancel-race",
                )
        self.assertEqual(raised.exception.status_code, 409)
        rollback.assert_called_once()

    # --- withdraw_reschedule ---

    def _reschedule_match(self):
        match = self._match()
        match["date_coordination"].update({
            "status": "active", "revision": 4, "confirmations": {},
            "rescheduling_event_id": "event", "last_action_key": "reschedule:1",
            "form": {
                "date": "2026-08-04", "start_time": "16:00", "end_time": "17:00",
                "timezone": "Asia/Taipei", "title": "", "activity": "吃晚餐",
                "location": "", "budget": "", "notes": "",
            },
        })
        return match

    def _reschedule_event(self, *, revision=4):
        event = self._event(status="pending_reconfirmation", revision=revision)
        event["pending_change"] = {
            "date": "2026-08-04", "start_time": "16:00", "end_time": "17:00",
            "activity": "吃晚餐",
        }
        event["last_agent_action_key"] = "reschedule:1"
        return event

    def test_withdraw_restores_confirmed_event_and_completed_coordination(self):
        match = self._reschedule_match()
        restored_match = self._reschedule_match()
        restored_match["date_coordination"]["status"] = "completed"
        restored_match["date_coordination"]["form"] = {
            "date": "2026-08-03", "start_time": "14:00", "end_time": "15:00",
            "timezone": "Asia/Taipei", "title": "", "activity": "喝咖啡",
            "location": "", "budget": "", "notes": "",
        }
        restored_match["date_coordination"]["confirmations"] = {"owner": True, "other": True}
        restored_match["date_coordination"].pop("rescheduling_event_id", None)
        event_write = Mock(modified_count=1)
        confirmed_event = self._event(status="confirmed", revision=5)
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch(
                 "services.date_coordination_service.calendar_events_coll.find_one",
                 side_effect=[self._reschedule_event(), confirmed_event],
             ), \
             patch("services.date_coordination_service.calendar_events_coll.update_one", return_value=event_write) as event_update, \
             patch("services.date_coordination_service.matches_coll.find_one_and_update", return_value=restored_match), \
             patch("services.date_coordination_service._sync_card") as sync:
            coordination, event = withdraw_reschedule(
                "owner", "other", "event", expected_revision=4, idempotency_key="wd:1",
            )
        self.assertEqual(coordination["status"], "completed")
        self.assertEqual(coordination["form"]["date"], "2026-08-03")
        self.assertEqual(coordination["confirmations"], {"owner": True, "other": True})
        self.assertNotIn("rescheduling_event_id", coordination)
        self.assertEqual(event["status"], "confirmed")
        event_update.assert_called_once()
        args, kwargs = event_update.call_args
        self.assertIn("status", args[1]["$set"])
        self.assertEqual(args[1]["$set"]["status"], "confirmed")
        self.assertIn("$inc", args[1])
        self.assertIn("$unset", args[1])
        sync.assert_called_once()

    def test_withdraw_losing_event_cas_rolls_back_coordination(self):
        match = self._reschedule_match()
        event_write = Mock(modified_count=0)
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch(
                 "services.date_coordination_service.calendar_events_coll.find_one",
                 side_effect=[self._reschedule_event(), self._reschedule_event()],
             ), \
             patch("services.date_coordination_service.calendar_events_coll.update_one", return_value=event_write), \
             patch("services.date_coordination_service.matches_coll.find_one_and_update", return_value=match), \
             patch("services.date_coordination_service.matches_coll.update_one") as rollback:
            with self.assertRaises(HTTPException) as raised:
                withdraw_reschedule(
                    "owner", "other", "event", expected_revision=4, idempotency_key="wd:lost",
                )
        self.assertEqual(raised.exception.status_code, 409)
        rollback.assert_called_once()

    def test_withdraw_stale_revision_rejected_without_writes(self):
        match = self._reschedule_match()
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch(
                 "services.date_coordination_service.calendar_events_coll.find_one",
                 return_value=self._reschedule_event(),
             ), \
             patch("services.date_coordination_service.matches_coll.find_one_and_update") as match_update, \
             patch("services.date_coordination_service.calendar_events_coll.update_one") as event_write:
            with self.assertRaises(HTTPException) as raised:
                withdraw_reschedule(
                    "owner", "other", "event", expected_revision=1, idempotency_key="wd:stale",
                )
        self.assertEqual(raised.exception.status_code, 409)
        match_update.assert_not_called()
        event_write.assert_not_called()

    def test_withdraw_idempotent_retry_returns_current_without_rewrite(self):
        match = self._reschedule_match()
        match["date_coordination"]["last_action_key"] = "wd:1"
        event = self._reschedule_event()
        event["last_agent_action_key"] = "wd:1"
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch(
                 "services.date_coordination_service.calendar_events_coll.find_one",
                 return_value=event,
             ), \
             patch("services.date_coordination_service.matches_coll.find_one_and_update") as match_update, \
             patch("services.date_coordination_service.calendar_events_coll.update_one") as event_write:
            coordination, returned_event = withdraw_reschedule(
                "owner", "other", "event", expected_revision=4, idempotency_key="wd:1",
            )
        self.assertEqual(returned_event["status"], "pending_reconfirmation")
        match_update.assert_not_called()
        event_write.assert_not_called()

    def test_withdraw_idempotent_retry_after_success_returns_restored_state(self):
        # 成功撤回後 coordination 是 completed、event 已回 confirmed；
        # 重送同一 key 必須回傳現況而不是 409。
        match = self._reschedule_match()
        match["date_coordination"]["status"] = "completed"
        match["date_coordination"]["confirmations"] = {"owner": True, "other": True}
        match["date_coordination"]["form"] = {
            "date": "2026-08-03", "start_time": "14:00", "end_time": "15:00",
            "timezone": "Asia/Taipei", "title": "", "activity": "喝咖啡",
            "location": "", "budget": "", "notes": "",
        }
        match["date_coordination"]["last_action_key"] = "wd:1"
        match["date_coordination"].pop("rescheduling_event_id", None)
        event = self._reschedule_event()
        event["status"] = "confirmed"
        event["last_agent_action_key"] = "wd:1"
        with patch("services.date_coordination_service.find_accepted_match", return_value=match), \
             patch(
                 "services.date_coordination_service.calendar_events_coll.find_one",
                 return_value=event,
             ), \
             patch("services.date_coordination_service.matches_coll.find_one_and_update") as match_update, \
             patch("services.date_coordination_service.calendar_events_coll.update_one") as event_write:
            coordination, returned_event = withdraw_reschedule(
                "owner", "other", "event", expected_revision=4, idempotency_key="wd:1",
            )
        self.assertEqual(coordination["status"], "completed")
        self.assertEqual(returned_event["status"], "confirmed")
        match_update.assert_not_called()
        event_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
