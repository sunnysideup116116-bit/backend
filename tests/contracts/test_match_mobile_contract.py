import importlib.util
import json
import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = SERVER_ROOT / "tests" / "contracts" / "fixtures" / "ayue_v3_contracts.py"
SCHEMA_PATH = SERVER_ROOT / "docs" / "api" / "ayue-v3-match-decision.schema.json"
MATCH_ROUTER = SERVER_ROOT / "social" / "routers" / "match.py"
WEBSITE = SERVER_ROOT / "social" / "frontend.html"


def load_contracts():
    spec = importlib.util.spec_from_file_location("ayue_v3_contracts", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MatchMobileContractTests(unittest.TestCase):
    def setUp(self):
        self.contracts = load_contracts()

    def test_request_builder_requires_visible_live_state(self):
        for status, actions in {
            "draft": ("accept", "decline"),
            "pending": ("accept", "decline", "cancel"),
        }.items():
            for action in actions:
                payload = self.contracts.build_match_decision(
                    user_id="owner",
                    match_id="match-1",
                    action=action,
                    expected_status=status,
                    expected_revision=4,
                    explicit_reasons=["節奏不合"],
                )
                self.assertEqual(payload["expected_status"], status)
                self.assertEqual(payload["expected_revision"], 4)
        for terminal in ("accepted", "declined", "expired"):
            with self.assertRaises(ValueError):
                self.contracts.build_match_decision(
                    user_id="owner", match_id="match-1", action="accept",
                    expected_status=terminal,
                )

    def test_duplicate_tap_guard_and_conflict_policy(self):
        guard = self.contracts.MatchDecisionTapGuard()
        self.assertTrue(guard.begin("match-1"))
        self.assertFalse(guard.begin("match-1"))
        self.assertTrue(guard.begin("match-2"))
        guard.finish("match-1")
        self.assertTrue(guard.begin("match-1"))
        self.assertEqual(self.contracts.match_decision_error_action(409), "refresh_without_replay")
        self.assertEqual(self.contracts.match_decision_error_action(503), "show_retry")

    def test_schema_keeps_revision_optional_for_website_compatibility(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertIn("expected_status", schema["required"])
        self.assertNotIn("expected_revision", schema["required"])
        self.assertEqual(schema["properties"]["action"]["enum"], ["accept", "decline", "cancel"])
        self.assertEqual(schema["x-http-409-policy"], "refresh_without_replay")

    def test_backend_returns_current_authority_on_stale_decision(self):
        source = MATCH_ROUTER.read_text(encoding="utf-8")
        self.assertIn('status_code=409', source)
        self.assertIn('"current_status": result.get("current_status")', source)
        self.assertIn('"current_revision": result.get("current_revision")', source)
        self.assertIn('expected_status=req.expected_status', source)
        self.assertIn('expected_revision=req.expected_revision', source)

    def test_website_matches_optional_revision_contract(self):
        source = WEBSITE.read_text(encoding="utf-8")
        start = source.index("async function decideMatch(")
        end = source.index("async function cancelPendingMatch", start)
        decision_source = source[start:end]
        self.assertIn("expected_status: expectedStatus", decision_source)
        self.assertNotIn("expected_revision", decision_source)
        self.assertIn("res.status === 409", decision_source)
        self.assertIn("refreshMatchStatus", decision_source)


if __name__ == "__main__":
    unittest.main()
