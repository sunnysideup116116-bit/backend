import unittest
from unittest.mock import patch

from routers.match import (
    build_active_proposal_card,
    build_validated_match_explanation,
    build_directional_match_explanations,
    build_directional_reason_v3_from_snapshot,
    candidate_qualification,
    validated_distinctive_tags,
)


class MatchQualificationTests(unittest.TestCase):
    @patch("routers.match.public_display_name", side_effect=lambda user_id: {"owner": "小安", "candidate": "小林"}.get(user_id, "對方"))
    def test_active_proposal_reason_is_projected_for_the_viewer(self, _display_name):
        match = {
            "_id": "proposal-1", "from_user": "owner", "to_user": "candidate", "status": "pending",
            "proposal_revision": 2, "reason": "給小安看的理由", "receiver_reason": "給小林看的理由",
            "reason_items": [], "receiver_reason_items": [], "match_context_snapshot": {},
        }
        owner_card = build_active_proposal_card(match, "owner")
        candidate_card = build_active_proposal_card(match, "candidate")
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
    def test_semantically_similar_recent_context_is_eligible_without_exact_activity(self, _graph_memories):
        qualification = candidate_qualification(
            {"user_id": "owner", "current_context": "近期想去京都逛市集"},
            {"user_id": "candidate", "current_context": "週末想逛老街和手作攤位"},
            vector_score=0.72,
        )
        self.assertTrue(qualification["eligible"])
        self.assertIn("semantic_context_similarity", qualification["strong_reason_codes"])

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


if __name__ == "__main__":
    unittest.main()
