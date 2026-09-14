import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers import system
from tests.match_flow_store import Collection


class MatchTestOverviewTests(unittest.TestCase):
    def setUp(self):
        self.profiles = Collection([
            {
                "_id": "p1", "user_id": "seed_user_01", "display_name": "小安",
                "current_context": "想喝咖啡", "test_match_cohort": "match_v1",
            },
            {
                "_id": "p2", "user_id": "match_test_01", "display_name": "咖啡小伴01",
                "current_context": "想找人喝咖啡", "test_match_cohort": "match_v1",
                "test_login_email": "matchtest01@gmail.com", "test_match_topic": "咖啡",
            },
            {"_id": "p3", "user_id": "real_user", "display_name": "真人"},
        ])
        self.matches = Collection([
            {
                "_id": "m1", "from_user": "seed_user_01",
                "to_user": "match_test_01", "status": "pending",
                "proposal_namespace": "relationship_match", "created_at": 10,
            },
            {
                "_id": "m2", "from_user": "seed_user_01",
                "to_user": "real_user", "status": "draft", "created_at": 11,
            },
        ])

    def test_returns_accounts_and_exact_next_login_without_real_user(self):
        with patch.object(system, "profiles_coll", self.profiles), \
             patch.object(system, "matches_coll", self.matches):
            result = system.get_match_test_overview("seed_user_01")

        self.assertEqual(result["shared_password"], "12345678")
        self.assertEqual(len(result["accounts"]), 2)
        self.assertEqual(len(result["proposals"]), 1)
        proposal = result["proposals"][0]
        self.assertEqual(proposal["waiting_for"]["email"], "matchtest01@gmail.com")
        self.assertEqual(proposal["status_label"], "等待接收者回覆")
        self.assertNotIn("real_user", str(result))

    def test_non_cohort_user_cannot_read_test_credentials(self):
        with patch.object(system, "profiles_coll", self.profiles), \
             patch.object(system, "matches_coll", self.matches):
            with self.assertRaises(HTTPException) as raised:
                system.get_match_test_overview("real_user")
        self.assertEqual(raised.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
