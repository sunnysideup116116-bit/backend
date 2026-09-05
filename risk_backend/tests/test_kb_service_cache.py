"""KBService TTL cache regression tests."""

import json

import pytest

from app.services import kb_service as kb_module
from app.services.kb_service import KBService


@pytest.fixture(autouse=True)
def isolated_cache(monkeypatch):
    """Keep module-level cache state from leaking between tests."""
    monkeypatch.setattr(kb_module, "_CACHE_TTL", 300.0, raising=False)
    cache = getattr(kb_module, "_cache", None)
    if cache is not None:
        cache.clear()
    yield
    cache = getattr(kb_module, "_cache", None)
    if cache is not None:
        cache.clear()


def test_list_reuses_cached_result_for_equivalent_query(monkeypatch):
    calls = []

    def fake_fetch(collection_id, queries=None, limit=100):
        calls.append((collection_id, queries, limit))
        return [{"feature_name": "pressure"}]

    monkeypatch.setattr(KBService, "_fetch", staticmethod(fake_fetch))
    first_queries = [
        {"method": "equal", "attribute": "enabled", "values": [True]},
    ]
    equivalent_queries = [
        {"values": [True], "attribute": "enabled", "method": "equal"},
    ]

    first = KBService._list("kb_features", first_queries)
    second = KBService._list("kb_features", equivalent_queries)

    assert first == second == [{"feature_name": "pressure"}]
    assert len(calls) == 1


def test_list_separates_collection_query_and_limit_cache_keys(monkeypatch):
    calls = []

    def fake_fetch(collection_id, queries=None, limit=100):
        calls.append((collection_id, queries, limit))
        return [{"call": len(calls)}]

    monkeypatch.setattr(KBService, "_fetch", staticmethod(fake_fetch))

    KBService._list("kb_features", limit=100)
    KBService._list("kb_rules", limit=100)
    KBService._list("kb_features", queries=[{"enabled": True}], limit=100)
    KBService._list("kb_features", limit=1)

    assert len(calls) == 4


def test_list_refetches_after_ttl_expires(monkeypatch):
    calls = []
    ticks = iter([100.0, 104.0, 106.0])

    def fake_fetch(collection_id, queries=None, limit=100):
        calls.append(collection_id)
        return [{"version": len(calls)}]

    monkeypatch.setattr(kb_module, "_CACHE_TTL", 5.0)
    monkeypatch.setattr(kb_module.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(KBService, "_fetch", staticmethod(fake_fetch))

    assert KBService._list("kb_rules") == [{"version": 1}]
    assert KBService._list("kb_rules") == [{"version": 1}]
    assert KBService._list("kb_rules") == [{"version": 2}]
    assert len(calls) == 2


def test_empty_fetch_result_is_not_cached(monkeypatch):
    calls = []

    def fake_fetch(collection_id, queries=None, limit=100):
        calls.append(collection_id)
        return []

    monkeypatch.setattr(KBService, "_fetch", staticmethod(fake_fetch))

    assert KBService._list("kb_rules") == []
    assert KBService._list("kb_rules") == []
    assert len(calls) == 2


def test_clear_cache_forces_next_request_to_refetch(monkeypatch):
    calls = []

    def fake_fetch(collection_id, queries=None, limit=100):
        calls.append(collection_id)
        return [{"version": len(calls)}]

    monkeypatch.setattr(KBService, "_fetch", staticmethod(fake_fetch))

    assert KBService._list("kb_rules") == [{"version": 1}]
    KBService.clear_cache()
    assert KBService._list("kb_rules") == [{"version": 2}]
    assert len(calls) == 2


def test_non_positive_ttl_disables_cache(monkeypatch):
    calls = []

    def fake_fetch(collection_id, queries=None, limit=100):
        calls.append(collection_id)
        return [{"version": len(calls)}]

    monkeypatch.setattr(kb_module, "_CACHE_TTL", 0.0)
    monkeypatch.setattr(KBService, "_fetch", staticmethod(fake_fetch))

    assert KBService._list("kb_rules") == [{"version": 1}]
    assert KBService._list("kb_rules") == [{"version": 2}]
    assert len(calls) == 2


def test_fetch_uses_query_pagination_and_reads_beyond_appwrite_default_25(
    monkeypatch,
):
    calls = []

    class Response:
        status_code = 200
        text = ""

        def __init__(self, documents):
            self._documents = documents

        def json(self):
            return {"total": 27, "documents": self._documents}

    def fake_get(url, *, headers, params, verify):
        decoded = [json.loads(value) for key, value in params if key == "queries[]"]
        offset = next(
            query["values"][0]
            for query in decoded
            if query.get("method") == "offset"
        )
        calls.append(decoded)
        start, end = (0, 25) if offset == 0 else (25, 27)
        return Response([
            {"$id": str(index), "id": index, "enabled": True}
            for index in range(start, end)
        ])

    monkeypatch.setattr(KBService, "_endpoint", "https://127.0.0.1/v1")
    monkeypatch.setattr(KBService, "_project_id", "project")
    monkeypatch.setattr(KBService, "_api_key", "key")
    monkeypatch.setattr(KBService, "_kb_db_id", "kb")
    monkeypatch.setattr(kb_module.requests, "get", fake_get)

    rows = KBService._fetch(
        "kb_hard_blocks",
        queries=[{"method": "equal", "attribute": "enabled", "values": [True]}],
        limit=100,
    )

    assert [row["id"] for row in rows] == list(range(27))
    assert len(calls) == 2
    assert any(query == {"method": "offset", "values": [25]} for query in calls[1])
