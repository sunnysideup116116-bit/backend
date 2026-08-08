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

    def test_legacy_runtimes_and_rollout_flags_are_removed(self):
        public_chat_source = (ROOT / "routers" / "public_chat.py").read_text(encoding="utf-8")
        private_source = (ROOT / "routers" / "private_mediator.py").read_text(encoding="utf-8")
        for filename in ("runtime.py", "legacy_match_routing.py", "private_runtime.py"):
            self.assertFalse((ROOT / "services" / "ayue_agent" / filename).exists())
        for source in (public_chat_source, private_source):
            self.assertNotIn("agent_mode_for_user_v3", source)
            self.assertNotIn("private_v2_mode_for_user", source)
            self.assertNotIn("private_runtime", source)

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
