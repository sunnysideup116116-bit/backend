import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from services.ayue_agent.v3.calendar_commands import (
    CalendarCommand,
    CalendarMutationPlan,
    CalendarPreflightResult,
    normalize_calendar_batch_payload,
    preflight_calendar_commands,
)
from services.ayue_agent.v3.calendar_drafts import clear_draft, get_draft, merge_command, save_draft
from services.ayue_agent.v3.sub_agents.calendar_agent import _tools_schema
from services.ayue_agent.v3.sub_agents.calendar_agent import CalendarAgentResult
from services.ayue_agent.v3.sub_agents.calendar_agent import run as run_calendar_agent
from services.ayue_agent.v3.contracts import SubTask
from services.ayue_agent.v3.contracts import AgentContextSlice
from services.ayue_agent.v3.scheduler import _run_sub_task
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ai_service import ToolCallResult
from services.ayue_agent.v3.write_executors import execute_write


def _event(event_id="event-1", revision=4, *, source_type="personal"):
    return {
        "event_id": event_id,
        "revision": revision,
        "source_type": source_type,
        "participants": ["owner"] if source_type == "personal" else ["owner", "other"],
        "title": "雞排約會" if source_type == "personal" else None,
        "activity": "雞排約會" if source_type == "date" else None,
        "start_at": datetime(2026, 8, 25, 10, 0, tzinfo=timezone.utc),
        "end_at": datetime(2026, 8, 25, 11, 0, tzinfo=timezone.utc),
        "timezone": "Asia/Taipei",
        "location": "高雄",
        "notes": "",
        "status": "confirmed",
        "coordination_id": "coord-1" if source_type == "date" else None,
    }


class V3CalendarCommandTests(unittest.TestCase):
    def _ctx(self):
        return SimpleNamespace(user_id="owner")

    def test_llm_command_rejects_authority_fields(self):
        with self.assertRaises(ValidationError):
            CalendarCommand(
                action="update", target_hint="雞排約會", date="2026-08-15",
                event_id="server-id", revision=4,
            )

    def test_calendar_agent_command_schema_has_no_authority_fields(self):
        command_tool = next(item for item in _tools_schema()
                            if item["function"]["name"] == "calendar.submit_commands")
        schema_text = str(command_tool["function"]["parameters"])
        for field in ("event_id", "revision", "expected_revision", "user_id", "coordination_id"):
            self.assertNotIn(field, schema_text)

    def test_calendar_agent_command_schema_is_provider_compatible(self):
        command_tool = next(item for item in _tools_schema()
                            if item["function"]["name"] == "calendar.submit_commands")
        schema_text = json.dumps(command_tool["function"]["parameters"])
        self.assertNotIn("$ref", schema_text)
        self.assertNotIn("$defs", schema_text)

    def test_provider_aliases_are_normalized_before_strict_validation(self):
        payload = normalize_calendar_batch_payload({
            "commands": [{
                "type": "create", "summary": "去駁二", "date": "後天",
                "start_time": "08:00",
            }],
        })
        command = CalendarCommand.model_validate(payload["commands"][0])
        self.assertEqual(command.action, "create")
        self.assertEqual(command.title, "去駁二")
        self.assertIsNone(command.end_time)

    def test_draft_continuation_fills_only_missing_fields(self):
        clear_draft("owner")
        original = CalendarCommand(
            action="create", title="去駁二", date="後天", start_time="08:00",
        )
        record = save_draft("owner", original, missing_fields=["end_time"])
        continuation = CalendarCommand(
            action="create", start_time="08:00", end_time="10:00", draft_mode="continue",
        )
        merged = merge_command(continuation, record)
        self.assertEqual(merged.title, "去駁二")
        self.assertEqual(merged.date, "後天")
        self.assertEqual(merged.end_time, "10:00")
        clear_draft("owner")

    def test_recent_reference_resolves_without_event_hint(self):
        command = CalendarCommand(action="cancel")
        reference = {"event_id": "event-1", "revision": 4, "source_type": "personal"}
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.calendar_service.resolve_owned_event_reference", return_value=(_event(), None)) as resolve:
            result = preflight_calendar_commands(
                self._ctx(), [command], recent_references={0: reference},
            )
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.plans[0].event_id, "event-1")
        resolve.assert_called_once_with("owner", reference)

    def test_server_reference_prevents_second_natural_language_resolution(self):
        command = CalendarCommand(action="cancel", target_hint="睡覺刪掉")
        reference = {"event_id": "event-1", "revision": 4, "source_type": "personal", "_force": True}
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.calendar_service.resolve_owned_event_reference", return_value=(_event(), None)) as reference_resolve, \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event") as natural_resolve:
            result = preflight_calendar_commands(
                self._ctx(), [command], recent_references={0: reference},
            )
        self.assertEqual(result.status, "ready")
        reference_resolve.assert_called_once()
        natural_resolve.assert_not_called()

    def test_calendar_agent_parses_typed_command_without_exposing_plan_fields(self):
        context = AgentContextSlice(agent="calendar", payload={"message": "新增看牙醫"})
        result = ToolCallResult(content="", tool_calls=[{
            "name": "calendar.submit_commands",
            "arguments": {"commands": [{
                "action": "create", "title": "看牙醫", "date": "2026-08-20",
                "start_time": "10:00", "end_time": "11:00",
            }]},
        }])
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=result,
        ):
            proposals, _metrics = run_calendar_agent(context, task_brief="新增看牙醫")
        self.assertEqual(len(proposals), 0)
        self.assertEqual(len(proposals.commands), 1)
        self.assertEqual(proposals.commands[0].action, "create")
        self.assertNotIn("event_id", proposals.commands[0].model_dump())

    def test_calendar_agent_reports_invalid_command_instead_of_silent_no_proposal(self):
        context = AgentContextSlice(agent="calendar", payload={"message": "新增去日本"})
        result = ToolCallResult(content="", tool_calls=[{
            "name": "calendar.submit_commands",
            "arguments": {"commands": [{
                "action": "create", "title": "去日本", "date": "後天",
                "start_time": "08:00", "end_time": "10:00", "event_id": "forbidden",
            }]},
        }])
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=result,
        ):
            proposals, metrics = run_calendar_agent(context, task_brief="新增去日本")
        self.assertEqual(len(proposals.commands), 0)
        self.assertIn("calendar_command_schema_invalid", metrics.rejected_calls)
        self.assertEqual(metrics.error, "no_valid_proposal")

    def test_create_missing_end_time_is_clarification(self):
        command = CalendarCommand(
            action="create", title="看牙醫", date="2026-08-20", start_time="10:00",
        )
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event") as resolve:
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification.code, "missing_fields")
        self.assertEqual(result.clarification.missing_fields, ["end_time"])
        resolve.assert_not_called()

    def test_generic_calendar_title_is_clarification(self):
        command = CalendarCommand(
            action="create", title="行事曆", date="2026-08-20",
            start_time="10:00", end_time="11:00",
        )
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification.missing_fields, ["title"])

    def test_cancel_clarification_can_be_saved_as_a_typed_draft(self):
        clear_draft("owner")
        command = CalendarCommand(action="cancel", target_hint="睡覺")
        save_draft("owner", command, missing_fields=["target_hint"])
        draft = get_draft("owner")
        self.assertEqual((draft or {}).get("command", {}).get("action"), "cancel")
        clear_draft("owner")

    def test_fuzzy_unique_resolution_is_server_owned_and_requires_confirmation(self):
        command = CalendarCommand(action="cancel", target_hint="睡覺")
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event", return_value=(_event(), "fuzzy_suggestion")), \
             patch("services.ayue_agent.v3.calendar_commands.get_owned_event_resolution_candidates", return_value=[_event()]), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.plans[0].resolution_kind, "fuzzy_suggestion")
        self.assertIn("變更", result.preview)

    def test_create_preflight_returns_server_owned_plan_without_resolution(self):
        command = CalendarCommand(
            action="create", title="看牙醫", date="2026-08-20",
            start_time="10:00", end_time="11:00",
        )
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]), \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event") as resolve:
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "ready")
        plan = result.plans[0]
        self.assertEqual(plan.action, "create")
        self.assertIsNone(plan.event_id)
        self.assertIsNone(plan.expected_revision)
        self.assertNotIn("target_hint", plan.model_dump())
        resolve.assert_not_called()

    def test_create_relative_date_uses_authoritative_clock(self):
        command = CalendarCommand(
            action="create", title="去日本", date="後天",
            start_time="8:00", end_time="10:00",
        )
        ctx = SimpleNamespace(
            user_id="owner",
            clock=SimpleNamespace(temporal_references={"後天": "2026-08-10"}),
        )
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]):
            result = preflight_calendar_commands(ctx, [command])
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.plans[0].form["date"], "2026-08-10")
        self.assertEqual(result.plans[0].form["start_time"], "08:00")
        self.assertEqual(result.plans[0].form["end_time"], "10:00")

    def test_unknown_relative_date_is_clarification(self):
        command = CalendarCommand(
            action="create", title="去日本", date="下週",
            start_time="08:00", end_time="10:00",
        )
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification.code, "invalid_date")

    def test_update_unique_event_resolves_once_and_carries_cas(self):
        command = CalendarCommand(
            action="update", target_hint="8/25 雞排約會", date="2026-08-15",
        )
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event", return_value=(_event(), None)) as resolve, \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.plans[0].event_id, "event-1")
        self.assertEqual(result.plans[0].expected_revision, 4)
        resolve.assert_called_once_with("owner", "8/25 雞排約會")

    def test_shared_date_update_keeps_date_coordination_metadata_server_side(self):
        command = CalendarCommand(action="update", target_hint="8/25 雞排約會", date="2026-08-15")
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event", return_value=(_event(source_type="date"), None)), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "ready")
        plan = result.plans[0]
        self.assertEqual(plan.source_type, "date")
        self.assertEqual(plan.other_id, "other")
        self.assertEqual(plan.coordination_id, "coord-1")
        self.assertEqual(plan.form["activity"], "雞排約會")

    def test_update_ambiguous_and_not_found_are_normal_outcomes(self):
        command = CalendarCommand(action="update", target_hint="雞排")
        for resolution, expected in (("ambiguous", "ambiguous"), ("not_found", "not_found")):
            with self.subTest(resolution=resolution), \
                 patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
                 patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event", return_value=(None, resolution)):
                result = preflight_calendar_commands(self._ctx(), [command])
            self.assertEqual(result.status, "needs_clarification")
            self.assertEqual(result.clarification.code, expected)

    def test_selected_cancel_uses_bounded_resolution_once(self):
        command = CalendarCommand(
            action="cancel_selected", target_hints=["A", "B"],
        )
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_events_for_cancel", return_value=([_event("a"), _event("b", 2)], None)) as resolve:
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "ready")
        self.assertEqual([plan.event_id for plan in result.plans], ["a", "b"])
        resolve.assert_called_once_with("owner", mode="selected", event_hints=["A", "B"])

    def test_plan_executor_is_sequential_and_stops_after_first_failure(self):
        payload = {
            "calendar_plan_version": 1,
            "plans": [
                {"action": "create", "form": {"title": "A", "date": "2026-08-20", "start_time": "10:00", "end_time": "11:00"}},
                {"action": "update", "event_id": "event-2", "expected_revision": 3, "changes": {"date": "2026-08-21"}},
                {"action": "cancel", "event_id": "event-3", "expected_revision": 1},
            ],
        }
        created = []

        def create(_user_id, form, *, agent_action_key=None):
            created.append((form["title"], agent_action_key))
            return _event("created")

        with patch("services.calendar_service.create_personal_event", side_effect=create), \
             patch("services.calendar_service.update_personal_event", side_effect=RuntimeError("write failed")) as update, \
             patch("services.calendar_service.cancel_event") as cancel, \
             patch("services.calendar_service.resolve_owned_event") as resolve:
            ok, _reply, code = execute_write(
                "calendar.submit_commands", {}, self._ctx(), SimpleNamespace(), "run", 0,
                confirmation_id="confirmation-1", payload=payload,
            )
        self.assertTrue(ok)
        self.assertEqual(code, "partial")
        self.assertEqual(created[0][1], "calendar-confirmation:confirmation-1:0")
        update.assert_called_once()
        cancel.assert_not_called()
        resolve.assert_not_called()

    def test_plan_cancel_batch_checks_all_revisions_before_writing(self):
        payload = {
            "calendar_plan_version": 1,
            "plans": [
                {"action": "cancel", "event_id": "event-a", "expected_revision": 2},
                {"action": "cancel", "event_id": "event-b", "expected_revision": 5},
            ],
        }
        with patch("services.calendar_service.cancel_targets_are_current", return_value=False) as current, \
             patch("services.calendar_service.cancel_event") as cancel:
            ok, _reply, code = execute_write(
                "calendar.submit_commands", {}, self._ctx(), SimpleNamespace(), "run", 0,
                confirmation_id="confirmation-2", payload=payload,
            )
        self.assertFalse(ok)
        self.assertEqual(code, "stale_revision")
        current.assert_called_once()
        cancel.assert_not_called()

    def test_plan_shared_update_routes_to_date_coordination_service(self):
        payload = {
            "calendar_plan_version": 1,
            "plans": [{
                "action": "update", "source_type": "date", "event_id": "event-date",
                "expected_revision": 7, "other_id": "other", "coordination_id": "coord-1",
                "form": {"activity": "雞排約會", "date": "2026-08-15", "start_time": "10:00", "end_time": "11:00"},
            }],
        }
        with patch(
            "services.date_coordination_service.request_reschedule",
            return_value=({"form": {"activity": "雞排約會", "date": "2026-08-15", "start_time": "10:00", "end_time": "11:00"}}, {}),
        ) as reschedule, patch("services.calendar_service.update_personal_event") as personal_update:
            ok, _reply, code = execute_write(
                "calendar.submit_commands", {}, self._ctx(), SimpleNamespace(), "run", 0,
                confirmation_id="confirmation-3", payload=payload,
            )
        self.assertTrue(ok)
        self.assertIsNone(code)
        reschedule.assert_called_once()
        personal_update.assert_not_called()

    def test_scheduler_creates_one_confirmation_from_typed_commands(self):
        task = SubTask(id="calendar-1", agent="calendar", task_brief="修改行程")
        command = CalendarCommand(action="update", target_hint="雞排約會", date="2026-08-15")
        plan = CalendarMutationPlan(
            action="update", event_id="event-1", expected_revision=4,
            changes={"date": "2026-08-15"}, safe_label="8/25 18:00–19:00 雞排約會",
        )
        turn_ctx = SimpleNamespace(
            user_id="owner", message="改行程", recent_messages=[], recent_context="",
            user_location="", relevant_memories=[], active_proposal=None,
            latest_match_outcome=None, pending_confirmation=None, action_draft=None,
            place_search_draft=None, recent_context_draft=None, mentioned_contacts=[],
            mentioned_contact_overflow=False,
            clock=SimpleNamespace(model_dump=lambda: {"timezone": "Asia/Taipei"}),
            _raw_ctx=self._ctx(),
        )
        preflight = CalendarPreflightResult(status="ready", plans=[plan], preview="修改雞排約會")
        with patch(
            "services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS",
            {"calendar": lambda _slice, task_brief: (CalendarAgentResult(commands=[command]), SubAgentMetrics())},
        ), patch(
            "services.ayue_agent.v3.scheduler.preflight_calendar_commands",
            return_value=preflight,
        ), patch(
            "services.ayue_agent.v3.scheduler.ConfirmationManager.create_confirmation",
            return_value={"confirmation_id": "confirmation-1"},
        ) as create_confirmation:
            results, _metrics = _run_sub_task(
                task, turn_ctx, [], seen_keys=set(), step_counts={"__writes": 0},
                guard_lock=__import__("threading").Lock(), on_progress=None,
                run_id="run-1", trace={"event_sequence": [], "guard_results": [], "tool_results": []},
            )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].observation["pending_confirmation"])
        self.assertNotIn("event_id", results[0].observation)
        create_confirmation.assert_called_once()
        payload = create_confirmation.call_args.kwargs["payload"]
        self.assertEqual(payload["calendar_plan_version"], 1)
        self.assertEqual(payload["plans"][0]["event_id"], "event-1")


if __name__ == "__main__":
    unittest.main()
