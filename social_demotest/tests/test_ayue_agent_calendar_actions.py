import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch

from fastapi import HTTPException
from services.ayue_agent.contracts import AgentDecision, AgentIntent, AgentTurnContext, AgentTurnContextV2, DecisionKind
from services.ayue_agent.router import guard_v2_decision, tool_policy_for_turn
from services.ayue_agent.runtime import (
    _calendar_details_question,
    _execute_calendar_pending,
    _prepare_calendar_confirmation,
)
from services.calendar_service import cancel_event, normalize_form, update_personal_event


class AyueAgentCalendarActionTests(unittest.TestCase):
    def test_normalize_form_preserves_personal_event_title(self):
        form = normalize_form({
            "title": "和小安喝咖啡", "date": "2026-08-03", "start_time": "14:00", "end_time": "15:30",
        })
        self.assertEqual(form["title"], "和小安喝咖啡")

    def test_create_never_invents_a_missing_end_time(self):
        self.assertEqual(
            _calendar_details_question("calendar.create_my_event", {
                "title": "和小安喝咖啡", "date": "2026-08-03", "start_time": "14:00",
            }),
            "可以，還需要結束時間，才能幫你新增這筆行程。",
        )

    def test_calendar_create_requires_confirmation_but_no_read(self):
        message = "幫我在 8/3 下午兩點到三點半新增和小安喝咖啡"
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message=message)
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.CALENDAR_ACTION,
            tool_name="calendar.create_my_event", confidence=.95, evidence_span=message,
            arguments={
                "title": "和小安喝咖啡", "date": "2026-08-03", "start_time": "14:00", "end_time": "15:30",
            },
        )
        self.assertEqual(guard_v2_decision(ctx, tool_policy_for_turn(ctx), decision), (True, "allowed"))

    def test_calendar_update_must_read_before_confirmation(self):
        message = "把週三的咖啡改到三點"
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message=message)
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.CALENDAR_ACTION,
            tool_name="calendar.update_my_event", confidence=.95, evidence_span=message,
            arguments={"event_hint": "週三的咖啡", "start_time": "15:00"},
        )
        self.assertEqual(
            guard_v2_decision(ctx, tool_policy_for_turn(ctx), decision),
            (False, "calendar_target_requires_read"),
        )
        self.assertEqual(
            guard_v2_decision(ctx, tool_policy_for_turn(ctx), decision, has_calendar_observation=True),
            (True, "allowed"),
        )

    def test_create_confirmation_stores_only_a_draft_before_write(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="新增行程")
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.CALENDAR_ACTION,
            tool_name="calendar.create_my_event", confidence=.95, evidence_span="新增行程",
            arguments={
                "title": "和小安喝咖啡", "date": "2026-08-03", "start_time": "14:00", "end_time": "15:30",
            },
        )
        write = Mock()
        write.modified_count = 1
        trace = {"confirmation": None}
        with patch("services.calendar_service.calendar_access_enabled", return_value=True), \
             patch("services.calendar_service.conflicts_for_viewer", return_value=[]), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one", return_value=write) as persist:
            result = _prepare_calendar_confirmation(ctx, None, decision, trace, "run")
        self.assertIn("回覆「確認」", result.reply)
        pending = persist.call_args.args[1]["$set"]["agentic_pending_confirmation"]
        self.assertEqual(pending["action"], "calendar.create_my_event")
        self.assertIsNone(pending["event_id"])
        self.assertEqual(trace["confirmation"], "created")

    def test_shared_date_cancel_confirmation_keeps_executor_owned_relationship_data(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="取消做愛")
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.CALENDAR_ACTION,
            tool_name="calendar.cancel_my_event", confidence=.95, evidence_span="取消做愛",
            arguments={"event_hint": "做愛"},
        )
        shared = {
            "event_id": "shared-event", "source_type": "date",
            "participants": ["owner", "other"], "coordination_id": "coord",
            "revision": 4, "status": "confirmed", "title": "與對方的約會", "activity": "做愛",
            "start_at": datetime(2026, 7, 30, 14, 29, tzinfo=timezone.utc),
            "end_at": datetime(2026, 7, 30, 15, 29, tzinfo=timezone.utc),
            "timezone": "Asia/Taipei",
        }
        write = Mock(modified_count=1)
        with patch("services.calendar_service.calendar_access_enabled", return_value=True), \
             patch("services.calendar_service.resolve_owned_event", return_value=(shared, None)), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one", return_value=write) as persist:
            result = _prepare_calendar_confirmation(ctx, None, decision, {"confirmation": None}, "run")
        self.assertIn("共同約會", result.reply)
        self.assertIn("通知對方", result.reply)
        pending = persist.call_args.args[1]["$set"]["agentic_pending_confirmation"]
        self.assertEqual(pending["event_source_type"], "date")
        self.assertEqual(pending["event_other_id"], "other")
        self.assertEqual(pending["coordination_id"], "coord")

    def test_shared_date_cancel_uses_coordination_domain_service(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        pending = {
            "action": "calendar.cancel_my_event", "event_source_type": "date",
            "event_id": "shared-event", "event_revision": 4,
            "event_other_id": "other", "coordination_id": "coord",
            "arguments": {"event_hint": "做愛"},
        }
        coordination = {"form": {"activity": "做愛"}}
        with patch(
            "services.date_coordination_service.cancel_coordination_or_event",
            return_value=coordination,
        ) as cancel:
            ok, reply, code = _execute_calendar_pending(ctx, pending, "confirmation")
        self.assertTrue(ok)
        self.assertIsNone(code)
        self.assertIn("雙方行事曆", reply)
        self.assertIn("對方已收到通知", reply)
        cancel.assert_called_once_with(
            "owner", "other", "coord", expected_revision=4,
            idempotency_key="calendar-confirmation:confirmation",
        )

    def test_calendar_success_reply_uses_event_timezone_not_utc_slice(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        pending = {
            "action": "calendar.create_my_event",
            "arguments": {
                "title": "喝咖啡", "date": "2026-08-03",
                "start_time": "14:00", "end_time": "15:00",
            },
        }
        serialized = {
            "source_type": "personal", "title": "喝咖啡",
            "start_at": "2026-08-03T06:00:00+00:00",
            "end_at": "2026-08-03T07:00:00+00:00",
            "timezone": "Asia/Taipei",
        }
        with patch("services.calendar_service.create_personal_event", return_value=serialized):
            ok, reply, _ = _execute_calendar_pending(ctx, pending, "confirmation")
        self.assertTrue(ok)
        self.assertIn("14:00–15:00", reply)
        self.assertNotIn("06:00", reply)

    def test_stale_cancel_confirmation_cannot_claim_someone_elses_cancel_as_its_own(self):
        cancelled = {"_id": "event", "event_id": "event", "status": "cancelled", "revision": 2}
        with patch("services.calendar_service.calendar_events_coll.find_one", return_value=cancelled), \
             patch("services.calendar_service.calendar_events_coll.update_one") as write:
            with self.assertRaises(HTTPException) as raised:
                cancel_event("owner", "event", personal_only=True, expected_revision=1, agent_action_key="run:1")
        self.assertEqual(raised.exception.status_code, 409)
        write.assert_not_called()

    def test_cancelled_personal_event_cannot_be_updated(self):
        with patch("services.calendar_service.calendar_events_coll.find_one", return_value=None), \
             patch("services.calendar_service.calendar_events_coll.update_one") as write:
            with self.assertRaises(HTTPException) as raised:
                update_personal_event("owner", "event", {"title": "新名稱"}, expected_revision=1)
        self.assertEqual(raised.exception.status_code, 404)
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
