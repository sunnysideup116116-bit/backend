import json
import unittest
from unittest.mock import patch
from matchmaker_agent.concept_identity import canonicalize_concept

K_POP = canonicalize_concept("K-pop").key

from routers.match import (
    build_active_proposal_card,
    build_validated_match_explanation,
    build_directional_match_explanations,
    build_directional_reason_v3,
    build_directional_reason_v3_from_snapshot,
    build_friend_intro_v4,
    candidate_qualification,
    reason_for_viewer,
    get_single_match_state,
    validated_distinctive_tags,
)
from services.match_reason_service import (
    MATCH_PROPOSAL_STYLE_IDS,
    accepted_opening_for_viewer,
    match_reason_style_id,
    topic_friend_intro_fallback,
    valid_friend_intro_text,
)


class MatchQualificationTests(unittest.TestCase):
    def test_topic_fallback_preserves_together_activity_and_viewer_role(self):
        initiator = {"user_id": "owner", "current_context": "最近想去雪地旅行"}
        receiver = {"user_id": "candidate", "current_context": "週末想拍照"}
        owner_copy = topic_friend_intro_fallback(
            initiator, receiver, "滑雪", requester_id="owner",
            query_text="我想找人一起滑雪",
        )
        receiver_copy = topic_friend_intro_fallback(
            receiver, initiator, "滑雪", requester_id="owner",
            query_text="我想找人一起滑雪",
        )
        self.assertIn("找人一起滑雪", owner_copy["viewer_text"])
        self.assertIn("找人一起滑雪", receiver_copy["viewer_text"])
        self.assertNotIn("找人聊聊滑雪", owner_copy["viewer_text"])
        self.assertNotEqual(owner_copy["viewer_text"], receiver_copy["viewer_text"])
        self.assertIn("不能確認", owner_copy["viewer_text"])
        self.assertIn("有位朋友", receiver_copy["viewer_text"])
        sent_copy = topic_friend_intro_fallback(
            initiator, receiver, "滑雪", requester_id="owner",
            query_text="我想找人一起滑雪", auto_invite=True,
        )
        self.assertIn("邀請已送出", sent_copy["viewer_text"])
        self.assertIn("正在等對方回覆", sent_copy["viewer_text"])
        self.assertFalse(sent_copy["viewer_text"].endswith(("？", "?")))

    def test_match_reason_style_is_stable_but_not_a_single_global_first_shot(self):
        first = match_reason_style_id("owner", "candidate", context_revision="r1")
        self.assertEqual(first, match_reason_style_id("owner", "candidate", context_revision="r1"))
        styles = {
            match_reason_style_id(f"owner-{index}", f"candidate-{index}", context_revision="r1")
            for index in range(12)
        }
        self.assertGreaterEqual(len(styles), 2)
        self.assertTrue(styles.issubset(set(MATCH_PROPOSAL_STYLE_IDS)))

    def test_v4_validator_accepts_all_approved_natural_opening_shapes(self):
        common = {
            "required_context": "晚上想看電影",
            "introduced_personality": "比較外向、容易帶起話題",
            "viewer_personality": "偏安靜、重視舒服節奏",
        }
        samples = (
            "欸，我想到一個你可能會想認識的人。他比較外向、容易帶起話題，最近提到「晚上想看電影」；你偏安靜、重視舒服節奏，或許能形成舒服的互動節奏。想讓我幫你們牽個線嗎？",
            "我腦中突然有個畫面：他比較外向、容易帶起話題，最近提到「晚上想看電影」；你偏安靜、重視舒服節奏，感覺不用硬找話題也能相處得不錯。你會想認識看看嗎？",
            "我這裡有一位感覺可以介紹給你。他比較外向、容易帶起話題，最近提到「晚上想看電影」；你偏安靜、重視舒服節奏，可能滿容易進入狀況。要不要讓我問問他？",
        )
        for sample in samples:
            with self.subTest(sample=sample[:16]):
                accepted = valid_friend_intro_text(sample, **common)
                self.assertTrue(accepted)
                self.assertIn("晚上想看電影", accepted)

    @patch("routers.match.public_display_name", side_effect=lambda user_id: {"owner": "小安", "candidate": "小林"}.get(user_id, "對方"))
    def test_active_proposal_reason_is_projected_for_the_viewer(self, _display_name):
        match = {
            "_id": "proposal-1", "from_user": "owner", "to_user": "candidate", "status": "pending",
            "proposal_revision": 2, "reason": "給小安看的理由", "receiver_reason": "給小林看的理由",
            "reason_items": [], "receiver_reason_items": [], "match_context_snapshot": {},
        }
        owner_card = build_active_proposal_card(match, "owner")
        candidate_card = build_active_proposal_card(match, "candidate")
        self.assertEqual(owner_card["other_label"], "對方")
        self.assertEqual(candidate_card["other_label"], "對方")
        self.assertNotIn("小林", owner_card["opening"])
        self.assertNotIn("小安", candidate_card["opening"])
        self.assertEqual(owner_card["viewer_reason"], "給小安看的理由")
        self.assertEqual(candidate_card["viewer_reason"], "給小林看的理由")
        self.assertEqual(owner_card["recommendation_reason"], "給小安看的理由")
        self.assertEqual(candidate_card["receiver_reason"], "給小林看的理由")
        self.assertNotIn("candidate", candidate_card["opening"])
    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_directional_reason_uses_other_activity_and_viewer_personality(self, _graph_memories):
        target, candidate = build_directional_match_explanations(
            {"user_id": "owner", "current_context": "想去郊區讀書", "big_five": {"E": 8}},
            {"user_id": "candidate", "current_context": "想去居酒屋小酌", "big_five": {"E": 3}},
            0.72,
        )
        self.assertIn("居酒屋", target["viewer_text"])
        self.assertIn("外向", target["viewer_text"])
        self.assertIn("郊區讀書", candidate["viewer_text"])
        self.assertIn("安靜", candidate["viewer_text"])
        self.assertNotIn("已同意", target["viewer_text"])

    @patch("routers.match.public_display_name", return_value="對方")
    def test_v3_snapshot_reason_is_bound_to_each_actual_viewer(self, _display_name):
        match = {
            "_id": "proposal-v3", "proposal_revision": 0,
            "from_user": "owner", "to_user": "candidate", "status": "pending",
            "recommendation_tier": "exploratory",
            "match_context_snapshot": {
                "target": {"user_id": "owner", "current_context": "想去郊區讀書", "big_five": {"E": 8}},
                "candidate": {"user_id": "candidate", "current_context": "想去居酒屋小酌", "big_five": {"E": 3}},
            },
        }
        entries = build_directional_reason_v3_from_snapshot(match)
        self.assertEqual(len(entries), 2)
        match["directional_reason_v3"] = entries
        owner_card = build_active_proposal_card(match, "owner")
        candidate_card = build_active_proposal_card(match, "candidate")
        self.assertIn("居酒屋", owner_card["viewer_reason"])
        self.assertNotIn("郊區讀書」", owner_card["viewer_reason"])
        self.assertIn("郊區讀書", candidate_card["viewer_reason"])
        self.assertNotIn("居酒屋」", candidate_card["viewer_reason"])

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_v3_refines_each_bound_direction_as_a_natural_invitation(self, _graph_memories):
        responses = [
            '{"viewer_text":"有位偏安靜、重視舒服節奏的人，最近提到「想去居酒屋小酌」。你比較外向，或許能讓第一次聊天不冷場；你有興趣認識他嗎？","conversation_starter":"可以先聊聊喜歡什麼樣的小酌氣氛。"}',
            '{"viewer_text":"有位比較外向、容易帶起話題的人，最近提到「想去郊區讀書」。你偏安靜，或許能讓相處保有舒服的留白；你會想認識他嗎？","conversation_starter":"可以先問他偏好的閱讀地點。"}',
        ]
        with patch("routers.match.generate_chat_completion", side_effect=responses) as model:
            entries = build_directional_reason_v3(
                {"user_id": "owner", "current_context": "想去郊區讀書", "big_five": {"E": 8}},
                {"user_id": "candidate", "current_context": "想去居酒屋小酌", "big_five": {"E": 3}},
                0.72, refine=True,
            )
        self.assertEqual(model.call_count, 2)
        by_viewer = {item["viewer_id"]: item for item in entries}
        self.assertIn("居酒屋", by_viewer["owner"]["viewer_text"])
        self.assertNotIn("郊區讀書", by_viewer["owner"]["viewer_text"])
        self.assertIn("郊區讀書", by_viewer["candidate"]["viewer_text"])
        self.assertNotIn("居酒屋", by_viewer["candidate"]["viewer_text"])
        self.assertTrue(by_viewer["candidate"]["viewer_text"].endswith(("？", "?")))

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_v4_friend_introductions_are_bound_and_use_two_separate_model_calls(self, _graph_memories):
        responses = [
            '{"viewer_text":"有位偏安靜、重視舒服節奏的人，最近提到「想去居酒屋小酌」。你比較外向、容易帶起話題，或許能讓聊天更自然；你想認識他嗎？","conversation_starter":"可以聊聊喜歡什麼樣的小酌氣氛。","accepted_opening":"好消息，{{counterparty}}也點頭了！他最近提到「想去居酒屋小酌」，你比較外向、容易帶起話題、他偏安靜、重視舒服節奏，可以先問他最期待哪一部分。"}',
            '{"viewer_text":"有位比較外向、容易帶起話題的人，最近提到「想去郊區讀書」。你偏安靜、重視舒服節奏，或許能讓相處保有舒服的留白；你願意一起參加嗎？","conversation_starter":"可以先問他偏好的閱讀地點。","accepted_opening":"好消息，{{counterparty}}也點頭了！他最近提到「想去郊區讀書」，你偏安靜、重視舒服節奏、他比較外向、容易帶起話題，可以先問他最期待哪一部分。"}',
        ]
        owner = {"user_id": "owner", "current_context": "想去郊區讀書", "big_five": {"E": 8}}
        candidate = {"user_id": "candidate", "current_context": "想去居酒屋小酌", "big_five": {"E": 3}}
        with patch("routers.match.generate_chat_completion", side_effect=responses) as model:
            projections = build_friend_intro_v4(owner, candidate, 0.72, refine=True)
        self.assertEqual(model.call_count, 2)
        initiator = projections["initiator_preview"]
        receiver = projections["receiver_invitation"]
        self.assertEqual((initiator["viewer_id"], initiator["counterparty_id"]), ("owner", "candidate"))
        self.assertEqual((receiver["viewer_id"], receiver["counterparty_id"]), ("candidate", "owner"))
        self.assertIn("居酒屋", initiator["viewer_text"])
        self.assertIn("郊區讀書", receiver["viewer_text"])
        self.assertNotIn("郊區讀書", initiator["viewer_text"])
        self.assertNotIn("居酒屋", receiver["viewer_text"])
        self.assertIn("{{counterparty}}", initiator["accepted_opening"])
        self.assertIn("{{counterparty}}", receiver["accepted_opening"])
        self.assertIn("欸，我想到一個你可能會想認識的人", model.call_args_list[0].args[0])
        self.assertIn("成功牽上啦", model.call_args_list[0].args[0])

    def test_accepted_opening_is_role_bound_and_uses_name_only_after_acceptance(self):
        match = {
            "from_user": "owner", "to_user": "candidate", "reason_version": "v4_friend_intro",
            "friend_intro_v4": {
                "initiator_preview": {
                    "viewer_id": "owner", "counterparty_id": "candidate",
                    "counterparty_context_snapshot": "想去居酒屋小酌",
                    "counterparty_public_personality": "偏安靜、重視舒服節奏",
                    "viewer_public_personality": "比較外向、容易帶起話題",
                    "accepted_opening": "好消息，{{counterparty}}也點頭了！他最近提到「想去居酒屋小酌」，你比較外向、容易帶起話題、他偏安靜、重視舒服節奏，可以先問他最期待哪一部分。",
                },
                "receiver_invitation": {"viewer_id": "candidate", "counterparty_id": "owner"},
            },
        }
        opening = accepted_opening_for_viewer(match, "owner", "candidate", "小林")
        self.assertIn("小林", opening)
        self.assertNotIn("{{counterparty}}", opening)
        self.assertIn("居酒屋", opening)

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_v4_invalid_model_reply_falls_back_to_complete_receiver_invitation(self, _graph_memories):
        owner = {"user_id": "owner", "current_context": "想去郊區讀書", "big_five": {"E": 8}}
        candidate = {"user_id": "candidate", "current_context": "想去居酒屋小酌", "big_five": {"E": 3}}
        with patch("routers.match.generate_chat_completion", side_effect=['{"viewer_text":"好啊","conversation_starter":""}', "not-json"]):
            projections = build_friend_intro_v4(owner, candidate, 0.72, refine=True)
        invitation = projections["receiver_invitation"]["viewer_text"]
        self.assertIn("郊區讀書", invitation)
        self.assertIn("比較外向", invitation)
        self.assertIn("偏安靜", invitation)
        self.assertTrue(any(token in invitation for token in ("可能", "或許", "可以")))
        self.assertTrue(invitation.endswith(("？", "?")))

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_v4_unverified_provider_fact_is_rejected_without_truncating_fallback(self, _graph_memories):
        long_context = "想在週末找一個安靜而且有大片窗景的郊區閱讀空間讀完最近買的散文集"
        owner = {"user_id": "owner", "current_context": long_context, "big_five": {"E": 8}}
        candidate = {"user_id": "candidate", "current_context": "想去居酒屋小酌", "big_five": {"E": 3}}
        fabricated = (
            '{"viewer_text":"有位比較外向、容易帶起話題的人，最近提到「' + long_context
            + '」，而且住在台北。你偏安靜、重視舒服節奏，或許很適合；你想認識對方嗎？",'
            '"conversation_starter":"可以先聊聊。"}'
        )
        with patch("routers.match.generate_chat_completion", side_effect=["not-json", fabricated]):
            projections = build_friend_intro_v4(owner, candidate, 0.72, refine=True)
        invitation = projections["receiver_invitation"]["viewer_text"]
        self.assertNotIn("台北", invitation)
        self.assertIn(long_context, invitation)
        self.assertIn("比較外向", invitation)
        self.assertIn("偏安靜", invitation)
        self.assertTrue(invitation.endswith(("？", "?")))

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_v4_provider_prompts_and_public_card_redact_internal_references(self, _graph_memories):
        owner = {
            "user_id": "seed_user_01", "current_context": "想和 seed_user_09 去郊區讀書",
            "big_five": {"E": 8},
        }
        candidate = {
            "user_id": "seed_user_02", "current_context": "想去居酒屋小酌",
            "big_five": {"E": 3},
        }
        with patch("routers.match.generate_chat_completion", return_value="not-json") as model:
            projections = build_friend_intro_v4(owner, candidate, 0.72, refine=True)
        for call in model.call_args_list:
            self.assertNotIn("seed_user", call.args[0])
        match = {
            "_id": "proposal", "from_user": "seed_user_01", "to_user": "seed_user_02",
            "status": "pending", "reason_version": "v4_friend_intro",
            "friend_intro_v4": projections,
            "match_context_snapshot": {"target": owner, "candidate": candidate},
        }
        with patch("routers.match.public_display_name", return_value="對方"):
            card = build_active_proposal_card(match, "seed_user_02")
        self.assertNotIn("seed_user", json.dumps(card, ensure_ascii=False))

    @patch("routers.match.public_display_name", return_value="對方")
    def test_v4_missing_projection_uses_only_create_time_snapshot(self, _display_name):
        match = {
            "_id": "v4-live", "from_user": "owner", "to_user": "candidate", "status": "pending",
            "reason_version": "v4_friend_intro", "friend_intro_v4": {}, "recommendation_tier": "exploratory",
            "match_context_snapshot": {
                "target": {"user_id": "owner", "current_context": "想去郊區讀書", "public_personality": "比較外向、容易帶起話題"},
                "candidate": {"user_id": "candidate", "current_context": "想去居酒屋小酌", "public_personality": "偏安靜、重視舒服節奏"},
            },
        }
        owner_reason = build_active_proposal_card(match, "owner")["viewer_reason"]
        candidate_reason = build_active_proposal_card(match, "candidate")["viewer_reason"]
        self.assertIn("居酒屋", owner_reason)
        self.assertIn("郊區讀書", candidate_reason)
        self.assertNotIn("candidate", owner_reason)
        self.assertNotIn("owner", candidate_reason)
        owner_card = build_active_proposal_card(match, "owner")
        self.assertNotIn("friend_intro_v4", owner_card)
        self.assertNotIn("recommendation_reason", owner_card)
        self.assertNotIn("receiver_reason", owner_card)
        self.assertNotIn("other_id", owner_card)
        # Revision is the existing public CAS token, not counterparty identity.
        self.assertEqual(owner_card["proposal_revision"], 0)

    @patch("routers.match.public_display_name", return_value="對方")
    def test_old_live_v4_copy_is_reprojected_without_mutating_the_record(self, _display_name):
        match = {
            "_id": "v4-old-copy", "from_user": "owner", "to_user": "candidate",
            "status": "pending", "proposal_revision": 3,
            "reason_version": "v4_friend_intro",
            "friend_intro_v4": {
                "initiator_preview": {
                    "viewer_id": "owner", "counterparty_id": "candidate",
                    "counterparty_context_snapshot": "晚上想看電影",
                    "counterparty_public_personality": "偏安靜、重視舒服節奏",
                    "viewer_public_personality": "比較外向、容易帶起話題",
                    "viewer_text": "有位偏安靜、重視舒服節奏的人，最近提到「晚上想看電影」。你比較外向、容易帶起話題，或許能自然聊聊；你想認識對方嗎？",
                },
            },
            "match_context_snapshot": {
                "target": {"user_id": "owner", "current_context": "最近想去郊區讀書", "public_personality": "比較外向、容易帶起話題"},
                "candidate": {"user_id": "candidate", "current_context": "晚上想看電影", "public_personality": "偏安靜、重視舒服節奏"},
            },
        }
        original = json.dumps(match, ensure_ascii=False, sort_keys=True)
        reason = build_active_proposal_card(match, "owner")["viewer_reason"]
        self.assertTrue(reason.startswith("欸,我想到一個你可能會想認識的人"))
        self.assertIn("晚上想看電影", reason)
        self.assertEqual(json.dumps(match, ensure_ascii=False, sort_keys=True), original)

    @patch("routers.match.public_display_name", return_value="對方")
    @patch("routers.match.matches_coll.find_one")
    def test_single_match_state_hydrates_current_viewer_copy(self, find_one, _display_name):
        find_one.return_value = {
            "_id": "proposal", "from_user": "owner", "to_user": "candidate",
            "status": "pending", "proposal_revision": 1,
            "reason_version": "v4_friend_intro",
            "match_context_snapshot": {
                "target": {"user_id": "owner", "current_context": "想去郊區讀書", "public_personality": "比較外向、容易帶起話題"},
                "candidate": {"user_id": "candidate", "current_context": "晚上想看電影", "public_personality": "偏安靜、重視舒服節奏"},
            },
        }
        state = get_single_match_state("owner", "64b64b64b64b64b64b64b64b")
        self.assertEqual(state["stage"], "waiting_other")
        self.assertIn("晚上想看電影", state["viewer_reason"])
        self.assertNotIn("candidate", state["viewer_reason"])

    @patch("routers.match.public_display_name", return_value="對方")
    def test_v4_snapshot_fallback_never_recomputes_old_v3_records(self, _display_name):
        old_match = {
            "_id": "legacy", "from_user": "owner", "to_user": "candidate", "status": "pending",
            "reason_version": "v3", "reason": "舊提案理由", "receiver_reason": "舊接收方理由",
            "match_context_snapshot": {
                "target": {"user_id": "owner", "current_context": "想去郊區讀書", "public_personality": "比較外向、容易帶起話題"},
                "candidate": {"user_id": "candidate", "current_context": "想去居酒屋小酌", "public_personality": "偏安靜、重視舒服節奏"},
            },
        }
        self.assertIn("居酒屋", build_active_proposal_card(old_match, "owner")["viewer_reason"])
        self.assertIn("郊區讀書", build_active_proposal_card(old_match, "candidate")["viewer_reason"])
        self.assertNotIn("friend_intro_v4", old_match)
        historical = {**old_match, "status": "accepted", "reason": "原本發起方理由", "receiver_reason": "原本接收方理由"}
        self.assertEqual(reason_for_viewer(historical, "owner"), "原本發起方理由")
        self.assertEqual(reason_for_viewer(historical, "candidate"), "原本接收方理由")
    @patch("routers.match.get_user_graph_memories")
    def test_reciprocal_hard_conflict_is_never_eligible(self, graph_memories):
        graph_memories.side_effect = lambda user_id, _limit: (
            [{"key": "smoking", "stance": "avoid"}]
            if user_id == "owner" else [{"key": "smoking", "stance": "like"}]
        )
        qualification = candidate_qualification({"user_id": "owner"}, {"user_id": "candidate"})
        self.assertFalse(qualification["eligible"])
        self.assertEqual(qualification["hard_conflict_keys"], ["smoking"])

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_shared_activity_is_an_explicit_strong_link(self, _graph_memories):
        qualification = candidate_qualification(
            {"user_id": "owner", "context_signals": {"activity": "合掌村旅行"}},
            {"user_id": "candidate", "context_signals": {"activity": "合掌村旅行"}},
        )
        self.assertTrue(qualification["eligible"])
        self.assertIn("shared_activity", qualification["strong_reason_codes"])

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_no_shared_evidence_is_not_eligible(self, _graph_memories):
        qualification = candidate_qualification({"user_id": "owner"}, {"user_id": "candidate"})
        self.assertFalse(qualification["eligible"])

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_semantically_similar_recent_context_reaches_the_matchmaker(self, _graph_memories):
        qualification = candidate_qualification(
            {"user_id": "owner", "current_context": "近期想去京都逛市集"},
            {"user_id": "candidate", "current_context": "週末想逛老街和手作攤位"},
            vector_score=0.72,
            allow_adjacent=True,
        )
        self.assertTrue(qualification["eligible"])
        self.assertIn("semantic_context_similarity", qualification["strong_reason_codes"])
        self.assertEqual(qualification["match_basis"]["level"], "adjacent")

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_vector_similarity_alone_is_not_enough_without_one_shot_widening(self, _graph_memories):
        qualification = candidate_qualification(
            {"user_id": "owner", "current_context": "最近想找人出門"},
            {"user_id": "candidate", "current_context": "週末想放鬆"},
            vector_score=0.95,
        )
        self.assertFalse(qualification["eligible"])
        self.assertEqual(qualification["match_basis"]["level"], "insufficient")

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_requested_topic_can_select_without_candidate_skill_evidence(self, _graph_memories):
        target = {"user_id": "owner", "current_context": "最近想找戶外活動"}
        candidate = {"user_id": "candidate", "current_context": "週末想放鬆"}
        qualification = candidate_qualification(
            target, candidate, vector_score=0.82,
            search_context={"invitation_topic": "衝浪", "query_text": "想找人一起衝浪"},
        )
        self.assertTrue(qualification["eligible"])
        self.assertIn("requested_topic", qualification["strong_reason_codes"])
        self.assertEqual(qualification["match_basis"]["level"], "adjacent")
        intro = build_friend_intro_v4(
            target, candidate, 0.82,
            search_context={"invitation_topic": "衝浪"},
        )
        for entry in intro.values():
            self.assertIn("衝浪", entry["viewer_text"])
            self.assertNotIn("會衝浪", entry["viewer_text"])
            self.assertNotIn("對方會出席", entry["viewer_text"])
            self.assertNotIn("對方已安排出席", entry["viewer_text"])

    @patch("routers.match.generate_chat_completion")
    def test_topic_introductions_are_generated_separately_for_both_roles(self, generate):
        generate.side_effect = [
            type("Reply", (), {"content": json.dumps({
                "viewer_text": "你想找人一起滑雪，我找到一位可以替你詢問的人選。邀請已送出，正在等對方回覆。",
                "conversation_starter": "配對成功後，可以先聊聊想怎麼體驗滑雪。",
                "accepted_opening": "{{counterparty}}也願意認識！可以先聊聊對滑雪的想法。",
            }, ensure_ascii=False)})(),
            type("Reply", (), {"content": json.dumps({
                "viewer_text": "有人想找人一起滑雪，並邀請你先認識看看。你願意先認識對方嗎？",
                "conversation_starter": "配對成功後，可以先聊聊各自想怎麼體驗滑雪。",
                "accepted_opening": "{{counterparty}}也願意認識！可以先聊聊彼此的滑雪想法。",
            }, ensure_ascii=False)})(),
        ]
        result = build_friend_intro_v4(
            {"user_id": "owner", "current_context": "最近想找滑雪伴"},
            {"user_id": "candidate", "current_context": "週末想去雪地走走"},
            0.82,
            refine=True,
            search_context={
                "invitation_topic": "滑雪",
                "query_text": "我想找人一起滑雪",
            },
            auto_invite=True,
        )
        self.assertEqual(generate.call_count, 2)
        requester = result["initiator_preview"]["viewer_text"]
        receiver = result["receiver_invitation"]["viewer_text"]
        self.assertIn("邀請已送出", requester)
        self.assertNotIn("週末想去雪地走走", requester)
        self.assertFalse(requester.endswith("？"))
        self.assertNotIn("最近想找滑雪伴", receiver)
        self.assertNotIn("有興趣", receiver)
        self.assertTrue(receiver.endswith(("？", "?")))
        self.assertNotEqual(requester, receiver)
        match_doc = {
            "from_user": "owner",
            "to_user": "candidate",
            "status": "pending",
            "delivery_mode": "invite_on_match",
            "reason_version": "v4_friend_intro",
            "reason_copy_version": "v8_source_directional",
            "friend_intro_v4": result,
        }
        self.assertEqual(reason_for_viewer(match_doc, "owner"), requester)
        self.assertEqual(reason_for_viewer(match_doc, "candidate"), receiver)

    @patch("routers.match.generate_chat_completion")
    def test_topic_introduction_retries_claims_without_candidate_evidence(self, generate):
        def reply(viewer_text):
            return type("Reply", (), {"content": json.dumps({
                "viewer_text": viewer_text,
                "conversation_starter": "配對成功後再聊吃東港生魚片的安排。",
                "accepted_opening": "{{counterparty}}也願意認識！可以先聊聊吃東港生魚片的想法。",
            }, ensure_ascii=False)})()

        generate.side_effect = [
            reply("我幫你找到一位也對吃東港生魚片有興趣的人選，邀請已送出，正在等對方回覆。"),
            reply("你想找人一起吃東港生魚片，我找到一位可以替你詢問的人選。邀請已送出，正在等對方回覆。"),
            reply("有位也喜歡吃東港生魚片的朋友想認識你，你願意先認識對方嗎？"),
            reply("有人想找人一起吃東港生魚片，並邀請你先認識看看。你願意先認識對方嗎？"),
        ]
        result = build_friend_intro_v4(
            {"user_id": "owner", "current_context": "最近想找人去安靜的咖啡館聊天"},
            {"user_id": "candidate", "current_context": "週末想拍照"},
            0.82,
            refine=True,
            search_context={
                "invitation_topic": "吃東港生魚片",
                "query_text": "我要找人去吃東港生魚片叫它幫我邀請",
            },
            auto_invite=True,
        )
        self.assertEqual(generate.call_count, 4)
        for call in generate.call_args_list:
            self.assertNotIn("咖啡館", call.args[0])
            self.assertNotIn("週末想拍照", call.args[0])
            self.assertIn('"candidate_interest_confirmed": false', call.args[0])
        for entry in result.values():
            self.assertNotIn("有興趣", entry["viewer_text"])
            self.assertNotIn("喜歡", entry["viewer_text"])
            self.assertNotIn("咖啡館", entry["viewer_text"])
            self.assertNotIn("週末想拍照", entry["viewer_text"])

    def test_live_topic_card_reprojects_old_unverified_claims(self):
        match_doc = {
            "from_user": "owner",
            "to_user": "candidate",
            "status": "pending",
            "delivery_mode": "invite_on_match",
            "reason_version": "v4_friend_intro",
            "reason_copy_version": "v8_source_directional",
            "recommendation_tier": "exploratory",
            "search_context": {
                "invitation_topic": "吃東港生魚片",
                "query_text": "找人去吃東港生魚片",
            },
            "match_context_snapshot": {
                "target": {
                    "user_id": "owner",
                    "current_context": "最近想找人去安靜的咖啡館聊天",
                },
                "candidate": {
                    "user_id": "candidate",
                    "current_context": "週末想拍照",
                },
            },
            "friend_intro_v4": {
                "initiator_preview": {
                    "style_id": "topic_request",
                    "viewer_id": "owner",
                    "counterparty_id": "candidate",
                    "counterparty_context_snapshot": "週末想拍照",
                    "viewer_text": "我找到一位也對吃東港生魚片有興趣的人選，你們可以聊看看。",
                },
                "receiver_invitation": {
                    "style_id": "topic_request",
                    "viewer_id": "candidate",
                    "counterparty_id": "owner",
                    "counterparty_context_snapshot": "最近想找人去安靜的咖啡館聊天",
                    "viewer_text": "對方最近想去安靜的咖啡館，也想請你吃東港生魚片，你有興趣嗎？",
                },
            },
        }
        owner = reason_for_viewer(match_doc, "owner")
        receiver = reason_for_viewer(match_doc, "candidate")
        self.assertIn("邀請已送出", owner)
        self.assertIn("邀請你先認識", receiver)
        for text in (owner, receiver):
            self.assertNotIn("有興趣", text)
            self.assertNotIn("咖啡館", text)
            self.assertNotIn("週末想拍照", text)

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_requested_topic_does_not_bypass_hard_conflict(self, _graph_memories):
        qualification = candidate_qualification(
            {"user_id": "owner"}, {"user_id": "candidate"},
            target_stances={"surfing": {"avoid"}},
            candidate_stances={"surfing": {"like"}},
            vector_score=0.99,
            search_context={"invitation_topic": "衝浪"},
        )
        self.assertFalse(qualification["eligible"])
        self.assertEqual(qualification["hard_conflict_keys"], ["surfing"])

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_requested_preference_is_direct_evidence_without_recent_context(self, _graph_memories):
        qualification = candidate_qualification(
            {"user_id": "owner", "current_context": "最近在游泳"},
            {"user_id": "candidate", "current_context": "最近在爬山"},
            target_stances={}, candidate_stances={K_POP: {"like"}},
            vector_score=0,
            search_context={
                "search_intent": "preference", "normalized_topic": "K-pop",
                "canonical_preference_key": K_POP,
            },
        )
        self.assertTrue(qualification["eligible"])
        self.assertIn("requested_preference", qualification["strong_reason_codes"])
        self.assertEqual(qualification["match_basis"]["level"], "direct")

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_requested_preference_never_bypasses_hard_conflict(self, _graph_memories):
        qualification = candidate_qualification(
            {"user_id": "owner"}, {"user_id": "candidate"},
            target_stances={K_POP: {"avoid"}},
            candidate_stances={K_POP: {"like"}},
            search_context={
                "search_intent": "preference", "normalized_topic": "K-pop",
                "canonical_preference_key": K_POP,
            },
        )
        self.assertFalse(qualification["eligible"])
        self.assertEqual(qualification["hard_conflict_keys"], [K_POP])

    @patch("routers.match.get_user_graph_memories")
    def test_preference_intro_uses_exact_topic_without_recent_context_claims(self, memories):
        memories.side_effect = lambda user_id, _limit: (
            [{**canonicalize_concept("K-pop").as_dict(), "stance": "like"}]
            if user_id == "candidate" else []
        )
        projection = build_friend_intro_v4(
            {"user_id": "owner", "current_context": "最近在游泳"},
            {"user_id": "candidate", "current_context": "最近在爬山"},
            0,
            search_context={
                "search_intent": "preference", "normalized_topic": "K-pop",
                "canonical_preference_key": K_POP,
            },
        )
        initiator = projection["initiator_preview"]["viewer_text"]
        receiver = projection["receiver_invitation"]["viewer_text"]
        assert "K-pop" in initiator and "明確提過喜歡" in initiator
        assert "K-pop" in receiver and "明確提過喜歡" in receiver
        assert "游泳" not in initiator + receiver
        assert "爬山" not in initiator + receiver

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_user_visible_explanation_never_contains_internal_candidate_id(self, _graph_memories):
        _, _, _, reason = build_validated_match_explanation(
            {"user_id": "owner", "current_context": "近期想逛市集"},
            {"user_id": "seed_user_09", "current_context": "週末想逛老街"},
            0.72,
        )
        self.assertNotIn("seed_user", reason)
        self.assertIn("對方", reason)

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_independent_recent_plans_are_an_exploratory_recommendation_not_common_ground(self, _graph_memories):
        _, items, _, reason = build_validated_match_explanation(
            {"user_id": "owner", "current_context": "近期想去京都賞櫻", "big_five": {"O": 6}},
            {"user_id": "candidate", "current_context": "想去居酒屋小酌", "big_five": {"O": 6}},
            0.72,
        )
        self.assertIn("探索型推薦", reason)
        self.assertIn("尚未找到明確共同點", reason)
        self.assertNotIn("你最近提到", reason)
        self.assertNotIn("個性節奏", reason)
        self.assertIn(
            {"kind": "recommendation_tier", "text": "exploratory", "target_evidence_ids": [], "candidate_evidence_ids": []},
            items,
        )

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_verified_shared_activity_is_presented_as_grounded_common_ground(self, _graph_memories):
        _, items, _, reason = build_validated_match_explanation(
            {"user_id": "owner", "context_signals": {"activity": "京都賞櫻"}},
            {"user_id": "candidate", "context_signals": {"activity": "京都賞櫻"}},
            0.7,
        )
        self.assertIn("已確認共同點", reason)
        self.assertIn("你們近期都提到京都賞櫻", reason)
        self.assertIn(
            {"kind": "recommendation_tier", "text": "grounded", "target_evidence_ids": [], "candidate_evidence_ids": []},
            items,
        )

    def test_distinctive_tags_are_derived_from_candidate_owned_fields(self):
        tags = validated_distinctive_tags({
            "context_signals": {"activity": "週末登山"},
            "initial_interest": "底片攝影",
            "deep_profile": {"values": ["真誠溝通", "生活平衡"]},
            "big_five": {"summary": "溫和而願意傾聽"},
        })
        self.assertEqual(tags, ["週末登山", "底片攝影", "真誠溝通", "生活平衡"])
        self.assertNotIn("模型自由生成", tags)

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_semantic_preference_is_adjacent_and_never_shared_preference(self, _memories):
        qualification = candidate_qualification(
            {"user_id": "owner"}, {"user_id": "candidate"},
            target_stances={},
            candidate_stances={"korean_pop": {"like"}},
            search_context={
                "search_intent": "preference", "normalized_topic": "K-pop",
                "canonical_preference_key": K_POP,
            },
            preference_evidence=[{
                "kind": "semantic_related", "concept_key": "korean_pop",
                "similarity": 0.91,
            }],
        )
        self.assertTrue(qualification["eligible"])
        self.assertEqual(qualification["match_basis"]["level"], "adjacent")
        self.assertIn(
            "semantic_related_preference", qualification["strong_reason_codes"],
        )
        self.assertEqual(qualification["shared_preference_keys"], [])
        self.assertFalse(qualification["requested_preference_matched"])
        self.assertNotIn(
            "共同偏好：k_pop",
            " ".join(qualification["match_basis"]["concrete_overlap"]),
        )

    @patch("routers.match.get_user_graph_memories", return_value=[])
    def test_semantic_preference_still_loses_to_verified_hard_conflict(self, _memories):
        qualification = candidate_qualification(
            {"user_id": "owner"}, {"user_id": "candidate"},
            target_stances={"smoking": {"avoid"}},
            candidate_stances={
                "korean_pop": {"like"}, "smoking": {"like"},
            },
            search_context={
                "search_intent": "preference", "normalized_topic": "K-pop",
                "canonical_preference_key": K_POP,
            },
            preference_evidence=[{
                "kind": "semantic_related", "concept_key": "korean_pop",
                "similarity": 0.91,
            }],
        )
        self.assertFalse(qualification["eligible"])
        self.assertEqual(qualification["hard_conflict_keys"], ["smoking"])


if __name__ == "__main__":
    unittest.main()
