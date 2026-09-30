"""Exercise the real direct-chat HTTP dependencies with offline collaborators."""

import json
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import public_chat
from services import appwrite_identity_service, voice_chat_status


@pytest.fixture
def direct_chat_auth_fixture(monkeypatch):
    monkeypatch.setenv("APPWRITE_PROJECT_ID", "offline-test-project")
    monkeypatch.setenv("APPWRITE_ENDPOINT", "https://identity.invalid/v1")
    monkeypatch.delenv("APPWRITE_INTERNAL_ENDPOINT", raising=False)

    def account_lookup(url, *, headers, **kwargs):
        assert url == "https://identity.invalid/v1/account"
        assert kwargs["allow_redirects"] is False
        token = headers["X-Appwrite-JWT"]
        if token == "unavailable":
            raise requests.Timeout("offline identity failure")
        if token not in {"owner-a-jwt", "owner-b-jwt"}:
            return SimpleNamespace(status_code=401, json=lambda: {})
        owner = "owner-a" if token == "owner-a-jwt" else "owner-b"
        return SimpleNamespace(status_code=200, json=lambda: {"$id": owner})

    account = MagicMock(side_effect=account_lookup)
    monkeypatch.setattr(
        appwrite_identity_service.requests, "Session",
        lambda: SimpleNamespace(get=account),
    )
    side_effects = {}
    for name in (
        "save_message", "save_pair_owner_message_once", "write_profile",
        "record_owner_activity", "check_and_trigger_date_activation",
        "process_relationship_semantic_plan", "ai_process_date_coordination_step",
    ):
        mocked = MagicMock(return_value={"created": True, "message_id": "offline-message"})
        monkeypatch.setattr(public_chat, name, mocked)
        side_effects[name] = mocked
    ai = MagicMock(return_value={"reply": "offline reply"})
    monkeypatch.setattr(public_chat, "_complete_public_turn", ai)
    side_effects["ai"] = ai
    lookup = MagicMock(return_value={
        "_id": "offline-match", "from_user": "owner-a", "to_user": "contact-b",
        "status": "accepted",
    })
    monkeypatch.setattr(public_chat, "find_accepted_match", lookup)
    side_effects["lookup"] = lookup
    monkeypatch.setattr(public_chat, "_validated_requested_mentions", lambda _req: ([], False))
    monkeypatch.setattr(public_chat, "mentioned_contact_refs", lambda *_args: [])
    monkeypatch.setattr(public_chat, "mark_post_chat_activity", lambda *_args: 0)
    risk = MagicMock(return_value=SimpleNamespace(
        may_persist=True,
        public_projection=lambda: {"level": "safe", "delivery": "delivered", "ui_priority": "coach"},
    ))
    monkeypatch.setattr(public_chat.pair_message_risk_gate, "evaluate", risk)
    side_effects["risk"] = risk
    monkeypatch.setattr(voice_chat_status, "read_delivery_receipt", lambda *_args: None)
    monkeypatch.setattr(voice_chat_status, "read_cooldown", lambda *_args: {"state": "clear"})
    quota = MagicMock()
    monkeypatch.setattr("agent_quota.api.require_quota", quota)
    monkeypatch.setattr("services.ayue_agent.pi.settings.pi_available", lambda: True)

    # A stream worker outlives response delivery by design. Join the offline
    # workers before monkeypatch teardown so no collaborator becomes live.
    workers = []
    monkeypatch.setattr(public_chat, "threading", SimpleNamespace(
        Event=threading.Event,
        Thread=lambda **kwargs: (workers.append(threading.Thread(**kwargs)) or workers[-1]),
    ))
    app = FastAPI()
    app.include_router(public_chat.router, prefix="/api")
    client = TestClient(app)  # This app has no startup handlers or real workers.
    yield SimpleNamespace(client=client, effects=side_effects, account=account, quota=quota, app=app)
    for worker in workers:
        worker.join(timeout=2)
        assert not worker.is_alive(), "offline direct-chat worker did not finish"
    client.close()


@pytest.mark.parametrize("path", ["/api/direct_chat", "/api/direct_chat/stream"])
@pytest.mark.parametrize("token,claimed_owner,status", [
    (None, "owner-a", 401),
    ("invalid-jwt", "owner-a", 401),
    ("owner-a-jwt", "owner-b", 403),
    ("owner-b-jwt", "owner-a", 403),
    ("unavailable", "owner-a", 503),
    ("owner-a-jwt", "owner-a", 200),
])
def test_direct_chat_http_owner_boundary(direct_chat_auth_fixture, path, token, claimed_owner, status):
    setup = direct_chat_auth_fixture
    response = setup.client.post(path, json={
        "user_id": claimed_owner, "contact_id": "contact-b", "message": "offline message",
        "client_message_id": "offline-attempt",
    }, headers={"Authorization": f"Bearer {token}"} if token else {})
    assert response.status_code == status
    if status != 200:
        # Includes model, Risk, profile, source-message and downstream-task
        # entry points; none can be reached through a denied HTTP request.
        assert all(effect.call_count == 0 for effect in setup.effects.values())
        setup.quota.assert_not_called()
    else:
        setup.quota.assert_not_called()  # Pair messages remain outside AI quotas.
        setup.effects["save_pair_owner_message_once"].assert_called_once()
        setup.effects["risk"].assert_called_once()
        assert setup.effects["save_pair_owner_message_once"].call_args.args[1] == "owner-a"
        if path.endswith("/stream"):
            assert json.loads(response.text.strip())["type"] == "final"


@pytest.mark.parametrize("path,contact,attempt", [
    ("/api/direct_chat", "contact-b", "voice-offline-attempt"),
    ("/api/direct_chat", "ai_assistant", "offline-attempt"),
    ("/api/direct_chat/stream", "ai_assistant", "offline-attempt"),
])
def test_voice_and_public_ai_auth_paths(direct_chat_auth_fixture, path, contact, attempt):
    setup = direct_chat_auth_fixture
    response = setup.client.post(path, json={
        "user_id": "owner-a", "contact_id": contact, "message": "offline message",
        "client_message_id": attempt,
    }, headers={"Authorization": "Bearer owner-a-jwt"})
    assert response.status_code == 200
    if contact == "ai_assistant":
        setup.quota.assert_called_once_with("owner-a")
        setup.effects["ai"].assert_called_once()
        setup.effects["save_pair_owner_message_once"].assert_not_called()
    else:
        setup.quota.assert_not_called()
        setup.effects["save_pair_owner_message_once"].assert_called_once()


def test_direct_chat_request_model_unchanged(direct_chat_auth_fixture):
    schema = direct_chat_auth_fixture.app.openapi()
    for path in ("/api/direct_chat", "/api/direct_chat/stream"):
        body = schema["paths"][path]["post"]["requestBody"]["content"]["application/json"]["schema"]
        assert body == {"$ref": "#/components/schemas/DirectChatRequest"}


@pytest.mark.parametrize("path", ["/api/direct_chat", "/api/direct_chat/stream"])
@pytest.mark.parametrize("message,padding,status,code", [
    ("x" * 8001, "", 422, "direct_chat_input_too_long"),
    ("offline message", "x" * (64 * 1024), 413, "direct_chat_request_too_large"),
    ("x" * 8000, "", 200, None),
])
def test_direct_chat_input_boundaries(direct_chat_auth_fixture, path, message, padding, status, code):
    setup = direct_chat_auth_fixture
    response = setup.client.post(path, json={
        "user_id": "owner-a", "contact_id": "contact-b", "message": message,
        "client_message_id": "offline-attempt", "padding": padding,
    }, headers={"Authorization": "Bearer owner-a-jwt"})
    assert response.status_code == status
    if status != 200:
        assert response.json()["detail"]["code"] == code
        setup.account.assert_not_called()
        setup.quota.assert_not_called()
        assert all(effect.call_count == 0 for effect in setup.effects.values())
    else:
        saved = setup.effects["save_pair_owner_message_once"].call_args.args
        assert saved[2] == message  # The accepted input is never truncated.


@pytest.mark.parametrize("path", ["/api/direct_chat", "/api/direct_chat/stream"])
def test_empty_choice_action_remains_compatible(direct_chat_auth_fixture, path):
    setup = direct_chat_auth_fixture
    response = setup.client.post(path, json={
        "user_id": "owner-a", "contact_id": "ai_assistant", "message": "",
        "choice_id": "offline-choice", "choice_action": "confirm",
    }, headers={"Authorization": "Bearer owner-a-jwt"})
    assert response.status_code == 200
    setup.effects["save_message"].assert_not_called()
    setup.effects["ai"].assert_called_once()


@pytest.mark.parametrize("path", ["/api/direct_chat", "/api/direct_chat/stream"])
@pytest.mark.parametrize("claimed_owner,status", [("owner-a", 200), ("owner-b", 403)])
def test_image_message_owner_boundary(direct_chat_auth_fixture, path, claimed_owner, status):
    setup = direct_chat_auth_fixture
    response = setup.client.post(path, json={
        "user_id": claimed_owner, "contact_id": "contact-b", "message": "",
        "file_id": "offline-file", "client_message_id": "offline-image-attempt",
    }, headers={"Authorization": "Bearer owner-a-jwt"})
    assert response.status_code == status
    setup.effects["risk"].assert_not_called()
    if status == 403:
        assert all(effect.call_count == 0 for effect in setup.effects.values())
    else:
        saved = setup.effects["save_pair_owner_message_once"]
        saved.assert_called_once()
        assert saved.call_args.kwargs["file_id"] == "offline-file"
