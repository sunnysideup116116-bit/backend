import inspect
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.v3.contracts import (
    AgentContextSlice,
    ResolvedTemporalTarget,
    SubTask,
    SubTaskResult,
    SubTaskStatus,
    ToolProposal,
)
from services.ayue_agent.v3 import scheduler
from services.ayue_agent.v3 import calendar_runtime
from services.ayue_agent.v3.sub_agents import calendar_agent
from services.ayue_agent.v3.runtime_registry import RuntimeRegistration, TaskRunnerResult


class V3CalendarRuntimeBoundaryTests(unittest.TestCase):
    def test_scheduler_does_not_interpret_calendar_domain_shapes(self):
        source = inspect.getsource(scheduler)
        for forbidden in (
            "CalendarCommand",
            "CalendarAgentResult",
            "calendar_drafts",
            "calendar_references",
            "preflight_calendar_commands",
            "_calendar_reference_for_command",
        ):
            self.assertNotIn(forbidden, source)

    def test_runner_result_is_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            TaskRunnerResult()
        with self.assertRaises(ValueError):
            TaskRunnerResult(proposals=[], completed_results=[])
        proposals = TaskRunnerResult.from_proposals([])
        completed = TaskRunnerResult.from_completed([])
        self.assertIsNotNone(proposals.proposals)
        self.assertIsNotNone(completed.completed_results)

    def test_calendar_registration_owns_blocker_and_result_projection(self):
        registration = scheduler._SUB_AGENT_RUNNERS["calendar"]
        self.assertIsInstance(registration, RuntimeRegistration)
        self.assertIsNotNone(registration.direct_chat_blocker)
        self.assertIsNotNone(registration.confirmed_result_projector)

    def test_place_create_protocol_hint_requires_typed_calendar_command(self):
        prompt = calendar_agent._prompt(
            AgentContextSlice(
                agent="calendar",
                payload={"_place_selection_requires_create": True},
            ),
            "安排已選地點",
        )
        self.assertIn("calendar.submit_commands", prompt)
        self.assertIn("action=create", prompt)
        self.assertIn("不要提出 calendar 的唯讀查詢", prompt)

    def test_resolved_temporal_target_overrides_calendar_list_arguments(self):
        seen = {}

        class Services:
            def execute(self, proposal, **_kwargs):
                seen.update(proposal.arguments)
                return SimpleNamespace(
                    attempted=True,
                    result=SubTaskResult(
                        task_id="c1", status=SubTaskStatus.OK,
                        tool_name="calendar.list_my_events", observation={"events": []},
                    ),
                    private_data={},
                )

        task = SubTask(
            id="c1", agent="calendar", task_brief="查這週六行程",
            resolved_time_target=ResolvedTemporalTarget(
                source_text="這週六", date="2026-09-19", timezone="Asia/Taipei",
            ),
        )
        calendar_runtime._run_calendar_reads(
            task=task,
            turn_ctx=SimpleNamespace(user_id="owner"),
            proposals=[ToolProposal(
                tool_name="calendar.list_my_events",
                arguments={"range_label": "這週六"},
            )],
            prior_observations=[], services=Services(), max_reads=1,
        )
        self.assertEqual(seen, {
            "start_date": "2026-09-19", "end_date": "2026-09-19",
        })

    def test_next_google_event_without_write_reference_clears_an_old_reference(self):
        class Services:
            def execute(self, proposal, **_kwargs):
                return SimpleNamespace(
                    attempted=True,
                    result=SubTaskResult(
                        task_id="c1",
                        status=SubTaskStatus.OK,
                        tool_name="calendar.get_next_my_event",
                        observation={"status": "found", "event": {}},
                    ),
                    private_data={},
                )

        task = SubTask(id="c1", agent="calendar", task_brief="下一個行程")
        with patch.object(calendar_runtime, "clear_reference") as clear_reference:
            calendar_runtime._run_calendar_reads(
                task=task,
                turn_ctx=SimpleNamespace(user_id="owner"),
                proposals=[ToolProposal(
                    tool_name="calendar.get_next_my_event",
                    arguments={},
                )],
                prior_observations=[],
                services=Services(),
                max_reads=1,
            )

        clear_reference.assert_called_once_with("owner")


if __name__ == "__main__":
    unittest.main()
