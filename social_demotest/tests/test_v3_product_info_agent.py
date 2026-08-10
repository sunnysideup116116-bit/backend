import inspect
import unittest
from unittest.mock import patch

from services.ayue_agent.capabilities import get_product_knowledge
from services.ayue_agent.contracts import PublicAgentTurnContext, TurnClockV1
from services.ayue_agent.v3.contracts import Plan, SubTask, SubTaskResult, SubTaskStatus
from services.ayue_agent.v3.planner import _decompose_tool_schema, _PLANNER_SYSTEM
from services.ayue_agent.v3.planner import plan_turn
from services.ai_service import ToolCallResult
from services.ayue_agent.v3.scheduler import _SUB_AGENT_RUNNERS
from services.ayue_agent.v3.sub_agents.product_info_agent import (
    MAX_RETRIEVAL_ROUNDS,
    retrieve_product_info,
)
from services.ayue_agent.v3.runtime_registry import TaskRunnerResult


class ProductInfoAgentTests(unittest.TestCase):
    def test_planner_routes_product_info_as_task_without_section_ids(self):
        turn = PublicAgentTurnContext(
            user_id="owner",
            room_id="room",
            message="How does matching work?",
            clock=TurnClockV1(
                timezone="Asia/Taipei",
                utc_iso="2026-08-04T12:00:00+00:00",
                local_iso="2026-08-04T20:00:00+08:00",
                local_date="2026-08-04",
                local_time="20:00",
                weekday_zh_tw="Monday",
            ),
        )
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=ToolCallResult(content="", tool_calls=[{
                "name": "decompose_tasks",
                "arguments": {
                    "tasks": [
                        {"id": "p", "agent": "product_info", "depends_on": [], "task_brief": turn.message},
                        {"id": "s", "agent": "synthesizer", "depends_on": ["p"], "task_brief": "Compose."},
                    ],
                },
            }]),
        ):
            plan, _metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual([task.agent for task in plan.tasks], ["product_info", "synthesizer"])

    def test_semantic_question_retrieves_multiple_structured_sections(self):
        result = retrieve_product_info(
            "Why do I need confirmation when you already know my preferences?"
        )
        self.assertEqual(result["coverage"], "sufficient")
        self.assertEqual(
            set(result["knowledge_sections"]),
            {"matching.profile_usage", "matching.confirmation"},
        )
        self.assertIn("matching.profile_usage", result["facts"])
        self.assertNotIn("reply", result)
        self.assertLessEqual(result["retrieval"]["max_rounds"], MAX_RETRIEVAL_ROUNDS)

    def test_unknown_question_is_explicitly_insufficient(self):
        result = retrieve_product_info("What is the weather on Mars in 2099?")
        self.assertEqual(result["coverage"], "insufficient")
        self.assertEqual(result["failure_code"], "product_knowledge_insufficient")
        self.assertEqual(result["facts"], {})

    def test_retriever_rejects_unknown_section_without_guessing(self):
        result = get_product_knowledge(["matching.confirmation", "not.a.real.section"])
        self.assertEqual(result["coverage"], "insufficient")
        self.assertEqual(result["unknown_sections"], ["not.a.real.section"])
        self.assertEqual(len(result["knowledge_sections"]), 1)

    def test_product_info_is_a_normal_mixed_dag_node(self):
        plan = Plan(
            mode="tasks",
            tasks=[
                SubTask(id="product", agent="product_info", task_brief="How does matching work?"),
                SubTask(id="match", agent="match", task_brief="Start finding someone."),
                SubTask(id="synth", agent="synthesizer", depends_on=["product", "match"], task_brief="Compose."),
            ],
        )
        self.assertEqual({task.agent for task in plan.tasks}, {"product_info", "match", "synthesizer"})
        self.assertIn("product_info", _SUB_AGENT_RUNNERS)

    def test_product_info_can_join_calendar_or_profile_in_parallel_dag(self):
        for companion_agent, companion_brief in (
            ("calendar", "Move tomorrow's workout to 5pm."),
            ("profile", "What preferences do you currently remember about me?"),
        ):
            with self.subTest(companion_agent=companion_agent):
                plan = Plan(
                    mode="tasks",
                    tasks=[
                        SubTask(id="product", agent="product_info", task_brief="How does matching work?"),
                        SubTask(id="companion", agent=companion_agent, task_brief=companion_brief),
                        SubTask(
                            id="synth",
                            agent="synthesizer",
                            depends_on=["product", "companion"],
                            task_brief="Compose.",
                        ),
                    ],
                )
                self.assertEqual(plan.tasks[0].depends_on, [])
                self.assertEqual(plan.tasks[1].depends_on, [])

    def test_product_info_context_does_not_receive_profile_or_tool_observations(self):
        from services.ayue_agent.v3.context_slicer import slice_for_agent

        turn = PublicAgentTurnContext(
            user_id="owner",
            room_id="room",
            message="How does matching work?",
            recent_messages=[{"role": "user", "content": "A bounded recent message"}],
            clock=TurnClockV1(
                timezone="Asia/Taipei",
                utc_iso="2026-08-04T12:00:00+00:00",
                local_iso="2026-08-04T20:00:00+08:00",
                local_date="2026-08-04",
                local_time="20:00",
                weekday_zh_tw="Monday",
            ),
        )
        context = slice_for_agent(
            "product_info",
            turn,
            prior_observations=[{"tool": "profile.read", "result": {"private": "secret"}}],
        )
        self.assertNotIn("prior_observations", context.payload)
        self.assertNotIn("secret", str(context.payload))

    def test_planner_does_not_expose_product_info_section_taxonomy(self):
        schema = _decompose_tool_schema()["function"]["parameters"]
        self.assertNotIn("product_info_topics", schema["properties"])
        self.assertIn("product_info", _PLANNER_SYSTEM)
        self.assertIn("normal `product_info` task", _PLANNER_SYSTEM)

    def test_scheduler_has_no_product_info_orchestration_branch(self):
        from services.ayue_agent.v3 import scheduler

        source = inspect.getsource(scheduler.run_public_agent_turn_v3)
        self.assertNotIn('if plan.mode == "product_info"', source)

    def test_completed_runner_result_is_not_a_finished_reply(self):
        result = TaskRunnerResult.from_completed([SubTaskResult(
            task_id="product",
            status=SubTaskStatus.OK,
            observation={"product_info": {"facts": {}}},
        )])
        self.assertNotIn("reply", result.completed_results[0].observation["product_info"])


if __name__ == "__main__":
    unittest.main()
