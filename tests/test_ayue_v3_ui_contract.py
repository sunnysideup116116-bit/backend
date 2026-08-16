import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = SERVER_ROOT / "social" / "frontend.html"
CONTRACT = SERVER_ROOT / "docs" / "ayue-v3-ui-contract.md"


class AyueV3UiContractTests(unittest.TestCase):
    def test_contract_covers_every_mobile_surface(self):
        contract = CONTRACT.read_text(encoding="utf-8")
        required_sections = (
            "Public Ayue stream",
            "Assessment controls",
            "Recent-context confirmation",
            "Match search progress",
            "Match proposal decision",
            "Calendar",
            "Date coordination",
            "Private mediator",
            "Notifications and contacts",
            "Settings and memories",
        )
        for section in required_sections:
            self.assertIn(section, contract)

    def test_documented_website_contract_markers_still_exist(self):
        source = FRONTEND.read_text(encoding="utf-8")
        markers = (
            'fetch("/api/direct_chat/stream"',
            'event.type === "run_started"',
            'event.type === "tool_started"',
            'event.type === "final"',
            'event.type === "error"',
            "presentation_blocks",
            "place_cards",
            "assessment_state",
            "context_confirmation_needed",
            "/api/profile/recent-context/status",
            "/api/match/status",
            "/api/match/decision",
            "/api/calendar/events",
            "/api/relationship/quiz/start",
            "/api/mediator/private/stream",
            "/api/notifications",
            "/api/settings",
            "/api/profile/memories",
        )
        for marker in markers:
            self.assertIn(marker, source)

    def test_contract_forbids_state_inference_from_reply_copy(self):
        contract = CONTRACT.read_text(encoding="utf-8")
        self.assertIn("must not infer", contract.lower())
        self.assertIn("canonical state", contract.lower())

    def test_contract_places_shared_date_card_outside_private_mediator(self):
        contract = CONTRACT.read_text(encoding="utf-8")
        self.assertIn("render in the shared pair room", contract)
        self.assertIn("no relationship-quiz UI", contract)


if __name__ == "__main__":
    unittest.main()
