import unittest

from services.ayue_agent.context import _safe_place_search_draft


class PlaceSearchDraftProjectionTests(unittest.TestCase):
    def test_cuisine_is_projected_into_planner_context(self):
        draft = _safe_place_search_draft({
            "version": "v1", "anchor": "", "categories": ["restaurant"],
            "cuisine": "火鍋", "radius_m": 1500, "limit": 3,
            "use_saved_location": True, "created_at": 1,
        })
        self.assertEqual(draft["cuisine"], "火鍋")

    def test_cuisine_is_bounded_and_cleaned(self):
        draft = _safe_place_search_draft({
            "version": "v1", "anchor": "", "categories": ["restaurant"],
            "cuisine": "火鍋\n日式\t料理" + "長" * 100,
            "radius_m": 1500, "limit": 3, "use_saved_location": True, "created_at": 1,
        })
        self.assertNotIn("\n", draft["cuisine"])
        self.assertNotIn("\t", draft["cuisine"])
        self.assertLessEqual(len(draft["cuisine"]), 30)

    def test_cuisine_absent_when_missing_or_invalid(self):
        draft = _safe_place_search_draft({
            "version": "v1", "anchor": "", "categories": ["restaurant"],
            "radius_m": 1500, "limit": 3, "use_saved_location": True, "created_at": 1,
        })
        self.assertEqual(draft["cuisine"], "")
        dirty = _safe_place_search_draft({
            "version": "v1", "anchor": "", "categories": ["restaurant"],
            "cuisine": 12345, "radius_m": 1500, "limit": 3,
            "use_saved_location": True, "created_at": 1,
        })
        self.assertEqual(dirty["cuisine"], "")


if __name__ == "__main__":
    unittest.main()
