import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock


SERVER_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = SERVER_ROOT / "tests" / "contracts" / "fixtures" / "ayue_v3_risk_adapter.py"
DOCUMENT = SERVER_ROOT / "docs" / "api" / "ayue-v3-risk-projection.md"


def load_adapter():
    spec = importlib.util.spec_from_file_location("ayue_v3_risk_adapter", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class AyueV3RiskAdapterContractTests(unittest.TestCase):
    def setUp(self):
        self.adapter = load_adapter()

    def _request(self):
        return self.adapter.RiskCheckRequest(
            conversation_id="room-1",
            sender_id="owner",
            receiver_id="other",
            content="hello",
            idempotency_key="message-attempt-1",
        )

    def test_safe_warning_and_restricted_are_deliverable_once(self):
        for level in ("safe", "observation", "warning", "restricted"):
            transport = MagicMock(return_value={
                "risk_level": level,
                "diagnosis": {"private": "must not project"},
                "risk_state": {"private": True},
            })
            gate = self.adapter.RiskAssessmentGate(transport=transport)
            decision = gate.evaluate(self._request())
            self.assertEqual(decision.persist_policy, "allow")
            permit = self.adapter.MessagePersistencePermit(decision)
            self.assertTrue(permit.consume())
            self.assertFalse(permit.consume())
            projection = decision.public_projection()
            self.assertEqual(projection["level"], level)
            self.assertNotIn("diagnosis", projection)
            self.assertNotIn("risk_state", projection)

    def test_blocked_never_yields_a_persistence_permit(self):
        gate = self.adapter.RiskAssessmentGate(
            transport=MagicMock(return_value={"risk_level": "blocked"})
        )
        decision = gate.evaluate(self._request())
        self.assertEqual(decision.persist_policy, "block")
        self.assertFalse(self.adapter.MessagePersistencePermit(decision).consume())

    def test_unavailable_service_is_delivered_as_degraded(self):
        gate = self.adapter.RiskAssessmentGate(
            transport=MagicMock(side_effect=TimeoutError("private endpoint detail"))
        )
        decision = gate.evaluate(self._request())
        self.assertEqual(decision.persist_policy, "allow")
        self.assertTrue(self.adapter.MessagePersistencePermit(decision).consume())
        self.assertEqual(
            decision.public_projection(),
            {"level": "unavailable", "ui_priority": "coach", "delivery": "delivered"},
        )

    def test_duplicate_request_key_calls_risk_service_once(self):
        transport = MagicMock(return_value={"risk_level": "safe"})
        gate = self.adapter.RiskAssessmentGate(transport=transport)
        first = gate.evaluate(self._request())
        second = gate.evaluate(self._request())
        self.assertEqual(first, second)
        transport.assert_called_once()

    def test_receiver_history_filter_excludes_blocked_messages(self):
        self.assertEqual(
            self.adapter.receiver_history_filter("room-1"),
            {"room_id": "room-1", "is_blocked": {"$ne": True}},
        )

    def test_document_keeps_risk_outside_planner_tools(self):
        document = DOCUMENT.read_text(encoding="utf-8")
        self.assertIn("before persistence", document)
        self.assertIn("not a Planner-selected tool", document)
        self.assertIn("restricted", document)
        self.assertIn("blocked", document)
        self.assertIn("fail-open", document.lower())


if __name__ == "__main__":
    unittest.main()
