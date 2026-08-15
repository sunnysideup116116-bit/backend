import unittest
from unittest.mock import patch

from services.mediator_context_service import (
    private_counterparty_strategy_context,
    private_viewer_profile_context,
)


class PrivateContextProjectionTests(unittest.TestCase):
    def test_viewer_projection_is_bounded_and_has_no_id(self):
        with patch(
            "services.mediator_context_service.profiles_coll.find_one",
            return_value={
                "user_id": "owner",
                "current_context": "最近去爬山",
                "initial_interest": "x" * 300,
                "big_five": {"summary": "y" * 300},
                "profile_memory_preview": [{"label": str(index)} for index in range(20)],
            },
        ):
            result = private_viewer_profile_context("owner")
        self.assertEqual(set(result), {"recent_context", "initial_interest", "personality_summary", "memories"})
        self.assertLessEqual(len(result["initial_interest"]), 120)
        self.assertLessEqual(len(result["personality_summary"]), 180)
        self.assertLessEqual(len(result["memories"]), 8)
        self.assertNotIn("user_id", result)

    def test_counterparty_projection_is_strategy_only_and_allowlisted(self):
        with patch(
            "services.mediator_context_service.profiles_coll.find_one",
            return_value={
                "user_id": "other",
                "big_five": {"summary": "summary"},
                "current_context": "current",
                "deep_profile": {
                    "values": ["honesty"],
                    "life_goals": "stable life",
                    "private_secret": "must not appear",
                },
                "profile_memory_preview": [{"label": "likes hiking", "stance": "like", "category": "activity"}],
            },
        ):
            result = private_counterparty_strategy_context("other")
        self.assertEqual(set(result), {"big_five_summary", "deep_profile", "current_context", "memories"})
        self.assertIn("values", result["deep_profile"])
        self.assertNotIn("private_secret", str(result))
        self.assertNotIn("user_id", str(result))


if __name__ == "__main__":
    unittest.main()
