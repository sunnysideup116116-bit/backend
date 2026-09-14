import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from services import post_date_followup_service as service


class _Cursor(list):
    def sort(self, *_args):
        return self

    def limit(self, _limit):
        return self


class PostDateFollowupTests(unittest.TestCase):
    def test_pending_record_is_cancelled_when_date_is_cancelled(self):
        rows = _Cursor([{"_id": "followup-1", "event_id": "date-1", "status": "pending"}])
        with patch.object(service.POST_DATE_FOLLOWUPS, "find", return_value=rows), \
             patch.object(service.calendar_events_coll, "find_one", return_value={"status": "cancelled"}), \
             patch.object(service.POST_DATE_FOLLOWUPS, "update_one") as update:
            service._reconcile_pending_records(5000)
        self.assertEqual(update.call_args.args[1]["$set"]["status"], "cancelled")

    def _event(self):
        return {
            "_id": "event-doc", "event_id": "date-1", "source_type": "date",
            "status": "confirmed", "coordination_id": "coord-1", "match_id": "match-1",
            "participants": ["owner", "other"],
            "end_at": datetime.fromtimestamp(1000, timezone.utc), "revision": 2,
        }

    def test_confirmed_date_delivers_once_to_each_private_room(self):
        match = {"_id": "match-1", "from_user": "owner", "to_user": "other"}
        claimed = {"_id": "claim", "status": "processing"}
        with patch.object(service, "followup_mode_for_user", return_value="on"), \
             patch.object(service, "_rollout_started_at", return_value=0), \
             patch.object(service.calendar_events_coll, "find", return_value=_Cursor([self._event()])) as find, \
             patch.object(service.POST_DATE_FOLLOWUPS, "find_one", return_value=None), \
             patch.object(service.POST_DATE_FOLLOWUPS, "find_one_and_update", return_value=claimed), \
             patch.object(service.POST_DATE_FOLLOWUPS, "update_one"), \
             patch.object(service.profiles_coll, "find_one", return_value={"proactive_care_enabled": True, "mediator_calendar_access": False}), \
             patch.object(service.profiles_coll, "update_one"), \
             patch.object(service, "_valid_match", return_value=match), \
             patch.object(service, "next_quiet_end", return_value=None), \
             patch.object(service, "save_system_message_once", return_value={"message_id": "m1", "created": True}) as save, \
             patch.object(service.matches_coll, "update_one") as unread:
            stats = service.run_post_date_followups_once(now=5000)

        self.assertEqual(stats["delivered"], 2)
        self.assertEqual(
            {call.args[0] for call in save.call_args_list},
            {"mediator_private::owner::other", "mediator_private::other::owner"},
        )
        self.assertEqual(unread.call_count, 2)
        self.assertEqual(find.call_args.args[0]["source_type"], "date")

    def test_shadow_records_candidates_without_sending(self):
        with patch.object(service, "followup_mode_for_user", return_value="shadow"), \
             patch.object(service.calendar_events_coll, "find", return_value=_Cursor([self._event()])), \
             patch.object(service.POST_DATE_FOLLOWUPS, "find_one", return_value=None), \
             patch.object(service.POST_DATE_FOLLOWUPS, "update_one") as update, \
             patch.object(service, "save_system_message_once") as save:
            stats = service.run_post_date_followups_once(now=5000)

        self.assertEqual(stats["shadowed"], 2)
        self.assertEqual(update.call_count, 2)
        save.assert_not_called()

    def test_cancelled_or_unverified_relationship_is_never_delivered(self):
        with patch.object(service, "followup_mode_for_user", return_value="on"), \
             patch.object(service, "_rollout_started_at", return_value=0), \
             patch.object(service.calendar_events_coll, "find", return_value=_Cursor([self._event()])), \
             patch.object(service.POST_DATE_FOLLOWUPS, "find_one", return_value=None), \
             patch.object(service.POST_DATE_FOLLOWUPS, "update_one"), \
             patch.object(service.profiles_coll, "find_one", return_value={"proactive_care_enabled": True}), \
             patch.object(service, "_valid_match", return_value=None), \
             patch.object(service, "save_system_message_once") as save:
            stats = service.run_post_date_followups_once(now=5000)
        self.assertEqual(stats["skipped"], 2)
        save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
