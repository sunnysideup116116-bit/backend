import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.contracts import (
    AgentDecision,
    AgentIntent,
    AgentTurnContext,
    AgentTurnContextV2,
    DecisionKind,
    ToolResult,
)
from services.ai_service import ToolCallResult
from services.ayue_agent.runtime import (
    _place_search_arguments,
    _save_place_search_draft,
    run_public_agent_turn,
)
from services.ayue_agent.match_opportunity import MatchOpportunityAssessment


def _fc_result(content="", tool_calls=None):
    """Build a ToolCallResult for the function-calling planner path."""
    return ToolCallResult(content=content, tool_calls=tool_calls or [])


def planner_payload(**overrides):
    payload = {
        "kind": "final",
        "intent": "chat",
        "tool_name": None,
        "arguments": {},
        "confidence": 0.95,
        "evidence_span": None,
        "reply": "",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


class AyueAgentPlannerSequenceTests(unittest.TestCase):
    def _context(self, message):
        ctx = AgentTurnContext(
            user_id="fixture_owner", room_id="fixture_public_room", message=message,
        )
        turn = AgentTurnContextV2(
            user_id=ctx.user_id, room_id=ctx.room_id, message=message,
        )
        return ctx, turn

    def test_bad_json_fails_closed_and_composes_without_a_tool(self):
        ctx, turn = self._context("最近還好嗎")
        trace = {}
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.router.generate_chat_completion_with_tools", side_effect=ValueError("bad response")), \
             patch("services.ayue_agent.router.generate_chat_completion", return_value=SimpleNamespace(content="我在呀。", input_tokens=0, output_tokens=0, duration_ms=0, prompt="")), \
             patch("services.ayue_agent.runtime.execute_tool") as execute_tool, \
             patch("services.ayue_agent.runtime._save_trace", side_effect=lambda _run, _ctx, value: trace.update(value)):
            result = run_public_agent_turn(ctx, mode="on")
        execute_tool.assert_not_called()
        self.assertEqual(result.agent_mode, "v2")
        self.assertEqual(result.fallback_reason, "planner_invalid")
        self.assertEqual(result.reply, "我在呀。")
        self.assertEqual(trace["composer_outcome"]["result_code"], "llm_reply")

    def test_planner_timeout_fails_closed_without_legacy_or_tool_execution(self):
        ctx, turn = self._context("幫我看看")
        trace = {}
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.router.generate_chat_completion_with_tools", side_effect=TimeoutError("provider timeout")), \
             patch("services.ayue_agent.router.generate_chat_completion", side_effect=Exception("provider error")), \
             patch("services.ayue_agent.runtime.execute_tool") as execute_tool, \
             patch("services.ayue_agent.runtime._save_trace", side_effect=lambda _run, _ctx, value: trace.update(value)):
            result = run_public_agent_turn(ctx, mode="on")
        execute_tool.assert_not_called()
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v2")
        self.assertEqual(result.fallback_reason, "planner_invalid")
        self.assertEqual(
            trace["composer_outcome"]["result_code"],
            "deterministic_fallback:provider_error",
        )

    def test_low_confidence_tool_call_is_guarded_without_execution(self):
        ctx, turn = self._context("我什麼時候沒空")
        low_confidence_tool = _fc_result(tool_calls=[{
            "name": "calendar.list_my_events", "arguments": {},
        }])
        # Attach low confidence via meta line in content so the parser fills it.
        low_confidence_tool = ToolCallResult(
            content='\n[[meta]]{"confidence":0.2}',
            tool_calls=[{"name": "calendar.list_my_events", "arguments": {}}],
        )
        trace = {}
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.router.generate_chat_completion_with_tools", return_value=low_confidence_tool), \
             patch("services.ayue_agent.runtime.execute_tool") as execute_tool, \
             patch("services.ayue_agent.runtime._save_trace", side_effect=lambda _run, _ctx, value: trace.update(value)):
            result = run_public_agent_turn(ctx, mode="on")
        execute_tool.assert_not_called()
        self.assertEqual(result.fallback_reason, "guard:low_confidence")
        self.assertIn("low_confidence", trace["guard_results"])

    def test_planner_confirmation_uses_match_readiness_clarification_not_guard_text(self):
        ctx, turn = self._context("你幫我約個人一起去好不好")
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION,
            intent=AgentIntent.MATCH_ACTION,
            tool_name="match.start_search",
            confidence=.95,
            evidence_span="約個人一起去",
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.assess_match_opportunity", return_value=MatchOpportunityAssessment("not_ready")), \
             patch("services.ayue_agent.runtime.missing_basis_question", return_value="你是想找一位新的旅伴，還是邀請已經認識的人？"), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertIsNone(result.fallback_reason)
        self.assertIn("你是想找一位新的旅伴，還是邀請已經認識的人？", result.reply)
        self.assertNotIn("暫時不會執行", result.reply)

    def test_social_opening_creates_one_gentle_offer_when_profile_is_ready(self):
        ctx, turn = self._context("我一個人去合掌村有點孤單")
        decision = AgentDecision(
            kind=DecisionKind.FINAL, intent=AgentIntent.CHAT, confidence=.95,
            opportunity_signal="social_opening", opportunity_confidence=.95,
            opportunity_evidence_span="一個人去合掌村有點孤單",
        )
        assessment = MatchOpportunityAssessment("ready", fingerprint="ready-fingerprint")
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.assess_match_opportunity", return_value=assessment), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one") as update, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.conversation_intent, "match_guidance")
        self.assertTrue(result.match_guidance_shown)
        self.assertIn("不會隨機配", result.reply)
        self.assertNotIn("物件", result.reply)
        self.assertEqual(update.call_args.args[1]["$set"]["agentic_pending_confirmation"]["source"], "opportunity_guidance")

    def test_low_confidence_confirmation_cannot_write_pending_state_before_guard(self):
        ctx, turn = self._context("幫我找一位旅伴")
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.MATCH_ACTION,
            tool_name="match.start_search", confidence=.2, evidence_span="幫我找一位旅伴",
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.assess_match_opportunity") as assess, \
             patch("services.ayue_agent.runtime.profiles_coll.update_one") as update, \
             patch("services.ayue_agent.runtime.generate_clarification_reply_v2", return_value="你想找新的旅伴嗎？"), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.fallback_reason, "guard:low_confidence")
        assess.assert_not_called()
        update.assert_not_called()

    def test_social_opening_requires_current_message_evidence_at_runtime_boundary(self):
        ctx, turn = self._context("我最近想去合掌村")
        decision = AgentDecision(
            kind=DecisionKind.FINAL, intent=AgentIntent.CHAT, confidence=.95,
            opportunity_signal="social_opening", opportunity_confidence=.95,
            opportunity_evidence_span="我一個人很孤單",
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.assess_match_opportunity") as assess, \
             patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value="合掌村很漂亮。"), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        assess.assert_not_called()
        self.assertEqual(result.reply, "合掌村很漂亮。")

    def test_concurrent_explicit_search_cannot_overwrite_existing_confirmation(self):
        ctx, turn = self._context("幫我找一位旅伴")
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.MATCH_ACTION,
            tool_name="match.start_search", confidence=.95, evidence_span="幫我找一位旅伴",
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.assess_match_opportunity", return_value=MatchOpportunityAssessment("ready")), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=0)), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertIn("上一個找人確認", result.reply)
        self.assertEqual(result.conversation_intent, "match_confirmation")

    def test_tool_validation_failure_is_composed_as_a_natural_follow_up(self):
        ctx, turn = self._context("我下周三趣如何")
        decision = AgentDecision(
            kind=DecisionKind.TOOL_CALL,
            intent=AgentIntent.TIME,
            tool_name="system.get_current_time",
            confidence=.95,
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=ToolResult(
                 ok=False,
                 error_code="invalid_tool_output",
                 user_message="我現在無法安全地整理這項資訊。",
             )), \
             patch(
                 "services.ayue_agent.runtime.generate_clarification_reply_v2",
                 return_value="你是想問下週三有沒有空，還是想討論那天的安排？",
             ), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.fallback_reason, "tool:invalid_tool_output")
        self.assertEqual(result.reply, "你是想問下週三有沒有空，還是想討論那天的安排？")
        self.assertNotIn("無法安全地整理", result.reply)

    def test_function_calling_duplicate_sequence_reuses_observation(self):
        ctx, turn = self._context("今天幾月幾號？")
        time_tool_call = _fc_result(tool_calls=[{
            "name": "system.get_current_time", "arguments": {},
        }])
        # Second planner call returns the same tool call (should be deduped).
        # Third returns a final text reply.
        fc_side_effects = [
            time_tool_call,
            time_tool_call,
            _fc_result(content="今天是 2026-07-30。"),
        ]
        trace = {}
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch(
                 "services.ayue_agent.router.generate_chat_completion_with_tools",
                 side_effect=fc_side_effects,
             ), \
             patch("services.ayue_agent.router.generate_chat_completion", return_value=SimpleNamespace(content="今天是 2026-07-30。", input_tokens=0, output_tokens=0, duration_ms=0, prompt="")), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=ToolResult(
                 ok=True,
                 data={
                     "timezone": "Asia/Taipei", "local_date": "2026-07-30",
                     "local_time": "14:30", "weekday_zh_tw": "星期四",
                 },
             )) as execute_tool, \
             patch("services.ayue_agent.runtime._save_trace", side_effect=lambda _run, _ctx, value: trace.update(value)):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(execute_tool.call_count, 1)
        self.assertEqual(result.reply, "今天是 2026-07-30。")
        self.assertEqual(trace["tool_cache_hits"], ["system.get_current_time"])
        self.assertEqual(trace["composer_outcome"]["result_code"], "llm_reply")

    def test_relationship_comparison_reads_contact_then_owner_before_final(self):
        ctx, turn = self._context("我跟小玟會擦出什麼火花？")
        decisions = [
            AgentDecision(
                kind=DecisionKind.FINAL, intent=AgentIntent.RELATIONSHIP,
                confidence=.95, reply="你們應該聊得來。",
            ),
            AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.RELATIONSHIP, confidence=.95),
            AgentDecision(
                kind=DecisionKind.FINAL, intent=AgentIntent.RELATIONSHIP,
                confidence=.95, reply="你們都重視真誠溝通，可以先從最近想看的電影聊起。",
            ),
        ]
        contact_result = ToolResult(ok=True, data={
            "contacts": [{
                "display_name": "小玟", "initial_interest": "獨立電影",
                "personality_summary": "願意主動分享想法",
                "verified_common_ground": ["真誠溝通"],
            }],
            "truncated": False,
        })
        owner_result = ToolResult(ok=True, data={
            "display_name": "小安", "initial_interest": "紀錄片",
            "personality_summary": "好奇且願意主動開話題",
            "values": ["真誠溝通"], "preferences": ["獨立電影"],
            "missing_sections": [],
        })
        trace = {}
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", side_effect=[contact_result, owner_result]) as execute_tool, \
             patch("services.ayue_agent.runtime._save_trace", side_effect=lambda _run, _ctx, value: trace.update(value)):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(execute_tool.call_count, 2)
        self.assertEqual(
            [call.args[0].name for call in execute_tool.call_args_list],
            ["relationship.list_accepted_contacts", "profile.get_self_summary"],
        )
        self.assertIn("relationship_requires_read", trace["guard_results"])
        self.assertIn("relationship_comparison_requires_self", trace["guard_results"])
        self.assertIn("真誠溝通", result.reply)

    def test_distance_reuses_success_when_planner_paraphrases_arguments(self):
        ctx, turn = self._context("新咖哩到弗金森市集多遠？")
        decisions = [
            AgentDecision(
                kind=DecisionKind.TOOL_CALL, intent=AgentIntent.PLACES,
                tool_name="places.measure_distance",
                arguments={"origin": "新咖哩", "destination": "弗金森市集", "use_saved_origin": False},
                confidence=.95,
            ),
            AgentDecision(
                kind=DecisionKind.TOOL_CALL, intent=AgentIntent.PLACES,
                tool_name="places.measure_distance",
                arguments={"origin": "新咖哩", "destination": "弗金森市集（高雄）", "use_saved_origin": False},
                confidence=.95,
            ),
        ]
        distance_result = ToolResult(ok=True, data={
            "origin_label": "新咖哩", "destination_label": "弗金森市集",
            "origin_kind": "explicit", "distance_m": 780,
            "distance_basis": "straight_line", "attribution": "© OpenStreetMap contributors",
            "attribution_url": "https://www.openstreetmap.org/copyright",
        })
        trace = {}
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=distance_result) as execute_tool, \
             patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value="兩地直線距離約 780 公尺。"), \
             patch("services.ayue_agent.runtime._save_trace", side_effect=lambda _run, _ctx, value: trace.update(value)):
            result = run_public_agent_turn(ctx, mode="on")
        execute_tool.assert_called_once()
        self.assertEqual(execute_tool.call_args.args[0].name, "places.measure_distance")
        self.assertEqual(result.reply, "兩地直線距離約 780 公尺。")
        self.assertEqual(trace["tool_cache_hits"], ["places.measure_distance"])
        self.assertIn("successful_observation_reused", trace["guard_results"])

    def test_distance_does_not_reuse_a_different_endpoint_pair(self):
        ctx, turn = self._context("新咖哩到弗金森市集，以及新咖哩到駁二，各多遠？")
        decisions = [
            AgentDecision(
                kind=DecisionKind.TOOL_CALL, intent=AgentIntent.PLACES,
                tool_name="places.measure_distance",
                arguments={"origin": "新咖哩", "destination": "弗金森市集", "use_saved_origin": False},
                confidence=.95,
            ),
            AgentDecision(
                kind=DecisionKind.TOOL_CALL, intent=AgentIntent.PLACES,
                tool_name="places.measure_distance",
                arguments={"origin": "新咖哩", "destination": "駁二藝術特區", "use_saved_origin": False},
                confidence=.95,
            ),
            AgentDecision(
                kind=DecisionKind.FINAL, intent=AgentIntent.PLACES, confidence=.95,
                reply="新咖哩到弗金森市集約 780 公尺，到駁二藝術特區約 1.2 公里，都是直線距離。",
            ),
        ]
        results = [
            ToolResult(ok=True, data={
                "origin_label": "新咖哩", "destination_label": "弗金森市集",
                "origin_kind": "explicit", "distance_m": 780,
                "distance_basis": "straight_line", "attribution": "© OpenStreetMap contributors",
                "attribution_url": "https://www.openstreetmap.org/copyright",
            }),
            ToolResult(ok=True, data={
                "origin_label": "新咖哩", "destination_label": "駁二藝術特區",
                "origin_kind": "explicit", "distance_m": 1200,
                "distance_basis": "straight_line", "attribution": "© OpenStreetMap contributors",
                "attribution_url": "https://www.openstreetmap.org/copyright",
            }),
        ]
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", side_effect=results) as execute_tool, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(execute_tool.call_count, 2)
        self.assertIn("弗金森市集", result.reply)
        self.assertIn("駁二藝術特區", result.reply)

    def test_decline_followup_uses_canonical_status_read(self):
        ctx, turn = self._context("為什麼？")
        turn.recent_messages = [
            {"role": "assistant", "content": "這次對方先婉拒了。"},
        ]
        decisions = [
            AgentDecision(
                kind=DecisionKind.TOOL_CALL,
                intent=AgentIntent.MATCH_STATUS,
                tool_name="match.get_status",
                confidence=.95,
            ),
            AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.MATCH_STATUS, confidence=.95),
        ]
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=ToolResult(
                 ok=True,
                 data={
                     "state": "declined", "scope": "latest_terminal", "is_terminal": True,
                     "chat_opened": False, "counterparty": "對方", "reason_code": None,
                 },
             )) as execute_tool, \
             patch(
                 "services.ayue_agent.runtime.generate_final_reply_v2",
                 return_value="這次對方婉拒了，但沒有留下可確認的原因，所以我不替對方猜。",
             ), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertIn("不替對方猜", result.reply)
        self.assertEqual(execute_tool.call_count, 1)
        self.assertEqual(execute_tool.call_args.args[0].name, "match.get_status")

    def test_random_restaurant_followup_forces_search_instead_of_repeating_cuisine_question(self):
        ctx, turn = self._context("你隨意推薦")
        turn.user_location = "高雄市鹽埕區"
        turn.place_search_draft = {
            "version": "v1", "anchor": "", "categories": ["restaurant"],
            "radius_m": 1500, "limit": 3, "use_saved_location": True,
            "created_at": 1,
        }
        decisions = [
            AgentDecision(
                kind=DecisionKind.FINAL, intent=AgentIntent.PLACES, confidence=.95,
                place_search_followup="recommend", reply="那你想找哪一類的料理呢？",
            ),
            AgentDecision(
                kind=DecisionKind.FINAL, intent=AgentIntent.PLACES, confidence=.95,
                reply="我先幫你挑三間鹽埕區附近的餐廳。",
            ),
        ]
        place_result = ToolResult(ok=True, data={
            "anchor_label": "高雄市鹽埕區",
            "places": [{
                "name": "六姐傳統飯糰特製蛋餅專賣店", "category": "restaurant",
                "distance_m": 420, "address_summary": "高雄市鹽埕區",
                "map_url": "https://www.openstreetmap.org/?mlat=22.62&mlon=120.28",
                "provider": "openstreetmap",
            }],
            "attribution": "© OpenStreetMap contributors",
            "attribution_url": "https://www.openstreetmap.org/copyright",
        })
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=place_result) as execute_tool, \
             patch("services.ayue_agent.runtime.profiles_coll.update_one") as update, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        execute_tool.assert_called_once()
        call = execute_tool.call_args.args[0]
        self.assertEqual(call.name, "places.search_nearby")
        self.assertEqual(call.arguments["categories"], ["restaurant"])
        self.assertTrue(call.arguments["use_saved_location"])
        self.assertNotIn("哪一類", result.reply)
        self.assertEqual(result.place_cards[0]["name"], "六姐傳統飯糰特製蛋餅專賣店")
        self.assertIn("agentic_place_search_draft", update.call_args.args[1]["$unset"])

    def test_first_place_clarification_persists_bounded_search_state(self):
        ctx, turn = self._context("還有什麼吃的")
        turn.user_location = "高雄市鹽埕區"
        decision = AgentDecision(
            kind=DecisionKind.FINAL, intent=AgentIntent.PLACES, confidence=.95,
            place_search_followup="recommend", reply="你是在問鹽埕區附近的餐廳嗎？",
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one") as update, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        draft = update.call_args.args[1]["$set"]["agentic_place_search_draft"]
        self.assertEqual(draft["categories"], ["restaurant"])
        self.assertTrue(draft["use_saved_location"])
        self.assertEqual(draft["limit"], 5)
        self.assertIn("鹽埕區", result.reply)

    def test_place_search_arguments_carries_cuisine_from_draft(self):
        ctx, turn = self._context("我想吃火鍋")
        turn.user_location = "高雄市鹽埕區"
        turn.place_search_draft = {
            "version": "v1", "anchor": "", "categories": ["restaurant"],
            "cuisine": "火鍋", "radius_m": 1500, "limit": 3,
            "use_saved_location": True, "created_at": 1,
        }
        arguments = _place_search_arguments(turn)
        self.assertEqual(arguments["cuisine"], "火鍋")

    def test_place_search_arguments_merges_planner_cuisine(self):
        ctx, turn = self._context("我想吃日式料理")
        turn.user_location = "高雄市鹽埕區"
        turn.place_search_draft = {
            "version": "v1", "anchor": "", "categories": ["restaurant"],
            "radius_m": 1500, "limit": 3, "use_saved_location": True, "created_at": 1,
        }
        decision = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.PLACES, confidence=.95,
            tool_name="places.search_nearby",
            arguments={"categories": ["restaurant"], "cuisine": "日式"},
        )
        arguments = _place_search_arguments(turn, decision)
        self.assertEqual(arguments["cuisine"], "日式")

    def test_save_place_search_draft_persists_cuisine(self):
        ctx, turn = self._context("我想吃火鍋")
        turn.user_location = "高雄市鹽埕區"
        decision = AgentDecision(
            kind=DecisionKind.FINAL, intent=AgentIntent.PLACES, confidence=.95,
            place_search_followup="recommend", reply="你想找哪一類的料理呢？",
            arguments={"categories": ["restaurant"], "cuisine": "火鍋"},
        )
        with patch("services.ayue_agent.runtime.profiles_coll.update_one") as update:
            _save_place_search_draft(ctx, turn, decision)
        draft = update.call_args.args[1]["$set"]["agentic_place_search_draft"]
        self.assertEqual(draft["cuisine"], "火鍋")

    def test_self_profile_final_is_forced_through_the_owner_summary_read(self):
        ctx, turn = self._context("你了解我多少？")
        decisions = [
            AgentDecision(
                kind=DecisionKind.FINAL, intent=AgentIntent.PROFILE,
                confidence=.95, reply="你是正在跟我聊天的使用者。",
            ),
            AgentDecision(
                kind=DecisionKind.FINAL, intent=AgentIntent.PROFILE,
                confidence=.95, reply="你喜歡獨立電影，也很重視真誠溝通。",
            ),
        ]
        profile_result = ToolResult(ok=True, data={
            "display_name": "小安", "initial_interest": "喜歡獨立電影",
            "personality_summary": "好奇且願意主動開話題",
            "values": ["真誠溝通"], "preferences": ["喜歡獨立電影"],
            "missing_sections": ["深層資料"],
        })
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=profile_result) as execute_tool, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(execute_tool.call_count, 1)
        self.assertEqual(execute_tool.call_args.args[0].name, "profile.get_self_summary")
        self.assertIn("獨立電影", result.reply)
        self.assertNotIn("正在跟我聊天的使用者", result.reply)

    def test_assessment_confirmation_starts_a_session_without_replanning(self):
        ctx, turn = self._context("確認")
        turn.pending_confirmation = {
            "action": "profile.start_assessment", "arguments": {"kind": "basic"}, "confirmation_id": "confirm-1",
            "created_at": 1.0, "status": "pending",
        }
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one", return_value=SimpleNamespace(modified_count=1)), \
             patch("services.ayue_agent.runtime.start_assessment_session", return_value={
                 "status": "started", "reply": "你臨時有空時，通常怎麼安排？",
             }) as start, \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        start.assert_called_once_with("fixture_owner", "big_five", idempotency_key="assessment-confirmation:confirm-1")
        planner.assert_not_called()
        self.assertEqual(result.conversation_intent, "assessment")
        self.assertIn("通常怎麼安排", result.reply)

    def test_active_assessment_bypasses_general_planner_and_can_exit(self):
        ctx, turn = self._context("我會先找朋友一起討論")
        ctx.user_profile = {
            "agentic_assessment_session": {
                "session_id": "session-1", "kind": "big_five", "status": "active",
                "expires_at": 9_999_999_999,
            }
        }
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.advance_assessment_session", return_value={
                 "status": "continued", "reply": "聽起來你很重視一起想辦法。那遇到臨時變動時呢？",
             }) as advance, \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        advance.assert_called_once()
        planner.assert_not_called()
        self.assertEqual(result.conversation_intent, "assessment")

        ctx.message = "結束測驗"
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.cancel_assessment_session", return_value={"status": "cancelled"}) as cancel, \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            exited = run_public_agent_turn(ctx, mode="on")
        cancel.assert_called_once_with("fixture_owner", "session-1", "big_five")
        planner.assert_not_called()
        self.assertIn("原本已完成的資料", exited.reply)

    def test_expired_assessment_answer_does_not_fall_through_to_planner_or_profile_extraction(self):
        ctx, turn = self._context("這是上一輪測驗的回答")
        ctx.user_profile = {
            "agentic_assessment_session": {
                "session_id": "expired-session", "kind": "big_five", "status": "active",
                "revision": 3, "expires_at": 1,
            }
        }
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.expire_assessment_session", return_value={
                 "status": "expired", "session_state": "expired", "kind": "big_five", "revision": 4,
             }) as expire, \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        expire.assert_called_once_with("fixture_owner", "expired-session", "big_five")
        planner.assert_not_called()
        self.assertEqual(result.assessment_state, "expired")
        self.assertEqual(result.profile_write_reason, "assessment")

    def test_commit_cancel_race_reports_the_winning_completed_state(self):
        ctx, turn = self._context("取消")
        ctx.user_profile = {
            "agentic_assessment_session": {
                "session_id": "session-race", "kind": "big_five", "status": "awaiting_commit",
                "revision": 4, "expires_at": 9_999_999_999, "draft": {},
            }
        }
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.cancel_assessment_session", return_value={
                 "status": "stale", "session_state": "completed", "kind": "big_five",
                 "revision": 5, "reply": "這份結果已由另一個分頁套用。",
             }), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        planner.assert_not_called()
        self.assertEqual(result.assessment_state, "completed")
        self.assertIn("另一個分頁", result.reply)

    def test_commit_confirmation_reports_a_concurrent_cancellation(self):
        ctx, turn = self._context("確認")
        ctx.user_profile = {
            "agentic_assessment_session": {
                "session_id": "session-race", "kind": "big_five", "status": "awaiting_commit",
                "revision": 4, "expires_at": 9_999_999_999, "draft": {},
            }
        }
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.commit_assessment_session", return_value={
                 "status": "stale", "session_state": "cancelled", "kind": "big_five",
                 "revision": 5, "reply": "這份草稿已由另一個分頁取消。",
             }), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        planner.assert_not_called()
        self.assertEqual(result.assessment_state, "cancelled")
        self.assertIn("另一個分頁", result.reply)

    def test_completed_assessment_requires_a_second_commit_confirmation(self):
        ctx, turn = self._context("確認")
        ctx.user_profile = {
            "agentic_assessment_session": {
                "session_id": "session-commit", "kind": "big_five", "status": "awaiting_commit",
                "revision": 4, "expires_at": 9_999_999_999,
                "draft": {"O": 7, "C": 6, "E": 6, "A": 7, "N": 4, "summary": "喜歡探索"},
            }
        }
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.commit_assessment_session", return_value={
                 "status": "committed", "reply": "新的結果已套用。", "kind": "big_five",
             }) as commit, \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        commit.assert_called_once_with(
            "fixture_owner", "session-commit", expected_revision=4,
            idempotency_key="assessment-commit:session-commit:4",
        )
        planner.assert_not_called()
        self.assertEqual(result.assessment_state, "completed")
        self.assertIn("套用", result.reply)

    def test_assessment_start_failure_clears_executing_confirmation(self):
        ctx, turn = self._context("確認")
        turn.pending_confirmation = {
            "action": "profile.start_assessment", "arguments": {"kind": "deep"}, "confirmation_id": "confirm-2",
            "created_at": 2.0, "status": "pending",
        }
        updates = []

        def update_one(query, mutation, **kwargs):
            updates.append((query, mutation))
            return SimpleNamespace(modified_count=1)

        events = []
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one", side_effect=update_one), \
             patch("services.ayue_agent.runtime.start_assessment_session", side_effect=RuntimeError("db secret")), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on", on_progress=events.append)
        planner.assert_not_called()
        self.assertIn("沒有改動原本的資料", result.reply)
        self.assertNotIn("secret", result.reply)
        self.assertEqual(updates[-1][0]["agentic_pending_confirmation.confirmation_id"], "confirm-2")
        self.assertIn("agentic_pending_confirmation", updates[-1][1]["$unset"])
        finished = [event for event in events if event.get("type") == "tool_finished"]
        self.assertEqual(finished[-1]["outcome"], "error")


if __name__ == "__main__":
    unittest.main()
