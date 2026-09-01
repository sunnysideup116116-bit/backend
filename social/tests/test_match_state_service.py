import unittest
from unittest.mock import patch

from services.match_state_service import get_match_status_snapshot, reconcile_live_match


class MatchStateSnapshotTests(unittest.TestCase):
    @patch("services.match_state_service.profiles_coll")
    @patch("services.match_state_service.matches_coll")
    def test_old_live_proposal_waits_for_explicit_decision(self, matches, profiles):
        old_proposal = {
            "_id": "match", "from_user": "owner", "to_user": "other",
            "status": "draft", "proposal_revision": 0, "created_at": 1,
        }
        matches.find.return_value.sort.return_value.limit.return_value = [old_proposal]

        result = reconcile_live_match("owner")

        self.assertEqual(result, old_proposal)
        matches.update_many.assert_not_called()

    @patch("services.match_state_service.profiles_coll")
    @patch("services.match_state_service.matches_coll")
    def test_latest_timeout_expiry_is_restored_when_no_live_proposal_exists(
        self, matches, profiles,
    ):
        expired = {
            "_id": "match", "from_user": "owner", "to_user": "other",
            "status": "expired", "proposal_revision": 1,
            "expired_reason": "pending_timeout", "created_at": 1,
        }
        restored = {**expired, "status": "pending", "proposal_revision": 2}
        matches.find.return_value.sort.return_value.limit.return_value = []
        matches.find_one.return_value = expired
        matches.find_one_and_update.return_value = restored

        result = reconcile_live_match("owner")

        self.assertEqual(result, restored)
        update = matches.find_one_and_update.call_args.args[1]
        self.assertEqual(update["$set"]["status"], "pending")
        self.assertEqual(update["$inc"]["proposal_revision"], 1)
        self.assertIn("expired_reason", update["$unset"])

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
        self.assertEqual(matches.find_one.call_count, 1)
        self.assertIn("draft_timeout", str(matches.find_one.call_args))

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
