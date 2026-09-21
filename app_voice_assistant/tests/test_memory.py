import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from fastapi import FastAPI, HTTPException

from app_voice_assistant.duplex_session import _system_instruction
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.memory import (
    AppwriteVoiceMemoryService,
    VoiceMemoryError,
    VoiceMemoryRecord,
    VoiceMemorySettings,
    VoiceOwner,
    bounded_transcript,
    normalize_internal_appwrite_endpoint,
)
from app_voice_assistant.router import AppVoiceRuntime, AppVoiceSessionRequest, create_router
from app_voice_assistant.settings import AppVoiceSettings
from .test_api import FakeProvider, FakeWebSocket, settings
from .test_duplex_runtime import (
    FakeLimiter,
    FakeLive,
    FakeProvider as DuplexFakeProvider,
    FakeWebSocket as DuplexFakeWebSocket,
    _append,
    message,
    wait_until,
)


@pytest.fixture(autouse=True)
def run_blocking_calls_inline(monkeypatch):
    """The sandboxed pytest runner cannot start asyncio's default executor."""

    async def run_inline(function, *args):
        return function(*args)

    monkeypatch.setattr(asyncio, "to_thread", run_inline)


class Response:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


class MemoryHttp:
    def __init__(self):
        self.calls = []
        self.document = {
            "$id": "owner",
            "user_id": "owner",
            "username": "舊名字",
            "older_summary": "舊名字以前在聊旅行",
            "recent_summary": "最近想約小安喝咖啡",
            "revision": 2,
            "last_session_id": "anon2:old-session",
        }

    def request(self, method, url, **kwargs):
        path = urlsplit(url).path
        self.calls.append((method, url, kwargs))
        if path.endswith("/account"):
            return Response(200, {"$id": "owner", "name": "帳號名稱"})
        if path.endswith("/databases/dating_db/collections/user_profiles/documents/owner"):
            return Response(200, {"$id": "owner", "name": "新名字"})
        if path.endswith("/databases/voice_memory/collections/voice_session_memories/documents/owner"):
            if method == "GET":
                return Response(200, dict(self.document))
            self.document.update(kwargs["json"]["data"])
            return Response(200, dict(self.document))
        raise AssertionError((method, path))


class MemoryOllama:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "message": {
                "content": json.dumps({
                    "older_summary": "舊" * 90,
                    "recent_summary": "近期保留較多細節" * 30,
                }, ensure_ascii=False),
            }
        }


class OwnerNameOllama:
    def __init__(self):
        self.calls = []

    def chat(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "message": {
                "content": json.dumps({
                    "older_summary": "Candy 喜歡爬山",
                    "recent_summary": "Candy 決定週日喝咖啡，小安喜歡看電影",
                }, ensure_ascii=False),
            }
        }


class FailingMemoryOllama:
    def __init__(self):
        self.calls = 0

    def chat(self, **kwargs):
        self.calls += 1
        raise RuntimeError("temporary ollama failure")


class MarkdownClosingOllama:
    def chat(self, **kwargs):
        return {
            "message": {
                "content": """```json
{"older_summary":"之前聊旅行","recent_summary":"使用者想喝咖啡；阿月：好，語音模式已關閉。","extra":"ignored"}
```""",
            },
        }


def memory_settings(endpoint="https://127.0.0.1/v1"):
    normalized, verify = normalize_internal_appwrite_endpoint(endpoint)
    return VoiceMemorySettings(
        endpoint=normalized,
        verify_tls=verify,
        project_id="project",
        api_key="server-key",
        profile_database_id="dating_db",
        profile_collection_id="user_profiles",
        memory_database_id="voice_memory",
        memory_collection_id="voice_session_memories",
        ollama_host="https://ollama.invalid",
        ollama_api_key="ollama-key",
        ollama_model="deepseek-test",
        request_timeout_seconds=2,
        ollama_timeout_seconds=5,
    )


@pytest.mark.parametrize("endpoint", [
    "https://appwrite.misproject.us.ci/v1",
    "https://example.com/v1",
    "http://8.8.8.8/v1",
])
def test_public_appwrite_endpoints_are_rejected(endpoint):
    with pytest.raises(VoiceMemoryError) as error:
        normalize_internal_appwrite_endpoint(endpoint)
    assert error.value.code == "voice_memory_internal_endpoint_required"


def test_loopback_port_80_is_normalized_without_external_redirects():
    endpoint, verify = normalize_internal_appwrite_endpoint("http://127.0.0.1:80/v1")
    assert endpoint == "https://127.0.0.1/v1"
    assert verify is False


def test_enabling_memory_defaults_to_a_new_consent_version():
    configured = AppVoiceSettings.from_env({"VOICE_MEMORY_ENABLED": "on"})

    assert configured.consent_version == "demo-free-gemini-live-ollama-memory-v1"


def test_username_sync_uses_verified_user_id_and_only_internal_appwrite():
    http = MemoryHttp()
    service = AppwriteVoiceMemoryService(
        memory_settings(), http_session=http, ollama_client=MemoryOllama(),
    )
    owner, memory = service.authenticate_and_load("jwt-value", "owner")

    assert owner == VoiceOwner("owner", "新名字")
    assert memory.revision == 2
    assert http.document["username"] == "新名字"
    assert "舊名字" not in memory.older_summary
    assert "使用者" in memory.older_summary
    assert all(urlsplit(call[1]).hostname == "127.0.0.1" for call in http.calls)
    assert all(call[2]["allow_redirects"] is False for call in http.calls)
    account_headers = http.calls[0][2]["headers"]
    assert account_headers["X-Appwrite-JWT"] == "jwt-value"
    assert "X-Appwrite-Key" not in account_headers


def test_verified_user_must_match_claimed_user_id():
    service = AppwriteVoiceMemoryService(
        memory_settings(), http_session=MemoryHttp(), ollama_client=MemoryOllama(),
    )
    with pytest.raises(VoiceMemoryError) as error:
        service.authenticate_owner("jwt-value", "someone-else")
    assert error.value.code == "voice_memory_user_mismatch"


def test_two_layer_summary_is_bounded_recursive_and_idempotent():
    http = MemoryHttp()
    ollama = MemoryOllama()
    service = AppwriteVoiceMemoryService(
        memory_settings(), http_session=http, ollama_client=ollama,
    )
    owner = VoiceOwner("owner", "新名字")
    turns = [
        {"role": "user", "content": "改成星期日下午四點，地點想找信義區安靜的咖啡店"},
        {"role": "assistant", "content": "好，我們還沒有確定地點。"},
    ]

    saved = service.finalize_session(owner, "session-1", turns)
    repeated = service.finalize_session(owner, "session-1", turns)

    assert len(saved.older_summary) == 70
    assert len(saved.recent_summary) == 120
    assert len(saved.older_summary) + len(saved.recent_summary) <= 200
    assert saved.revision == 3
    assert repeated == saved
    assert len(ollama.calls) == 1
    prompt = ollama.calls[0]["messages"][1]["content"]
    assert "以前在聊旅行" in prompt
    assert "最近想約小安喝咖啡" in prompt
    assert "星期日下午四點" in prompt
    assert ollama.calls[0]["options"]["num_predict"] == 512


def test_summary_never_keeps_the_signed_in_users_name():
    http = MemoryHttp()
    http.document.update({
        "username": "Candy",
        "older_summary": "Candy 之前想去旅行",
        "recent_summary": "Candy 最近和小安聊電影",
    })
    ollama = OwnerNameOllama()
    service = AppwriteVoiceMemoryService(
        memory_settings(), http_session=http, ollama_client=ollama,
    )

    saved = service.finalize_session(
        VoiceOwner("owner", "Candy"),
        "session-with-name",
        [
            {"role": "user", "content": "我是 Candy，我週日想喝咖啡"},
            {"role": "assistant", "content": "Candy 可以和小安確認時間。"},
        ],
    )

    combined = saved.older_summary + saved.recent_summary
    assert "Candy" not in combined
    assert "使用者" in combined
    assert "小安" in combined
    prompt = ollama.calls[0]["messages"][1]["content"]
    assert "Candy" not in prompt
    assert "小安" in prompt


def test_transcript_redacts_secrets_and_greeting_only_does_not_generate():
    projected = bounded_transcript([
        {"role": "user", "content": "密碼是 secret123，電話 0912345678，Email a@example.com"},
    ])
    encoded = json.dumps(projected, ensure_ascii=False)
    assert "secret123" not in encoded
    assert "0912345678" not in encoded
    assert "a@example.com" not in encoded

    http = MemoryHttp()
    ollama = MemoryOllama()
    service = AppwriteVoiceMemoryService(
        memory_settings(), http_session=http, ollama_client=ollama,
    )
    current = service.finalize_session(
        VoiceOwner("owner", "新名字"), "greeting", [{"role": "user", "content": "你好"}],
    )
    assert current.revision == 2
    assert ollama.calls == []


def test_voice_close_turns_are_never_written_to_memory():
    projected = bounded_transcript([
        {"role": "user", "content": "我最近想去安靜的咖啡店"},
        {"role": "assistant", "content": "可以找週日下午的時間。"},
        {"role": "user", "content": "結束語音模式"},
        {"role": "assistant", "content": "好，語音模式已關閉。"},
    ])

    assert projected == [
        {"role": "user", "content": "我最近想去安靜的咖啡店"},
        {"role": "assistant", "content": "可以找週日下午的時間"},
    ]

    http = MemoryHttp()
    ollama = MemoryOllama()
    service = AppwriteVoiceMemoryService(
        memory_settings(), http_session=http, ollama_client=ollama,
    )
    unchanged = service.finalize_session(
        VoiceOwner("owner", "新名字"),
        "close-only",
        [
            {"role": "user", "content": "結束語音模式"},
            {"role": "assistant", "content": "好的，語音結束了。"},
        ],
    )

    assert unchanged.revision == 2
    assert unchanged.last_session_id == "anon2:old-session"
    assert ollama.calls == []


def test_deepseek_failure_uses_bounded_fallback_and_still_writes_appwrite():
    http = MemoryHttp()
    ollama = FailingMemoryOllama()
    service = AppwriteVoiceMemoryService(
        memory_settings(), http_session=http, ollama_client=ollama,
    )

    saved = service.finalize_session(
        VoiceOwner("owner", "新名字"),
        "fallback-session",
        [
            {"role": "user", "content": "我最近想去安靜的咖啡店"},
            {"role": "assistant", "content": "可以找週日下午的時間。"},
            {"role": "user", "content": "結束語音模式"},
            {"role": "assistant", "content": "好，語音模式已關閉。"},
        ],
    )

    combined = saved.older_summary + saved.recent_summary
    assert saved.revision == 3
    assert saved.last_session_id == "anon2:fallback-session"
    assert ollama.calls == 2
    assert "咖啡店" in combined
    assert "語音模式" not in combined
    assert len(saved.older_summary) <= 70
    assert len(saved.recent_summary) <= 120
    assert len(combined) <= 200


def test_existing_and_model_generated_close_boilerplate_are_removed():
    http = MemoryHttp()
    http.document["recent_summary"] = (
        "最近想約小安喝咖啡；阿月：好，語音模式已關閉。"
    )
    service = AppwriteVoiceMemoryService(
        memory_settings(),
        http_session=http,
        ollama_client=MarkdownClosingOllama(),
    )

    loaded = service.load_or_create(VoiceOwner("owner", "新名字"))
    saved = service.finalize_session(
        VoiceOwner("owner", "新名字"),
        "markdown-summary",
        [{"role": "user", "content": "我還是想找咖啡店"}],
    )

    assert "語音模式" not in loaded.recent_summary
    assert "語音模式" not in saved.recent_summary
    assert "咖啡" in loaded.recent_summary
    assert "咖啡" in saved.recent_summary
    assert saved.revision == 3


def test_long_transcript_keeps_the_most_recent_turns():
    turns = [
        {"role": "user", "content": f"較早內容{i:02d}" + "舊" * 990}
        for i in range(20)
    ]
    turns.append({"role": "user", "content": "最新決定是星期日下午四點"})

    projected = bounded_transcript(turns)
    content = "".join(turn["content"] for turn in projected)

    assert "最新決定是星期日下午四點" in content
    assert len(content) <= 12_000
    assert "較早內容00" not in content


@pytest.mark.parametrize("turn_complete", [True, False])
def test_duplex_remembers_user_when_finished_flag_never_becomes_true(
    turn_complete,
):
    async def scenario():
        live = FakeLive()
        socket = DuplexFakeWebSocket()
        events = []
        turns = []
        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=DuplexFakeProvider(live),
            limiter=FakeLimiter(),
            identity="test",
            initial_context={},
            max_session_seconds=30,
            send_event=lambda event: _append(events, event),
            memory_turns=turns,
        ))
        await live.incoming.put(message(content=SimpleNamespace(
            interim_input_transcription=None,
            input_transcription=SimpleNamespace(
                text="我週日想去喝咖啡",
                finished=False,
            ),
            output_transcription=None,
            interrupted=False,
            model_turn=None,
            turn_complete=turn_complete,
        )))
        await wait_until(lambda: any(
            event.get("type") == "user_transcript" for event in events
        ))
        await socket.incoming.put({"type": "websocket.disconnect"})
        await task

        user_turns = [turn for turn in turns if turn["role"] == "user"]
        assert user_turns == [{
            "role": "user",
            "content": "我週日想去喝咖啡",
        }]

    asyncio.run(scenario())


class RuntimeMemory:
    def __init__(self):
        self.finalized = []

    def authenticate_and_load(self, jwt, user_id):
        if jwt != "valid-jwt":
            raise VoiceMemoryError("voice_memory_authentication_failed")
        return VoiceOwner(user_id, "同步名稱"), VoiceMemoryRecord(
            older_summary="舊摘要", recent_summary="近期摘要", revision=4,
        )

    def finalize_session(self, owner, session_id, turns):
        self.finalized.append((owner, session_id, turns))
        return VoiceMemoryRecord(revision=5, last_session_id=session_id)


def memory_runtime():
    configured = replace(
        settings(), memory_enabled=True,
        consent_version="demo-free-gemini-live-ollama-memory-v1",
    )
    memory = RuntimeMemory()
    return AppVoiceRuntime(
        settings=configured,
        keys=[],
        provider=FakeProvider(),
        memory_service=memory,
    ), memory


def routes_for(runtime):
    app = FastAPI()
    app.include_router(create_router(runtime))
    return {route.path: route.endpoint for route in app.routes if hasattr(route, "endpoint")}


def request(headers=None):
    return SimpleNamespace(
        headers=headers or {}, client=SimpleNamespace(host="127.0.0.1"),
    )


def session_request():
    return AppVoiceSessionRequest(
        installation_id="installation-id-123456",
        user_id="test-user-id",
        consent_version="demo-free-gemini-live-ollama-memory-v1",
        consent_accepted_at="2026-09-12T12:00:00Z",
    )


def test_memory_enabled_session_requires_a_verified_bearer_token():
    runtime, _ = memory_runtime()
    route = routes_for(runtime)["/api/app-voice/session"]
    with pytest.raises(HTTPException) as error:
        asyncio.run(route(session_request(), request()))
    assert error.value.status_code == 401


@pytest.mark.parametrize("end_message", [
    {"type": "websocket.receive", "text": json.dumps({"type": "stop"})},
    {"type": "websocket.disconnect"},
])
def test_normal_stop_and_abrupt_disconnect_finalize_server_transcript(end_message):
    async def scenario():
        runtime, memory = memory_runtime()
        routes = routes_for(runtime)
        issued = await routes["/api/app-voice/session"](
            session_request(), request({"authorization": "Bearer valid-jwt"}),
        )
        socket = FakeWebSocket(
            {"type": "hello", "ticket": issued["ticket"],
             "context": {"scope": "global", "revision": 0}},
            [
                {"type": "websocket.receive", "text": json.dumps({
                    "type": "utterance", "text": "我週日想去喝咖啡",
                })},
                end_message,
            ],
        )
        await routes["/api/app-voice"](socket)
        await runtime.wait_memory_tasks()
        assert len(memory.finalized) == 1
        owner, session_id, turns = memory.finalized[0]
        assert owner == VoiceOwner("test-user-id", "同步名稱")
        assert len(session_id) == 32
        assert any(turn["role"] == "user" and "週日" in turn["content"] for turn in turns)
        assert any(turn["role"] == "assistant" for turn in turns)

    asyncio.run(scenario())


def test_capability_reports_private_appwrite_memory_contract():
    runtime, _ = memory_runtime()
    capability = asyncio.run(routes_for(runtime)["/api/app-voice/capability"]())
    assert capability["session_memory"] is True
    assert capability["session_memory_storage"] == "appwrite_internal"
    assert capability["session_memory_max_chars"] == 200
    assert "shared_date_form_confirm" in capability["supported_intents"]


def test_duplex_instruction_marks_memory_as_untrusted_context():
    instruction = _system_instruction(
        {"language": "zh-TW"},
        VoiceMemoryRecord(
            older_summary="之前喜歡爬山",
            recent_summary="最近想找安靜的咖啡店",
        ).prompt_text(),
    )

    assert "之前喜歡爬山" in instruction
    assert "最近想找安靜的咖啡店" in instruction
    assert "不可信" in instruction
    assert "工具" in instruction
    assert "上一段或剛才的語音對話" in instruction
    assert "直接使用 SERVER_VOICE_CONVERSATION_MEMORY" in instruction
