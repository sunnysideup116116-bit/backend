import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI

from registration_voice.provider import ProviderEvent, ToolCall
from registration_voice.router import (
    VoiceRegistrationRuntime,
    VoiceSessionRequest,
    create_router,
)
from registration_voice.settings import VoiceRegistrationSettings


class FakeConnection:
    def __init__(self):
        self.finished = asyncio.Event()
        self.tool_results = []
        self.form_updates = []

    async def send_audio(self, data):
        return None

    async def send_audio_stream_end(self):
        self.finished.set()

    async def send_activity_start(self):
        return None

    async def send_activity_end(self):
        return None

    async def send_form_state(self, form, revision):
        self.form_updates.append((form, revision))

    async def send_tool_result(self, call, result):
        self.tool_results.append((call, result))

    async def events(self):
        yield ProviderEvent(
            type="tool_calls",
            tool_calls=(ToolCall(
                id="call-1",
                name="propose_registration_patch",
                args={
                    "base_revision": 0,
                    "nickname": "小歌",
                    "age": 16,
                    "region": "臺東卑南鄉",
                    "warning_codes": ["password_spoken"],
                },
            ),),
        )
        await self.finished.wait()
        yield ProviderEvent(type="turn_complete", final=True)


class MultiCallConnection(FakeConnection):
    async def events(self):
        yield ProviderEvent(
            type="tool_calls",
            tool_calls=(
                ToolCall(
                    id="call-name",
                    name="propose_registration_patch",
                    args={"base_revision": 0, "nickname": "小歌"},
                ),
                ToolCall(
                    id="call-age",
                    name="propose_registration_patch",
                    args={"base_revision": 0, "age": 25},
                ),
            ),
        )
        await self.finished.wait()
        yield ProviderEvent(type="turn_complete", final=True)


class FakeProvider:
    def __init__(
        self,
        failing_keys=(),
        connection_factory=FakeConnection,
        failure_message="429 quota",
    ):
        self.failing_keys = set(failing_keys)
        self.connection_factory = connection_factory
        self.failure_message = failure_message
        self.keys = []
        self.connections = []

    @asynccontextmanager
    async def connect(self, key, form, revision):
        self.keys.append(key)
        if key in self.failing_keys:
            raise RuntimeError(self.failure_message)
        connection = self.connection_factory()
        self.connections.append(connection)
        yield connection


def settings(**overrides):
    base = VoiceRegistrationSettings.from_env({
        "VOICE_REGISTRATION_ENABLED": "on",
        "VOICE_LOCAL_RATE_LIMIT_ENABLED": "off",
    })
    return replace(base, **overrides)


def app_for(provider, keys=("key-a", "key-b")):
    runtime = VoiceRegistrationRuntime(
        settings=settings(),
        keys=list(keys),
        provider=provider,
    )
    app = FastAPI()
    app.include_router(create_router(runtime))
    return app, runtime


def endpoints(app):
    by_path = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}
    return (
        by_path["/api/registration/voice/session"],
        by_path["/api/registration/voice"],
    )


class FakeWebSocket:
    def __init__(self, hello, messages=(), host="127.0.0.1"):
        self.headers = {}
        self.client = SimpleNamespace(host=host)
        self._hello = json.dumps(hello)
        self._messages = list(messages)
        self.sent = []
        self.accepted = False
        self.close_code = None
        self.close_reason = None

    async def accept(self):
        self.accepted = True

    async def receive_text(self):
        return self._hello

    async def receive(self):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(3600)

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=None):
        self.close_code = code
        self.close_reason = reason


async def issue_ticket(endpoint):
    req = VoiceSessionRequest(
        installation_id="installation-id-123456",
        consent_version="free-tier-voice-v1",
        consent_accepted_at="2026-09-10T12:00:00Z",
    )
    request = SimpleNamespace(
        headers={},
        client=SimpleNamespace(host="127.0.0.1"),
    )
    response = await endpoint(req, request)
    return response["ticket"]


def test_capability_reports_language_policy_and_two_minute_default():
    provider = FakeProvider()
    app, runtime = app_for(provider)
    by_path = {
        route.path: route.endpoint
        for route in app.routes
        if hasattr(route, "endpoint")
    }

    payload = asyncio.run(by_path["/api/registration/voice/capability"]())

    assert runtime.settings.max_session_seconds == 120
    assert payload["max_session_seconds"] == 120
    assert payload["silence_duration_ms"] == 500
    assert payload["supported_languages"] == ["zh-Hant-TW", "en"]


def test_websocket_applies_valid_fields_rejects_age_and_never_returns_password():
    async def scenario():
        provider = FakeProvider()
        app, _runtime = app_for(provider)
        session_endpoint, websocket_endpoint = endpoints(app)
        ticket = await issue_ticket(session_endpoint)
        websocket = FakeWebSocket(
            {
                "type": "hello",
                "ticket": ticket,
                "revision": 0,
                "form": {"password": "never-forward", "nickname": ""},
            },
            messages=({"type": "websocket.receive", "text": json.dumps({"type": "stop"})},),
        )
        await websocket_endpoint(websocket)

        assert websocket.sent[0]["type"] == "ready"
        patch = next(item for item in websocket.sent if item["type"] == "form_patch")
        assert patch["changes"] == {"nickname": "小歌", "region": "台東縣"}
        assert patch["rejected"] == [
            {"field": "age", "value": 16, "code": "age_out_of_range"},
        ]
        assert "password_spoken" in patch["warnings"]
        assert "never-forward" not in str(patch)
        assert "must-never-leave-the-model-boundary" not in str(patch)
        assert websocket.sent[-1]["type"] == "closed"

        tool_result = provider.connections[0].tool_results[0][1]
        assert tool_result["current_form"]["nickname"] == "小歌"
        assert "password" not in tool_result["current_form"]

    asyncio.run(scenario())


def test_new_sessions_rotate_keys_and_initial_failure_uses_next_key():
    async def scenario():
        provider = FakeProvider(failing_keys={"key-a"})
        app, _runtime = app_for(provider)
        session_endpoint, websocket_endpoint = endpoints(app)
        ticket = await issue_ticket(session_endpoint)
        websocket = FakeWebSocket(
            {"type": "hello", "ticket": ticket, "revision": 0, "form": {}},
            messages=({"type": "websocket.receive", "text": json.dumps({"type": "stop"})},),
        )
        await websocket_endpoint(websocket)

        assert [item["type"] for item in websocket.sent[:3]] == [
            "provider_reconnecting", "ready", "form_patch",
        ]
        assert websocket.sent[-1]["type"] == "closed"
        assert provider.keys[:2] == ["key-a", "key-b"]

    asyncio.run(scenario())


def test_non_retryable_error_does_not_rotate_or_cool_every_key():
    async def scenario():
        provider = FakeProvider(
            failing_keys={"key-a"},
            failure_message="invalid local schema",
        )
        app, runtime = app_for(provider)
        session_endpoint, websocket_endpoint = endpoints(app)
        ticket = await issue_ticket(session_endpoint)
        websocket = FakeWebSocket(
            {"type": "hello", "ticket": ticket, "revision": 0, "form": {}},
        )

        await websocket_endpoint(websocket)

        assert provider.keys == ["key-a"]
        assert websocket.sent[-1]["type"] == "provider_unavailable"
        assert len(runtime.key_pool.candidates()) == 2

    asyncio.run(scenario())


def test_local_rate_limiter_can_be_disabled_from_settings():
    async def scenario():
        provider = FakeProvider()
        app, runtime = app_for(provider)
        session_endpoint, _websocket_endpoint = endpoints(app)

        assert runtime.limiter.enabled is False
        for _ in range(5):
            assert await issue_ticket(session_endpoint)

    asyncio.run(scenario())


def test_multiple_function_calls_with_same_revision_are_combined_atomically():
    async def scenario():
        provider = FakeProvider(connection_factory=MultiCallConnection)
        app, _runtime = app_for(provider)
        session_endpoint, websocket_endpoint = endpoints(app)
        ticket = await issue_ticket(session_endpoint)
        websocket = FakeWebSocket(
            {"type": "hello", "ticket": ticket, "revision": 0, "form": {}},
            messages=(
                {
                    "type": "websocket.receive",
                    "text": json.dumps({"type": "stop"}),
                },
            ),
        )
        await websocket_endpoint(websocket)

        patches = [item for item in websocket.sent if item["type"] == "form_patch"]
        assert len(patches) == 1
        assert patches[0]["base_revision"] == 0
        assert patches[0]["revision"] == 1
        assert patches[0]["changes"] == {"nickname": "小歌", "age": 25}
        assert len(provider.connections[0].tool_results) == 2

    asyncio.run(scenario())


def test_session_ticket_is_bound_to_the_client_ip_that_requested_it():
    async def scenario():
        provider = FakeProvider()
        app, _runtime = app_for(provider)
        session_endpoint, websocket_endpoint = endpoints(app)
        ticket = await issue_ticket(session_endpoint)
        websocket = FakeWebSocket(
            {"type": "hello", "ticket": ticket, "revision": 0, "form": {}},
            host="203.0.113.10",
        )

        await websocket_endpoint(websocket)

        assert websocket.close_code == 1008
        assert websocket.close_reason == "ticket_client_mismatch"
        assert provider.keys == []

    asyncio.run(scenario())
