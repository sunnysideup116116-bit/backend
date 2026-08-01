import json
import unittest
from pathlib import Path


CATALOG_PATH = Path(__file__).parent / "fixtures" / "ayue_refactor_trajectory_catalog.json"
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "ayue_public_trajectories.json"


class AyueRefactorTrajectoryCatalogTests(unittest.TestCase):
    def test_catalog_is_deidentified_and_has_a_complete_baseline_shape(self):
        catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
        golden_ids = {
            item["id"] for item in json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
        }
        expected_ids = {
            "recent_future_activity", "recent_today_commitment", "recent_vague_then_draft",
            "recent_draft_answer", "durable_preference", "third_party_preference",
            "mixed_activity_companion", "explicit_no_store", "natural_match_search",
            "unsupported_direct_date", "match_status", "counterparty_summary",
            "ordinary_why", "decline_why", "confirmation_topic_switch",
            "random_restaurant_followup",
        }
        self.assertEqual({item["id"] for item in catalog}, expected_ids)
        for item in catalog:
            with self.subTest(trajectory=item["id"]):
                self.assertTrue(item["message"])
                self.assertIn(item["semantic_owner"], {"planner", "profile_extractor"})
                self.assertTrue(item["profile_write"])
                self.assertTrue(item["tool_category"])
                self.assertIn(item["coverage_status"], {
                    "runtime_replay", "unit_characterized", "catalog_only", "known_gap",
                })
                self.assertIn(item["target_phase"], {1, 2})
                if item["coverage_status"] == "runtime_replay":
                    self.assertIn(item.get("golden_fixture_id"), golden_ids)
                serialized = json.dumps(item, ensure_ascii=False)
                self.assertNotIn("seed_user_", serialized)
                self.assertNotIn("demo_user", serialized)


if __name__ == "__main__":
    unittest.main()
