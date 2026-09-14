"""Runtime regressions for blocking SDK bridges and ordered risk decisions."""

import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.async_io import IO_WORKERS, keyed_lock, run_blocking
from app.core.risk_state import RiskStateMachine
from app.models.schemas import RiskState
from app.services.chat_log_service import ChatLogService
from app.services.relationship_service import RelationshipService


def test_appwrite_reads_overlap_and_leave_event_loop_responsive():
    service = ChatLogService.__new__(ChatLogService)
    service.db_id = "test"
    threads = []

    def read(*args, **kwargs):
        threads.append(threading.get_ident())
        time.sleep(0.10)
        return SimpleNamespace(documents=[])

    service.db = SimpleNamespace(list_documents=read)

    async def run():
        ticks = 0
        done = False

        async def heartbeat():
            nonlocal ticks
            while not done:
                ticks += 1
                await asyncio.sleep(0.005)

        pulse = asyncio.create_task(heartbeat())
        start = time.perf_counter()
        results = await asyncio.gather(*(
            service.get_remaining_cooldown(f"conversation-{index}", "sender")
            for index in range(4)
        ))
        elapsed = time.perf_counter() - start
        done = True
        await pulse
        assert results == [0] * 4
        assert ticks >= 5
        assert elapsed < 0.30, f"four 100ms reads were serialized: {elapsed}"

    asyncio.run(run())
    assert all(thread != threading.get_ident() for thread in threads)


def test_offload_applies_backpressure_and_cancellation_waits_for_write():
    started = threading.Event()
    release = threading.Event()
    state = {"active": 0, "peak": 0, "finished": 0}
    mutex = threading.Lock()

    def write():
        with mutex:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            if state["active"] == IO_WORKERS:
                started.set()
        assert release.wait(3)
        with mutex:
            state["active"] -= 1
            state["finished"] += 1

    async def run():
        tasks = [asyncio.create_task(run_blocking(write)) for _ in range(IO_WORKERS + 3)]
        try:
            deadline = time.monotonic() + 2
            while not started.is_set() and time.monotonic() < deadline:
                await asyncio.sleep(0.005)
            assert started.is_set()
            tasks[0].cancel()
            await asyncio.sleep(0.02)
            assert not tasks[0].done(), "cancelled write released its slot too early"
            assert state["active"] == IO_WORKERS
        finally:
            release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert state["peak"] == IO_WORKERS
        assert state["finished"] == IO_WORKERS + 3

    asyncio.run(run())


def test_cancellation_keeps_conversation_locked_until_worker_finishes():
    started = threading.Event()
    release = threading.Event()
    events = []

    def write():
        started.set()
        assert release.wait(3)
        events.append("write-finished")

    async def run():
        async def first():
            async with keyed_lock("cancel-test", "same"):
                await run_blocking(write)

        async def second():
            async with keyed_lock("cancel-test", "same"):
                events.append("second-entered")

        pending = asyncio.create_task(first())
        try:
            while not started.is_set():
                await asyncio.sleep(0.005)
            pending.cancel()
            next_request = asyncio.create_task(second())
            await asyncio.sleep(0.02)
            assert events == []
        finally:
            release.set()
            await asyncio.gather(pending, next_request, return_exceptions=True)
        assert events == ["write-finished", "second-entered"]

    asyncio.run(run())


def test_mongo_cursor_is_consumed_in_worker():
    service = ChatLogService.__new__(ChatLogService)
    service.db_id = "test"
    service.db = MagicMock()
    service.db.list_documents.side_effect = RuntimeError("Appwrite unavailable")
    main_thread = threading.get_ident()

    class Cursor:
        def sort(self, *args):
            return self

        def limit(self, count):
            return self

        def __iter__(self):
            assert threading.get_ident() != main_thread
            time.sleep(0.02)
            return iter([{"risk_state": {"harassment": 0.3}}])

    service.mongo_state_coll = SimpleNamespace(find=lambda *args: Cursor())
    states = asyncio.run(service.get_recent_risk_state_history("conversation", "sender"))
    assert states[0].harassment == 0.3


def test_cumulative_updates_preserve_order_and_diagnostics_are_request_local(monkeypatch):
    machine = RiskStateMachine()
    stored = {}
    saved = []
    log = MagicMock()

    async def latest(conversation_id, user_id):
        state = stored.get((conversation_id, user_id), RiskState())
        await asyncio.sleep(0.01)
        return state, None

    async def save(conversation_id, user_id, msg_id, state, *args, **kwargs):
        await asyncio.sleep(0.015)
        stored[(conversation_id, user_id)] = state
        saved.append((conversation_id, msg_id))

    log.get_latest_risk_state_with_time = latest
    log.save_risk_state_history = save
    log.get_recent_risk_state_history = AsyncMock(return_value=[])
    log.get_recent_feedbacks = AsyncMock(return_value=[])
    log.get_recent_guardrail_context_reviews = AsyncMock(return_value=[])
    machine.chat_log_service = log
    monkeypatch.setattr("app.core.risk_state.KBService.get_fusion_config", lambda *_: {"decay_factor": 1.0})

    async def run():
        async def update(conversation_id, msg_id, delta):
            result = await machine.update(conversation_id, "sender", msg_id, delta)
            # Allow other requests to finish before reading our own diagnostic.
            await asyncio.sleep(0.05)
            return result, machine.last_diagnostic

        results = await asyncio.gather(
            update("same", "first", RiskState(harassment=0.1)),
            update("same", "second", RiskState(harassment=0.2)),
            update("other", "parallel", RiskState(harassment=0.8)),
        )
        assert [item[1]["max_score"] for item in results] == pytest.approx([0.1, 0.3, 0.8])
        assert stored[("same", "sender")].harassment == pytest.approx(0.3)
        assert saved.index(("same", "first")) < saved.index(("same", "second"))
        assert saved.index(("other", "parallel")) < saved.index(("same", "second"))
        assert machine.last_diagnostic == {}, "child diagnostics leaked into parent task"

    asyncio.run(run())


def test_relationship_read_modify_write_does_not_lose_increment():
    service = RelationshipService.__new__(RelationshipService)
    service.db_id = "test"
    service.metrics_coll = "relationship_metrics"
    data = {"user_a_id": "alice", "user_b_id": "bob", "user_a_message_count": 0,
            "user_b_message_count": 0}

    def read(*args, **kwargs):
        snapshot = dict(data)
        time.sleep(0.02)
        return SimpleNamespace(documents=[SimpleNamespace(id="metrics", data=snapshot)])

    def write(_db, _collection, _id, updates):
        time.sleep(0.02)
        data.update(updates)

    service.db = SimpleNamespace(list_documents=read, update_document=write)
    service._get_conversation_participants = AsyncMock(return_value={"user_a_id": "alice", "user_b_id": "bob"})

    async def run():
        result = await asyncio.gather(*(
            service.update_metrics("same", "alice", "bob") for _ in range(3)
        ))
        assert result == [1, 2, 3]

    asyncio.run(run())
    assert data["user_a_message_count"] == 3
