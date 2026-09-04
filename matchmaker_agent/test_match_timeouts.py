"""No real provider or database calls: exercise cancellation and ASGI liveness."""
import asyncio
import json
import os
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

os.environ.setdefault("LLM_API_KEY", "test")
os.environ.setdefault("LLM_BASE_URL", "http://provider.invalid/v1")
os.environ.setdefault("LLM_MODEL_ID", "test-model")
with patch("dotenv.load_dotenv", return_value=False):
    import agent_api
    import matchmaker


def completion(content, finish="stop"):
    return {"id": "test", "object": "chat.completion", "created": 1, "model": "test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": finish}]}


class MatchProviderDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def subject(self):
        result = matchmaker.MatchmakerAgent.__new__(matchmaker.MatchmakerAgent)
        result.model = "test-model"
        result.client = SimpleNamespace(api_key="test", base_url="http://provider.invalid/v1")
        result.system_prompt = "[GRAPH_MEMORY_PLACEHOLDER] [GLOBAL_HEURISTICS_PLACEHOLDER] [DEEP_PROFILE_PLACEHOLDER]"
        return result

    def transport_factory(self, handler, clients, options):
        original = matchmaker.AsyncOpenAI
        def factory(**kwargs):
            options.append(kwargs)
            client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            clients.append(client)
            return original(**kwargs, http_client=client)
        return factory

    async def test_wall_deadline_cancels_provider_and_closes_transport_without_retry(self):
        cancelled = asyncio.Event()
        attempts = []
        async def handler(request):
            attempts.append(request)
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        clients, options = [], []
        factory = self.transport_factory(handler, clients, options)
        with patch.object(matchmaker, "AsyncOpenAI", factory), patch.object(matchmaker, "MATCH_LLM_TIMEOUT_SECONDS", 0.04):
            started = time.monotonic()
            with self.assertRaises(matchmaker.MatchEvaluationError) as error:
                await self.subject().match_async({}, [])
        self.assertEqual(error.exception.code, "matchmaker_timeout")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(options[0]["max_retries"], 0)
        self.assertTrue(clients[0].is_closed)

    async def test_empty_and_truncated_responses_are_failures_not_no_candidates(self):
        for content, finish, code in [("", "stop", "matchmaker_empty_response"), ("{}", "length", "matchmaker_output_truncated")]:
            with self.subTest(code=code):
                async def handler(_request):
                    return httpx.Response(200, json=completion(content, finish))
                factory = self.transport_factory(handler, [], [])
                with patch.object(matchmaker, "AsyncOpenAI", factory):
                    with self.assertRaises(matchmaker.MatchEvaluationError) as error:
                        await self.subject().match_async({}, [])
                self.assertEqual(error.exception.code, code)

    async def test_provider_500_is_not_retried_or_echoed(self):
        attempts = []
        async def handler(request):
            attempts.append(request)
            return httpx.Response(500, json={"error": {"message": "private provider details"}})
        factory = self.transport_factory(handler, [], [])
        with patch.object(matchmaker, "AsyncOpenAI", factory):
            with self.assertRaises(matchmaker.MatchEvaluationError) as error:
                await self.subject().match_async({}, [])
        self.assertEqual(error.exception.code, "matchmaker_provider_error")
        self.assertNotIn("private", str(error.exception))
        self.assertEqual(len(attempts), 1)

    async def test_valid_response_uses_same_model_and_bounded_output(self):
        payloads = []
        async def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200, json=completion('{"outcome":"no_suitable_candidate","matches":[]}'))
        factory = self.transport_factory(handler, [], [])
        with patch.object(matchmaker, "AsyncOpenAI", factory):
            response = await self.subject().match_async({}, [])
        self.assertEqual(json.loads(response)["outcome"], "no_suitable_candidate")
        self.assertEqual(payloads[0]["model"], "test-model")
        self.assertEqual(payloads[0]["max_tokens"], matchmaker.MATCH_MAX_OUTPUT_TOKENS)


class MatchEndpointDeadlineTests(unittest.IsolatedAsyncioTestCase):
    def request(self):
        return {"target_user": {"user_id": "isolated"}, "candidates": [{"user_id": "candidate"}]}

    async def test_health_responds_while_graph_read_is_blocked(self):
        started, release = threading.Event(), threading.Event()
        def blocked(*_args, **_kwargs):
            started.set()
            release.wait(2)
            return ""
        model = AsyncMock(return_value='{"outcome":"no_suitable_candidate","matches":[]}')
        try:
            with patch.object(agent_api, "get_user_graph_memory", blocked), patch.object(agent_api, "get_global_rules", return_value=""), \
                 patch.object(agent_api, "MATCH_GRAPH_TIMEOUT_SECONDS", 0.15), patch.object(agent_api.agent, "match_async", model):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_api.app), base_url="http://test") as client:
                    request = asyncio.create_task(client.post("/api/match", json=self.request()))
                    for _ in range(100):
                        if started.is_set():
                            break
                        await asyncio.sleep(0.001)
                    self.assertTrue(started.is_set())
                    health = await asyncio.wait_for(client.get("/health"), timeout=0.1)
                    self.assertEqual(health.status_code, 200)
                    response = await asyncio.wait_for(request, timeout=0.5)
            self.assertEqual(response.status_code, 504)
            self.assertEqual(response.json()["detail"]["code"], "matchmaker_graph_timeout")
            model.assert_not_called()
        finally:
            release.set()

    async def test_outer_deadline_cancels_model_but_does_not_block_health(self):
        started, cancelled = asyncio.Event(), asyncio.Event()
        async def slow(*_args):
            started.set()
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()
        with patch.object(agent_api, "get_user_graph_memory", return_value=""), patch.object(agent_api, "get_global_rules", return_value=""), \
             patch.object(agent_api, "MATCH_REQUEST_TIMEOUT_SECONDS", 0.1), patch.object(agent_api.agent, "match_async", slow):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=agent_api.app), base_url="http://test") as client:
                request = asyncio.create_task(client.post("/api/match", json=self.request()))
                await asyncio.wait_for(started.wait(), timeout=0.5)
                self.assertEqual((await asyncio.wait_for(client.get("/health"), timeout=0.1)).status_code, 200)
                response = await asyncio.wait_for(request, timeout=0.5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(response.status_code, 504)
        self.assertEqual(response.json()["detail"]["code"], "matchmaker_timeout")

    async def test_graph_failure_is_not_used_as_empty_matching_evidence(self):
        failure = matchmaker.MatchEvaluationError("matchmaker_graph_unavailable")
        model = AsyncMock()
        with patch.object(agent_api, "get_user_graph_memory", side_effect=failure), patch.object(agent_api, "get_global_rules", return_value=""), \
             patch.object(agent_api.agent, "match_async", model):
            with self.assertRaises(agent_api.HTTPException) as error:
                await agent_api.match_endpoint(agent_api.MatchRequest(**self.request()))
        self.assertEqual(error.exception.detail["code"], "matchmaker_graph_unavailable")
        model.assert_not_called()

    async def test_explicit_no_candidate_and_selected_results_still_work(self):
        responses = [
            {"outcome": "no_suitable_candidate", "matches": []},
            {"outcome": "selected", "matches": [{"matched_user_id": "candidate"}]},
        ]
        for result in responses:
            with patch.object(agent_api, "get_user_graph_memory", return_value=""), patch.object(agent_api, "get_global_rules", return_value=""), \
                 patch.object(agent_api.agent, "match_async", AsyncMock(return_value=json.dumps(result))):
                self.assertEqual(await agent_api.match_endpoint(agent_api.MatchRequest(**self.request())), result)

    def test_deadline_order_is_below_social_and_job_lease(self):
        self.assertLess(agent_api.MATCH_GRAPH_TIMEOUT_SECONDS + matchmaker.MATCH_LLM_TIMEOUT_SECONDS, agent_api.MATCH_REQUEST_TIMEOUT_SECONDS)
        self.assertLess(agent_api.MATCH_REQUEST_TIMEOUT_SECONDS, 120)
        self.assertLess(120, 180)
