import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock


SERVER_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = (
    SERVER_ROOT
    / "social"
    / "services"
    / "risk_policy_service.py"
)
ROUTER_PATH = (
    SERVER_ROOT
    / "social"
    / "routers"
    / "public_chat.py"
)
HISTORY_PATH = (
    SERVER_ROOT
    / "social"
    / "routers"
    / "chat_messages.py"
)
CHAT_SERVICE_PATH = (
    SERVER_ROOT
    / "social"
    / "services"
    / "chat_service.py"
)


def load_policy():
    spec = importlib.util.spec_from_file_location("pair_chat_risk_policy", POLICY_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PairChatRiskWiringTests(unittest.TestCase):
    def setUp(self):
        self.policy = load_policy()

    def _evaluate(self, gate, key="attempt-1"):
        return gate.evaluate(
            conversation_id="owner_other",
            sender_id="owner",
            receiver_id="other",
            content="hello",
            idempotency_key=key,
        )

    def test_restricted_is_deliverable_and_projection_is_bounded(self):
        gate = self.policy.PairMessageRiskGate(
            transport=MagicMock(return_value={
                "risk_level": "restricted",
                "diagnosis": {"private": True},
            })
        )
        decision = self._evaluate(gate)
        self.assertTrue(decision.may_persist)
        self.assertEqual(decision.public_projection(), {
            "level": "restricted",
            "ui_priority": "risk",
            "delivery": "delivered",
        })

    def test_only_blocked_prevents_persistence(self):
        blocked = self.policy.PairMessageRiskGate(
            transport=MagicMock(return_value={"risk_level": "blocked"})
        )
        unavailable = self.policy.PairMessageRiskGate(
            transport=MagicMock(side_effect=TimeoutError("private"))
        )
        self.assertFalse(self._evaluate(blocked).may_persist)
        result = self._evaluate(unavailable)
        self.assertTrue(result.may_persist)
        self.assertEqual(result.public_projection(), {
            "level": "unavailable",
            "ui_priority": "coach",
            "delivery": "delivered",
        })

    def test_invalid_risk_response_fails_open(self):
        gate = self.policy.PairMessageRiskGate(
            transport=MagicMock(return_value={"risk_level": "unexpected"})
        )
        result = self._evaluate(gate)
        self.assertTrue(result.may_persist)
        self.assertEqual(result.level, "unavailable")

    def test_duplicate_attempt_calls_risk_transport_once(self):
        transport = MagicMock(return_value={"risk_level": "safe"})
        gate = self.policy.PairMessageRiskGate(transport=transport)
        self._evaluate(gate)
        self._evaluate(gate)
        transport.assert_called_once()

    def test_router_orders_risk_before_pair_persistence_and_history_filters(self):
        router = ROUTER_PATH.read_text(encoding="utf-8")
        pair_branch = router[router.index("match_doc = find_accepted_match"):]
        self.assertLess(
            pair_branch.index("pair_message_risk_gate.evaluate"),
            pair_branch.index("save_pair_owner_message_once"),
        )
        history = HISTORY_PATH.read_text(encoding="utf-8")
        self.assertIn('"is_blocked": {"$ne": True}', history)

    def test_pair_owner_storage_uses_deterministic_insert_once_id(self):
        collection = MagicMock()
        collection.update_one.return_value = types.SimpleNamespace(upserted_id="created")
        database_module = types.ModuleType("database")
        database_module.messages_coll = collection
        previous = sys.modules.get("database")
        sys.modules["database"] = database_module
        try:
            spec = importlib.util.spec_from_file_location(
                "pair_chat_service_for_contract", CHAT_SERVICE_PATH,
            )
            module = importlib.util.module_from_spec(spec)
            assert spec.loader is not None
            spec.loader.exec_module(module)
        finally:
            if previous is None:
                sys.modules.pop("database", None)
            else:
                sys.modules["database"] = previous

        first = module.save_pair_owner_message_once(
            "owner_other", "owner", "hello",
            client_message_id="attempt-1",
            risk_projection={"level": "safe", "delivery": "delivered"},
        )
        second = module.save_pair_owner_message_once(
            "owner_other", "owner", "hello",
            client_message_id="attempt-1",
            risk_projection={"level": "safe", "delivery": "delivered"},
        )

        self.assertEqual(first["message_id"], second["message_id"])
        query, update = collection.update_one.call_args.args
        self.assertEqual(query["_id"], first["message_id"])
        self.assertIn("$setOnInsert", update)


if __name__ == "__main__":
    unittest.main()
