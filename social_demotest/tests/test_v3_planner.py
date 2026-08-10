# social_demotest/tests/test_v3_planner.py
import json
import os
import unittest
from unittest.mock import patch

from services.ayue_agent.contracts import PublicAgentTurnContext, TurnClockV1
from services.ayue_agent.v3.contracts import Plan
from services.ayue_agent.v3.planner import (
    _PLANNER_SYSTEM, _decompose_tool_schema, plan_turn,
)
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
        return PublicAgentTurnContext(
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
            plan, _metrics = plan_turn(turn)
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
            plan, _metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.tasks), 1)
        self.assertEqual(plan.tasks[0].agent, "synthesizer")

    def test_simple_chat_can_produce_strict_direct_reply_plan(self):
        turn = self._turn("哈囉")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "mode": "direct_chat", "tasks": [], "direct_reply": "嗨～怎麼啦？",
                }},
            ]),
        ):
            plan, metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.mode, "direct_chat")
        self.assertEqual(plan.tasks, [])
        self.assertEqual(plan.direct_reply, "嗨～怎麼啦？")
        self.assertEqual(metrics.decision_mode, "direct_chat")

    def test_incompatible_direct_payload_preserves_valid_domain_dag(self):
        turn = self._turn("幫我查明天行程")
        tasks = [{
            "id": "t1", "agent": "calendar", "depends_on": [], "task_brief": "查行程",
        }, {
            "id": "s1", "agent": "synthesizer", "depends_on": ["t1"], "task_brief": "彙整",
        }]
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "mode": "direct_chat", "tasks": tasks, "direct_reply": "我幫你看",
                }},
            ]),
        ):
            plan, metrics = plan_turn(turn)
        self.assertEqual(plan.mode, "tasks")
        self.assertEqual([task.agent for task in plan.tasks], ["calendar", "synthesizer"])
        self.assertEqual(metrics.direct_chat_fallback_reason, "incompatible_direct_chat_payload")

    def test_malformed_direct_payload_falls_back_to_synthesizer_only(self):
        turn = self._turn("笑死")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "mode": "direct_chat", "tasks": [], "direct_reply": 42,
                }},
            ]),
        ):
            plan, metrics = plan_turn(turn)
        self.assertEqual(plan.mode, "tasks")
        self.assertEqual([task.agent for task in plan.tasks], ["synthesizer"])
        self.assertEqual(metrics.direct_chat_fallback_reason, "direct_chat_schema_invalid")

    def test_legacy_product_info_mode_is_repaired_into_normal_dag(self):
        turn = self._turn("你跟這個 App 是做什麼的？")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "decompose_tasks",
                "arguments": {
                    "mode": "product_info",
                    "product_info_topics": [
                        "這個交友 App 的用途與定位",
                        "阿月（媒人）在 App 裡的角色",
                    ],
                },
            }]),
        ):
            plan, metrics = plan_turn(turn)

        self.assertIsNotNone(plan)
        self.assertEqual(plan.mode, "tasks")
        self.assertEqual([task.agent for task in plan.tasks], ["product_info", "synthesizer"])
        self.assertEqual(metrics.decision_mode, "tasks")
        self.assertEqual(metrics.product_info_fallback_reason, "legacy_product_info_mode")
        self.assertEqual(metrics.error, "")

    def test_planner_tool_schema_inlines_subtask_refs(self):
        schema = _decompose_tool_schema()["function"]["parameters"]
        serialized = json.dumps(schema, ensure_ascii=False)
        self.assertNotIn("$defs", serialized)
        self.assertNotIn("$ref", serialized)
        self.assertEqual(
            set(schema["properties"]),
            {
                "mode", "presentation_mode", "tasks", "direct_reply",
                "direct_messages", "opportunity",
            },
        )
        task_schema = schema["properties"]["tasks"]["items"]
        self.assertEqual(
            set(task_schema["properties"]),
            {"id", "agent", "depends_on", "task_brief", "evidence_policy"},
        )
        self.assertEqual(
            set(task_schema["required"]),
            {"id", "agent", "task_brief"},
        )

    def test_planner_system_policy_has_routing_catalog_and_task_contract(self):
        for agent in ("calendar", "places", "match", "relationship", "profile", "product_info", "synthesizer"):
            self.assertIn(f"`{agent}`", _PLANNER_SYSTEM)
        self.assertIn("id", _PLANNER_SYSTEM)
        self.assertIn("agent", _PLANNER_SYSTEM)
        self.assertIn("depends_on", _PLANNER_SYSTEM)
        self.assertIn("task_brief", _PLANNER_SYSTEM)
        self.assertIn("不要使用 `type`", _PLANNER_SYSTEM)
        self.assertIn('mode="direct_chat"', _PLANNER_SYSTEM)
        self.assertIn("direct_reply", _PLANNER_SYSTEM)
        self.assertIn("t1=web", _PLANNER_SYSTEM)
        self.assertIn("t2=places depends_on=[t1]", _PLANNER_SYSTEM)
        self.assertIn("t3=web depends_on=[t1,t2]", _PLANNER_SYSTEM)
        self.assertIn("terminal `t4=synthesizer depends_on=[t3]`", _PLANNER_SYSTEM)
        self.assertIn("不得回答行事曆", _PLANNER_SYSTEM)
        self.assertIn("normal `product_info` task", _PLANNER_SYSTEM)
        self.assertNotIn("product_info_topics", _PLANNER_SYSTEM)

    def test_planner_policy_distinguishes_contact_aggregate_from_match_singleton(self):
        self.assertIn("我現在總共配到誰了", _PLANNER_SYSTEM)
        self.assertIn("我一共配到幾個人", _PLANNER_SYSTEM)
        self.assertIn("建立 relationship task", _PLANNER_SYSTEM)
        self.assertIn("這一筆配對目前什麼狀態", _PLANNER_SYSTEM)
        self.assertIn("才建立 match task", _PLANNER_SYSTEM)
        self.assertIn("不能推導 accepted contact 總數", _PLANNER_SYSTEM)

    @unittest.skip("legacy prompt fixture; profile routing is covered by the current planner contract")
    def test_planner_policy_routes_inaccurate_personality_profile_to_basic_assessment(self):
        self.assertIn("既有個性資料", _PLANNER_SYSTEM)
        self.assertIn("profile.start_assessment(kind=basic)", _PLANNER_SYSTEM)
        self.assertIn("不只是追問哪一段", _PLANNER_SYSTEM)
        self.assertIn("即使沒有明說", _PLANNER_SYSTEM)
        self.assertIn("特定對方的實際聊天內容", _PLANNER_SYSTEM)
        self.assertIn("一般、不依賴特定聊天紀錄", _PLANNER_SYSTEM)

    def test_specific_chat_advice_can_route_to_private_surface_product_info(self):
        turn = self._turn("你看得到我跟小安剛才聊什麼嗎？我下一句要怎麼回？")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
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

        self.assertEqual([task.agent for task in plan.tasks], ["product_info", "synthesizer"])

    def test_matching_principles_is_a_typed_product_info_topic(self):
        turn = self._turn("你們到底是怎麼配對的？原理是什麼？")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "decompose_tasks",
                "arguments": {
                    "tasks": [
                        {"id": "p", "agent": "product_info", "depends_on": [], "task_brief": turn.message},
                        {"id": "s", "agent": "synthesizer", "depends_on": ["p"], "task_brief": "Compose."},
                    ],
                },
            }]),
        ):
            plan, metrics = plan_turn(turn)

        self.assertEqual(plan.mode, "tasks")
        self.assertEqual(plan.tasks[0].agent, "product_info")
        self.assertEqual(metrics.decision_mode, "tasks")

    def test_planner_system_policy_has_bounded_social_opening_contract(self):
        self.assertIn('opportunity.signal="social_opening"', _PLANNER_SYSTEM)
        self.assertIn("evidence_span", _PLANNER_SYSTEM)
        self.assertIn("0.8", _PLANNER_SYSTEM)
        self.assertIn('signal="none"', _PLANNER_SYSTEM)

    def test_planner_system_policy_routes_calendar_draft_continuations(self):
        self.assertIn("calendar_draft", _PLANNER_SYSTEM)
        self.assertIn("missing_fields", _PLANNER_SYSTEM)
        self.assertIn("candidates", _PLANNER_SYSTEM)
        self.assertIn("只負責路由", _PLANNER_SYSTEM)

    def test_planner_exposes_recent_mutation_only_as_bounded_verification_context(self):
        turn = self._turn("剛才那筆有成功嗎？").model_copy(update={
            "calendar_recent_mutation": {
                "action": "cancel", "outcome": "success", "labels": ["看牙醫"],
            },
        })
        from services.ayue_agent.v3.planner import _planner_prompt
        prompt = _planner_prompt(turn)
        self.assertIn("calendar_recent_mutation", prompt)
        self.assertIn("calendar_recent_mutation", _PLANNER_SYSTEM)
        self.assertIn("唯讀驗證", _PLANNER_SYSTEM)

    def test_planner_uses_system_role_and_minimal_routing_context(self):
        turn = self._turn("最近有什麼行程？")
        turn = turn.model_copy(update={
            "recent_context": "不要送進 Planner",
            "user_location": "不要送進 Planner",
            "relevant_memories": ["不要送進 Planner"],
        })
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "tasks": [{"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "回覆"}],
                }},
            ]),
        ) as call:
            plan_turn(turn)
        self.assertTrue(call.call_args.kwargs["system_prompt"])
        user_prompt = call.call_args.args[0]
        self.assertIn("最近有什麼行程", user_prompt)
        self.assertNotIn("不要送進 Planner", user_prompt)
        self.assertNotIn("pending_confirmations", user_prompt)
        self.assertNotIn("find_my_event", call.call_args.kwargs["system_prompt"])

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
            plan, _metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertEqual([task.agent for task in plan.tasks], ["profile", "synthesizer"])

    def test_inaccurate_existing_personality_profile_can_produce_basic_assessment_task(self):
        turn = self._turn("我覺得我資料上的個性不是我欸")
        expected = {
            "tasks": [
                {"id": "p1", "agent": "profile", "depends_on": [],
                 "task_brief": "既有個性資料不符合本人，重新開始基本性格探索並呼叫 profile.start_assessment(kind=basic)"},
                {"id": "s1", "agent": "synthesizer", "depends_on": ["p1"],
                 "task_brief": "呈現重新測驗的確認預覽"},
            ]
        }
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": expected},
            ]),
        ):
            plan, _metrics = plan_turn(turn)

        self.assertEqual([task.agent for task in plan.tasks], ["profile", "synthesizer"])

    def test_no_tool_call_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(content="我想先聊聊"),
        ):
            plan, _metrics = plan_turn(turn)
        self.assertIsNone(plan)

    def test_wrong_tool_name_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "nope.bad", "arguments": {}},
            ]),
        ):
            plan, _metrics = plan_turn(turn)
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
            plan, _metrics = plan_turn(turn)
        self.assertIsNone(plan)

    def test_planner_does_not_accept_type_as_agent_alias(self):
        turn = self._turn("幫我回覆這句話")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[
                {"name": "decompose_tasks", "arguments": {
                    "tasks": [{
                        "type": "synthesizer", "depends_on": [], "task_brief": "回覆",
                    }],
                }},
            ]),
        ):
            plan, _metrics = plan_turn(turn)
        self.assertIsNone(plan)

    def test_timeout_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion_with_tools",
            side_effect=TimeoutError("timeout"),
        ):
            plan, _metrics = plan_turn(turn)
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
        turn = PublicAgentTurnContext(
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
            plan, _metrics = plan_turn(turn)
        self.assertIsNotNone(plan)
        self.assertIsInstance(plan.opportunity, OpportunitySignal)
        self.assertEqual(plan.opportunity.signal, "social_opening")
        self.assertEqual(plan.opportunity.evidence_span, "一個人去有點孤單")


@unittest.skipUnless(
    os.getenv("AYUE_LIVE_PLANNER_SMOKE", "").strip().lower() in {"1", "true", "on"},
    "set AYUE_LIVE_PLANNER_SMOKE=1 to run provider-backed Planner smoke tests",
)
class V3PlannerLiveSmokeTests(unittest.TestCase):
    """Optional smoke coverage for the configured real function-calling provider."""

    def _turn(self, message):
        return PublicAgentTurnContext(
            user_id="live-planner-smoke", room_id="live-planner-smoke", message=message,
            clock=_clock(),
        )

    def _assert_agents(self, message, expected):
        plan, metrics = plan_turn(self._turn(message))
        self.assertIsNotNone(plan, metrics.error)
        self.assertEqual(plan.mode, "tasks")
        self.assertEqual({task.agent for task in plan.tasks}, set(expected) | {"synthesizer"})
        self.assertEqual(sum(task.agent == "synthesizer" for task in plan.tasks), 1)

    def test_live_simple_chat(self):
        plan, metrics = plan_turn(self._turn("我今天有點累"))
        self.assertIsNotNone(plan, metrics.error)
        self.assertEqual(plan.mode, "direct_chat")
        self.assertEqual(plan.tasks, [])
        self.assertTrue(plan.direct_reply)

    def test_live_places_request(self):
        self._assert_agents("幫我找台北車站附近的咖啡廳", {"places"})

    def test_live_explicit_match_request(self):
        self._assert_agents("請開始幫我找一位適合一起吃飯的人", {"match"})


if __name__ == "__main__":
    unittest.main()
