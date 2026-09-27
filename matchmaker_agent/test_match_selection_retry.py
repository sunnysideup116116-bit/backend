"""Synthetic provider only: truncated output never becomes a silent no-match."""
import asyncio
import json
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import test_match_timeouts as fixtures

matchmaker = fixtures.matchmaker


class ManualClock:
    """Test-only loop clock; advancing time never sleeps or extends a deadline."""
    def __init__(self, now):
        self.now = now

    def time(self):
        return self.now

    def advance_to(self, when):
        if when < self.now:
            raise AssertionError("test clock cannot move backwards")
        self.now = when


@contextmanager
def observed_deadlines():
    """Keep real asyncio.Timeout/cancellation, recording each initial expiry."""
    create = asyncio.timeout
    records = []

    def observe(delay):
        scope = create(delay)
        records.append((scope, scope.when(), delay))
        return scope

    with patch.object(matchmaker.asyncio, "timeout", observe):
        yield records


class ControlledClient:
    """No SDK initialization/I/O in the deterministic budget tests."""
    def __init__(self, handler):
        self.handler = handler
        self.closed = False
        self.options = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=handler))

    def factory(self, **options):
        self.options.append(options)
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        self.closed = True


def controlled_completion(finish):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content='{"outcome":"no_suitable_candidate","matches":[]}'))])


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

    async def drain_until(self, predicate, task=None):
        # Only deterministic Event/Timeout callbacks use this helper. No SDK
        # cold start, thread startup or real-time sleep must fit these turns.
        for _ in range(30):
            if predicate():
                return
            if task is not None and task.done():
                task.result()  # Surface the contract assertion, not a later wait failure.
                self.fail("match completed before the controlled transport stage")
            await asyncio.sleep(0)
        self.fail("controlled callbacks did not reach the expected state")

    def pending_since(self, before):
        return {task for task in asyncio.all_tasks() - before if not task.done()}

    async def cleanup_tasks(self, before):
        # Failure cleanup is deliberately AFTER assertions so an orphan fails
        # the test instead of being silently hidden by teardown cancellation.
        pending = self.pending_since(before)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), 10)

    async def budget_case(self, stop):
        loop = asyncio.get_running_loop()
        clock = ManualClock(loop.time())
        budget = matchmaker.MATCH_LLM_TIMEOUT_SECONDS  # production value unchanged
        before = asyncio.all_tasks()
        first_started, second_started = asyncio.Event(), asyncio.Event()
        release_first, release_second = asyncio.Event(), asyncio.Event()
        attempts, cancelled, active = [], [], set()

        async def create(**_request):
            attempt = len(attempts) + 1
            self.assertEqual(len(deadlines), 1, "retry created another full deadline")
            scope, initial_expiry, _ = deadlines[0]
            self.assertEqual(scope.when(), initial_expiry, "shared deadline was reset")
            attempts.append((clock.time(), scope.when() - clock.time()))
            active.add(attempt)
            (first_started if attempt == 1 else second_started).set()
            try:
                await (release_first if attempt == 1 else release_second).wait()
                return controlled_completion("length" if attempt == 1 else "stop")
            except asyncio.CancelledError:
                cancelled.append(attempt)
                raise
            finally:
                active.remove(attempt)

        client = ControlledClient(create)
        try:
            with patch.object(loop, "time", clock.time), observed_deadlines() as deadlines, \
                 patch.object(matchmaker, "AsyncOpenAI", client.factory):
                task = asyncio.create_task(self.subject().match_async({}, []))
                await self.drain_until(first_started.is_set, task)
                scope, expiry, configured_budget = deadlines[0]
                self.assertEqual(configured_budget, budget)
                self.assertAlmostEqual(attempts[0][1], budget)

                if stop != "first_timeout":
                    clock.advance_to(expiry - budget * 0.6)
                    release_first.set()
                    await self.drain_until(second_started.is_set, task)
                    self.assertEqual(len(attempts), 2)
                    self.assertAlmostEqual(attempts[1][1], budget * 0.6)
                    self.assertLess(attempts[1][1], attempts[0][1])
                    self.assertEqual(scope.when(), expiry)

                if stop == "success":
                    release_second.set()
                    await self.drain_until(task.done)
                    self.assertEqual(json.loads(task.result())["outcome"], "no_suitable_candidate")
                    self.assertFalse(scope.expired())
                    self.assertEqual(cancelled, [])
                else:
                    # Expire at the ORIGINAL absolute deadline, not a fresh
                    # budget measured from retry start. Real Timeout cancels.
                    clock.advance_to(expiry)
                    await self.drain_until(task.done)
                    with self.assertRaises(matchmaker.MatchEvaluationError) as exc:
                        task.result()
                    self.assertEqual(exc.exception.code, "matchmaker_timeout")
                    self.assertTrue(scope.expired())
                    expected_attempt = 1 if stop == "first_timeout" else 2
                    self.assertEqual(len(attempts), expected_attempt)
                    self.assertEqual(cancelled, [expected_attempt])

                self.assertEqual(len(deadlines), 1)
                self.assertEqual(client.options[0]["max_retries"], 0)
                self.assertTrue(client.closed)
                self.assertFalse(active)
                self.assertFalse(self.pending_since(before), "orphan task survived completion")
        finally:
            await self.cleanup_tasks(before)

    async def test_retry_uses_remaining_budget_from_shared_absolute_deadline(self):
        await self.budget_case("success")

    async def test_retry_times_out_at_original_deadline_without_budget_reset(self):
        await self.budget_case("retry_timeout")

    async def test_first_attempt_budget_exhaustion_does_not_start_retry(self):
        await self.budget_case("first_timeout")

    async def test_active_sdk_transport_cancellation_closes_client_without_orphans(self):
        # Real SDK + httpx.MockTransport integration: an Event proves request2
        # is active before we deliver expiry/cancellation. No 80ms/500ms race.
        for stop in ("deadline", "external_cancel"):
            with self.subTest(stop=stop):
                before = asyncio.all_tasks()
                attempts, clients, options, cancelled = [], [], [], []
                active = set()
                second_started, hold = asyncio.Event(), asyncio.Event()

                async def handler(request):
                    attempt = len(attempts) + 1
                    attempts.append(request)
                    active.add(attempt)
                    try:
                        if attempt == 1:
                            return httpx.Response(200, json=fixtures.completion("{}", "length"))
                        second_started.set()
                        await hold.wait()
                        raise AssertionError("blocked transport unexpectedly resumed")
                    except asyncio.CancelledError:
                        cancelled.append(attempt)
                        raise
                    finally:
                        active.remove(attempt)

                try:
                    with observed_deadlines() as deadlines, patch.object(matchmaker, "AsyncOpenAI",
                            self.transport_factory(handler, clients, options)):
                        task = asyncio.create_task(self.subject().match_async({}, []))
                        # Infrastructure watchdog only; no requirement that
                        # SDK initialization leaves a tiny wall-clock budget.
                        await asyncio.wait_for(second_started.wait(), 10)
                        self.assertEqual(len(deadlines), 1)
                        scope, expiry, budget = deadlines[0]
                        self.assertEqual(budget, matchmaker.MATCH_LLM_TIMEOUT_SECONDS)
                        self.assertEqual(scope.when(), expiry)
                        self.assertEqual(len(attempts), 2)
                        self.assertEqual(active, {2})
                        if stop == "deadline":
                            # Advance only this test loop's clock. Do not
                            # reschedule/replace/extend the production scope.
                            loop = asyncio.get_running_loop()
                            with patch.object(loop, "time", lambda: expiry):
                                await self.drain_until(task.done)
                            with self.assertRaises(matchmaker.MatchEvaluationError) as exc:
                                task.result()
                            self.assertEqual(exc.exception.code, "matchmaker_timeout")
                            self.assertTrue(scope.expired())
                        else:
                            task.cancel()
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(task, 10)
                            self.assertFalse(scope.expired())
                        self.assertEqual(cancelled, [2])
                        self.assertEqual(options[0]["max_retries"], 0)
                        self.assertTrue(clients and all(c.is_closed for c in clients))
                        self.assertFalse(active)
                        self.assertFalse(self.pending_since(before), "orphan transport/task")
                finally:
                    await self.cleanup_tasks(before)

    def test_selection_prompt_does_not_require_duplicate_proposal_copy(self):
        subject = matchmaker.MatchmakerAgent()
        self.assertIn('"matched_user_id"', subject.system_prompt)
        self.assertNotIn('"recommendation_reason"', subject.system_prompt)
        self.assertNotIn('"score_breakdown"', subject.system_prompt)
        self.assertIn("不可為了湊結果硬選", subject.system_prompt)
