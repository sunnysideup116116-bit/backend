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
from services.ayue_agent.runtime import run_public_agent_turn
from services.ayue_agent.match_opportunity import MatchOpportunityAssessment


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
             patch("services.ayue_agent.router.generate_chat_completion", side_effect=["not-json", "我在呀。"]), \
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
             patch("services.ayue_agent.router.generate_chat_completion", side_effect=TimeoutError("provider timeout")), \
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
        low_confidence = planner_payload(
            kind="tool_call", intent="calendar",
            tool_name="calendar.list_my_events", confidence=0.2,
        )
        trace = {}
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.router.generate_chat_completion", return_value=low_confidence), \
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
             patch("services.ayue_agent.runtime.plan_turn_v2", return_value=decision), \
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
             patch("services.ayue_agent.runtime.plan_turn_v2", return_value=decision), \
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
             patch("services.ayue_agent.runtime.plan_turn_v2", return_value=decision), \
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
             patch("services.ayue_agent.runtime.plan_turn_v2", return_value=decision), \
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
             patch("services.ayue_agent.runtime.plan_turn_v2", return_value=decision), \
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
             patch("services.ayue_agent.runtime.plan_turn_v2", return_value=decision), \
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

    def test_real_json_adapter_duplicate_sequence_reuses_observation(self):
        ctx, turn = self._context("今天幾月幾號？")
        time_call = planner_payload(
            kind="tool_call", intent="time",
            tool_name="system.get_current_time", confidence=0.95,
        )
        trace = {}
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch(
                 "services.ayue_agent.router.generate_chat_completion",
                 side_effect=[time_call, time_call, "今天是 2026-07-30。"],
             ), \
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
             patch("services.ayue_agent.runtime.plan_turn_v2", side_effect=decisions), \
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


if __name__ == "__main__":
    unittest.main()
