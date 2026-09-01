import unittest
from unittest.mock import patch

from services.match_state_service import get_match_status_snapshot


class MatchStateSnapshotTests(unittest.TestCase):
    @patch("services.match_state_service.profiles_coll")
    @patch("services.match_state_service.matches_coll")
    def test_latest_accepted_is_not_lost_when_search_is_idle(self, matches, profiles):
        matches.count_documents.return_value = 0
        matches.find_one.return_value = {
            "from_user": "owner", "to_user": "other", "status": "accepted",
            "proposal_revision": 2, "updated_at": 100,
        }
        profiles.find_one.side_effect = [
            {"match_search": {"status": "idle"}},
            {"display_name": "小安"},
        ]
        snapshot = get_match_status_snapshot("owner")
        self.assertEqual(snapshot["state"], "accepted")
        self.assertEqual(snapshot["scope"], "latest_match")
        self.assertTrue(snapshot["is_terminal"])
        self.assertTrue(snapshot["chat_opened"])
        self.assertEqual(snapshot["counterparty"], "小安")

    @patch("services.match_state_service.profiles_coll")
    @patch("services.match_state_service.matches_coll")
    def test_active_search_precedes_old_terminal_result(self, matches, profiles):
        matches.count_documents.return_value = 0
        profiles.find_one.return_value = {"match_search": {"status": "searching", "started_at": 100}}
        snapshot = get_match_status_snapshot("owner")
        self.assertEqual(snapshot["state"], "searching")
        self.assertEqual(snapshot["scope"], "search")
        self.assertFalse(snapshot["chat_opened"])
        matches.find_one.assert_not_called()

    @patch("services.match_state_service.profiles_coll")
    @patch("services.match_state_service.matches_coll")
    def test_newer_search_failure_precedes_old_accepted_result(self, matches, profiles):
        matches.count_documents.return_value = 0
        profiles.find_one.return_value = {"match_search": {"status": "no_candidates", "completed_at": 200}}
        matches.find_one.return_value = {
            "from_user": "owner", "to_user": "other", "status": "accepted", "updated_at": 100,
        }
        snapshot = get_match_status_snapshot("owner")
        self.assertEqual(snapshot["state"], "no_candidates")
        self.assertEqual(snapshot["scope"], "search")
        self.assertFalse(snapshot["chat_opened"])

    @patch("services.match_state_service.profiles_coll")
    @patch("services.match_state_service.matches_coll")
    def test_multiple_live_matches_fail_closed(self, matches, profiles):
        matches.count_documents.return_value = 2
        snapshot = get_match_status_snapshot("owner")
        self.assertEqual(snapshot["state"], "failed")
        self.assertFalse(snapshot["chat_opened"])
        self.assertEqual(snapshot["reason_code"], "ambiguous_live_match")


if __name__ == "__main__":
    unittest.main()
