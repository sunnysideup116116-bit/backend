import os
import unittest
from unittest.mock import ANY, Mock, patch
from types import SimpleNamespace

from services.ayue_agent.contracts import AgentDecision, AgentIntent, AgentResult, AgentTurnContext, AgentTurnContextV2, DecisionKind, ToolResult
from services.ayue_agent.legacy_match_routing import (
    is_explicit_match_request,
    is_match_outcome_followup,
    should_answer_match_outcome_followup,
)
from services.ayue_agent.runtime import agent_mode_for_user, run_public_agent_turn
from services.ayue_agent.match_opportunity import MatchOpportunityAssessment


class AyueAgentRouterTests(unittest.TestCase):
    def test_direct_counterparty_date_request_never_enters_calendar_tools(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="你可以直接幫我跟對方約會嗎")
        turn = AgentTurnContextV2(user_id="owner", room_id="room", message=ctx.message)
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=AgentDecision(
                 kind=DecisionKind.FINAL, intent=AgentIntent.RELATIONSHIP, confidence=.95,
             )) as planner, \
             patch("services.ayue_agent.runtime.execute_tool") as execute_tool, \
             patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value="我不能替你直接向對方發邀請，但你可以先在共同聊天室問他。"), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        planner.assert_called_once()
        execute_tool.assert_not_called()
        self.assertIn("不能替你直接", result.reply)
        self.assertFalse(result.profile_write_allowed)

    def test_calendar_clarification_creates_a_draft_instead_of_a_generic_template(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="可以幫我修改嗎")
        turn = AgentTurnContextV2(user_id="owner", room_id="room", message=ctx.message)
        decision = AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.CALENDAR_ACTION, confidence=.95,
                                 clarification_goal="calendar_action", missing_fields=["event_hint"])
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one") as persist, \
             patch("services.ayue_agent.runtime.generate_clarification_reply_v2", return_value="你想改剛才哪一筆行程？"), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.reply, "你想改剛才哪一筆行程？")
        self.assertNotIn("新增、修改，還是取消", result.reply)
        self.assertEqual(persist.call_args.args[1]["$set"]["agentic_action_draft"]["domain"], "calendar")
        self.assertFalse(result.profile_write_allowed)

    def test_calendar_cancel_tool_call_is_promoted_to_one_confirmation(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="取消週三的咖啡")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        decision = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.CALENDAR_ACTION,
            tool_name="calendar.cancel_my_event", arguments={"event_hint": "週三的咖啡"},
            confidence=.95, evidence_span=ctx.message,
        )
        prepared = AgentResult(handled=True, reply="要取消嗎？", conversation_intent="calendar_confirmation")
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision) as planner, \
             patch("services.ayue_agent.runtime._prepare_calendar_confirmation", return_value=prepared) as prepare, \
             patch("services.ayue_agent.runtime.execute_tool") as execute, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.reply, "要取消嗎？")
        self.assertEqual(planner.call_count, 1)
        prepare.assert_called_once()
        execute.assert_not_called()

    def test_vague_recent_intent_opens_one_context_draft(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="最近想做點事情")
        turn = AgentTurnContextV2(user_id="owner", room_id="room", message=ctx.message)
        decision = AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.CHAT, confidence=.95,
                                 evidence_span=ctx.message, recent_context_followup="ask_activity")
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one") as persist, \
             patch("services.ayue_agent.runtime.generate_clarification_reply_v2", return_value="你最近比較想做什麼，或想去哪裡走走？"), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertIn("想做什麼", result.reply)
        self.assertIn("recent_context_draft", persist.call_args.args[1]["$set"])
        self.assertFalse(result.profile_write_allowed)
    def test_public_v2_mode_has_explicit_legacy_rollback_and_allowlist(self):
        with patch.dict(os.environ, {"AYUE_AGENT_V2_MODE": "off", "AYUE_AGENT_V2_USER_ALLOWLIST": ""}, clear=False):
            self.assertEqual(agent_mode_for_user("fixture_owner"), "off")
        with patch.dict(os.environ, {"AYUE_AGENT_V2_MODE": "on", "AYUE_AGENT_V2_USER_ALLOWLIST": "fixture_owner"}, clear=False):
            self.assertEqual(agent_mode_for_user("fixture_owner"), "on")
            self.assertEqual(agent_mode_for_user("another_owner"), "off")

    def test_match_outcome_question_never_starts_search(self):
        for message in ("為什麼!!!", "怎麼回事？", "為什麼他婉拒我"):
            self.assertTrue(is_match_outcome_followup(message))
            self.assertFalse(is_explicit_match_request(message))

    def test_bare_why_requires_a_previous_decline_message(self):
        self.assertFalse(should_answer_match_outcome_followup("為什麼!!!", "要不要看看你的行事曆？"))
        self.assertTrue(should_answer_match_outcome_followup("為什麼!!!", "對方這次先婉拒了邀請。"))
        self.assertTrue(should_answer_match_outcome_followup("為什麼他婉拒我", ""))

    def test_explicit_match_request_still_requires_actual_search_language(self):
        for message in ("幫我找人", "找下一位", "重新找一個對象"):
            self.assertTrue(is_explicit_match_request(message))
        for message in ("為什麼", "我只是想知道原因", "他怎麼婉拒了"):
            self.assertFalse(is_explicit_match_request(message))
    def test_runtime_rejects_hidden_match_tool_before_execution(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="ai_assistant_demo_user", message="我不喜歡抽菸的人")
        wrong = AgentDecision(kind=DecisionKind.TOOL_CALL, tool_name="match.get_active_state", confidence=0.95)
        turn = AgentTurnContextV2(user_id="demo_user", room_id="ai_assistant_demo_user", message=ctx.message)
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=wrong), \
             patch("services.ayue_agent.runtime._save_trace") as save_trace, \
             patch("services.ayue_agent.runtime.execute_tool") as execute_tool:
            result = run_public_agent_turn(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertTrue(result.fallback_reason.startswith("guard:"))
        execute_tool.assert_not_called()
        self.assertIn("tool_not_visible", save_trace.call_args.args[2]["guard_results"])

    def test_runtime_status_question_uses_canonical_snapshot(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="ai_assistant_demo_user", message="她到底有沒有點？")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        decisions = [
            AgentDecision(kind=DecisionKind.TOOL_CALL, intent=AgentIntent.MATCH_STATUS,
                          tool_name="match.get_status", confidence=.95),
            AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.MATCH_STATUS, confidence=.95),
        ]
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=ToolResult(
                 ok=True, data={"state": "accepted", "counterparty": "對方"}
             )) as execute_tool, \
             patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value="有，對方已經接受了。") as composer, \
             patch("services.ayue_agent.runtime._save_trace") as save_trace:
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.reply, "有，對方已經接受了。")
        self.assertEqual(execute_tool.call_args.args[0].name, "match.get_status")
        self.assertEqual(composer.call_args.args[1][0]["tool"], "match.get_status")

    def test_runtime_executes_typed_grounded_proposal_decision(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="有興趣，幫我問問")
        turn = AgentTurnContextV2(
            user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message,
            active_proposal={"user_can_decide": True, "proposal_revision": 3},
        )
        decision = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.MATCH_ACTION,
            tool_name="match.decide_active_proposal",
            arguments={"decision": "interested"}, confidence=.95,
            evidence_span="有興趣，幫我問問",
        )
        executor = Mock(return_value=(True, "好，我已更新這張牽線提案。", None))
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch.dict("services.ayue_agent.runtime._WRITE_EXECUTORS", {"decide_active_proposal": executor}), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.reply, "好，我已更新這張牽線提案。")
        executor.assert_called_once_with(ctx, turn, ANY, 0, {"decision": "interested"})

    def test_pending_confirmation_executes_before_planner(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="確認")
        turn = AgentTurnContextV2(
            user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message,
            pending_confirmation={
                "version": "v2", "confirmation_id": "confirm-1", "action": "match.start_search",
                "status": "pending", "created_at": 100.0, "expires_at": 9999999999.0,
            },
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one", side_effect=[SimpleNamespace(modified_count=1), SimpleNamespace(modified_count=1)]), \
             patch("services.ayue_agent.runtime.assess_match_opportunity", return_value=MatchOpportunityAssessment("ready")), \
             patch("services.ayue_agent.runtime._start_search", return_value=(True, "已開始找人", None)) as start, \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.reply, "已開始找人")
        self.assertEqual(start.call_args.kwargs["idempotency_key"], "confirmation:confirm-1")
        planner.assert_not_called()

    def test_pending_search_confirmation_is_invalidated_when_match_state_advances(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="確認")
        turn = AgentTurnContextV2(
            user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message,
            pending_confirmation={
                "version": "v2", "confirmation_id": "confirm-stale",
                "action": "match.start_search", "status": "pending",
                "created_at": 100.0, "expires_at": 9999999999.0,
            },
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.assess_match_opportunity", return_value=MatchOpportunityAssessment("active_match_blocked", ("active_match",))), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one") as update, \
             patch("services.ayue_agent.runtime._start_search") as start, \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling") as planner, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.match_readiness_state, "active_match_blocked")
        self.assertIn("取消這個舊搜尋確認", result.reply)
        self.assertIn("$unset", update.call_args.args[1])
        start.assert_not_called()
        planner.assert_not_called()

    def test_semantic_match_action_creates_confirmation_without_keyword_router(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="能不能介紹一個旅伴？")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.MATCH_ACTION,
            tool_name="match.start_search", confidence=.95, evidence_span="介紹一個旅伴",
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.assess_match_opportunity", return_value=MatchOpportunityAssessment("ready")), \
             patch("services.ayue_agent.runtime.profiles_coll.update_one") as update, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.conversation_intent, "match_confirmation")
        self.assertEqual(update.call_args.args[1]["$set"]["agentic_pending_confirmation"]["action"], "match.start_search")

    def test_runtime_time_question_uses_turn_clock(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="後天是幾號？")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        decisions = [
            AgentDecision(kind=DecisionKind.TOOL_CALL, intent=AgentIntent.TIME,
                          tool_name="system.get_current_time", confidence=.95),
            AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.TIME, confidence=.95),
        ]
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=ToolResult(ok=True, data={
                 "timezone": "Asia/Taipei", "local_date": "2026-07-30", "local_time": "14:30",
                 "weekday_zh_tw": "星期四", "temporal_references": {"後天": "2026-08-01"},
             })), \
             patch(
                 "services.ayue_agent.runtime.generate_final_reply_v2",
                 return_value="後天是 2026-08-01。",
             ), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertIn("2026-08-01", result.reply)

    def test_runtime_reuses_duplicate_read_observation_without_guard_reply(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="今天幾月幾號")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        repeated = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.TIME,
            tool_name="system.get_current_time", confidence=.95,
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=[repeated, repeated]), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=ToolResult(ok=True, data={
                 "timezone": "Asia/Taipei", "local_date": "2026-07-30", "local_time": "14:30",
                 "weekday_zh_tw": "星期四",
             })) as execute_tool, \
             patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value="今天是 2026-07-30。"), \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(execute_tool.call_count, 1)
        self.assertEqual(result.reply, "今天是 2026-07-30。")
        self.assertNotIn("重複執行", result.reply)
        self.assertEqual(result.fallback_reason, "duplicate_observation_reused")

    def test_planner_noise_arguments_cannot_bypass_read_deduplication(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="今天幾月幾號")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        noisy = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.TIME,
            tool_name="system.get_current_time", arguments={"noise": 1}, confidence=.95,
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=noisy), \
             patch("services.ayue_agent.runtime.execute_tool") as execute_tool, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        execute_tool.assert_not_called()
        self.assertEqual(result.fallback_reason, "guard:model_arguments_not_allowed")

    def test_three_read_tools_still_use_final_composer_with_all_observations(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="幫我綜合回答")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        decisions = [
            AgentDecision(kind=DecisionKind.TOOL_CALL, tool_name="calendar.list_my_events", confidence=.95),
            AgentDecision(kind=DecisionKind.TOOL_CALL, tool_name="match.get_counterparty_summary", confidence=.95),
            AgentDecision(kind=DecisionKind.TOOL_CALL, tool_name="profile.get_recent_context", confidence=.95),
        ]
        tool_results = [
            ToolResult(ok=True, data={"events": [], "range": "next_90_days"}),
            ToolResult(ok=True, data={
                "found": False, "match_state": None, "display_name": "對方",
                "safe_summary": "", "chat_opened": False,
            }),
            ToolResult(ok=True, data={
                "current_context": "近期規劃前往合掌村旅行", "revision": 2, "exists": True,
            }),
        ]
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", side_effect=tool_results), \
             patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value="綜合完成") as composer, \
             patch("services.ayue_agent.runtime._save_trace"):
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.reply, "綜合完成")
        self.assertEqual(result.fallback_reason, "read_limit_composed")
        self.assertEqual(len(composer.call_args.args[1]), 3)

    def test_progress_events_only_wrap_a_guarded_tool_execution(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="今天幾月幾號")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        decisions = [
            AgentDecision(kind=DecisionKind.TOOL_CALL, intent=AgentIntent.TIME,
                          tool_name="system.get_current_time", confidence=.95),
            AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.TIME, confidence=.95),
        ]
        events = []
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", side_effect=decisions), \
             patch("services.ayue_agent.runtime.execute_tool", return_value=ToolResult(ok=True, data={
                 "timezone": "Asia/Taipei", "local_date": "2026-07-30", "local_time": "14:30",
                 "weekday_zh_tw": "星期四", "utc_iso": "2026-07-30T06:30:00+00:00",
                 "local_iso": "2026-07-30T14:30:00+08:00", "temporal_references": {},
             })), \
             patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value="今天是 2026-07-30。"), \
             patch("services.ayue_agent.runtime._save_trace"):
            run_public_agent_turn(ctx, mode="on", on_progress=events.append)
        self.assertEqual([event["type"] for event in events], ["run_started", "tool_started", "tool_finished"])
        self.assertEqual(events[1]["text"], "我確認一下現在的時間…")
        self.assertEqual(events[2]["outcome"], "ok")
        self.assertNotIn("arguments", str(events))
        self.assertNotIn("result", str(events))

    def test_pure_chat_emits_no_progress_tool_events(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="最近還好嗎")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        events = []
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=AgentDecision(kind=DecisionKind.FINAL, confidence=.95)), \
             patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value="我在呀。"), \
             patch("services.ayue_agent.runtime._save_trace"):
            run_public_agent_turn(ctx, mode="on", on_progress=events.append)
        self.assertEqual([event["type"] for event in events], ["run_started"])

    def test_terminal_planner_reply_skips_redundant_composer(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="最近還好嗎")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        decision = AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.CHAT, confidence=.95, reply="我在呀，今天過得怎麼樣？")
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.generate_final_reply_v2") as composer, \
             patch("services.ayue_agent.runtime._save_trace") as save_trace:
            result = run_public_agent_turn(ctx, mode="on")
        self.assertEqual(result.reply, "我在呀，今天過得怎麼樣？")
        composer.assert_not_called()
        self.assertEqual(save_trace.call_args.args[2]["composer_outcome"]["result_code"], "planner_reply")

    def test_progress_trace_records_delivery_drop_without_affecting_reply(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="最近還好嗎")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=AgentDecision(kind=DecisionKind.FINAL, confidence=.95)), \
             patch("services.ayue_agent.runtime.generate_final_reply_v2", return_value="我在呀。"), \
             patch("services.ayue_agent.runtime._save_trace") as save_trace:
            result = run_public_agent_turn(ctx, mode="on", on_progress=lambda _event: False)
        self.assertEqual(result.reply, "我在呀。")
        trace = save_trace.call_args.args[2]
        self.assertEqual(trace["public_progress_result_codes"], ["run_started:dropped"])

    def test_tool_exception_finishes_progress_with_safe_error_outcome(self):
        ctx = AgentTurnContext(user_id="demo_user", room_id="room", message="今天幾月幾號")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        events = []
        decision = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.TIME,
            tool_name="system.get_current_time", confidence=.95,
        )
        with patch("services.ayue_agent.runtime.build_agent_turn_context_v2", return_value=turn), \
             patch("services.ayue_agent.runtime.plan_turn_v2_function_calling", return_value=decision), \
             patch("services.ayue_agent.runtime.execute_tool", side_effect=RuntimeError("raw tool failure")), \
             patch("services.ayue_agent.runtime._save_trace") as save_trace:
            result = run_public_agent_turn(ctx, mode="on", on_progress=events.append)
        self.assertEqual([event["type"] for event in events], ["run_started", "tool_started", "tool_finished"])
        self.assertEqual(events[-1]["outcome"], "error")
        self.assertEqual(result.fallback_reason, "RuntimeError")
        self.assertNotIn("raw tool failure", result.reply)
        trace = save_trace.call_args.args[2]
        self.assertEqual(trace["event_sequence"], ["run_started", "tool_started", "tool_finished", "final"])
        self.assertEqual(trace["public_progress_result_codes"], [
            "run_started:emitted", "tool_started:emitted", "tool_finished:emitted",
        ])

if __name__ == "__main__":
    unittest.main()
