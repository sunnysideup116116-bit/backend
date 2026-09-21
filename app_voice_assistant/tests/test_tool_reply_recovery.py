"""Completed results must survive a missing Live turn_complete without replay."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from app_voice_assistant import duplex_runtime
from app_voice_assistant.tests.test_duplex_runtime import (
    FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, message, wait_until, _append,
)
from app_voice_assistant.tests.test_memory import (
    MemoryHttp, MemoryOllama, memory_settings,
)
from app_voice_assistant.memory import AppwriteVoiceMemoryService, VoiceOwner


@pytest.mark.parametrize("user_is_speaking", [False, True])
def test_finished_tool_recovers_lost_turn_end_only_after_user_finishes(monkeypatch, user_is_speaking):
    monkeypatch.setattr(duplex_runtime, "BACKGROUND_RESULT_IDLE_SECONDS", 0.04)

    async def scenario():
        live, socket, events = FakeLive(), FakeWebSocket(), []
        task = asyncio.create_task(duplex_runtime.run_duplex_session(
            socket, provider=FakeProvider(live), limiter=FakeLimiter(),
            identity="test", initial_context={
                "scope": "global", "revision": 0,
                "permissions": {"match_ayue": True, "match_read": True},
            }, max_session_seconds=30,
            send_event=lambda event: _append(events, event),
        ))
        try:
            await live.incoming.put(message(tool_calls=[SimpleNamespace(
                id="only-tool-call", name="ask_matching_ayue",
                args={"question": "目前進度？"},
            )]))
            await wait_until(lambda: any(e.get("intent") == "match.ayue_query" for e in events))
            action = next(e for e in events if e.get("intent") == "match.ayue_query")
            await live.incoming.put(message(content=SimpleNamespace(
                interim_input_transcription=None, input_transcription=None,
                output_transcription=SimpleNamespace(text="我找一下。"),
                interrupted=False, model_turn=SimpleNamespace(parts=[SimpleNamespace(
                    inline_data=SimpleNamespace(data=b"\x00\x01" * 128),
                )]), turn_complete=False,
            )))
            await wait_until(lambda: any(e.get("type") == "assistant_reply" for e in events))
            # Do not abandon a device action even after the model is idle.
            await asyncio.sleep(0.15)
            assert not any(e.get("stage") == "reply_recovering" for e in events)
            if user_is_speaking:
                await live.incoming.put(message(voice_activity=SimpleNamespace(voice_activity_type="ACTIVITY_START")))
                await wait_until(lambda: any(e.get("type") == "microphone_state" for e in events))
            await socket.incoming.put({"type": "websocket.receive", "text": json.dumps({
                "type": "action_result", "action_id": action["action_id"],
                "success": True, "message": "已有一則匿名邀請等待回覆。",
            })})
            if user_is_speaking:
                await asyncio.sleep(0.2)
                assert not any("[DELEGATED_AYUE_RESULT]" in t for t in live.text)
                await live.incoming.put(message(voice_activity=SimpleNamespace(voice_activity_type="ACTIVITY_END")))
            # Intentionally never send turn_complete.
            await wait_until(lambda: any("[DELEGATED_AYUE_RESULT]" in t for t in live.text))
            assert len([e for e in events if e.get("intent") == "match.ayue_query"]) == 1
            assert len([t for t in live.text if "[DELEGATED_AYUE_RESULT]" in t]) == 1
            assert any(e.get("stage") == "reply_recovering" for e in events)
        finally:
            await socket.incoming.put({"type": "websocket.receive", "text": '{"type":"stop"}'})
            await task

    asyncio.run(scenario())


def test_legacy_voice_summary_is_not_reused_or_sent_to_new_summarizer():
    http, model = MemoryHttp(), MemoryOllama()
    http.document.update(last_session_id="legacy-session", recent_summary="牽線候選人叫不可公開姓名")
    service = AppwriteVoiceMemoryService(memory_settings(), http_session=http, ollama_client=model)
    owner = VoiceOwner("owner", "新名字")
    memory = service.load_or_create(owner)
    assert memory.recent_summary == memory.older_summary == ""
    assert "不可公開姓名" in http.document["recent_summary"]  # read does not delete history
    saved = service.finalize_session(owner, "new-session", [{"role": "user", "content": "下次想去公園散步"}])
    assert "不可公開姓名" not in json.dumps(model.calls, ensure_ascii=False)
    assert saved.last_session_id == "anon2:new-session"
    assert service.load_or_create(owner).recent_summary == saved.recent_summary
