"""Synthetic provider only: truncated output never becomes a silent no-match."""
import asyncio
import json
import unittest
from unittest.mock import patch

import httpx
import test_match_timeouts as fixtures

matchmaker = fixtures.matchmaker


class MatchSelectionRetryTests(unittest.IsolatedAsyncioTestCase):
    subject = fixtures.MatchProviderDeadlineTests.subject
    transport_factory = fixtures.MatchProviderDeadlineTests.transport_factory

    async def test_truncation_retries_once_with_compact_prompt_and_larger_budget(self):
        payloads, clients = [], []
        async def handler(request):
            payloads.append(json.loads(request.content))
            content = "private-partial-text" if len(payloads) == 1 else '{"outcome":"selected","matches":[{"matched_user_id":"candidate"}]}'
            return httpx.Response(200, json=fixtures.completion(content, "length" if len(payloads) == 1 else "stop"))
        with patch.object(matchmaker, "AsyncOpenAI", self.transport_factory(handler, clients, [])):
            result = await self.subject().match_async({"user_id": "owner"}, [{"user_id": "candidate"}])
        self.assertEqual(json.loads(result)["matches"][0]["matched_user_id"], "candidate")
        self.assertEqual([p["max_tokens"] for p in payloads], [4096, 8192])
        self.assertNotIn("private-partial-text", json.dumps(payloads[1]))
        self.assertIn("最短決策 JSON", payloads[1]["messages"][0]["content"])
        self.assertTrue(all(c.is_closed for c in clients))

    async def test_two_truncations_end_in_failure_without_third_request(self):
        attempts = []
        async def handler(request):
            attempts.append(request)
            return httpx.Response(200, json=fixtures.completion("{}", "length"))
        with patch.object(matchmaker, "AsyncOpenAI", self.transport_factory(handler, [], [])):
            with self.assertRaises(matchmaker.MatchEvaluationError) as exc:
                await self.subject().match_async({}, [])
        self.assertEqual(exc.exception.code, "matchmaker_output_truncated")
        self.assertEqual(len(attempts), 2)

    async def test_retry_shares_deadline_and_cancels_transport(self):
        attempts, clients = [], []
        cancelled = asyncio.Event()
        async def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                await asyncio.sleep(0.08)
                return httpx.Response(200, json=fixtures.completion("{}", "length"))
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        with patch.object(matchmaker, "AsyncOpenAI", self.transport_factory(handler, clients, [])), \
             patch.object(matchmaker, "MATCH_LLM_TIMEOUT_SECONDS", 0.5):
            with self.assertRaises(matchmaker.MatchEvaluationError) as exc:
                await self.subject().match_async({}, [])
        self.assertEqual(exc.exception.code, "matchmaker_timeout")
        self.assertEqual(len(attempts), 2)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(all(c.is_closed for c in clients))

    def test_selection_prompt_does_not_require_duplicate_proposal_copy(self):
        subject = matchmaker.MatchmakerAgent()
        self.assertIn('"matched_user_id"', subject.system_prompt)
        self.assertNotIn('"recommendation_reason"', subject.system_prompt)
        self.assertNotIn('"score_breakdown"', subject.system_prompt)
        self.assertIn("不可為了湊結果硬選", subject.system_prompt)
