import unittest
import inspect
from pathlib import Path
from unittest.mock import patch

from services.ayue_agent.contracts import AgentDecision, AgentIntent, AgentTurnContext, AgentTurnContextV2, DecisionKind
from services.ayue_agent.router import (
    _concise_public_reply,
    _planner_prompt,
    generate_final_reply_v2,
    guard_v2_decision,
    tool_policy_for_turn,
)
from services.ayue_agent.tool_registry import ASSESSMENT_TOOLS, PLACES_TOOLS, READ_ONLY_TOOLS, WEB_TOOLS
from services.ayue_agent.maps_client import maps_enabled
from services.ayue_agent.web_tools import web_enabled


class AyueAgentV2PolicyTests(unittest.TestCase):
    @staticmethod
    def _base_policy():
        return READ_ONLY_TOOLS | ASSESSMENT_TOOLS \
            | (PLACES_TOOLS if maps_enabled() else set()) \
            | (WEB_TOOLS if web_enabled() else set()) | {
            "calendar.create_my_event", "calendar.update_my_event", "calendar.cancel_my_event",
        }

    def test_decline_followup_keeps_only_safe_status_read_visible(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="為什麼!!!",
                                 latest_match_outcome={"declined_by_other": True, "reason_available": False})
        self.assertEqual(tool_policy_for_turn(ctx) - {"match.start_search"}, self._base_policy())

    def test_plain_why_has_no_special_match_route(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="為什麼")
        self.assertEqual(tool_policy_for_turn(ctx) - {"match.start_search"}, self._base_policy())

    def test_planner_prompt_keeps_the_in_app_matchmaker_identity(self):
        prompt = _planner_prompt(
            AgentTurnContextV2(user_id="owner", room_id="room", message="你根本不認識我"),
            tool_policy_for_turn(AgentTurnContextV2(user_id="owner", room_id="room", message="你根本不認識我")), [],
        )
        self.assertIn("交友 App 內", prompt)
        self.assertIn("AI 媒人", prompt)
        self.assertIn("不是另一位使用者", prompt)

    def test_planner_prompt_carries_place_search_state_and_forbids_cuisine_reprompt(self):
        ctx = AgentTurnContextV2(
            user_id="owner", room_id="room", message="你隨意推薦",
            user_location="高雄市鹽埕區",
            place_search_draft={
                "version": "v1", "categories": ["restaurant"],
                "use_saved_location": True, "created_at": 1,
            },
        )
        prompt = _planner_prompt(ctx, tool_policy_for_turn(ctx), [])
        self.assertIn('"place_search_draft"', prompt)
        self.assertIn("不可再追問料理種類", prompt)
        self.assertIn('"place_search_followup":"none|recommend"', prompt)

    def test_guard_does_not_reclassify_a_planner_confirmation_from_text(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="我不喜歡抽菸的人")
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.MATCH_ACTION,
            tool_name="match.start_search", confidence=.95, evidence_span="我不喜歡抽菸的人",
        )
        self.assertEqual(
            guard_v2_decision(ctx, tool_policy_for_turn(ctx), decision),
            (True, "allowed"),
        )

    def test_search_is_visible_but_can_only_be_requested_as_confirmation(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="幫我找下一個")
        self.assertIn("match.start_search", tool_policy_for_turn(ctx))
        direct_call = AgentDecision(kind=DecisionKind.TOOL_CALL, tool_name="match.start_search", confidence=.95)
        self.assertEqual(
            guard_v2_decision(ctx, tool_policy_for_turn(ctx), direct_call),
            (False, "search_must_use_confirmation_executor"),
        )

    def test_assessment_starts_are_visible_but_require_a_grounded_confirmation(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="我想重新做基本性格測驗")
        self.assertIn("profile.start_assessment", tool_policy_for_turn(ctx))
        direct = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.ASSESSMENT,
            tool_name="profile.start_assessment", arguments={"kind": "basic"}, confidence=.95,
        )
        self.assertEqual(
            guard_v2_decision(ctx, tool_policy_for_turn(ctx), direct),
            (False, "confirmation_required"),
        )
        confirmation = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.ASSESSMENT,
            tool_name="profile.start_assessment", arguments={"kind": "basic"}, confidence=.95,
            evidence_span="重新做基本性格測驗",
        )
        self.assertEqual(guard_v2_decision(ctx, tool_policy_for_turn(ctx), confirmation), (True, "allowed"))

    def test_confirmation_guard_uses_typed_evidence_not_keyword_routing(self):
        for message, evidence in (
            ("為什麼!!!", "為什麼"),
            ("我不喜歡抽菸的人", "我不喜歡抽菸的人"),
            ("我最近想去合掌村", "想去合掌村"),
            ("不要幫我找人", "幫我找人"),
            ("我不想找抽菸的人", "找抽菸的人"),
            ("為什麼他不想找人", "想找人"),
        ):
            ctx = AgentTurnContextV2(user_id="owner", room_id="room", message=message)
            decision = AgentDecision(
                kind=DecisionKind.CONFIRMATION, intent=AgentIntent.MATCH_ACTION,
                tool_name="match.start_search", confidence=.95, evidence_span=evidence,
            )
            defense_in_depth_policy = frozenset({*tool_policy_for_turn(ctx), "match.start_search"})
            self.assertEqual(
                guard_v2_decision(ctx, defense_in_depth_policy, decision),
                (True, "allowed"),
            )

    def test_natural_search_request_can_create_confirmation(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="能不能介紹一個旅伴？")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        decision = AgentDecision(
            kind=DecisionKind.CONFIRMATION, intent=AgentIntent.MATCH_ACTION,
            tool_name="match.start_search", confidence=.95, evidence_span="介紹一個旅伴",
        )
        self.assertEqual(guard_v2_decision(turn, tool_policy_for_turn(turn), decision), (True, "allowed"))

    def test_planner_arguments_are_not_an_execution_channel(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="今天幾月幾號")
        turn = AgentTurnContextV2(user_id=ctx.user_id, room_id=ctx.room_id, message=ctx.message)
        decision = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.TIME,
            tool_name="system.get_current_time", arguments={"noise": 1}, confidence=.95,
        )
        self.assertEqual(
            guard_v2_decision(turn, tool_policy_for_turn(turn), decision),
            (False, "model_arguments_not_allowed"),
        )

    def test_planner_contract_rejects_top_level_ids_and_revisions(self):
        for forbidden in ({"user_id": "owner"}, {"proposal_id": "internal"}, {"revision": 3}):
            payload = {"kind": "final", "intent": "chat", "confidence": .9, **forbidden}
            with self.assertRaises(Exception):
                AgentDecision.model_validate(payload)

    def test_only_unique_actionable_proposal_can_decide(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="有興趣，幫我問問",
                                 active_proposal={"user_can_decide": True, "proposal_revision": 3})
        policy = tool_policy_for_turn(ctx)
        self.assertIn("match.decide_active_proposal", policy)
        decision = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.MATCH_ACTION,
            tool_name="match.decide_active_proposal", evidence_span="有興趣，幫我問問",
            arguments={"decision": "interested"}, confidence=.9,
        )
        self.assertEqual(guard_v2_decision(ctx, policy, decision), (True, "allowed"))

    def test_proposal_decision_uses_typed_argument_and_evidence(self):
        ctx = AgentTurnContextV2(
            user_id="owner", room_id="room", message="有興趣，幫我問問",
            active_proposal={"user_can_decide": True, "proposal_revision": 3},
        )
        wrong = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.MATCH_ACTION,
            tool_name="match.decide_active_proposal", evidence_span="有興趣，幫我問問",
            arguments={"decision": "declined"}, confidence=.9,
        )
        self.assertEqual(
            guard_v2_decision(ctx, tool_policy_for_turn(ctx), wrong),
            (True, "allowed"),
        )

    def test_v2_planner_and_guard_do_not_reference_legacy_text_classifiers(self):
        legacy_names = (
            "_is_calendar_query", "_is_match_request", "_is_match_status_query",
            "_is_memory_query", "validate_tool_intent", "route_turn",
            "explicit_search_request", "is_unsupported_direct_date_request",
        )
        for function in (tool_policy_for_turn, _planner_prompt, guard_v2_decision):
            source = inspect.getsource(function)
            for legacy_name in legacy_names:
                self.assertNotIn(legacy_name, source)
        runtime_source = (Path(__file__).parents[1] / "services" / "ayue_agent" / "runtime.py").read_text(encoding="utf-8")
        for legacy_name in legacy_names:
            self.assertNotIn(legacy_name, runtime_source)

    def test_v2_runtime_uses_the_match_action_service_not_the_http_router(self):
        runtime_source = (Path(__file__).parents[1] / "services" / "ayue_agent" / "runtime.py").read_text(encoding="utf-8")
        self.assertIn("services.match_action_service", runtime_source)
        self.assertNotIn("routers.match", runtime_source)

    def test_accepted_reply_status_variants_use_canonical_status_tool(self):
        for message in ("他回復了沒", "她回覆了嗎", "對方回覆了沒有"):
            ctx = AgentTurnContextV2(
                user_id="owner", room_id="room", message=message,
                latest_match_outcome={"status": "accepted", "counterparty": "對方"},
            )
            self.assertEqual(tool_policy_for_turn(ctx) - {"match.start_search"}, self._base_policy())

    def test_pending_proposal_status_query_uses_canonical_status_tool(self):
        ctx = AgentTurnContextV2(
            user_id="owner", room_id="room", message="他回復了沒",
            active_proposal={"status": "pending", "user_can_decide": False},
            latest_match_outcome={"status": "declined"},
        )
        self.assertEqual(tool_policy_for_turn(ctx) - {"match.start_search"}, self._base_policy())

    def test_safe_reads_are_visible_without_keyword_routing(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="我什麼時候沒空")
        self.assertEqual(tool_policy_for_turn(ctx) - {"match.start_search"}, self._base_policy())
        self.assertIn("calendar.list_my_events", tool_policy_for_turn(ctx))
        self.assertIn("match.get_counterparty_summary", tool_policy_for_turn(ctx))
        self.assertIn("profile.get_recent_context", tool_policy_for_turn(ctx))
        self.assertIn("profile.get_self_summary", tool_policy_for_turn(ctx))
        self.assertIn("memory.search_my_profile", tool_policy_for_turn(ctx))

    def test_pending_confirmation_is_runtime_only_not_planner_context(self):
        ctx = AgentTurnContextV2(
            user_id="owner", room_id="room", message="你有記得我的旅遊計畫嗎？",
            pending_confirmation={"action": "match.start_search", "status": "pending"},
        )
        prompt = _planner_prompt(ctx, tool_policy_for_turn(ctx), [])
        self.assertNotIn('"pending_confirmation"', prompt)

    def test_planner_prompt_exposes_typed_proposal_decision_contract(self):
        ctx = AgentTurnContextV2(
            user_id="owner", room_id="room", message="有興趣，幫我問問",
            active_proposal={"user_can_decide": True, "proposal_revision": 3},
        )
        prompt = _planner_prompt(ctx, tool_policy_for_turn(ctx), [])
        self.assertIn('"decision"', prompt)
        self.assertIn('"interested"', prompt)
        self.assertIn('"declined"', prompt)
        self.assertNotIn("proposal_revision", prompt)

    def test_match_status_intent_requires_canonical_read_before_final(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="她到底有沒有點？")
        decision = AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.MATCH_STATUS, confidence=.9)
        self.assertEqual(
            guard_v2_decision(ctx, tool_policy_for_turn(ctx), decision),
            (False, "match_status_requires_read"),
        )

    def test_ready_place_recommendation_requires_a_nearby_read_before_final(self):
        ctx = AgentTurnContextV2(
            user_id="owner", room_id="room", message="你隨意推薦",
            user_location="高雄市鹽埕區",
            place_search_draft={
                "version": "v1", "categories": ["restaurant"],
                "use_saved_location": True, "created_at": 1,
            },
        )
        final = AgentDecision(
            kind=DecisionKind.FINAL, intent=AgentIntent.PLACES, confidence=.95,
            place_search_followup="recommend", reply="那你想找哪一類的料理呢？",
        )
        self.assertEqual(
            guard_v2_decision(
                ctx, tool_policy_for_turn(ctx), final,
                place_search_ready=True, has_places_observation=False,
            ),
            (False, "places_search_requires_read"),
        )
        self.assertEqual(
            guard_v2_decision(
                ctx, tool_policy_for_turn(ctx), final,
                place_search_ready=True, has_places_observation=True,
            ),
            (True, "allowed"),
        )

    def test_self_profile_question_requires_owner_summary_before_final(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="你了解我多少")
        final = AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.PROFILE, confidence=.9)
        self.assertEqual(
            guard_v2_decision(ctx, tool_policy_for_turn(ctx), final),
            (False, "profile_requires_read"),
        )
        read = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.PROFILE,
            tool_name="profile.get_self_summary", confidence=.9,
        )
        self.assertEqual(guard_v2_decision(ctx, tool_policy_for_turn(ctx), read), (True, "allowed"))

    def test_named_calendar_question_requires_a_calendar_read_not_match_state(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="看電影是跟誰去")
        final = AgentDecision(kind=DecisionKind.FINAL, intent=AgentIntent.CALENDAR, confidence=.9)
        self.assertEqual(
            guard_v2_decision(ctx, tool_policy_for_turn(ctx), final),
            (False, "calendar_read_requires_read"),
        )
        find = AgentDecision(
            kind=DecisionKind.TOOL_CALL, intent=AgentIntent.CALENDAR,
            tool_name="calendar.find_my_event", arguments={"event_hint": "看電影", "date_hint": ""},
            confidence=.9,
        )
        self.assertEqual(guard_v2_decision(ctx, tool_policy_for_turn(ctx), find), (True, "allowed"))

    def test_low_confidence_final_is_safe_but_low_confidence_action_is_blocked(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="最近還好嗎")
        final = AgentDecision(kind=DecisionKind.FINAL, confidence=0.1)
        action = AgentDecision(kind=DecisionKind.TOOL_CALL, tool_name="calendar.list_my_events", confidence=0.1)
        self.assertEqual(guard_v2_decision(ctx, frozenset(), final), (True, "allowed"))
        self.assertEqual(guard_v2_decision(ctx, frozenset({"calendar.list_my_events"}), action), (False, "low_confidence"))

    def test_internal_tool_language_is_replaced_with_conversation(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="我最近想去非洲旅行")
        with patch(
            "services.ayue_agent.router.generate_chat_completion",
            return_value="抱歉，我目前沒有工具可以協助規劃非洲旅行。",
        ):
            reply = generate_final_reply_v2(ctx, [])
        self.assertNotIn("工具", reply)
        self.assertIn("非洲", reply)

    def test_capability_question_uses_product_truth_without_calling_the_provider(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="你有什麼能力？")
        with patch("services.ayue_agent.router.generate_chat_completion") as provider:
            reply = generate_final_reply_v2(ctx, [])
        provider.assert_not_called()
        self.assertIn("不會隨機配", reply)
        self.assertNotIn("物件", reply)

    def test_final_reply_rewrites_object_wording_and_rejects_random_claims(self):
        ctx = AgentTurnContextV2(user_id="owner", room_id="room", message="幫我找人")
        with patch(
            "services.ayue_agent.router.generate_chat_completion",
            return_value="我會隨機幫你找一個配對物件。",
        ):
            reply = generate_final_reply_v2(ctx, [])
        self.assertNotIn("物件", reply)
        self.assertIn("不會隨機配對", reply)

    def test_ordinary_chat_reply_is_limited_to_two_short_sentences(self):
        long_reply = (
            "京都的街道很有味道，慢慢走可以看到很多細節。"
            "你也可以逛市集、喝茶、看看町家。"
            "接著還能去很多地方，安排非常多活動。"
        )
        concise = _concise_public_reply(long_reply)
        self.assertLessEqual(len(concise), 110)
        self.assertNotIn("接著還能", concise)

    def test_planner_prompt_allows_one_offer_after_sustained_activity_engagement(self):
        ctx = AgentTurnContextV2(
            user_id="owner", room_id="room", message="我心情超好",
            recent_messages=[
                {"role": "user", "content": "我想慢慢逛京都"},
                {"role": "assistant", "content": "聽起來很愜意。"},
                {"role": "user", "content": "當地市集我要去"},
            ],
        )
        prompt = _planner_prompt(ctx, tool_policy_for_turn(ctx), [])
        self.assertIn("已連續談出具體活動", prompt)
        self.assertIn("自然問一次", prompt)


if __name__ == "__main__":
    unittest.main()
