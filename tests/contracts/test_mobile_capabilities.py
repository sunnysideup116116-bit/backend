import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = SERVER_ROOT / "tests" / "contracts" / "fixtures" / "ayue_v3_capabilities.py"
MANIFEST_PATH = SERVER_ROOT / "docs" / "api" / "ayue-v3-mobile-capabilities.json"


def load_capabilities():
    spec = importlib.util.spec_from_file_location("ayue_v3_capabilities", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class MobileCapabilitiesContractTests(unittest.TestCase):
    def setUp(self):
        self.capabilities = load_capabilities()

    def test_manifest_has_stable_required_capabilities(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        self.assertEqual(manifest["contract_version"], 1)
        for key in (
            "public_stream", "assessment_controls", "calendar", "date_coordination",
            "match_decision", "place_cards", "private_mediator", "relationship_quiz",
            "risk_projection",
        ):
            self.assertIn(key, manifest["capabilities"])
            self.assertIsInstance(manifest["capabilities"][key]["available"], bool)
        self.assertEqual(manifest["capabilities"]["match_decision"]["mode"], "status_cas")
        self.assertFalse(manifest["capabilities"]["relationship_quiz"]["random_topic_route"])

    def test_place_cards_are_derived_from_config_and_keep_text_fallback(self):
        with tempfile.TemporaryDirectory() as raw:
            env_path = Path(raw) / ".env"
            env_path.write_text(
                "AYUE_GOOGLE_PLACE_CARDS_ENABLED=on\n"
                "AYUE_PUBLIC_PLACE_CARDS_ENABLED=on\n"
                "GOOGLE_PLACES_SERVER_API_KEY=server-secret\n"
                "GOOGLE_MAPS_BROWSER_API_KEY=browser-secret\n",
                encoding="utf-8",
            )
            enabled = self.capabilities.build_mobile_capabilities(env_path)
            env_path.write_text("AYUE_PUBLIC_PLACE_CARDS_ENABLED=off\n", encoding="utf-8")
            disabled = self.capabilities.build_mobile_capabilities(env_path)
        self.assertTrue(enabled["capabilities"]["place_cards"]["available"])
        self.assertFalse(disabled["capabilities"]["place_cards"]["available"])
        self.assertTrue(enabled["capabilities"]["place_cards"]["text_fallback_required"])
        serialized = json.dumps(enabled)
        self.assertNotIn("server-secret", serialized)
        self.assertNotIn("browser-secret", serialized)

    def test_pair_chat_risk_projection_is_advertised_as_fail_open(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        risk = manifest["capabilities"]["risk_projection"]
        self.assertTrue(risk["available"])
        self.assertEqual(risk["status"], "pair_chat_pre_persistence")
        self.assertEqual(risk["failure_policy"], "deliver_degraded")


if __name__ == "__main__":
    unittest.main()
