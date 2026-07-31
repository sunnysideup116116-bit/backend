import unittest
from unittest.mock import patch

from services.ayue_agent.match_opportunity import (
    MatchOpportunityAssessment,
    assess_match_opportunity,
)


class MatchOpportunityPolicyTests(unittest.TestCase):
    def _ready_profile(self):
        return {
            "current_context": "最近想去合掌村旅行",
            "current_context_revision": 3,
            "profile_memory_preview": ["不喜歡抽菸"],
        }

    @patch("services.ayue_agent.match_opportunity.matches_coll.find_one", return_value=None)
    def test_two_grounded_profile_bases_are_ready(self, _find_one):
        assessment = assess_match_opportunity(self._ready_profile(), "owner")
        self.assertEqual(assessment.state, "ready")
        self.assertEqual(assessment.profile_basis_count, 2)

    @patch("services.ayue_agent.match_opportunity.matches_coll.find_one", return_value=None)
    def test_personality_alone_never_triggers_a_match_offer(self, _find_one):
        assessment = assess_match_opportunity({
            "big_five": {"O": 70, "C": 60, "E": 55, "A": 65, "N": 40},
        }, "owner")
        self.assertEqual(assessment.state, "not_ready")
        self.assertIn("profile_basis_insufficient", assessment.reason_codes)

    @patch("services.ayue_agent.match_opportunity.matches_coll.find_one", return_value=None)
    def test_same_profile_fingerprint_is_suppressed_after_an_offer(self, _find_one):
        profile = self._ready_profile()
        profile["match_guidance"] = {"last_fingerprint": "placeholder"}
        first = assess_match_opportunity(profile, "owner")
        profile["match_guidance"] = {"last_fingerprint": first.fingerprint}
        repeated = assess_match_opportunity(profile, "owner")
        self.assertEqual(repeated.state, "suppressed")
        self.assertIn("same_fingerprint", repeated.reason_codes)

    @patch("services.ayue_agent.match_opportunity.matches_coll.find_one", return_value=None)
    def test_changed_preference_content_changes_fingerprint_even_at_same_count(self, _find_one):
        first_profile = self._ready_profile()
        first = assess_match_opportunity(first_profile, "owner")
        changed_profile = self._ready_profile()
        changed_profile["profile_memory_preview"] = ["喜歡戶外活動"]
        changed_profile["match_guidance"] = {"last_fingerprint": first.fingerprint}
        changed = assess_match_opportunity(changed_profile, "owner")
        self.assertEqual(changed.state, "ready")
        self.assertNotEqual(changed.fingerprint, first.fingerprint)

    @patch("services.ayue_agent.match_opportunity.matches_coll.find_one")
    def test_active_match_blocks_guidance(self, find_one):
        find_one.return_value = {"_id": "active"}
        assessment = assess_match_opportunity(self._ready_profile(), "owner")
        self.assertEqual(assessment.state, "active_match_blocked")
        self.assertIn("active_match", assessment.reason_codes)

    @patch("services.ayue_agent.match_opportunity.matches_coll.find_one", return_value=None)
    def test_accepted_connection_is_terminal_and_does_not_block_explicit_next_search(self, find_one):
        assessment = assess_match_opportunity(self._ready_profile(), "owner", explicit_search=True)
        self.assertEqual(assessment.state, "ready")
        blocking_query = find_one.call_args_list[0].args[0]
        self.assertEqual(set(blocking_query["status"]["$in"]), {"draft", "pending"})
        self.assertNotIn("accepted", blocking_query["status"]["$in"])


if __name__ == "__main__":
    unittest.main()
