# social_demotest/tests/test_v3_trajectories.py
import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentResult, AgentTurnContext
from services.ayue_agent.v3.contracts import Plan, SubTask, ToolProposal
from services.ayue_agent.v3.planner import PlannerMetrics
from services.ayue_agent.v3.synthesizer import SynthesizerMetrics
from services.ayue_agent.v3.sub_agents.base import SubAgentMetrics


def _sm():
    return SubAgentMetrics(input_tokens=10, output_tokens=20, duration_ms=100)
def _pm():
    return PlannerMetrics(input_tokens=50, output_tokens=60, duration_ms=200)
def _synm():
    return SynthesizerMetrics(input_tokens=30, output_tokens=40, duration_ms=150)


class V3TrajectoryTests(unittest.TestCase):
    """End-to-end trajectory tests for the canonical steak example."""

    def test_steak_example_produces_calendar_conflict_and_places_recommendation(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room",
            message="我下週五晚上想吃牛排，幫我找餐廳並看看我那天有沒有空",
        )
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查詢使用者下週五晚上的行程空檔"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="搜尋附近的牛排餐廳"),
            SubTask(id="t3", agent="places", depends_on=["t2"], task_brief="依 t2 結果篩選推薦餐廳"),
            SubTask(id="t4", agent="synthesizer", depends_on=["t1", "t2", "t3"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _pm())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=([ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sm())),
                 "places": MagicMock(side_effect=[
                     ([ToolProposal(tool_name="places.search_nearby", arguments={"anchor": "牛排餐廳", "categories": ["restaurant"]})], _sm()),
                     ([ToolProposal(tool_name="places.search_nearby", arguments={"anchor": "附近咖啡廳", "categories": ["cafe"]})], _sm()),
                 ]),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool",
                   side_effect=lambda call, ctx, clock: {
                       "calendar.list_my_events": MagicMock(ok=True, data={"events": [{"title": "家庭聚餐", "date": "2026-08-09", "start_time": "20:00"}]}, error_code=None),
                       "places.search_nearby": MagicMock(ok=True, data={"places": [{"name": "附近咖啡廳", "distance_m": 726}]}, error_code=None),
                   }.get(call.name, MagicMock(ok=True, data={}, error_code=None))), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value=("這週末有家庭聚餐，我也找到附近一家義式料理餐廳，要不要一起去？", None, _synm())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_id = "owner"
            from services.ayue_agent.v3.scheduler import run_public_agent_turn_v3
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v3")
        self.assertIn("家庭聚餐", result.reply)
        self.assertIn("餐廳", result.reply)

    def test_v3_mode_off_falls_back_to_v2(self):
        from services.ayue_agent.v3.scheduler import agent_mode_for_user_v3
        with patch.dict("os.environ", {"AYUE_AGENT_V3_MODE": "off"}):
            self.assertEqual(agent_mode_for_user_v3("anyone"), "off")

    def test_v3_mode_on_with_allowlist_blocks_unlisted_user(self):
        from services.ayue_agent.v3.scheduler import agent_mode_for_user_v3
        with patch.dict("os.environ", {"AYUE_AGENT_V3_MODE": "on", "AYUE_AGENT_V3_USER_ALLOWLIST": "alice,bob"}):
            self.assertEqual(agent_mode_for_user_v3("alice"), "on")
            self.assertEqual(agent_mode_for_user_v3("charlie"), "off")


if __name__ == "__main__":
    unittest.main()