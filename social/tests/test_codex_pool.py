"""Hermetic pool tests: fake workers or fake subprocesses, never live Codex."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import ast
import json
import os
from pathlib import Path
import threading
import time
from unittest.mock import Mock

import pytest

from services import codex_chat_provider as provider
from tests.test_codex_chat_provider import FakeRpc, mock_session


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(provider, "_pool", None)
    monkeypatch.setattr(provider, "_shutdown", False)
    monkeypatch.setattr(provider.subprocess, "Popen", Mock(side_effect=AssertionError("No live Codex")))
    for name in ("AYUE_GPT_SERVICE_TIER", "AYUE_GPT_POOL_SIZE", "AYUE_GPT_WORKER_MAX_REQUESTS",
                 "AYUE_GPT_METADATA_TTL_SECONDS", "AYUE_GPT_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    yield
    provider.shutdown()


class Worker:
    def __init__(self):
        self.busy = False
        self.calls = 0
        self.closed = False
        self.rpc = object()
        self.cwd = "/tmp/fake-worker"

    def start(self, deadline):
        assert deadline > time.monotonic()
        assert not self.closed

    def close(self):
        self.closed = True


def test_two_exclusive_workers_and_queue_deadline(monkeypatch):
    monkeypatch.setattr(provider, "_Worker", Worker)
    pool = provider._Pool()
    entered = threading.Barrier(3)
    release = threading.Event()
    active = []

    def hold():
        with pool.lease(time.monotonic() + 3) as worker:
            active.append(worker)
            entered.wait(timeout=2)
            assert release.wait(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(hold) for _ in range(2)]
        entered.wait(timeout=2)
        try:
            assert len(set(active)) == len(pool.workers) == 2
            started = time.monotonic()
            with pytest.raises(TimeoutError, match="pool queue"):
                with pool.lease(started + 0.03):
                    pytest.fail("queue admitted a third request")
            assert time.monotonic() - started < 0.5
            assert not any(w.closed for w in active)
        finally:
            release.set()
        for future in futures:
            future.result(timeout=2)
    with pool.lease(time.monotonic() + 1) as reused:
        assert reused in active
    pool.close()


def test_queue_time_is_not_reset_before_worker_start(monkeypatch):
    monkeypatch.setattr(provider, "_Worker", Worker)
    pool = provider._Pool(size=1)
    deadline = time.monotonic() + 1
    with pool.lease(deadline) as worker:
        worker.start = Mock()
    with pool.lease(deadline):
        pass
    worker.start.assert_called_once_with(deadline)


def test_recycle_and_failure_discard_without_retry(monkeypatch):
    monkeypatch.setattr(provider, "_Worker", Worker)
    pool = provider._Pool(size=1, max_requests=2)
    with pool.lease(time.monotonic() + 1) as first:
        pass
    with pool.lease(time.monotonic() + 1) as second:
        assert first is second
    assert first.closed and not pool.workers
    with pytest.raises(ValueError):
        with pool.lease(time.monotonic() + 1) as third:
            assert third is not first
            raise ValueError("invalid proposal")
    assert third.closed and not pool.workers
    with pool.lease(time.monotonic() + 1) as fourth:
        assert fourth is not third
    pool.close()


def test_start_failure_and_expired_queue_leave_no_worker(monkeypatch):
    factory = Mock(side_effect=Worker)
    monkeypatch.setattr(provider, "_Worker", factory)
    pool = provider._Pool(size=1)
    with pytest.raises(TimeoutError):
        with pool.lease(time.monotonic() - 1):
            pass
    factory.assert_not_called()
    with pool.lease(time.monotonic() + 1) as worker:
        worker.start = Mock(side_effect=provider.CodexProviderError("failed"))
    with pytest.raises(provider.CodexProviderError):
        with pool.lease(time.monotonic() + 1):
            pass
    worker.start.assert_called_once()
    assert worker.closed and not pool.workers


def test_shutdown_wakes_waiter_and_closes_active_worker(monkeypatch):
    monkeypatch.setattr(provider, "_Worker", Worker)
    pool = provider._Pool(size=1)
    with pool.lease(time.monotonic() + 3) as worker:
        with ThreadPoolExecutor(max_workers=1) as executor:
            def wait():
                with pool.lease(time.monotonic() + 5):
                    pytest.fail("closed pool admitted work")
            future = executor.submit(wait)
            pool.close()
            with pytest.raises(provider.CodexProviderError, match="closed"):
                future.result(timeout=1)
            assert worker.closed
    pool.close()
    with pytest.raises(provider.CodexProviderError, match="closed"):
        with pool.lease(time.monotonic() + 1):
            pass


def test_metadata_cache_ttl_and_single_entry(monkeypatch):
    rpc = FakeRpc()
    now = [100.0]
    monkeypatch.setattr(provider.time, "monotonic", lambda: now[0])
    first = provider.verify_account_and_models(rpc, ["test-gpt"])
    first["test-gpt"] = "tampered"
    assert provider.verify_account_and_models(rpc, ["test-gpt"]) == {"test-gpt": None}
    assert len(rpc.calls) == 3
    now[0] += 301
    provider.verify_account_and_models(rpc, ["test-gpt"])
    assert len(rpc.calls) == 6
    monkeypatch.setenv("AYUE_GPT_SERVICE_TIER", "missing")
    with pytest.raises(provider.CodexProviderError):
        provider.verify_account_and_models(rpc, ["test-gpt"])
    assert rpc._verified is None


def test_fresh_ephemeral_threads_and_unsubscribe(monkeypatch):
    rpc = FakeRpc()
    base = rpc.call
    thread_ids = []

    def call(method, params):
        result = base(method, params)
        if method == "thread/start":
            thread_id = str(len(thread_ids))
            thread_ids.append(thread_id)
            result["thread"]["id"] = thread_id
            rpc.events = [
                {"method": "item/completed", "params": {"threadId": thread_id, "turnId": "u",
                 "item": {"type": "agentMessage", "text": "hello", "phase": "final_answer"}}},
                {"method": "turn/completed", "params": {"threadId": thread_id,
                 "turn": {"id": "u", "status": "completed"}}},
            ]
        return result

    rpc.call = call
    mock_session(monkeypatch, rpc)
    for prompt in ("user A only", "user B only"):
        assert provider.generate(prompt, model="test-gpt")["content"] == "hello"
    assert thread_ids == ["0", "1"]
    assert [p["threadId"] for m, p in rpc.calls if m == "thread/unsubscribe"] == thread_ids
    assert all(p["ephemeral"] for m, p in rpc.calls if m == "thread/start")
    assert [p["input"][0]["text"] for m, p in rpc.calls if m == "turn/start"] == ["user A only", "user B only"]
    assert sum(m == "account/read" for m, _ in rpc.calls) == 1
    assert not any(m in {"thread/archive", "thread/resume"} for m, _ in rpc.calls)


def test_plain_text_streams_only_final_answer_before_completion(monkeypatch):
    rpc = FakeRpc()
    rpc.events[:0] = [
        {"method": "item/started", "params": {"threadId": "t", "turnId": "u",
         "item": {"id": "c", "type": "agentMessage", "phase": "commentary"}}},
        {"method": "item/agentMessage/delta", "params": {"threadId": "t", "turnId": "u", "itemId": "c", "delta": "hidden"}},
        {"method": "item/started", "params": {"threadId": "t", "turnId": "u",
         "item": {"id": "a", "type": "agentMessage", "phase": "final_answer"}}},
        {"method": "item/agentMessage/delta", "params": {"threadId": "t", "turnId": "u", "itemId": "a", "delta": "hel"}},
    ]
    mock_session(monkeypatch, rpc)
    chunks = []

    def token(text):
        if not chunks:
            assert rpc.events  # Actual incremental delivery, not final slicing.
        chunks.append(text)

    result = provider.generate("x", model="test-gpt", on_token=token)
    assert chunks == ["hel", "lo"]
    assert result["content"] == "hello"
    assert "outputSchema" not in dict(rpc.calls)["turn/start"]


@pytest.mark.parametrize("status", ["bad", None])
def test_unsubscribe_failure_discards_worker_and_output(monkeypatch, status):
    rpc = FakeRpc()
    base = rpc.call
    rpc.call = lambda method, params: {"status": status} if method == "thread/unsubscribe" else base(method, params)
    worker = Worker()
    worker.rpc = rpc
    monkeypatch.setattr(provider, "_Worker", lambda: worker)
    callback = Mock()
    with pytest.raises(provider.CodexProviderError, match="release"):
        provider.generate("x", model="test-gpt", on_token=callback)
    assert worker.closed and not provider._get_pool().workers
    callback.assert_not_called()


def test_owned_process_shutdown_reaps_and_removes_directory(monkeypatch):
    process = Mock(pid=987654)
    monkeypatch.setenv("AYUE_CODEX_HOME", "/tmp/fake-managed-home")
    monkeypatch.setattr(provider.subprocess, "Popen", Mock(return_value=process))
    monkeypatch.setattr(provider, "StdioRpc", Mock(return_value=Mock()))
    kill = Mock()
    monkeypatch.setattr(provider.os, "killpg", kill)
    worker = provider._Worker()
    worker.start(time.monotonic() + 1)
    cwd = worker.cwd
    assert Path(cwd).exists()
    worker.close()
    worker.close()
    assert kill.call_count == 2
    assert not Path(cwd).exists()
    process.stdin.close.assert_called_once()
    process.stdout.close.assert_called_once()


def test_foreign_process_is_never_signalled(monkeypatch):
    worker = provider._Worker()
    worker.owner = os.getpid() + 1
    worker.process = Mock()
    kill = Mock()
    monkeypatch.setattr(provider.os, "killpg", kill)
    worker.close()
    kill.assert_not_called()


def test_social_shutdown_is_registered_without_importing_or_starting_services():
    tree = ast.parse((Path(provider.__file__).parents[1] / "main.py").read_text())
    shutdown = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "stop_background_services")
    names = [n.func.id for n in ast.walk(shutdown) if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    callbacks = {name: Mock() for name in names}
    exec(compile(ast.Module(body=[ast.FunctionDef(name=shutdown.name, args=shutdown.args, body=shutdown.body,
         decorator_list=[], lineno=1, col_offset=0)], type_ignores=[]), "offline-lifecycle", "exec"), callbacks)
    callbacks[shutdown.name]()
    callbacks["shutdown_codex_provider"].assert_called_once()


@pytest.mark.parametrize("name,value", [("AYUE_GPT_POOL_SIZE", "0"), ("AYUE_GPT_POOL_SIZE", "17"),
    ("AYUE_GPT_WORKER_MAX_REQUESTS", "129"), ("AYUE_GPT_METADATA_TTL_SECONDS", "nan")])
def test_bounds_fail_closed(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(provider.CodexProviderError):
        if "TTL" in name:
            provider.verify_account_and_models(FakeRpc(), ["test-gpt"])
        else:
            provider._get_pool()


def test_shutdown_before_first_use_is_terminal():
    provider.shutdown()
    with pytest.raises(provider.CodexProviderError, match="shut down"):
        provider._get_pool()
    assert provider._pool is None


@pytest.mark.parametrize("name", ["AYUE_CODEX_HOME", "AYUE_CODEX_BIN"])
def test_runtime_identity_change_requires_restart(monkeypatch, name):
    pool = provider._get_pool()
    monkeypatch.setenv(name, "/tmp/changed-runtime")
    with pytest.raises(provider.CodexProviderError, match="restart Social"):
        provider._get_pool()
    assert provider._pool is pool


def test_callback_deadline_discards_worker(monkeypatch):
    worker = Worker()
    worker.rpc = FakeRpc()
    monkeypatch.setattr(provider, "_Worker", lambda: worker)
    now = [time.monotonic()]
    monkeypatch.setattr(provider.time, "monotonic", lambda: now[0])

    def callback(_text):
        now[0] += 2

    with pytest.raises(TimeoutError):
        provider.generate("x", model="test-gpt", deadline_monotonic=now[0] + 1, on_token=callback)
    assert worker.closed and not provider._pool.workers


def test_callback_failure_discards_worker_without_retry(monkeypatch):
    worker = Worker()
    worker.rpc = FakeRpc()
    monkeypatch.setattr(provider, "_Worker", lambda: worker)
    with pytest.raises(ValueError, match="callback"):
        provider.generate("x", model="test-gpt", on_token=Mock(side_effect=ValueError("callback")))
    assert worker.closed and not provider._pool.workers
    assert sum(m == "turn/start" for m, _ in worker.rpc.calls) == 1


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX ownership contract")
def test_fork_resets_inherited_locked_pool_without_signalling_parent():
    pool = provider._get_pool()
    read_fd, write_fd = os.pipe()
    provider._pool_lock.acquire()
    pid = os.fork()
    if pid == 0:
        try:
            os.close(read_fd)
            child_pool = provider._get_pool()
            os.write(write_fd, b"ok" if child_pool.owner == os.getpid() else b"bad")
        finally:
            os._exit(0)
    provider._pool_lock.release()
    os.close(write_fd)
    try:
        import select
        ready, _, _ = select.select([read_fd], [], [], 2)
        assert ready, "child blocked on an inherited lock"
        assert os.read(read_fd, 3) == b"ok"
        assert provider._pool is pool and not pool.closed
    finally:
        os.close(read_fd)
        if not ready:
            os.kill(pid, 9)
        os.waitpid(pid, 0)
