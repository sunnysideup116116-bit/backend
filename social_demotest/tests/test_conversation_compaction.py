import os
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException, Request

from routers.system import debug_conversation_context_metrics, debug_queue_conversation_shadow
from services import conversation_compaction_service as service
from services.conversation_compaction_contracts import (
    MAX_SUMMARY_CHARS,
    SUMMARY_FIELDS,
    SUMMARY_ITEM_LIMITS,
    ConversationEvaluationV1,
    ConversationSummaryV1,
    summary_from_payload,
)


class _FakeCompactions:
    def __init__(self, record):
        self.record = record

    def find_one(self, query):
        return self.record


class _FakeCursor:
    def __init__(self, items):
        self.items = list(items)

    def sort(self, *_args):
        return self

    def limit(self, value):
        return self.items[:value]


class _FakeMessages:
    def __init__(self, items):
        self.items = items

    def find(self, *_args, **_kwargs):
        return _FakeCursor(self.items)


class ConversationCompactionTests(unittest.TestCase):
    def test_inspector_is_loopback_and_flag_gated(self):
        request = Request({
            "type": "http", "method": "GET", "scheme": "http",
            "path": "/api/debug/conversation-context/metrics",
            "raw_path": b"/api/debug/conversation-context/metrics",
            "query_string": b"", "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 80),
        })
        with patch.dict(os.environ, {"AYUE_LOCAL_DEBUG_TRACE": "off"}):
            with self.assertRaises(HTTPException) as raised:
                debug_conversation_context_metrics(request)
        self.assertEqual(raised.exception.status_code, 404)

    def test_frontend_reports_manual_shadow_status(self):
        source = (Path(__file__).resolve().parents[1] / "frontend.html").read_text(encoding="utf-8")
        self.assertIn("conversationShadowStatusMessage", source)
        self.assertIn("Shadow 壓縮未採用", source)
        self.assertIn("issue_codes", source)

    def test_loopback_manual_endpoint_requests_cooldown_bypass(self):
        request = Request({
            "type": "http", "method": "POST", "scheme": "http",
            "path": "/api/debug/conversation-context/users/owner/shadow",
            "raw_path": b"/api/debug/conversation-context/users/owner/shadow",
            "query_string": b"", "headers": [(b"host", b"127.0.0.1")],
            "client": ("127.0.0.1", 12345), "server": ("127.0.0.1", 80),
        })
        with patch.dict(os.environ, {"AYUE_LOCAL_DEBUG_TRACE": "on"}), \
                patch("routers.system.queue_conversation_compaction_shadow", return_value={"status": "queued"}) as queue:
            result = debug_queue_conversation_shadow("owner", request, BackgroundTasks())
        self.assertEqual(result["status"], "queued")
        queue.assert_called_once_with(unittest.mock.ANY, "owner", manual_debug=True)

    def test_summary_is_bounded_and_private_projection_is_explicit(self):
        payload = {name: [f"item-{index}" for index in range(20)] for name in SUMMARY_FIELDS}
        payload["active_topics"].append("item-0")
        summary = summary_from_payload(payload)

        for name in SUMMARY_FIELDS:
            values = getattr(summary, name)
            self.assertLessEqual(len(values), SUMMARY_ITEM_LIMITS[name])
            self.assertEqual(len(values), len(set(values)))
        self.assertLessEqual(summary.char_count(), MAX_SUMMARY_CHARS)
        private = summary.as_private_projection()
        self.assertEqual(set(private), {"active_topics", "owner_goals", "known_continuity", "unresolved_questions"})
        self.assertNotIn("ayue_commitments", private)
        self.assertNotIn("recent_decisions", private)

    def test_continuity_requires_both_explicit_runtime_gates(self):
        with patch.dict(os.environ, {"AYUE_PUBLIC_CONVERSATION_CONTINUITY": "off", "AYUE_PRIVATE_PUBLIC_CONTINUITY": "on"}):
            self.assertIsNone(service.load_private_continuity("owner"))

    def test_private_continuity_projects_only_public_safe_fields(self):
        record = {
            "mode": "shadow",
            "revision": 3,
            "covered_message_count": 9,
            "evaluation": {"status": "pass"},
            "summary": {
                "active_topics": ["攝影"],
                "owner_goals": ["安排練習"],
                "known_continuity": ["上次提過器材"],
                "unresolved_questions": ["要不要改期"],
                "ayue_commitments": ["不應流向 private"],
                "recent_decisions": ["不應流向 private"],
            },
        }
        with patch.dict(os.environ, {"AYUE_CONVERSATION_COMPACTION_MODE": "shadow", "AYUE_PUBLIC_CONVERSATION_CONTINUITY": "on", "AYUE_PRIVATE_PUBLIC_CONTINUITY": "on"}), \
                patch.object(service, "COMPACTIONS", _FakeCompactions(record)):
            projected = service.load_private_continuity("owner")
        self.assertEqual(projected["revision"], 3)
        self.assertEqual(set(projected["summary"]), {"active_topics", "owner_goals", "known_continuity", "unresolved_questions"})
        self.assertNotIn("ayue_commitments", projected["summary"])
        self.assertNotIn("recent_decisions", projected["summary"])

    def test_evaluator_rejects_unsupported_commitment_and_identifiers(self):
        items = [{"sender_id": "owner", "content": "我想學攝影", "timestamp": 1.0}]
        summary = ConversationSummaryV1(
            active_topics=["攝影"],
            ayue_commitments=["match_id=secret"],
        )
        evaluation = service._evaluate(items, summary)
        self.assertEqual(evaluation.status, "fail")
        self.assertIn("canonical_state_leak:ayue_commitments", evaluation.issues)

    def test_generator_receives_previous_validated_summary(self):
        previous = ConversationSummaryV1(active_topics=["ongoing-topic"])
        response = SimpleNamespace(content=json.dumps({
            "active_topics": ["ongoing-topic", "new-topic"],
            "owner_goals": [], "known_continuity": [], "unresolved_questions": [],
            "ayue_commitments": [], "recent_decisions": [],
        }))
        items = [{"sender_id": "owner", "content": "new-topic", "timestamp": 1.0}]
        with patch.object(service, "generate_chat_completion", return_value=response) as generate:
            summary, metadata = service._generate_summary(items, previous)
        prompt = json.loads(generate.call_args.args[0])
        self.assertEqual(prompt["previous_validated_summary"]["active_topics"], ["ongoing-topic"])
        self.assertEqual(summary.active_topics, ["ongoing-topic", "new-topic"])
        self.assertEqual(metadata["attempts"], 1)
        self.assertEqual(generate.call_args.kwargs["model"], service.OLLAMA_FAST_CHAT_MODEL)

    def test_generator_exposes_bounded_provider_timeout_code(self):
        items = [{"sender_id": "owner", "content": "hello", "timestamp": 1.0}]
        with patch.object(service, "generate_chat_completion", side_effect=TimeoutError("provider timeout")):
            summary, metadata = service._generate_summary(items, ConversationSummaryV1())
        self.assertEqual(summary.char_count(), 0)
        self.assertEqual(metadata["error_code"], "provider_timeout")
        self.assertEqual(metadata["attempt_errors"], ["provider_timeout"])

    def test_generator_exposes_invalid_json_code_without_raw_provider_error(self):
        items = [{"sender_id": "owner", "content": "hello", "timestamp": 1.0}]
        with patch.object(service, "generate_chat_completion", return_value=SimpleNamespace(content="not-json")):
            summary, metadata = service._generate_summary(items, ConversationSummaryV1())
        self.assertEqual(summary.char_count(), 0)
        self.assertEqual(metadata["error_code"], "invalid_json")
        self.assertNotIn("not-json", json.dumps(metadata))

    def test_failed_candidate_does_not_overwrite_last_good_record(self):
        batch = [
            {"_id": "m1", "sender_id": "owner", "content": "hello", "timestamp": 1.0},
            {"_id": "m2", "sender_id": "ai_assistant", "content": "hi", "timestamp": 2.0},
        ]
        current = {
            "revision": 4, "covered_message_count": 40, "covered_through_timestamp": 0,
            "created_at": 1.0, "summary": ConversationSummaryV1(active_topics=["last-good"]).model_dump(mode="json"),
        }
        jobs = unittest.mock.MagicMock()
        jobs.update_one.return_value = SimpleNamespace(matched_count=1)
        compactions = unittest.mock.MagicMock()
        revisions = unittest.mock.MagicMock()
        runs = unittest.mock.MagicMock()
        invalid = ConversationSummaryV1(ayue_commitments=["not grounded"])
        with patch.object(service, "JOBS", jobs), patch.object(service, "COMPACTIONS", compactions), \
                patch.object(service, "REVISIONS", revisions), patch.object(service, "SHADOW_RUNS", runs), \
                patch.object(service, "_select_batch", return_value=(batch, current, "eligible")), \
                patch.object(service, "_generate_summary", return_value=(invalid, {"attempts": 1})):
            result = service.run_conversation_compaction_shadow("owner", "lease")
        self.assertFalse(result["accepted"])
        self.assertEqual(result["result_code"], "evaluation_failed")
        compactions.replace_one.assert_not_called()
        revisions.insert_one.assert_not_called()
        self.assertFalse(runs.insert_one.call_args.args[0]["accepted"])
        self.assertEqual(runs.insert_one.call_args.args[0]["issue_codes"], ["unsupported_content:ayue_commitments"])

    def test_accepted_candidate_advances_cumulative_coverage_and_snapshots(self):
        batch = [
            {"_id": "m1", "sender_id": "owner", "content": "new-topic", "timestamp": 3.0},
            {"_id": "m2", "sender_id": "ai_assistant", "content": "noted", "timestamp": 4.0},
        ]
        current = {
            "revision": 4, "covered_message_count": 40, "covered_through_timestamp": 2.0,
            "created_at": 1.0, "summary": ConversationSummaryV1(active_topics=["last-good"]).model_dump(mode="json"),
        }
        jobs = unittest.mock.MagicMock()
        jobs.update_one.return_value = SimpleNamespace(matched_count=1)
        compactions = unittest.mock.MagicMock()
        compactions.replace_one.return_value = SimpleNamespace(matched_count=1)
        revisions = unittest.mock.MagicMock()
        runs = unittest.mock.MagicMock()
        rolling = ConversationSummaryV1(active_topics=["last-good", "new-topic"])
        with patch.object(service, "JOBS", jobs), patch.object(service, "COMPACTIONS", compactions), \
                patch.object(service, "REVISIONS", revisions), patch.object(service, "SHADOW_RUNS", runs), \
                patch.object(service, "_select_batch", return_value=(batch, current, "eligible")), \
                patch.object(service, "_generate_summary", return_value=(rolling, {"attempts": 1})) as generate:
            result = service.run_conversation_compaction_shadow("owner", "lease")
        self.assertTrue(result["accepted"])
        written = compactions.replace_one.call_args.args[1]
        self.assertEqual(written["covered_message_count"], 42)
        self.assertEqual(written["revision"], 5)
        self.assertEqual(written["summary"]["active_topics"], ["last-good", "new-topic"])
        self.assertEqual(generate.call_args.args[1].active_topics, ["last-good"])
        revisions.insert_one.assert_called_once()

    def test_active_owner_job_is_coalesced(self):
        jobs = unittest.mock.MagicMock()
        jobs.find_one.return_value = {
            "status": "running", "lease_expires_at": 200.0, "next_retry_at": 0.0,
        }
        with patch.object(service, "JOBS", jobs), patch.object(service.time, "time", return_value=100.0):
            status, token = service._claim_job("owner")
        self.assertEqual(status, "coalesced")
        self.assertIsNone(token)

    def test_manual_debug_claim_skips_cooldown_but_not_active_lease(self):
        jobs = unittest.mock.MagicMock()
        jobs.find_one.return_value = {
            "status": "failed", "lease_expires_at": 0.0, "next_retry_at": 200.0,
        }
        jobs.update_one.return_value = SimpleNamespace(matched_count=1)
        with patch.object(service, "JOBS", jobs), patch.object(service.time, "time", return_value=100.0):
            status, token = service._claim_job("owner", ignore_cooldown=True)
        self.assertEqual(status, "queued")
        self.assertRegex(token or "", r"^[0-9a-f]{32}$")

        jobs.find_one.return_value = {
            "status": "running", "lease_expires_at": 200.0, "next_retry_at": 0.0,
        }
        with patch.object(service, "JOBS", jobs), patch.object(service.time, "time", return_value=100.0):
            status, token = service._claim_job("owner", ignore_cooldown=True)
        self.assertEqual(status, "coalesced")
        self.assertIsNone(token)

    def test_high_watermark_compacts_down_toward_hot_window(self):
        def messages(count):
            return [
                {"_id": str(index), "sender_id": "owner", "content": f"m{index}", "timestamp": float(index)}
                for index in range(count)
            ]
        with patch.object(service, "_record", return_value=None), \
                patch.object(service, "messages_coll", _FakeMessages(messages(30))):
            batch, _current, status = service._select_batch("owner")
        self.assertEqual(status, "below_threshold")
        self.assertEqual(batch, [])

        with patch.object(service, "_record", return_value=None), \
                patch.object(service, "messages_coll", _FakeMessages(messages(31))):
            batch, _current, status = service._select_batch("owner")
        self.assertEqual(status, "eligible")
        self.assertEqual(len(batch), 19)
        self.assertEqual(31 - len(batch), service.COMPACTION_KEEP_RECENT_MESSAGES)

    def test_manual_debug_uses_hot_window_as_its_only_message_threshold(self):
        def messages(count):
            return [
                {"_id": str(index), "sender_id": "owner", "content": f"m{index}", "timestamp": float(index)}
                for index in range(count)
            ]
        with patch.object(service, "_record", return_value=None), \
                patch.object(service, "messages_coll", _FakeMessages(messages(16))):
            batch, _current, status = service._select_batch("owner", manual_debug=True)
        self.assertEqual(status, "eligible")
        self.assertEqual(len(batch), 4)

        with patch.object(service, "_record", return_value=None), \
                patch.object(service, "messages_coll", _FakeMessages(messages(12))):
            batch, _current, status = service._select_batch("owner", manual_debug=True)
        self.assertEqual(status, "below_threshold")
        self.assertEqual(batch, [])


if __name__ == "__main__":
    unittest.main()
