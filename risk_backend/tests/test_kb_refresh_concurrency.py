"""KB refreshes share work, publish complete snapshots, and fail explicitly."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import requests

from app.services import kb_service as module
from app.services.kb_service import KBService, KBUnavailableError


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    KBService.clear_cache()
    monkeypatch.setattr(module, "_CACHE_TTL", 300)
    monkeypatch.setattr(KBService, "_endpoint", "https://test.invalid/v1")
    monkeypatch.setattr(KBService, "_project_id", "test")
    monkeypatch.setattr(KBService, "_api_key", "test")
    monkeypatch.setattr(KBService, "_kb_db_id", "kb")
    yield
    KBService.clear_cache()


def test_concurrent_cache_misses_fetch_once_and_return_independent_values(monkeypatch):
    calls = []
    barrier = threading.Barrier(4)

    def fetch(*args, **kwargs):
        calls.append(1)
        time.sleep(0.08)
        return [{"conditions": {"count": 1}}]

    def read():
        barrier.wait(timeout=2)
        return KBService._list("kb_rules")

    monkeypatch.setattr(KBService, "_fetch", staticmethod(fetch))
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: read(), range(4)))
    assert len(calls) == 1
    results[0][0]["conditions"]["count"] = 99
    assert results[1][0]["conditions"]["count"] == 1
    assert KBService._list("kb_rules")[0]["conditions"]["count"] == 1


def test_concurrent_failed_refresh_is_shared_and_later_request_can_retry(monkeypatch):
    calls = []
    barrier = threading.Barrier(4)

    def fetch(*args, **kwargs):
        calls.append(1)
        time.sleep(0.08)
        raise requests.ReadTimeout("mock timeout")

    def read():
        barrier.wait(timeout=2)
        with pytest.raises(KBUnavailableError):
            KBService.get_rules()

    monkeypatch.setattr(KBService, "_fetch", staticmethod(fetch))
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(lambda _: read(), range(4)))
    assert len(calls) == 1
    assert module._cache == {}
    monkeypatch.setattr(KBService, "_fetch", staticmethod(lambda *args, **kwargs: [{"rule_name": "restored"}]))
    assert KBService.get_rules() == [{"rule_name": "restored"}]


def test_expired_snapshot_does_not_hide_refresh_failure(monkeypatch):
    monkeypatch.setattr(KBService, "_fetch", staticmethod(lambda *args, **kwargs: [{"version": 1}]))
    KBService._list("kb_rules")
    with module._cache_lock:
        for key, (_, value) in list(module._cache.items()):
            module._cache[key] = (time.monotonic() - 301, value)

    def fail(*args, **kwargs):
        raise requests.ConnectTimeout("mock timeout")

    monkeypatch.setattr(KBService, "_fetch", staticmethod(fail))
    with pytest.raises(KBUnavailableError):
        KBService.get_rules()
    assert next(iter(module._cache.values()))[1] == [{"version": 1}]


def test_clear_during_refresh_does_not_repopulate_invalidated_snapshot(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def fetch(*args, **kwargs):
        started.set()
        assert release.wait(2)
        return [{"version": "old"}]

    monkeypatch.setattr(KBService, "_fetch", staticmethod(fetch))
    with ThreadPoolExecutor(max_workers=1) as executor:
        pending = executor.submit(KBService._list, "kb_rules")
        try:
            assert started.wait(2)
            KBService.clear_cache()
        finally:
            release.set()
        assert pending.result(timeout=2) == [{"version": "old"}]
    assert module._cache == {}


def test_fetch_timeout_is_explicit_and_partial_pages_are_never_cached(monkeypatch):
    calls = []

    def get(*args, **kwargs):
        assert kwargs["timeout"] == (3.0, 5.0)
        calls.append(1)
        if len(calls) == 1:
            return SimpleNamespace(status_code=200, json=lambda: {
                "documents": [{"$id": "first", "rule_name": "partial"}], "total": 2,
            })
        return SimpleNamespace(status_code=503)

    monkeypatch.setattr(module.requests, "get", get)
    with pytest.raises(KBUnavailableError, match="HTTP 503"):
        KBService.get_rules()
    assert len(calls) == 2
    assert module._cache == {}


@pytest.mark.parametrize("documents,total", [([], 4), (None, 0), ([], None)])
def test_malformed_or_incomplete_pages_fail_instead_of_returning_safe_empty(monkeypatch, documents, total):
    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: {"documents": documents, "total": total},
    ))
    with pytest.raises(KBUnavailableError):
        KBService.get_rules()
    assert module._cache == {}


def test_valid_empty_collection_is_distinct_from_failure(monkeypatch):
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return SimpleNamespace(status_code=200, json=lambda: {"documents": [], "total": 0})

    monkeypatch.setattr(module.requests, "get", get)
    assert KBService.get_rules() == []
    assert KBService.get_rules() == []
    assert len(calls) == 1


def test_repeated_last_page_is_not_mistaken_for_complete_snapshot(monkeypatch):
    monkeypatch.setattr(module.requests, "get", lambda *args, **kwargs: SimpleNamespace(
        status_code=200, json=lambda: {"documents": [{"$id": "same", "rule_name": "only-one"}], "total": 2},
    ))
    with pytest.raises(KBUnavailableError, match="Repeated"):
        KBService.get_rules()
    assert module._cache == {}
