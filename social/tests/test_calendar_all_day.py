import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from pydantic import ValidationError

from models import CalendarEventCreateRequest, CalendarEventUpdateRequest
from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.v3.calendar_commands import (
    CalendarCommand,
    normalize_calendar_batch_payload,
    preflight_calendar_commands,
)
from services.ayue_agent.v3.calendar_drafts import merge_command
from services.ayue_agent.v3.write_executors import execute_write
from services.ayue_agent.v3.sub_agents.calendar_agent import _tools_schema
from services.ayue_agent.tools import _calendar_events
from services.calendar_service import (
    _parse_local_interval,
    create_personal_event,
    normalize_form,
    update_personal_event,
)


class CalendarIntervalTests(unittest.TestCase):
    def test_single_day_all_day_uses_exclusive_next_midnight(self):
        start, end, zone = _parse_local_interval({
            "date": "2026-09-04",
            "all_day": True,
            "title": "去非洲",
            "timezone": "Asia/Taipei",
        })

        self.assertEqual(zone, "Asia/Taipei")
        self.assertEqual(start, datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 4, 16, 0, tzinfo=timezone.utc))

    def test_multi_day_all_day_end_date_is_inclusive(self):
        start, end, _zone = _parse_local_interval({
            "date": "2026-09-04",
            "end_date": "2026-09-05",
            "all_day": True,
            "timezone": "Asia/Taipei",
        })

        self.assertEqual(start, datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc))

    def test_timed_event_can_cross_midnight_with_end_date(self):
        start, end, _zone = _parse_local_interval({
            "date": "2026-09-04",
            "start_time": "22:00",
            "end_date": "2026-09-05",
            "end_time": "02:00",
            "timezone": "Asia/Taipei",
        })

        self.assertEqual(start, datetime(2026, 9, 4, 14, 0, tzinfo=timezone.utc))
        self.assertEqual(end, datetime(2026, 9, 4, 18, 0, tzinfo=timezone.utc))

    def test_legacy_same_day_reverse_interval_still_fails(self):
        with self.assertRaises(HTTPException):
            _parse_local_interval({
                "date": "2026-09-04",
                "start_time": "22:00",
                "end_time": "02:00",
            })

    def test_normalize_form_preserves_typed_interval_fields(self):
        form = normalize_form({
            "date": "2026/9/4",
            "end_date": "2026/9/5",
            "all_day": True,
            "start_time": "09:00",
            "end_time": "10:00",
        })

        self.assertEqual(form["date"], "2026-09-04")
        self.assertEqual(form["end_date"], "2026-09-05")
        self.assertTrue(form["all_day"])
        self.assertEqual(form["start_time"], "")
        self.assertEqual(form["end_time"], "")

    def test_public_api_models_accept_all_day_and_cross_day(self):
        all_day = CalendarEventCreateRequest(
            user_id="owner", title="去非洲", date="2026-09-04",
            end_date="2026-09-05", all_day=True,
        )
        cross_day = CalendarEventUpdateRequest(
            user_id="owner", date="2026-09-04", start_time="22:00",
            end_date="2026-09-05", end_time="02:00", all_day=False,
        )

        self.assertTrue(all_day.all_day)
        self.assertEqual(cross_day.end_date, "2026-09-05")

    def test_public_api_rejects_ambiguous_all_day_clock_values(self):
        with self.assertRaises(ValidationError):
            CalendarEventCreateRequest(
                user_id="owner", title="去非洲", date="2026-09-04",
                all_day=True, start_time="09:00", end_time="10:00",
            )

    def test_create_persists_all_day_marker_and_exclusive_boundary(self):
        collection = MagicMock()
        collection.find_one.return_value = None
        with patch("services.calendar_service.calendar_events_coll", collection):
            result = create_personal_event("owner", {
                "title": "去非洲",
                "date": "2026-09-04",
                "end_date": "2026-09-05",
                "all_day": True,
                "timezone": "Asia/Taipei",
            }, agent_action_key="confirmation:c1:0")

        stored = collection.insert_one.call_args.args[0]
        self.assertTrue(stored["all_day"])
        self.assertEqual(stored["start_at"], datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(stored["end_at"], datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc))
        self.assertTrue(result["all_day"])

    def test_direct_update_moves_whole_all_day_range_when_only_start_date_changes(self):
        existing = {
            "_id": "mongo-1",
            "event_id": "event-1",
            "source_type": "personal",
            "participants": ["owner"],
            "title": "去非洲",
            "start_at": datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc),
            "end_at": datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc),
            "all_day": True,
            "timezone": "Asia/Taipei",
            "location": "",
            "notes": "",
            "status": "confirmed",
            "revision": 1,
        }
        updated = dict(existing, revision=2)
        collection = MagicMock()
        collection.find_one.side_effect = [existing, updated]
        collection.update_one.return_value = MagicMock(matched_count=1)
        with patch("services.calendar_service.calendar_events_coll", collection):
            update_personal_event(
                "owner", "event-1", {"date": "2026-09-10"},
                expected_revision=1,
            )

        stored = collection.update_one.call_args.args[1]["$set"]
        self.assertEqual(stored["start_at"], datetime(2026, 9, 9, 16, 0, tzinfo=timezone.utc))
        self.assertEqual(stored["end_at"], datetime(2026, 9, 11, 16, 0, tzinfo=timezone.utc))
        self.assertTrue(stored["all_day"])

    def test_calendar_read_projection_keeps_all_day_semantics(self):
        event = {
            "event_id": "event-1",
            "source_type": "personal",
            "participants": ["owner"],
            "title": "去非洲",
            "start_at": "2026-09-03T16:00:00+00:00",
            "end_at": "2026-09-05T16:00:00+00:00",
            "all_day": True,
            "timezone": "Asia/Taipei",
            "status": "confirmed",
        }
        with patch(
            "services.ayue_agent.tools.calendar_access_enabled", return_value=True,
        ), patch(
            "services.ayue_agent.tools.get_calendar_context",
            return_value={"viewer_events": [event]},
        ):
            result = _calendar_events("owner")

        projected = result.data["events"][0]
        self.assertTrue(projected["all_day"])
        self.assertEqual(projected["date"], "2026-09-04")
        self.assertEqual(projected["end_date"], "2026-09-05")
        self.assertEqual(projected["start_time"], "")
        self.assertEqual(projected["end_time"], "")


class CalendarAllDayCommandTests(unittest.TestCase):
    @staticmethod
    def _ctx():
        return SimpleNamespace(
            user_id="owner",
            clock=SimpleNamespace(local_date="2026-08-24", temporal_references={}),
        )

    def _preflight(self, command):
        with patch(
            "services.ayue_agent.v3.calendar_commands.calendar_access_enabled",
            return_value=True,
        ), patch(
            "services.ayue_agent.v3.calendar_commands.conflicts_for_viewer",
            return_value=[],
        ):
            return preflight_calendar_commands(self._ctx(), [command])

    def test_single_day_all_day_create_reaches_confirmation(self):
        result = self._preflight(CalendarCommand(
            action="create", title="去非洲", date="2026-09-04", all_day=True,
        ))

        self.assertEqual(result.status, "ready")
        self.assertEqual(result.plans[0].form["end_date"], "2026-09-04")
        self.assertTrue(result.plans[0].form["all_day"])
        self.assertIn("9/04 全天", result.preview)
        self.assertIn("回覆「確認」", result.preview)

    def test_calendar_agent_schema_exposes_all_day_and_end_date(self):
        command_tool = next(
            item for item in _tools_schema()
            if item["function"]["name"] == "calendar.submit_commands"
        )
        schema_text = json.dumps(command_tool["function"]["parameters"])

        self.assertIn("all_day", schema_text)
        self.assertIn("end_date", schema_text)

    def test_provider_fields_wrapper_preserves_all_day_range(self):
        payload = normalize_calendar_batch_payload({
            "commands": [{
                "action": "create",
                "fields": {
                    "title": "去非洲",
                    "date": "2026-09-04",
                    "end_date": "2026-09-05",
                    "all_day": True,
                },
            }],
        })
        command = CalendarCommand.model_validate(payload["commands"][0])

        self.assertTrue(command.all_day)
        self.assertEqual(command.end_date, "2026-09-05")

    def test_command_rejects_all_day_mixed_with_clock_values(self):
        with self.assertRaises(ValidationError):
            CalendarCommand(
                action="create", title="去非洲", date="2026-09-04",
                all_day=True, start_time="09:00", end_time="10:00",
            )

    def test_multi_day_all_day_create_reaches_confirmation(self):
        result = self._preflight(CalendarCommand(
            action="create", title="去非洲", date="2026-09-04",
            end_date="2026-09-05", all_day=True,
        ))

        self.assertEqual(result.status, "ready")
        self.assertIn("09/04–09/05 全天", result.preview)

    def test_cross_day_timed_create_reaches_confirmation(self):
        result = self._preflight(CalendarCommand(
            action="create", title="搭夜車", date="2026-09-04", start_time="22:00",
            end_date="2026-09-05", end_time="02:00",
        ))

        self.assertEqual(result.status, "ready")
        self.assertIn("09/04 22:00–09/05 02:00", result.preview)

    def test_all_day_continuation_satisfies_missing_clock_fields(self):
        prior = CalendarCommand(
            action="create", title="去非洲", date="2026-09-04",
        )
        record = {
            "command": prior.model_dump(exclude_none=True),
            "missing_fields": ["start_time", "end_time"],
        }
        merged = merge_command(
            CalendarCommand(action="create", all_day=True, draft_mode="continue"),
            record,
        )
        result = self._preflight(merged)

        self.assertEqual(merged.title, "去非洲")
        self.assertEqual(merged.date, "2026-09-04")
        self.assertTrue(merged.all_day)
        self.assertEqual(result.status, "ready")

    def test_timed_personal_event_can_be_updated_to_all_day(self):
        event = {
            "event_id": "event-1",
            "revision": 2,
            "source_type": "personal",
            "participants": ["owner"],
            "title": "去非洲",
            "start_at": datetime(2026, 9, 4, 1, 0, tzinfo=timezone.utc),
            "end_at": datetime(2026, 9, 4, 2, 0, tzinfo=timezone.utc),
            "timezone": "Asia/Taipei",
            "status": "confirmed",
        }
        command = CalendarCommand(
            action="update", target_hint="去非洲", all_day=True,
        )
        with patch(
            "services.ayue_agent.v3.calendar_commands.calendar_access_enabled",
            return_value=True,
        ), patch(
            "services.ayue_agent.v3.calendar_commands.resolve_owned_event",
            return_value=(event, None),
        ), patch(
            "services.ayue_agent.v3.calendar_commands.conflicts_for_viewer",
            return_value=[],
        ):
            result = preflight_calendar_commands(self._ctx(), [command])

        self.assertEqual(result.status, "ready")
        self.assertTrue(result.plans[0].changes["all_day"])
        self.assertTrue(result.plans[0].form["all_day"])
        self.assertEqual(result.plans[0].form["start_time"], "")
        self.assertIn("全天", result.preview)

    def test_confirmed_all_day_plan_executes_typed_form(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        plan = self._preflight(CalendarCommand(
            action="create", title="去非洲", date="2026-09-04",
            end_date="2026-09-05", all_day=True,
        )).plans[0]
        returned_event = {
            "event_id": "event-1",
            "source_type": "personal",
            "participants": ["owner"],
            "title": "去非洲",
            "start_at": datetime(2026, 9, 3, 16, 0, tzinfo=timezone.utc),
            "end_at": datetime(2026, 9, 5, 16, 0, tzinfo=timezone.utc),
            "all_day": True,
            "timezone": "Asia/Taipei",
            "status": "confirmed",
            "revision": 1,
        }
        with patch(
            "services.calendar_service.create_personal_event",
            return_value=returned_event,
        ) as create, patch(
            "services.ayue_agent.v3.write_executors.remember_recent_mutation",
            create=True,
        ):
            ok, reply, code = execute_write(
                "calendar.submit_commands", {}, ctx, MagicMock(), "run-1", 0,
                confirmation_id="confirmation-1",
                payload={
                    "calendar_plan_version": 1,
                    "plans": [plan.model_dump(exclude_none=True)],
                },
            )

        self.assertTrue(ok)
        self.assertIsNone(code)
        self.assertIn("9/4–9/5 全天", reply)
        self.assertTrue(create.call_args.args[1]["all_day"])


if __name__ == "__main__":
    unittest.main()
