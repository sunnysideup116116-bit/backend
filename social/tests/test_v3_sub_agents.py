# tests/test_v3_sub_agents.py
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.contracts import PublicAgentTurnContext, TurnClockV1
from services.ayue_agent.v3.contracts import AgentContextSlice, ToolProposal
from services.ayue_agent.v3.sub_agents.calendar_agent import run as run_calendar
from services.ayue_agent.v3.sub_agents.places_agent import (
    _SYSTEM as PLACES_SYSTEM,
    _TOOLS as PLACES_AGENT_TOOLS,
    run as run_places,
)
from services.ayue_agent.v3.sub_agents.match_agent import run as run_match
from services.ayue_agent.v3.sub_agents.relationship_agent import (
    _SYSTEM as RELATIONSHIP_SYSTEM,
    run_date_invitation,
    run as run_relationship,
)
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
    def test_relationship_contract_owns_accepted_contacts_and_bounded_recommendations(self):
        self.assertIn("已經配到／已經聯絡哪些人", RELATIONSHIP_SYSTEM)
        self.assertIn("total_count", RELATIONSHIP_SYSTEM)
        self.assertIn("truncated=true", RELATIONSHIP_SYSTEM)
        self.assertIn("不能聲稱某人是所有已接受聯絡人中的最佳人選", RELATIONSHIP_SYSTEM)
        self.assertNotIn("誰在忙", RELATIONSHIP_SYSTEM)

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

    def test_places_agent_accepts_bounded_enrichment_request(self):
        slc = _slice("places", {
            "message": "找現在有開的餐廳",
            "recent_messages": [],
            "user_location": "City District",
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "places.search_nearby",
                "arguments": {
                    "anchor": "City District", "categories": ["restaurant"],
                    "enrichments": ["hours", "hours"],
                },
            }]),
        ):
            proposals, _metrics = run_places(slc, task_brief="找目前營業中的餐廳")
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].arguments["enrichments"], ["hours"])

    def test_places_agent_selects_combined_structured_enrichments(self):
        slc = _slice("places", {
            "message": "找今晚有開且價位不要太高的咖啡廳",
            "recent_messages": [],
            "user_location": "City District",
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "places.search_nearby",
                "arguments": {
                    "anchor": "City District", "categories": ["cafe"],
                    "enrichments": ["hours", "price", "hours"],
                },
            }]),
        ):
            proposals, _metrics = run_places(
                slc, task_brief="找目前營業且價格較低的咖啡廳",
            )
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].arguments["enrichments"], ["hours", "price"])

    def test_places_agent_keeps_price_enrichment_opt_in_and_grounded(self):
        self.assertIn("`price`", PLACES_SYSTEM)
        self.assertIn("價格資料缺失或只有部分端點時不可推測", PLACES_SYSTEM)

    def test_places_agent_uses_only_places_tools(self):
        self.assertTrue(PLACES_AGENT_TOOLS)
        self.assertTrue(all(name.startswith("places.") for name in PLACES_AGENT_TOOLS))
        self.assertNotIn("web.search", PLACES_AGENT_TOOLS)
        self.assertNotIn("web.extract", PLACES_AGENT_TOOLS)
        self.assertIn("Places Agent 不呼叫 Web", PLACES_SYSTEM)

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

    def test_relationship_read_agent_cannot_see_date_write_tool(self):
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
        ) as provider:
            run_relationship(slc, task_brief="請列出已建立聯絡的對象")
        visible = provider.call_args.args[1]
        self.assertEqual(
            [item["function"]["name"] for item in visible],
            ["relationship.get_mentioned_contact_summary", "relationship.get_verified_evidence", "relationship.list_accepted_contacts"],
        )

    def test_date_invitation_runtime_retries_wrong_call_with_only_write_tool(self):
        slc = _slice("relationship", {
            "message": "幫我約小安",
            "recent_messages": [],
            "mentioned_contacts": [],
            "mentioned_contact_overflow": False,
            "recent_contact_reference": None,
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            side_effect=[
                _fc_result(tool_calls=[{"name": "relationship.list_accepted_contacts", "arguments": {}}]),
                _fc_result(tool_calls=[{
                    "name": "relationship.start_date_coordination",
                    "arguments": {"target_source": "name", "target_evidence_span": "小安"},
                }]),
            ],
        ) as provider:
            proposals, metrics = run_date_invitation(
                slc, task_brief="Propose relationship.start_date_coordination exactly once",
            )
        self.assertEqual(provider.call_count, 2)
        self.assertEqual(len(proposals), 1)
        self.assertEqual(proposals[0].tool_name, "relationship.start_date_coordination")
        self.assertEqual(metrics.llm_call_count, 2)
        self.assertIn("required_tool_wrong_name", metrics.rejected_calls)
        for call in provider.call_args_list:
            visible = call.args[1]
            self.assertEqual(
                [item["function"]["name"] for item in visible],
                ["relationship.start_date_coordination"],
            )

    def test_date_invitation_provider_timeout_is_not_retried(self):
        slc = _slice("relationship", {
            "message": "幫我約小安",
            "recent_messages": [],
            "mentioned_contacts": [],
            "mentioned_contact_overflow": False,
            "recent_contact_reference": None,
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            side_effect=TimeoutError("provider timeout"),
        ) as provider:
            proposals, metrics = run_date_invitation(
                slc, task_brief="Propose relationship.start_date_coordination exactly once",
            )
        self.assertEqual(provider.call_count, 1)
        self.assertEqual(proposals, [])
        self.assertEqual(metrics.llm_call_count, 1)

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

    def test_places_agent_rejects_unknown_only_category_without_cafe_fallback(self):
        slc = _slice("places", {
            "message": "幫我找附近的神秘場所",
            "recent_messages": [],
            "user_location": "台北車站",
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "places.search_nearby",
                "arguments": {"anchor": "台北車站", "categories": ["mystery_place"]},
            }]),
        ):
            proposals, metrics = run_places(slc, task_brief="搜尋神秘場所")
        self.assertEqual(proposals, [])
        self.assertIn("schema_invalid", metrics.rejected_calls)

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
