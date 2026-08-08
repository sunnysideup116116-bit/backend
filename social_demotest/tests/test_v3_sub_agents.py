# social_demotest/tests/test_v3_sub_agents.py
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContextV2, TurnClockV1
from services.ayue_agent.v3.contracts import AgentContextSlice, ToolProposal
from services.ayue_agent.v3.sub_agents.calendar_agent import run as run_calendar
from services.ayue_agent.v3.sub_agents.places_agent import run as run_places
from services.ayue_agent.v3.sub_agents.match_agent import run as run_match
from services.ayue_agent.v3.sub_agents.relationship_agent import run as run_relationship
from services.ayue_agent.v3.sub_agents.profile_agent import run as run_profile
from services.ai_service import ToolCallResult


def _clock():
    return TurnClockV1(
        timezone="Asia/Taipei", utc_iso="2026-08-04T12:00:00+00:00",
        local_iso="2026-08-04T20:00:00+08:00", local_date="2026-08-04",
        local_time="20:00", weekday_zh_tw="星期二",
    )


def _slice(agent, payload):
    return AgentContextSlice(agent=agent, payload=payload)


def _fc_result(content="", tool_calls=None):
    return ToolCallResult(content=content, tool_calls=tool_calls or [])


class V3SubAgentTests(unittest.TestCase):
    def test_calendar_agent_produces_list_events_proposal(self):
        slc = _slice("calendar", {
            "message": "我這週有哪些行程？",
            "recent_messages": [],
            "clock": _clock().model_dump(),
            "recent_context": "",
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "calendar.list_my_events", "arguments": {}}]),
        ):
            proposals, _metrics = run_calendar(slc, task_brief="請查看本人近期行事曆事件")
        self.assertIsNotNone(proposals)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tool_name, "calendar.list_my_events")

    def test_places_agent_produces_search_nearby_proposal(self):
        slc = _slice("places", {
            "message": "幫我找附近的餐廳",
            "recent_messages": [],
            "user_location": "台北車站",
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "places.search_nearby",
                "arguments": {"anchor": "台北車站", "categories": ["restaurant"], "cuisine": "日式"},
            }]),
        ):
            proposals, _metrics = run_places(slc, task_brief="請搜尋使用者附近的日式餐廳")
        self.assertIsNotNone(proposals)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tool_name, "places.search_nearby")

    def test_match_agent_produces_get_status_proposal(self):
        slc = _slice("match", {
            "message": "我的配對進度如何？",
            "recent_messages": [],
            "active_proposal": None,
            "latest_match_outcome": None,
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "match.get_status", "arguments": {}}]),
        ):
            proposals, _metrics = run_match(slc, task_brief="請回報目前配對狀態")
        self.assertIsNotNone(proposals)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tool_name, "match.get_status")

    def test_relationship_agent_produces_list_contacts_proposal(self):
        slc = _slice("relationship", {
            "message": "我有哪些聯絡人？",
            "recent_messages": [],
            "mentioned_contacts": [],
            "mentioned_contact_overflow": False,
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "relationship.list_accepted_contacts", "arguments": {}}]),
        ):
            proposals, _metrics = run_relationship(slc, task_brief="請列出已建立聯絡的對象")
        self.assertIsNotNone(proposals)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tool_name, "relationship.list_accepted_contacts")

    def test_profile_agent_produces_get_self_summary_proposal(self):
        slc = _slice("profile", {
            "message": "我的簡介是什麼？",
            "recent_messages": [],
            "recent_context": "",
            "relevant_memories": [],
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "profile.get_self_summary", "arguments": {}}]),
        ):
            proposals, _metrics = run_profile(slc, task_brief="請回報本人 profile 摘要")
        self.assertIsNotNone(proposals)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tool_name, "profile.get_self_summary")

    def test_profile_agent_produces_assessment_start_proposal(self):
        slc = _slice("profile", {
            "message": "那我來做基本性格",
            "recent_messages": [],
            "recent_context": "",
            "relevant_memories": [],
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "profile.start_assessment", "arguments": {"kind": "basic"},
            }]),
        ):
            proposals, _metrics = run_profile(slc, task_brief="開始基本性格探索")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tool_name, "profile.start_assessment")
        self.assertEqual(proposals[0].arguments, {"kind": "basic"})

    def test_calendar_agent_returns_none_on_llm_timeout(self):
        slc = _slice("calendar", {
            "message": "x", "recent_messages": [], "clock": _clock().model_dump(),
            "recent_context": "", "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            side_effect=TimeoutError("timeout"),
        ):
            proposals, _metrics = run_calendar(slc, task_brief="x")
        self.assertEqual(proposals, [])

    def test_calendar_agent_returns_none_on_unknown_tool(self):
        slc = _slice("calendar", {
            "message": "x", "recent_messages": [], "clock": _clock().model_dump(),
            "recent_context": "", "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "nope.bad", "arguments": {}}]),
        ):
            proposals, _metrics = run_calendar(slc, task_brief="x")
        self.assertEqual(proposals, [])

    def test_places_agent_repairs_invalid_drink_category_to_cafe(self):
        """珍奶/飲料類別必須被修正為合法的 cafe，不能讓 schema 驗證失敗。"""
        slc = _slice("places", {
            "message": "幫我找附近的珍奶店",
            "recent_messages": [],
            "user_location": "三民區",
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "places.search_nearby",
                "arguments": {"categories": ["drink"], "use_saved_location": True, "limit": 10},
            }]),
        ):
            proposals, _metrics = run_places(slc, task_brief="查詢珍奶飲料店")
        self.assertIsNotNone(proposals)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tool_name, "places.search_nearby")
        self.assertEqual(proposals[0].arguments["categories"], ["cafe"])

    def test_places_agent_repairs_invalid_chicken_category_to_restaurant(self):
        """炸雞類別必須被修正為合法的 restaurant。"""
        slc = _slice("places", {
            "message": "幫我找炸雞店",
            "recent_messages": [],
            "user_location": "三民區",
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "places.search_nearby",
                "arguments": {"categories": ["chicken"], "use_saved_location": True, "limit": 8},
            }]),
        ):
            proposals, _metrics = run_places(slc, task_brief="查詢炸雞餐廳")
        self.assertIsNotNone(proposals)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].arguments["categories"], ["restaurant"])

    def test_places_agent_returns_all_multiple_tool_calls(self):
        """模型一次輸出多個 tool calls 時必須全部回傳，不能只取第一個。"""
        slc = _slice("places", {
            "message": "在高雄市三民區找牛排餐廳和冰店",
            "recent_messages": [],
            "user_location": "高雄市三民區",
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "places.search_nearby", "arguments": {"anchor": "高雄市三民區", "categories": ["restaurant"], "cuisine": "牛排"}},
                {"name": "places.search_nearby", "arguments": {"anchor": "高雄市三民區", "categories": ["cafe"], "cuisine": "冰"}},
            ]),
        ):
            proposals, _metrics = run_places(slc, task_brief="查牛排與冰店")
        self.assertEqual(len(proposals), 2)
        self.assertEqual(proposals[0].arguments["categories"], ["restaurant"])
        self.assertEqual(proposals[1].arguments["categories"], ["cafe"])

    def test_places_agent_skips_invalid_call_but_keeps_valid_one(self):
        """多 calls 中混入不合法者：合法者必須保留。"""
        slc = _slice("places", {
            "message": "查牛排和奇怪分類",
            "recent_messages": [],
            "user_location": "三民區",
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "places.search_nearby", "arguments": {"anchor": "三民區", "categories": ["restaurant"], "cuisine": "牛排"}},
                {"name": "places.search_nearby", "arguments": {"anchor": "三民區", "categories": ["restaurant"], "limit": 999}},
            ]),
        ):
            proposals, _metrics = run_places(slc, task_brief="查牛排")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].arguments["cuisine"], "牛排")


if __name__ == "__main__":
    unittest.main()
