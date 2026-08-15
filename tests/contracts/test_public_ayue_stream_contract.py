import importlib.util
import json
import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = SERVER_ROOT / "integrations" / "ayue_v3_contracts.py"
STREAM_SCHEMA = SERVER_ROOT / "docs" / "api" / "ayue-v3-public-stream.schema.json"
FINAL_SCHEMA = SERVER_ROOT / "docs" / "api" / "ayue-v3-final-response.schema.json"
PUBLIC_ROUTER = SERVER_ROOT / "ayue_for_demo" / "social_demotest" / "routers" / "public_chat.py"


def load_contracts():
    spec = importlib.util.spec_from_file_location("ayue_v3_contracts", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class PublicAyueStreamContractTests(unittest.TestCase):
    def setUp(self):
        self.contracts = load_contracts()

    def test_decoder_handles_every_utf8_byte_boundary(self):
        events = [
            {"type": "run_started", "agent_run_id": "run-1"},
            {"type": "tool_started", "agent_run_id": "run-1", "text": "阿月確認中…"},
            {"type": "tool_finished", "agent_run_id": "run-1", "outcome": "ok", "duration_ms": 12},
            {"type": "final", "response": {"reply": "完成。", "future_field": {"safe": True}}},
        ]
        payload = "".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events).encode("utf-8")
        for split in range(len(payload) + 1):
            decoder = self.contracts.NdjsonEventDecoder()
            actual = decoder.feed(payload[:split])
            actual.extend(decoder.feed(payload[split:], final=True))
            self.assertEqual(actual, events, f"split={split}")

    def test_only_bounded_public_progress_fields_are_allowed(self):
        safe = {"type": "tool_started", "agent_run_id": "run-1", "text": "我確認一下…"}
        self.contracts.validate_public_event(safe)
        for forbidden in ("prompt", "arguments", "result", "revision", "step_id", "tool_name"):
            unsafe = dict(safe, **{forbidden: "private"})
            with self.assertRaises(ValueError, msg=forbidden):
                self.contracts.validate_public_event(unsafe)

    def test_sequence_has_exactly_one_terminal_event_and_terminal_is_last(self):
        valid = [
            {"type": "run_started", "agent_run_id": "run-1"},
            {"type": "final", "response": {"reply": "好。"}},
        ]
        self.contracts.validate_public_sequence(valid)
        with self.assertRaises(ValueError):
            self.contracts.validate_public_sequence(valid + [{"type": "error", "agent_run_id": "run-1", "reply": "錯誤"}])
        with self.assertRaises(ValueError):
            self.contracts.validate_public_sequence(valid + [{"type": "tool_started", "agent_run_id": "run-1", "text": "late"}])

    def test_schemas_are_additive_and_keep_required_terminal_fields(self):
        stream_schema = json.loads(STREAM_SCHEMA.read_text(encoding="utf-8"))
        final_schema = json.loads(FINAL_SCHEMA.read_text(encoding="utf-8"))
        event_types = {
            variant["properties"]["type"]["const"]
            for variant in stream_schema["oneOf"]
        }
        self.assertEqual(event_types, {"run_started", "tool_started", "tool_finished", "final", "error"})
        self.assertEqual(final_schema["required"], ["reply"])
        self.assertTrue(final_schema["additionalProperties"])
        for field in (
            "messages", "assessment_state", "context_confirmation_needed",
            "match_readiness_state", "calendar_state_changed", "sources",
            "place_cards", "presentation_blocks", "agent_run_id", "agent_version",
        ):
            self.assertIn(field, final_schema["properties"])

    def test_public_stream_uses_one_owner_message_path(self):
        source = PUBLIC_ROUTER.read_text(encoding="utf-8")
        start = source.index('def direct_chat_stream(')
        end = source.index('@router.post("/direct_chat")', start)
        stream_source = source[start:end]
        self.assertIn('if req.contact_id == "ai_assistant":', stream_source)
        self.assertIn('_run_public_stream_turn(', stream_source)
        self.assertIn('else:', stream_source)
        self.assertIn('response = direct_chat(req, worker_background_tasks)', stream_source)
        self.assertNotIn('_complete_public_turn(', stream_source)


if __name__ == "__main__":
    unittest.main()
