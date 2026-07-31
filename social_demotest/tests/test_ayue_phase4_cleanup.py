import ast
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).parents[1]


class AyuePhase4CleanupTests(unittest.TestCase):
    def test_v2_router_has_no_legacy_keyword_router_or_legacy_contracts(self):
        source = (ROOT / "services" / "ayue_agent" / "router.py").read_text(encoding="utf-8")
        for removed_name in (
            "route_turn", "validate_tool_intent", "explicit_search_request",
            "is_unsupported_direct_date_request", "RouteDecision", "SkillName",
        ):
            self.assertNotIn(removed_name, source)
        self.assertFalse((ROOT / "services" / "ayue_agent" / "skills.py").exists())

    def test_legacy_helpers_are_isolated_to_the_rollback_adapter(self):
        chat_source = (ROOT / "routers" / "chat.py").read_text(encoding="utf-8")
        legacy_source = (ROOT / "services" / "ayue_agent" / "legacy_match_routing.py").read_text(encoding="utf-8")
        v2_router_source = (ROOT / "services" / "ayue_agent" / "router.py").read_text(encoding="utf-8")
        runtime_source = (ROOT / "services" / "ayue_agent" / "runtime.py").read_text(encoding="utf-8")
        chat_tree = ast.parse(chat_source)
        top_level_imports = {
            node.module
            for node in chat_tree.body
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("services.ayue_agent.legacy_match_routing", top_level_imports)
        self.assertIn("from services.ayue_agent import legacy_match_routing", chat_source)
        self.assertIn("def is_explicit_match_request", legacy_source)
        self.assertIn("def should_answer_match_outcome_followup", legacy_source)
        self.assertNotIn("legacy_match_routing", v2_router_source)
        self.assertNotIn("legacy_match_routing", runtime_source)

    def test_deidentified_trajectory_fixture_has_five_replayable_turns(self):
        fixtures = json.loads(
            (ROOT / "tests" / "fixtures" / "ayue_public_trajectories.json").read_text(encoding="utf-8")
        )
        self.assertGreaterEqual(len(fixtures), 5)
        for fixture in fixtures:
            self.assertTrue(fixture["id"])
            self.assertIn("expected_events", fixture)
            self.assertNotIn("seed_user_", json.dumps(fixture, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
