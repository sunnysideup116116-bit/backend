"""Exercise actual HTTP dependencies using in-process ASGI and fake I/O."""

import asyncio
import threading
from unittest.mock import MagicMock

from fastapi import FastAPI
import httpx
import pytest

from routers import chat_onboarding, demo, match, places, system
from services import admin_access_service as access
from services import expensive_request_guard as guard


def _app():
    app = FastAPI()
    app.include_router(system.router)
    app.include_router(demo.router, prefix="/api")
    app.include_router(places.router)
    app.include_router(chat_onboarding.router, prefix="/api")
    app.include_router(match.router)
    return app


async def _request(app, method, path, *, client_ip="203.0.113.10", host="service.misproject.us.ci", **kwargs):
    transport = httpx.ASGITransport(app=app, client=(client_ip, 1234))
    async with httpx.AsyncClient(transport=transport, base_url=f"http://{host}") as client:
        return await client.request(method, path, **kwargs)


@pytest.mark.parametrize("method,path,payload", [
    ("POST", "/api/seed", None),
    ("POST", "/api/clear", {"user_id": "owner"}),
    ("GET", "/api/demo/match-test?user_id=owner", None),
    ("POST", "/api/demo/reset_db_state", None),
    ("POST", "/api/demo/clear_graph", None),
    ("POST", "/api/demo/clear_all", None),
    ("POST", "/api/demo/events/reset?confirm=true", None),
    ("POST", "/api/demo/events/discover/start", None),
    ("POST", "/api/demo/events/invitations/scan?confirm=true", None),
])
def test_disabled_maintenance_rejects_before_any_work(monkeypatch, method, path, payload):
    monkeypatch.setattr(access.config, "DEMO_DESTRUCTIVE_TOOLS_ENABLED", False)
    monkeypatch.setenv("AYUE_DEMO_ADMIN_TOKEN", "t" * 32)
    spies = []
    for module, names in [(system, ("profiles_coll", "matches_coll", "messages_coll", "get_embedding", "clear_all_demo_state")),
                          (demo, ("profiles_coll", "matches_coll", "messages_coll", "clear_graph", "clear_all_demo_state",
                                  "reset_event_inventory", "enqueue_event_discovery_job", "scan_event_opportunities"))]:
        for name in names:
            spy = MagicMock()
            monkeypatch.setattr(module, name, spy)
            spies.append(spy)
    response = asyncio.run(_request(_app(), method, path, json=payload,
                                    headers={"X-Ayue-Admin-Token": "t" * 32}))
    assert response.status_code == 403
    assert all(not spy.mock_calls for spy in spies)


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer ordinary-user"},
                                      {"X-Ayue-Admin-Token": "wrong"}])
def test_feature_flag_is_not_admin_authority(monkeypatch, headers):
    monkeypatch.setattr(access.config, "DEMO_DESTRUCTIVE_TOOLS_ENABLED", True)
    monkeypatch.setenv("AYUE_DEMO_ADMIN_TOKEN", "t" * 32)
    messages = MagicMock()
    monkeypatch.setattr(demo, "messages_coll", messages)
    response = asyncio.run(_request(_app(), "POST", "/api/demo/reset_db_state", headers=headers))
    assert response.status_code == 403
    messages.delete_many.assert_not_called()


@pytest.mark.parametrize("path,payload,operation", [
    ("/api/match/events/discover", {"region": "test"}, "enqueue_event_discovery_job"),
    ("/api/match/events/relevance/rebuild", None, "rebuild_all_event_relevance"),
    ("/api/match/events/opportunities/scan", {}, "scan_event_opportunities"),
    ("/api/match/events/lifecycle/run", None, "run_event_lifecycle_once"),
])
def test_global_event_maintenance_requires_admin_before_job_or_graph_work(monkeypatch, path, payload, operation):
    monkeypatch.setattr(access.config, "DEMO_DESTRUCTIVE_TOOLS_ENABLED", True)
    monkeypatch.setenv("AYUE_DEMO_ADMIN_TOKEN", "t" * 32)
    work = MagicMock()
    monkeypatch.setattr(match, operation, work)
    response = asyncio.run(_request(_app(), "POST", path, json=payload))
    assert response.status_code == 403
    work.assert_not_called()


def test_enabled_authenticated_maintenance_can_run_against_fake_store(monkeypatch):
    monkeypatch.setattr(access.config, "DEMO_DESTRUCTIVE_TOOLS_ENABLED", True)
    monkeypatch.setenv("AYUE_DEMO_ADMIN_TOKEN", "t" * 32)
    stores = [MagicMock() for _ in range(3)]
    for name, store in zip(("messages_coll", "matches_coll", "profiles_coll"), stores):
        monkeypatch.setattr(demo, name, store)
    response = asyncio.run(_request(_app(), "POST", "/api/demo/reset_db_state",
                                    headers={"X-Ayue-Admin-Token": "t" * 32}))
    assert response.status_code == 200
    stores[0].delete_many.assert_called_once()
    stores[1].update_many.assert_called_once()
    stores[2].update_many.assert_called_once()


@pytest.mark.parametrize("flag,client_ip,host", [
    (False, "127.0.0.1", "localhost"),
    (True, "203.0.113.10", "localhost"),
    (True, "127.0.0.1", "service.misproject.us.ci"),
])
@pytest.mark.parametrize("path", ["/api/debug/profile_state", "/api/debug/profile_skill_runs"])
def test_private_debug_requires_flag_and_true_local_connection(monkeypatch, flag, client_ip, host, path):
    monkeypatch.setattr(access, "local_debug_enabled", lambda: flag)
    profiles, database = MagicMock(), MagicMock()
    monkeypatch.setattr(system, "profiles_coll", profiles)
    monkeypatch.setattr(system, "db", database)
    response = asyncio.run(_request(_app(), "GET", path + "?user_id=victim", client_ip=client_ip, host=host,
                                    headers={"X-Forwarded-For": "127.0.0.1"}))
    assert response.status_code == 404
    profiles.find_one.assert_not_called()
    assert not database.mock_calls


def test_debug_is_available_only_for_explicit_local_debug(monkeypatch):
    monkeypatch.setattr(access, "local_debug_enabled", lambda: True)
    profiles, matches = MagicMock(), MagicMock()
    profiles.find_one.return_value = {"user_id": "owner"}
    matches.find.return_value = []
    monkeypatch.setattr(system, "profiles_coll", profiles)
    monkeypatch.setattr(system, "matches_coll", matches)
    response = asyncio.run(_request(_app(), "GET", "/api/debug/profile_state?user_id=owner",
                                    client_ip="127.0.0.1", host="localhost"))
    assert response.status_code == 200


def test_init_does_not_enumerate_other_accounts(monkeypatch):
    profiles = MagicMock()
    profiles.find.return_value = [{"user_id": "owner"}]
    profiles.find_one.return_value = None
    monkeypatch.setattr(system, "profiles_coll", profiles)
    response = asyncio.run(_request(_app(), "GET", "/api/init?user_id=owner"))
    assert response.status_code == 200
    assert response.json()["users"] == ["owner"]
    assert profiles.find.call_args.args[0] == {"user_id": "owner"}


def test_places_limits_ignore_forged_ip_and_session_token(monkeypatch):
    budget = guard.RequestBudget(per_ip=1, total=10, concurrent=2)
    monkeypatch.setattr(guard, "PLACES_BUDGET", budget)
    provider = MagicMock(return_value=[])
    monkeypatch.setattr(places, "autocomplete_places", provider)
    app = _app()
    first = asyncio.run(_request(app, "POST", "/api/places/autocomplete", json={"input": "park", "session_token": "first"}))
    second = asyncio.run(_request(app, "POST", "/api/places/autocomplete", json={"input": "park", "session_token": "second"},
                                 headers={"X-Forwarded-For": "198.51.100.99"}))
    assert first.status_code == 200
    assert second.status_code == 429
    assert second.headers["retry-after"] == "60"
    provider.assert_called_once()


def test_global_budget_bounds_many_different_ips(monkeypatch):
    budget = guard.RequestBudget(per_ip=10, total=2, concurrent=2)
    monkeypatch.setattr(guard, "PLACES_BUDGET", budget)
    provider = MagicMock(return_value={})
    monkeypatch.setattr(places, "place_details", provider)
    app = _app()
    statuses = [asyncio.run(_request(app, "POST", "/api/places/details", client_ip=f"203.0.113.{i}",
                                    json={"place_id": "example", "session_token": "session"})).status_code
                for i in range(1, 4)]
    assert statuses == [200, 200, 429]
    assert provider.call_count == 2


def test_concurrent_admission_rejects_before_provider_and_releases_slot(monkeypatch):
    budget = guard.RequestBudget(per_ip=10, total=10, concurrent=1)
    monkeypatch.setattr(guard, "PLACES_BUDGET", budget)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def provider(query, **kwargs):
        calls.append(query)
        entered.set()
        assert release.wait(5)
        return []

    monkeypatch.setattr(places, "autocomplete_places", provider)

    async def scenario():
        app = _app()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            first = asyncio.create_task(client.post("/api/places/autocomplete", json={"input": "park"}))
            try:
                assert await asyncio.to_thread(entered.wait, 2)
                denied = await client.post("/api/places/autocomplete", json={"input": "lake"})
                assert denied.status_code == 429
                assert calls == ["park"]
            finally:
                release.set()
            assert (await first).status_code == 200
            assert (await client.post("/api/places/autocomplete", json={"input": "garden"})).status_code == 200
        assert budget._active == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("payload,status", [
    ({"user_id": "owner", "state": "big_five", "message": "x" * 8001}, 422),
    ({"user_id": "owner", "state": "big_five", "message": "hello", "padding": "x" * 70000}, 413),
])
def test_oversize_assessment_never_starts_model_or_writes(monkeypatch, payload, status):
    handle, profiles = MagicMock(), MagicMock()
    monkeypatch.setattr(chat_onboarding, "handle_assessment_ui_message", handle)
    monkeypatch.setattr(chat_onboarding, "profiles_coll", profiles)
    response = asyncio.run(_request(_app(), "POST", "/api/chat", json=payload))
    assert response.status_code == status
    handle.assert_not_called()
    profiles.find_one.assert_not_called()


def test_provider_failure_releases_concurrent_budget(monkeypatch):
    budget = guard.RequestBudget(per_ip=10, total=10, concurrent=1)
    monkeypatch.setattr(guard, "PLACES_BUDGET", budget)
    monkeypatch.setattr(places, "autocomplete_places", MagicMock(side_effect=places.GooglePlacesError("google_places_unavailable")))
    response = asyncio.run(_request(_app(), "POST", "/api/places/autocomplete", json={"input": "park"}))
    assert response.status_code == 503
    assert budget._active == 0


def test_expired_request_buckets_are_reclaimed():
    clock = [0.0]
    budget = guard.RequestBudget(per_ip=1, total=1, concurrent=1, clock=lambda: clock[0])

    async def scenario():
        async with budget.admit("old"):
            pass
        clock[0] = 61.0
        async with budget.admit("new"):
            assert "old" not in budget._ips
        assert budget._active == 0

    asyncio.run(scenario())
