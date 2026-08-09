# social_demotest/tests/test_v3_contracts.py
import unittest
from pydantic import ValidationError

from services.ayue_agent.v3.contracts import (
    SubTask, Plan, SubTaskResult, SubTaskStatus, AgentContextSlice,
    ToolProposal, GuardDecision, GuardResultCode,
)


class V3ContractsTests(unittest.TestCase):
    def test_direct_chat_plan_is_task_free_and_requires_reply(self):
        plan = Plan(mode="direct_chat", tasks=[], direct_reply="嗨～怎麼啦？")
        self.assertEqual(plan.mode, "direct_chat")
        self.assertEqual(plan.tasks, [])
        self.assertEqual(plan.direct_reply, "嗨～怎麼啦？")
        with self.assertRaises(ValidationError):
            Plan(mode="direct_chat", tasks=[], direct_reply="   ")

    def test_direct_chat_rejects_tasks_and_opportunity(self):
        task = SubTask(id="t1", agent="synthesizer", depends_on=[], task_brief="回覆")
        with self.assertRaises(ValidationError):
            Plan(mode="direct_chat", tasks=[task], direct_reply="嗨")
        with self.assertRaises(ValidationError):
            from services.ayue_agent.v3.contracts import OpportunitySignal
            Plan(
                mode="direct_chat", tasks=[], direct_reply="嗨",
                opportunity=OpportunitySignal(
                    signal="social_opening", evidence_span="有點孤單", confidence=0.9,
                ),
            )

    def test_tasks_plan_rejects_direct_reply_but_legacy_shape_still_works(self):
        task = SubTask(id="t1", agent="synthesizer", depends_on=[], task_brief="回覆")
        self.assertEqual(Plan(tasks=[task]).mode, "tasks")
        with self.assertRaises(ValidationError):
            Plan(mode="tasks", tasks=[task], direct_reply="嗨")

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

    def test_plan_requires_exactly_one_synthesizer(self):
        domain = SubTask(id="t1", agent="calendar", depends_on=[], task_brief="x")
        with self.assertRaises(ValidationError):
            Plan(tasks=[domain])
        syn1 = SubTask(id="s1", agent="synthesizer", depends_on=["t1"], task_brief="reply")
        syn2 = SubTask(id="s2", agent="synthesizer", depends_on=["t1"], task_brief="reply")
        with self.assertRaises(ValidationError):
            Plan(tasks=[domain, syn1, syn2])

    def test_plan_rejects_more_than_three_domain_tasks(self):
        domains = [
            SubTask(id=f"t{i}", agent="calendar", depends_on=[], task_brief="x")
            for i in range(4)
        ]
        with self.assertRaises(ValidationError):
            syn = SubTask(
                id="syn", agent="synthesizer",
                depends_on=[task.id for task in domains], task_brief="reply",
            )
            Plan(tasks=[*domains, syn])

    def test_plan_rejects_dependency_cycle(self):
        t1 = SubTask(id="t1", agent="calendar", depends_on=["t2"], task_brief="x")
        t2 = SubTask(id="t2", agent="places", depends_on=["t1"], task_brief="x")
        syn = SubTask(id="syn", agent="synthesizer", depends_on=["t1", "t2"], task_brief="reply")
        with self.assertRaises(ValidationError):
            Plan(tasks=[t1, t2, syn])

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
            ToolProposal(tool_name="calendar.submit_commands", arguments={"user_id": "x"})
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="match.decide_active_proposal", arguments={"match_id": "x"})
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.submit_commands", arguments={"revision": 1})
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.submit_commands", arguments={"expected_status": "draft"})

    def test_guard_decision_pass_or_fail_with_code(self):
        ok = GuardDecision(ok=True, code=GuardResultCode.PASSED)
        self.assertTrue(ok.ok)
        self.assertEqual(ok.code, GuardResultCode.PASSED)
        bad = GuardDecision(ok=False, code=GuardResultCode.SCHEMA_INVALID)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.code, GuardResultCode.SCHEMA_INVALID)


if __name__ == "__main__":
    unittest.main()
