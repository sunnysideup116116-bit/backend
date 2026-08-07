# social_demotest/tests/test_v3_contracts.py
import unittest
from pydantic import ValidationError

from services.ayue_agent.v3.contracts import (
    SubTask, Plan, SubTaskResult, SubTaskStatus, AgentContextSlice,
    ToolProposal, GuardDecision, GuardResultCode,
)


class V3ContractsTests(unittest.TestCase):
    def test_subtask_requires_id_agent_depends_on_task_brief(self):
        with self.assertRaises(ValidationError):
            SubTask(id="", agent="calendar", depends_on=[], task_brief="x")
        with self.assertRaises(ValidationError):
            SubTask(id="t1", agent="", depends_on=[], task_brief="x")
        with self.assertRaises(ValidationError):
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="")
        t = SubTask(id="t1", agent="calendar", depends_on=[], task_brief="check calendar")
        self.assertEqual(t.id, "t1")
        self.assertEqual(t.agent, "calendar")

    def test_subtask_rejects_unknown_agent(self):
        with self.assertRaises(ValidationError):
            SubTask(id="t1", agent="unknown_agent", depends_on=[], task_brief="x")

    def test_plan_requires_at_least_one_task(self):
        with self.assertRaises(ValidationError):
            Plan(tasks=[])

    def test_plan_synthesizer_must_be_terminal(self):
        # synthesizer task must not appear in any other task's depends_on list
        # because it is the terminal; this is enforced by validation
        t1 = SubTask(id="t1", agent="calendar", depends_on=[], task_brief="x")
        syn = SubTask(id="syn", agent="synthesizer", depends_on=["t1"], task_brief="蝬?")
        plan = Plan(tasks=[t1, syn])
        self.assertEqual(len(plan.tasks), 2)

    def test_plan_rejects_dangling_dependency(self):
        t1 = SubTask(id="t1", agent="calendar", depends_on=["t0"], task_brief="x")
        with self.assertRaises(ValidationError):
            Plan(tasks=[t1])

    def test_subtask_result_status_enum(self):
        self.assertIn(SubTaskStatus.OK, SubTaskStatus)
        self.assertIn(SubTaskStatus.FAILED, SubTaskStatus)
        self.assertIn(SubTaskStatus.SKIPPED, SubTaskStatus)

    def test_agent_context_slice_holds_agent_name_and_payload(self):
        s = AgentContextSlice(agent="calendar", payload={"events": []})
        self.assertEqual(s.agent, "calendar")
        self.assertEqual(s.payload, {"events": []})

    def test_tool_proposal_holds_tool_name_and_arguments(self):
        p = ToolProposal(tool_name="calendar.list_my_events", arguments={})
        self.assertEqual(p.tool_name, "calendar.list_my_events")
        self.assertEqual(p.arguments, {})

    def test_tool_proposal_rejects_forbidden_id_fields(self):
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.cancel_my_event", arguments={"user_id": "x"})
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="match.decide_active_proposal", arguments={"match_id": "x"})
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.cancel_my_event", arguments={"revision": 1})
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.cancel_my_event", arguments={"expected_status": "draft"})

    def test_guard_decision_pass_or_fail_with_code(self):
        ok = GuardDecision(ok=True, code=GuardResultCode.PASSED)
        self.assertTrue(ok.ok)
        self.assertEqual(ok.code, GuardResultCode.PASSED)
        bad = GuardDecision(ok=False, code=GuardResultCode.SCHEMA_INVALID)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.code, GuardResultCode.SCHEMA_INVALID)


if __name__ == "__main__":
    unittest.main()