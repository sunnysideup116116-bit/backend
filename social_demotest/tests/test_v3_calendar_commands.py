import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from pydantic import ValidationError

from services.ayue_agent.v3.calendar_commands import (
    CalendarCommand,
    CalendarMutationPlan,
    CalendarPreflightResult,
    canonicalize_calendar_command,
    normalize_calendar_batch_payload,
    preflight_calendar_commands,
)
from services.ayue_agent.v3.calendar_drafts import (
    candidate_reference_allowed, clear_draft, get_draft, merge_command, save_draft,
)
from services.ayue_agent.v3.sub_agents.calendar_agent import _tools_schema
from services.ayue_agent.v3.sub_agents.calendar_agent import CalendarAgentResult
from services.ayue_agent.v3.sub_agents.calendar_agent import run as run_calendar_agent
from services.ayue_agent.v3.contracts import SubTask
from services.ayue_agent.v3.contracts import AgentContextSlice
from services.ayue_agent.v3.scheduler import _calendar_reference_for_command, _run_sub_task
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics
from services.ai_service import ToolCallResult
from services.ayue_agent.v3.write_executors import execute_write
from services.calendar_service import resolve_owned_event_with_candidates


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

    @staticmethod
    def _resolver_cursor(events):
        class Cursor:
            def __init__(self, values):
                self.values = list(values)

            def sort(self, *_args, **_kwargs):
                return self

            def __iter__(self):
                return iter(self.values)

        return Cursor(events)

    @staticmethod
    def _dentist_event(event_id, title, day):
        return {
            "event_id": event_id,
            "revision": 1,
            "source_type": "personal",
            "participants": ["owner"],
            "title": title,
            "start_at": datetime(day.year, day.month, day.day, 7, 0, tzinfo=timezone.utc),
            "end_at": datetime(day.year, day.month, day.day, 8, 0, tzinfo=timezone.utc),
            "timezone": "Asia/Taipei",
            "location": "",
            "notes": "",
            "status": "confirmed",
        }

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

    def test_literal_duration_phrases_are_coerced_to_typed_minutes(self):
        for phrase, minutes in (("半小時", 30), ("一小時", 60), ("一個半小時", 90), ("兩小時", 120)):
            with self.subTest(phrase=phrase):
                command = CalendarCommand(action="create", duration_minutes=phrase)
                self.assertEqual(command.duration_minutes, minutes)

    def test_provider_duration_alias_is_normalized(self):
        payload = normalize_calendar_batch_payload({
            "commands": [{"action": "create", "duration": "大概一小時"}],
        })
        command = CalendarCommand.model_validate(payload["commands"][0])
        self.assertEqual(command.duration_minutes, 60)

    def test_draft_continuation_fills_only_missing_fields(self):
        clear_draft("owner")
        original = CalendarCommand(action="create", title="看牙醫", date="2026-08-12")
        record = save_draft("owner", original, missing_fields=["start_time", "end_time"])
        continuation = CalendarCommand(
            action="create", start_time="15:00", end_time="16:00", draft_mode="replace",
        )
        merged = merge_command(continuation, record)
        self.assertEqual(merged.title, "看牙醫")
        self.assertEqual(merged.date, "2026-08-12")
        self.assertEqual(merged.start_time, "15:00")
        self.assertEqual(merged.end_time, "16:00")
        self.assertEqual(merged.draft_mode, "continue")
        clear_draft("owner")

    def test_candidate_reference_must_be_advertised_by_active_draft(self):
        record = {"candidates": [{"reference": "candidate_1", "label": "8/25 雞排約會"}]}
        self.assertTrue(candidate_reference_allowed(record, "candidate_1"))
        self.assertFalse(candidate_reference_allowed(record, "candidate_2"))
        self.assertFalse(candidate_reference_allowed(None, "candidate_1"))

    def test_candidate_selection_replaces_prior_natural_language_hint(self):
        clear_draft("owner")
        original = CalendarCommand(action="cancel", target_hint="看牙一那個取消")
        record = save_draft(
            "owner", original,
            candidates=[{"reference": "candidate_1", "label": "8/12 15:00–16:00 看牙醫"}],
        )
        selected = merge_command(
            CalendarCommand(action="cancel", target_reference="candidate_1"), record,
        )
        self.assertEqual(selected.target_reference, "candidate_1")
        self.assertIsNone(selected.target_hint)
        clear_draft("owner")

    def test_target_reference_schema_documents_all_server_owned_options(self):
        field = CalendarCommand.model_fields["target_reference"]
        description = str(field.description or "")
        self.assertIn("recent_event", description)
        self.assertIn("candidate_1", description)
        self.assertIn("calendar_draft.candidates", description)

    def test_scheduler_does_not_load_unadvertised_candidate_reference(self):
        command = CalendarCommand(action="cancel", target_reference="candidate_1")
        with patch("services.ayue_agent.v3.scheduler.get_reference") as get_reference:
            result = _calendar_reference_for_command("owner", command, None)
        self.assertIsNone(result)
        get_reference.assert_not_called()

    def test_scheduler_loads_advertised_candidate_reference(self):
        command = CalendarCommand(action="cancel", target_reference="candidate_1")
        draft = {"candidates": [{"reference": "candidate_1", "label": "8/25 雞排約會"}]}
        with patch(
            "services.ayue_agent.v3.scheduler.get_reference",
            return_value={"event_id": "event-1", "revision": 4},
        ) as get_reference:
            result = _calendar_reference_for_command("owner", command, draft)
        self.assertEqual(result["event_id"], "event-1")
        get_reference.assert_called_once_with("owner", reference_key="candidate_1")

    def test_draft_continuation_allows_explicit_date_correction(self):
        clear_draft("owner")

    def test_draft_continuation_treats_duration_as_missing_end_time(self):
        clear_draft("owner")
        original = CalendarCommand(action="create", title="去駁二玩", date="2026-08-20")
        record = save_draft("owner", original, missing_fields=["start_time", "end_time"])
        continuation = CalendarCommand(
            action="create", start_time="09:00", duration_minutes=60, draft_mode="replace",
        )
        merged = merge_command(continuation, record)
        self.assertEqual(merged.title, "去駁二玩")
        self.assertEqual(merged.date, "2026-08-20")
        self.assertEqual(merged.start_time, "09:00")
        self.assertEqual(merged.duration_minutes, 60)
        self.assertEqual(merged.draft_mode, "continue")
        clear_draft("owner")
        original = CalendarCommand(action="create", title="看牙醫", date="2026-08-12")
        record = save_draft("owner", original, missing_fields=["start_time", "end_time"])
        correction = CalendarCommand(
            action="create", date="2026-08-13", start_time="15:00", end_time="16:00",
            draft_mode="replace",
        )
        merged = merge_command(correction, record)
        self.assertEqual(merged.title, "看牙醫")
        self.assertEqual(merged.date, "2026-08-13")
        self.assertEqual(merged.start_time, "15:00")
        self.assertEqual(merged.end_time, "16:00")
        clear_draft("owner")

    def test_complete_new_create_does_not_merge_into_old_draft(self):
        clear_draft("owner")
        record = save_draft(
            "owner", CalendarCommand(action="create", title="看牙醫", date="2026-08-12"),
            missing_fields=["start_time", "end_time"],
        )
        new_command = CalendarCommand(
            action="create", title="新行程", date="2026-08-14", start_time="10:00", end_time="11:00",
            draft_mode="replace",
        )
        merged = merge_command(new_command, record)
        self.assertEqual(merged.title, "新行程")
        self.assertEqual(merged.date, "2026-08-14")
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

    def test_fuzzy_unique_resolution_requires_candidate_clarification_before_mutation(self):
        command = CalendarCommand(action="cancel", target_hint="睡覺")
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event", return_value=(_event(), "fuzzy_suggestion")), \
             patch("services.ayue_agent.v3.calendar_commands.get_owned_event_resolution_candidates", return_value=[_event()]), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification.code, "ambiguous")
        self.assertEqual(len(result.clarification.candidates), 1)
        self.assertIn("嗎", result.clarification.message)

    def test_calendar_resolver_exact_wrappers_are_retrieval_first(self):
        event = self._dentist_event("dentist-1", "看牙醫", datetime(2026, 8, 12).date())
        collection = MagicMock()
        collection.find.return_value = self._resolver_cursor([event])
        with patch("services.calendar_service.calendar_events_coll", collection):
            resolved, kind, candidates = resolve_owned_event_with_candidates(
                "owner", "看牙醫那個行程不去了",
            )
        self.assertEqual(resolved["event_id"], "dentist-1")
        self.assertEqual(kind, "exact")
        self.assertEqual(candidates, [])

    def test_calendar_resolver_typo_returns_fuzzy_candidate_not_not_found(self):
        event = self._dentist_event("dentist-1", "看牙醫", datetime(2026, 8, 12).date())
        collection = MagicMock()
        collection.find.return_value = self._resolver_cursor([event])
        with patch("services.calendar_service.calendar_events_coll", collection):
            resolved, kind, candidates = resolve_owned_event_with_candidates(
                "owner", "看牙一那個取消",
            )
        self.assertEqual(resolved["event_id"], "dentist-1")
        self.assertEqual(kind, "fuzzy_suggestion")
        self.assertEqual([item["event_id"] for item in candidates], ["dentist-1"])

    def test_preflight_typo_returns_candidate_clarification_without_plan(self):
        event = self._dentist_event("dentist-1", "看牙醫", datetime(2026, 8, 12).date())
        collection = MagicMock()
        collection.find.return_value = self._resolver_cursor([event])
        command = CalendarCommand(action="cancel", target_hint="看牙一那個不去了")
        with patch("services.calendar_service.calendar_events_coll", collection), \
             patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification.code, "ambiguous")
        self.assertEqual(len(result.clarification.candidates), 1)
        self.assertFalse(result.plans)

    def test_calendar_resolver_removes_previous_event_wrappers(self):
        event = self._dentist_event("dentist-1", "看牙醫", datetime(2026, 8, 12).date())
        collection = MagicMock()
        collection.find.return_value = self._resolver_cursor([event])
        with patch("services.calendar_service.calendar_events_coll", collection):
            resolved, kind, _candidates = resolve_owned_event_with_candidates(
                "owner", "上次牙醫那個行程不要了",
            )
        self.assertEqual(resolved["event_id"], "dentist-1")
        self.assertEqual(kind, "exact")

    def test_calendar_resolver_ambiguous_returns_bounded_candidates(self):
        first = self._dentist_event("dentist-1", "看牙醫", datetime(2026, 8, 12).date())
        second = self._dentist_event("dentist-2", "牙醫回診", datetime(2026, 9, 3).date())
        collection = MagicMock()
        collection.find.return_value = self._resolver_cursor([first, second])
        with patch("services.calendar_service.calendar_events_coll", collection):
            resolved, kind, candidates = resolve_owned_event_with_candidates(
                "owner", "牙醫那個不去了",
            )
        self.assertIsNone(resolved)
        self.assertEqual(kind, "ambiguous")
        self.assertEqual([item["event_id"] for item in candidates], ["dentist-1", "dentist-2"])

    def test_calendar_resolver_weekday_constraint_ranks_matching_candidate(self):
        wednesday = self._dentist_event("dentist-wed", "看牙醫", datetime(2026, 8, 12).date())
        thursday = self._dentist_event("dentist-thu", "牙醫回診", datetime(2026, 8, 13).date())
        collection = MagicMock()
        collection.find.return_value = self._resolver_cursor([wednesday, thursday])
        with patch("services.calendar_service.calendar_events_coll", collection):
            resolved, kind, candidates = resolve_owned_event_with_candidates(
                "owner", "禮拜三牙醫那個取消",
                temporal_references={"禮拜三": "2026-08-12"},
            )
        self.assertEqual(resolved["event_id"], "dentist-wed")
        self.assertIn(kind, {"exact", "fuzzy_suggestion"})
        if kind == "fuzzy_suggestion":
            self.assertEqual([item["event_id"] for item in candidates], ["dentist-wed"])

    def test_calendar_resolver_no_relevant_event_requests_minimal_clue(self):
        event = self._dentist_event("lunch-1", "吃午餐", datetime(2026, 8, 12).date())
        collection = MagicMock()
        collection.find.return_value = self._resolver_cursor([event])
        with patch("services.calendar_service.calendar_events_coll", collection):
            resolved, kind, candidates = resolve_owned_event_with_candidates(
                "owner", "牙醫那個不去了",
            )
        self.assertIsNone(resolved)
        self.assertEqual(kind, "not_found")
        self.assertEqual(candidates, [])

    def test_calendar_resolver_low_similarity_typo_never_binds(self):
        event = self._dentist_event("dentist-1", "看牙醫", datetime(2026, 8, 12).date())
        collection = MagicMock()
        collection.find.return_value = self._resolver_cursor([event])
        with patch("services.calendar_service.calendar_events_coll", collection):
            resolved, kind, candidates = resolve_owned_event_with_candidates(
                "owner", "xyz那個取消",
            )
        self.assertIsNone(resolved)
        self.assertEqual(kind, "not_found")
        self.assertEqual(candidates, [])

    def test_create_duration_derives_end_time_server_side(self):
        command = CalendarCommand(
            action="create", title="去駁二玩", date="2026-08-20", start_time="09:00",
            duration_minutes=60,
        )
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.plans[0].form["end_time"], "10:00")

    def test_duration_and_explicit_end_mismatch_is_clarification(self):
        command = CalendarCommand(
            action="create", title="去駁二玩", date="2026-08-20", start_time="09:00",
            end_time="11:00", duration_minutes=60,
        )
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification.code, "invalid_interval")

    def test_update_duration_derives_end_time_from_existing_start(self):
        command = CalendarCommand(action="update", target_hint="雞排約會", duration_minutes=30)
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.calendar_commands.resolve_owned_event", return_value=(_event(), None)), \
             patch("services.ayue_agent.v3.calendar_commands.conflicts_for_viewer", return_value=[]):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.plans[0].form["end_time"], "18:30")
        self.assertEqual(result.plans[0].changes["end_time"], "18:30")

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

    def test_weekday_canonicalization_does_not_use_shorter_week_prefix(self):
        command = CalendarCommand(
            action="create", title="看牙醫", date="下週三", start_time="15:00", end_time="16:00",
        )
        ctx = SimpleNamespace(
            user_id="owner",
            clock=SimpleNamespace(temporal_references={
                "下週": "2026-08-10", "下週三": "2026-08-12",
            }),
        )
        canonical, error = canonicalize_calendar_command(ctx, command)
        self.assertIsNone(error)
        self.assertEqual(canonical.date, "2026-08-12")

    def test_create_missing_time_persists_canonical_relative_date(self):
        command = CalendarCommand(action="create", title="看牙醫", date="下禮拜三")
        clock = SimpleNamespace(
            temporal_references={"下禮拜三": "2026-08-12"},
        )
        ctx = SimpleNamespace(user_id="owner", clock=clock)
        canonical, error = canonicalize_calendar_command(ctx, command)
        self.assertIsNone(error)
        self.assertEqual(canonical.date, "2026-08-12")
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True):
            result = preflight_calendar_commands(ctx, [canonical])
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification.missing_fields, ["start_time", "end_time"])
        save_draft("owner", canonical, missing_fields=result.clarification.missing_fields)
        self.assertEqual(get_draft("owner")["command"]["date"], "2026-08-12")
        clear_draft("owner")

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
            if expected == "not_found":
                self.assertIn("相近", result.clarification.message)
                self.assertNotIn("完整日期、時間和行程名稱", result.clarification.message)

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

    def test_selected_cancel_does_not_execute_fuzzy_target(self):
        command = CalendarCommand(action="cancel_selected", target_hints=["牙一", "另一筆"])
        with patch("services.ayue_agent.v3.calendar_commands.calendar_access_enabled", return_value=True), \
             patch("services.calendar_service.resolve_owned_event", return_value=(_event(), None)), \
             patch("services.calendar_service.get_owned_event_resolution_kind", return_value="fuzzy_suggestion"):
            result = preflight_calendar_commands(self._ctx(), [command])
        self.assertEqual(result.status, "needs_clarification")
        self.assertEqual(result.clarification.code, "ambiguous")
        self.assertFalse(result.plans)

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
