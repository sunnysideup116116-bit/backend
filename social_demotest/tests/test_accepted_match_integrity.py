import unittest
from unittest.mock import MagicMock, patch

from routers.chat import get_contacts
from routers.system import seed_data
from services.match_state_service import (
    has_verified_acceptance,
    verified_accepted_match_query,
)


class AcceptedMatchIntegrityTests(unittest.TestCase):
    def test_bare_accepted_status_is_not_verified(self):
        self.assertFalse(has_verified_acceptance({
            "from_user": "owner",
            "to_user": "other",
            "status": "accepted",
        }))

    def test_pending_to_accepted_transition_is_verified(self):
        self.assertTrue(has_verified_acceptance({
            "status": "accepted",
            "state_history": [
                {"from": None, "to": "draft", "action": "created"},
                {"from": "draft", "to": "pending", "action": "accept"},
                {"from": "pending", "to": "accepted", "action": "accept"},
            ],
        }))

    @patch("routers.chat.profiles_coll")
    @patch("routers.chat.matches_coll")
    def test_contacts_require_canonical_acceptance_evidence(self, matches, profiles):
        matches.find.return_value = []
        profiles.find_one.return_value = {"ai_chat_locked": False}

        response = get_contacts("owner")

        self.assertEqual([item["id"] for item in response["contacts"]], ["ai_assistant"])
        matches.find.assert_called_once_with(
            verified_accepted_match_query("owner")
        )

    @patch("routers.system.get_embedding", return_value=[0.0])
    @patch("routers.system.profiles_coll")
    @patch("routers.system.matches_coll")
    def test_reseeding_removes_stale_seed_matches(
        self, matches, profiles, _embedding,
    ):
        matches.find.return_value = [{"_id": "stale-1"}, {"_id": "stale-2"}]
        profiles.insert_many = MagicMock()

        result = seed_data()

        matches.delete_many.assert_called_once_with(
            {"_id": {"$in": ["stale-1", "stale-2"]}}
        )
        self.assertEqual(result["stale_seed_matches_removed"], 2)
        self.assertEqual(len(profiles.insert_many.call_args.args[0]), 10)


if __name__ == "__main__":
    unittest.main()
