# social_demotest/tests/test_v3_planner.py
import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContextV2, TurnClockV1
from services.ayue_agent.v3.contracts import Plan
from services.ayue_agent.v3.planner import plan_turn
from services.ai_service import ToolCallResult


def _clock():
    return TurnClockV1(
        timezone="Asia/Taipei", utc_iso="2026-08-04T12:00:00+00:00",
        local_iso="2026-08-04T20:00:00+08:00", local_date="2026-08-04",
        local_time="20:00", weekday_zh_tw="星期二",
    )


def _fc_result(content="", tool_calls=None):
    return ToolCallResult(content=content, tool_calls=tool_calls or [])


def _steak_dag_arguments():
    return {
        "tasks": [
            {"id": "t1", "agent": "calendar", "depends_on": [], "task_brief": "查詢使用者下週五晚上的行程空檔"},
            {"id": "t2", "agent": "places", "depends_on": [], "task_brief": "搜尋附近的牛排餐廳"},
            {"id": "t3", "agent": "places", "depends_on": ["t2"], "task_brief": "依 t2 結果篩選推薦餐廳"},
            {"id": "t4", "agent": "synthesizer", "depends_on": ["t1", "t2", "t3"], "task_brief": "彙整行程空檔與餐廳推薦，產生最終回覆給使用者。"}
        ]
    }


class V3PlannerTests(unittest.TestCase):
    def _turn(self, message):
        return AgentTurnContextV2(
            user_id="owner", room_id="room", message=message,
            clock=_clock(),
        )

    def test_steak_example_produces_calendar_places_synthesizer_dag(self):
        turn = self._turn("我下週五晚上想吃牛排，幫我找餐廳並看看我那天有沒有空")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": _steak_dag_arguments()},
            ]),
        ):
            plan, _metrics = plan_turn(turn, pending_confirmations=[])
        self.assertIsNotNone(plan)
        self.assertIsInstance(plan, Plan)
        agents = [t.agent for t in plan.tasks]
        self.assertIn("calendar", agents)
        self.assertIn("places", agents)
        self.assertIn("synthesizer", agents)
        syn = [t for t in plan.tasks if t.agent == "synthesizer"]
        self.assertEqual(len(syn), 1)

    def test_simple_chat_produces_synthesizer_only(self):
        turn = self._turn("你好嗎")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "tasks": [{"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "回覆使用者的問候"}]
                }},
            ]),
        ):
            plan, _metrics = plan_turn(turn, pending_confirmations=[])
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.tasks), 1)
        self.assertEqual(plan.tasks[0].agent, "synthesizer")

    def test_basic_assessment_intent_has_profile_task(self):
        turn = self._turn("那我來做基本性格")
        expected = {
            "tasks": [
                {"id": "p1", "agent": "profile", "depends_on": [],
                 "task_brief": "開始基本性格探索，呼叫 profile.start_assessment(kind=basic)"},
                {"id": "s1", "agent": "synthesizer", "depends_on": ["p1"],
                 "task_brief": "根據探索 confirmation 結果回覆使用者"},
            ]
        }
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": expected},
            ]),
        ):
            plan, _metrics = plan_turn(turn, pending_confirmations=[])
        self.assertIsNotNone(plan)
        self.assertEqual([task.agent for task in plan.tasks], ["profile", "synthesizer"])

    def test_no_tool_call_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(content="我想先聊聊"),
        ):
            plan, _metrics = plan_turn(turn, pending_confirmations=[])
        self.assertIsNone(plan)

    def test_wrong_tool_name_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "nope.bad", "arguments": {}},
            ]),
        ):
            plan, _metrics = plan_turn(turn, pending_confirmations=[])
        self.assertIsNone(plan)

    def test_invalid_arguments_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "tasks": [{"id": "t1", "agent": "not_an_agent", "depends_on": [], "task_brief": "x"}],
                }},
            ]),
        ):
            plan, _metrics = plan_turn(turn, pending_confirmations=[])
        self.assertIsNone(plan)

    def test_timeout_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=TimeoutError("timeout"),
        ):
            plan, _metrics = plan_turn(turn, pending_confirmations=[])
        self.assertIsNone(plan)


class V3PlannerOpportunityTests(unittest.TestCase):
    def test_plan_parses_opportunity_signal(self):
        from services.ayue_agent.v3.contracts import OpportunitySignal
        from services.ayue_agent.v3.planner import _DecomposeTasksArguments
        args = _DecomposeTasksArguments.model_validate({
            "tasks": [{"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "回覆"}],
            "opportunity": {"signal": "social_opening", "evidence_span": "一個人去有點孤單", "confidence": 0.9},
        })
        self.assertEqual(args.opportunity.signal, "social_opening")
        self.assertEqual(args.opportunity.evidence_span, "一個人去有點孤單")

    def test_plan_turn_carries_opportunity(self):
        from services.ayue_agent.v3.contracts import OpportunitySignal
        turn = AgentTurnContextV2(
            user_id="owner", room_id="room", message="一個人去有點孤單",
            clock=_clock(),
        )
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "tasks": [{"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "回覆"}],
                    "opportunity": {"signal": "social_opening", "evidence_span": "一個人去有點孤單", "confidence": 0.9},
                }},
            ]),
        ):
            plan, _metrics = plan_turn(turn, pending_confirmations=[])
        self.assertIsNotNone(plan)
        self.assertIsInstance(plan.opportunity, OpportunitySignal)
        self.assertEqual(plan.opportunity.signal, "social_opening")
        self.assertEqual(plan.opportunity.evidence_span, "一個人去有點孤單")


if __name__ == "__main__":
    unittest.main()
