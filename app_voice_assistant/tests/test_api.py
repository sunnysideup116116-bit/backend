import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from fastapi import FastAPI

from app_voice_assistant.provider import AppVoiceProvider
from app_voice_assistant.router import AppVoiceRuntime, AppVoiceSessionRequest, create_router
from app_voice_assistant.settings import AppVoiceSettings


class FakeProvider:
    gemini_available = False

    async def interpret_text(self, text, *, context):
        from app_voice_assistant.contracts import deterministic_proposal
        return deterministic_proposal(text, context=context)

    async def interpret_audio(self, audio, *, context):
        return None

    async def synthesize(self, text):
        return None

    async def stream_synthesize(self, text):
        if False:
            yield b""


class FakeWebSocket:
    def __init__(self, hello, messages):
        self.headers = {}
        self.client = SimpleNamespace(host="127.0.0.1")
        self._hello = json.dumps(hello)
        self._messages = list(messages)
        self.sent = []
        self.closed = None

    async def accept(self):
        return None

    async def receive_text(self):
        return self._hello

    async def receive(self):
        await asyncio.sleep(0)
        message = self._messages.pop(0)
        raw = message.get("text")
        if raw and "__pending_confirmation__" in raw:
            pending = next(
                item for item in reversed(self.sent)
                if item.get("type") == "confirmation_required"
            )
            control = json.loads(raw)
            control["confirmation_id"] = pending["confirmation_id"]
            return {**message, "text": json.dumps(control)}
        if raw and "__last_action__" in raw:
            action = next(
                item for item in reversed(self.sent)
                if item.get("type") == "action_proposal"
            )
            control = json.loads(raw)
            control["action_id"] = action["action_id"]
            return {**message, "text": json.dumps(control)}
        return message

    async def send_json(self, payload):
        self.sent.append(payload)

    async def close(self, code=1000, reason=None):
        self.closed = (code, reason)


def settings(**overrides):
    base = AppVoiceSettings.from_env({
        "VOICE_APP_ENABLED": "on",
        "VOICE_APP_DEMO_ONLY": "on",
        "VOICE_APP_TEST_USER_IDS": "test-user-id",
    })
    return replace(base, **overrides)


def test_capability_and_demo_allowlist():
    runtime = AppVoiceRuntime(
        settings=settings(), keys=[], provider=AppVoiceProvider(settings(), []),
    )
    app = FastAPI()
    app.include_router(create_router(runtime))
    routes = {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}

    capability = asyncio.run(routes["/api/app-voice/capability"]())
    assert capability["enabled"] is True
    assert capability["demo_only"] is True
    assert capability["default_tts_mode"] == "gemini_live_duplex"
    assert capability["protocol_version"] == 3
    assert capability["full_duplex_live"] is False
    assert capability["structured_confirmation"] is True
    assert capability["barge_in"] is True
    assert len(capability["voice_options"]) == 30
    assert "voice_personality_exploration" in capability["supported_intents"]
    assert "contact_list" in capability["supported_intents"]
    assert "chat_send" in capability["supported_intents"]
    assert "visible_choice_action" in capability["supported_intents"]
    assert "direct_self_profile" in capability["supported_intents"]
    assert "direct_memory_read" in capability["supported_intents"]
    assert "direct_memory_add" in capability["supported_intents"]

    req = AppVoiceSessionRequest(
        installation_id="installation-id-123456",
        user_id="test-user-id",
        consent_version="demo-free-gemini-live-v2",
        consent_accepted_at="2026-09-10T12:00:00Z",
    )
    request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
    issued = asyncio.run(routes["/api/app-voice/session"](req, request))
    assert issued["ticket"]
    assert issued["websocket_url"].endswith("/api/app-voice")


def test_spoken_confirmation_is_bound_and_wrong_phrase_never_executes():
    async def scenario():
        runtime = AppVoiceRuntime(settings=settings(), keys=[], provider=FakeProvider())
        app = FastAPI()
        app.include_router(create_router(runtime))
        routes = {
            route.path: route.endpoint
            for route in app.routes
            if hasattr(route, "endpoint")
        }
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        issued = await routes["/api/app-voice/session"](
            AppVoiceSessionRequest(
                installation_id="installation-id-123456",
                user_id="test-user-id",
                consent_version="demo-free-gemini-live-v2",
                consent_accepted_at="2026-09-10T12:00:00Z",
            ),
            request,
        )
        websocket = FakeWebSocket(
            {
                "type": "hello",
                "ticket": issued["ticket"],
                "context": {"scope": "global", "revision": 0},
            },
            [
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "utterance", "text": "幫我關閉訊息通知",
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "utterance", "text": "關閉訊息通知",
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "utterance", "text": "確定關閉通知",
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "stop",
                })},
            ],
        )

        await routes["/api/app-voice"](websocket)

        actions = [item for item in websocket.sent if item["type"] == "action_proposal"]
        assert len(actions) == 1
        assert actions[0]["intent"] == "settings.set"
        assert actions[0]["confirmed"] is True
        assert any(item.get("code") == "confirmation_mismatch" for item in websocket.sent)

    asyncio.run(scenario())


def test_structured_confirmation_executes_once_and_cannot_loop():
    async def scenario():
        runtime = AppVoiceRuntime(settings=settings(), keys=[], provider=FakeProvider())
        app = FastAPI()
        app.include_router(create_router(runtime))
        routes = {
            route.path: route.endpoint
            for route in app.routes
            if hasattr(route, "endpoint")
        }
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        issued = await routes["/api/app-voice/session"](
            AppVoiceSessionRequest(
                installation_id="installation-id-123456",
                user_id="test-user-id",
                consent_version="demo-free-gemini-live-v2",
                consent_accepted_at="2026-09-10T12:00:00Z",
            ),
            request,
        )
        confirmation = {
            "type": "confirmation_response",
            "confirmation_id": "__pending_confirmation__",
            "accepted": True,
            "spoken_phrase": "確認關閉訊息通知",
        }
        websocket = FakeWebSocket(
            {
                "type": "hello",
                "ticket": issued["ticket"],
                "context": {"scope": "global", "revision": 0},
            },
            [
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "utterance", "text": "幫我關閉訊息通知",
                })},
                {"type": "websocket.receive", "text": json.dumps(confirmation)},
                {"type": "websocket.receive", "text": json.dumps(confirmation)},
                {"type": "websocket.receive", "text": json.dumps({"type": "stop"})},
            ],
        )

        await routes["/api/app-voice"](websocket)

        actions = [item for item in websocket.sent if item["type"] == "action_proposal"]
        assert len(actions) == 1
        assert actions[0]["intent"] == "settings.set"
        assert actions[0]["arguments"] == {
            "key": "notifications.global", "enabled": False,
        }
        assert any(item.get("code") == "stale_confirmation" for item in websocket.sent)

    asyncio.run(scenario())


def test_legacy_socket_confirms_direct_calendar_create_once():
    async def scenario():
        runtime = AppVoiceRuntime(settings=settings(), keys=[], provider=FakeProvider())
        app = FastAPI()
        app.include_router(create_router(runtime))
        routes = {
            route.path: route.endpoint
            for route in app.routes
            if hasattr(route, "endpoint")
        }
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        issued = await routes["/api/app-voice/session"](
            AppVoiceSessionRequest(
                installation_id="installation-id-123456",
                user_id="test-user-id",
                consent_version="demo-free-gemini-live-v2",
                consent_accepted_at="2026-09-10T12:00:00Z",
            ),
            request,
        )
        websocket = FakeWebSocket(
            {
                "type": "hello",
                "ticket": issued["ticket"],
                "context": {
                    "scope": "global",
                    "permissions": {
                        "public_ayue": True,
                        "calendar_read": True,
                        "calendar_write": True,
                    },
                },
            },
            [
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "utterance",
                    "text": "新增明天上午十點的籃球行程",
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "confirmation_response",
                    "confirmation_id": "__pending_confirmation__",
                    "spoken_phrase": "確認",
                    "accepted": True,
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "stop",
                })},
            ],
        )

        await routes["/api/app-voice"](websocket)

        actions = [
            item for item in websocket.sent
            if item.get("type") == "action_proposal"
        ]
        assert len(actions) == 1
        assert actions[0]["intent"] == "calendar.create"
        assert actions[0]["arguments"]["title"] == "籃球"
        assert actions[0]["arguments"]["start_time"] == "10:00"
        assert actions[0]["arguments"]["end_time"] == "11:00"

    asyncio.run(scenario())


def test_gemini_live_reply_streams_pcm_chunks_with_one_response_id():
    class LiveProvider(FakeProvider):
        gemini_available = True

        async def stream_synthesize(self, text):
            yield b"\x01\x02"
            yield b"\x03\x04"

    async def scenario():
        runtime = AppVoiceRuntime(settings=settings(), keys=[], provider=LiveProvider())
        app = FastAPI()
        app.include_router(create_router(runtime))
        routes = {
            route.path: route.endpoint
            for route in app.routes
            if hasattr(route, "endpoint")
        }
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        issued = await routes["/api/app-voice/session"](
            AppVoiceSessionRequest(
                installation_id="installation-id-123456",
                user_id="test-user-id",
                consent_version="demo-free-gemini-live-v2",
                consent_accepted_at="2026-09-10T12:00:00Z",
            ),
            request,
        )
        websocket = FakeWebSocket(
            {
                "type": "hello",
                "ticket": issued["ticket"],
                "output_mode": "gemini_live",
                "context": {"scope": "global", "revision": 0},
            },
            [
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "action_result", "success": True,
                    "message": "設定已開啟。",
                })},
                {"type": "websocket.receive", "text": json.dumps({"type": "stop"})},
            ],
        )

        await routes["/api/app-voice"](websocket)

        replies = [item for item in websocket.sent if item["type"] == "assistant_reply"]
        chunks = [item for item in websocket.sent if item["type"] == "audio_chunk"]
        completed = [item for item in websocket.sent if item["type"] == "audio_complete"]
        assert len(replies) == 1
        assert [item["sequence"] for item in chunks] == [0, 1]
        assert all(item["response_id"] == replies[0]["response_id"] for item in chunks)
        assert completed == [{
            "type": "audio_complete",
            "response_id": replies[0]["response_id"],
            "chunks": 2,
        }]

    asyncio.run(scenario())


def test_interrupt_cancels_live_audio_and_discards_remaining_chunks():
    class SlowLiveProvider(FakeProvider):
        gemini_available = True

        async def stream_synthesize(self, text):
            yield b"\x01\x02"
            await asyncio.sleep(1)
            yield b"\x03\x04"

    async def scenario():
        runtime = AppVoiceRuntime(settings=settings(), keys=[], provider=SlowLiveProvider())
        app = FastAPI()
        app.include_router(create_router(runtime))
        routes = {
            route.path: route.endpoint
            for route in app.routes
            if hasattr(route, "endpoint")
        }
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        issued = await routes["/api/app-voice/session"](
            AppVoiceSessionRequest(
                installation_id="installation-id-123456",
                user_id="test-user-id",
                consent_version="demo-free-gemini-live-v2",
                consent_accepted_at="2026-09-10T12:00:00Z",
            ),
            request,
        )
        websocket = FakeWebSocket(
            {
                "type": "hello",
                "ticket": issued["ticket"],
                "output_mode": "gemini_live",
                "context": {"scope": "global", "revision": 0},
            },
            [
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "action_result", "success": True,
                    "message": "準備播放。",
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "interrupt",
                })},
                {"type": "websocket.receive", "text": json.dumps({"type": "stop"})},
            ],
        )

        await routes["/api/app-voice"](websocket)

        chunks = [item for item in websocket.sent if item["type"] == "audio_chunk"]
        interrupted = [
            item for item in websocket.sent if item["type"] == "audio_interrupted"
        ]
        assert len(chunks) == 1
        assert len(interrupted) == 1
        assert interrupted[0]["response_id"] == chunks[0]["response_id"]
        assert not any(item["type"] == "audio_complete" for item in websocket.sent)

    asyncio.run(scenario())


def test_voice_mode_close_returns_an_explicit_client_close_signal():
    async def scenario():
        runtime = AppVoiceRuntime(settings=settings(), keys=[], provider=FakeProvider())
        app = FastAPI()
        app.include_router(create_router(runtime))
        routes = {
            route.path: route.endpoint
            for route in app.routes
            if hasattr(route, "endpoint")
        }
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        issued = await routes["/api/app-voice/session"](
            AppVoiceSessionRequest(
                installation_id="installation-id-123456",
                user_id="test-user-id",
                consent_version="demo-free-gemini-live-v2",
                consent_accepted_at="2026-09-10T12:00:00Z",
            ),
            request,
        )
        websocket = FakeWebSocket(
            {
                "type": "hello",
                "ticket": issued["ticket"],
                "context": {"scope": "global", "revision": 0},
            },
            [
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "utterance", "text": "關閉語音模式",
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "stop",
                })},
            ],
        )

        await routes["/api/app-voice"](websocket)

        replies = [item for item in websocket.sent if item["type"] == "assistant_reply"]
        assert any(item.get("code") == "voice_mode_closed" for item in replies)
        assert not any(item["type"] == "action_proposal" for item in websocket.sent)

    asyncio.run(scenario())


def test_non_confirmation_action_speaks_only_the_final_result_once():
    async def scenario():
        runtime = AppVoiceRuntime(settings=settings(), keys=[], provider=FakeProvider())
        app = FastAPI()
        app.include_router(create_router(runtime))
        routes = {
            route.path: route.endpoint
            for route in app.routes
            if hasattr(route, "endpoint")
        }
        request = SimpleNamespace(headers={}, client=SimpleNamespace(host="127.0.0.1"))
        issued = await routes["/api/app-voice/session"](
            AppVoiceSessionRequest(
                installation_id="installation-id-123456",
                user_id="test-user-id",
                consent_version="demo-free-gemini-live-v2",
                consent_accepted_at="2026-09-10T12:00:00Z",
            ),
            request,
        )
        websocket = FakeWebSocket(
            {
                "type": "hello",
                "ticket": issued["ticket"],
                "output_mode": "gemini_tts",
                "context": {"scope": "global", "revision": 0},
            },
            [
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "utterance", "text": "幫我開啟設定",
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "action_result", "success": True,
                    "message": "設定已開啟。",
                })},
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "stop",
                })},
            ],
        )

        await routes["/api/app-voice"](websocket)

        actions = [item for item in websocket.sent if item["type"] == "action_proposal"]
        replies = [item for item in websocket.sent if item["type"] == "assistant_reply"]
        unavailable = [
            item for item in websocket.sent if item["type"] == "audio_unavailable"
        ]
        assert len(actions) == 1
        assert len(replies) == 1
        assert replies[0]["text"] == "設定已開啟。"
        assert unavailable[0]["response_id"] == replies[0]["response_id"]

    asyncio.run(scenario())
