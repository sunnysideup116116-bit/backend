import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[2]
MODELS = SERVER_ROOT / "social" / "models.py"
ROUTER = SERVER_ROOT / "social" / "routers" / "chat_onboarding.py"
SERVICE = SERVER_ROOT / "social" / "services" / "assessment_session_service.py"
DOCUMENT = SERVER_ROOT / "docs" / "api" / "ayue-v3-mobile-bootstrap.md"


class MobileProfileBootstrapContractTests(unittest.TestCase):
    def test_chat_request_exposes_typed_initialize_flag(self):
        source = MODELS.read_text(encoding="utf-8")
        self.assertIn("class ChatRequest(BaseModel):", source)
        self.assertIn("initialize: bool = False", source)
        self.assertIn("initial_interest: str = None", source)

    def test_router_delegates_to_shared_assessment_state(self):
        source = ROUTER.read_text(encoding="utf-8")
        self.assertIn('@router.post("/chat")', source)
        self.assertIn("handle_assessment_ui_message(", source)
        self.assertIn("initial_interest=req.initial_interest, initialize=req.initialize", source)
        self.assertIn('req.state not in {"big_five", "deep_profile"}', source)

    def test_service_is_idempotent_and_preserves_completed_profile(self):
        source = SERVICE.read_text(encoding="utf-8")
        self.assertIn('idempotency_key=f"onboarding-ui:{kind}"', source)
        self.assertIn('if initialize:', source)
        self.assertIn('"$setOnInsert": {"user_id": user_id}', source)
        self.assertIn('"initial_interest": {"$exists": False}', source)
        self.assertIn('{"initial_interest": ""}', source)
        self.assertIn('completed profile stays untouched', source)
        self.assertIn('never clears completed results', source)

    def test_document_assigns_identity_and_data_ownership(self):
        document = DOCUMENT.read_text(encoding="utf-8")
        self.assertIn("Appwrite Account `$id`", document)
        self.assertIn('"initialize": true', document)
        self.assertIn("/api/profile/big-five/initialize", document)
        self.assertIn("must not be restored", document)
        self.assertIn("Appwrite JWT", document)


if __name__ == "__main__":
    unittest.main()
