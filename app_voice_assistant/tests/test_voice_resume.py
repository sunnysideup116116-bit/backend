import asyncio
import json
import time

import pytest

from app_voice_assistant import duplex_runtime
from app_voice_assistant.duplex_runtime import (
    run_duplex_session,
    sanitize_recent_turns,
    session_resumed_prompt,
)
from app_voice_assistant.tests.test_voice_script import (
    FakeScriptLive,
    FakeScriptLimiter,
    FakeScriptProvider,
    FakeScriptWebSocket,
    silence,
)


def _append(items, item):
    items.append(item)


async def run_scenario(
    monkeypatch,
    *,
    resume_voice=False,
    recent_turns=None,
):
    monkeypatch.setattr(
        duplex_runtime, "SCRIPT_SILENCE_SECONDS", 0.04
    )
    monkeypatch.setattr(
        duplex_runtime, "SCRIPT_MIN_SPEECH_SECONDS", 0.02
    )
    monkeypatch.setattr(
        duplex_runtime, "SCRIPT_ECHO_GRACE_SECONDS", 0.02
    )
    events = []
    live = FakeScriptLive()
    provider = FakeScriptProvider(live)
    socket = FakeScriptWebSocket()

    async def send_event(event):
        events.append(event)

    task = asyncio.create_task(run_duplex_session(
        socket,
        provider=provider,
        limiter=FakeScriptLimiter(),
        identity="resume-test",
        initial_context={},
        max_session_seconds=60,
        send_event=send_event,
        resume_voice=resume_voice,
        recent_turns=list(sanitize_recent_turns(recent_turns or [])),
    ))
    return live, provider, socket, events, task


async def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def test_sanitize_recent_turns():
    assert sanitize_recent_turns(None) == []
    assert sanitize_recent_turns("bad") == []
    assert sanitize_recent_turns([
        {"role": "user", "text": "你好"},
        {"role": "assistant", "content": "好的"},
        {"role": "system", "text": "ignore"},
        [{"not": "a map"}],
    ]) == [
        {"role": "user", "content": "你好"},
        {"role": "assistant", "content": "好的"},
    ]
    assert sanitize_recent_turns([
        {"role": "user", "text": "x" * 600},
    ]) == [{"role": "user", "content": "x" * 500}]
    assert len(sanitize_recent_turns([
        {"role": "user", "text": f"t{i}"}
        for i in range(20)
    ])) == 8


def test_session_resumed_prompt_never_greets_and_carries_turns():
    prompt = session_resumed_prompt(
        {"voice_config": {"response_language": "zh-TW"}},
        [
            {"role": "user", "content": "我們剛剛在聊打球的時間"},
            {"role": "assistant", "content": "我建議晚一點再去"},
        ],
    )
    assert "[VOICE_SESSION_RESUMED]" in prompt
    assert "不要重新打招呼" in prompt
    assert "我們剛剛在聊打球的時間" in prompt
    assert "我建議晚一點再去" in prompt
    started = duplex_runtime.session_started_prompt(
        {"voice_config": {"response_language": "zh-TW"}}
    )
    assert "主動打招呼" in started


def test_session_resumed_prompt_caps_turn_lines():
    turns = [
        {"role": "user", "content": "x" * 300},
    ]
    prompt = duplex_runtime._resume_turn_lines(turns)
    assert "x" * 300 not in prompt
    assert "x" * 200 in prompt
    many = duplex_runtime._resume_turn_lines([
        {"role": "user", "content": f"第{i}句"} for i in range(30)
    ])
    assert "第7句" in many
    assert "第8句" not in many


def test_resume_voice_sends_resumed_prompt(monkeypatch):
    async def scenario():
        live, provider, socket, events, task = await run_scenario(
            monkeypatch,
            resume_voice=True,
            recent_turns=[
                {"role": "user", "text": "剛剛在聊打球"},
                {"role": "assistant", "text": "我推薦了一個人"},
            ],
        )
        try:
            await wait_until(
                lambda: bool(live.text),
            )
            joined = "\n".join(live.text)
            assert "[VOICE_SESSION_STARTED]" not in joined
            assert "[VOICE_SESSION_RESUMED]" in joined
            assert "剛剛在聊打球" in joined
            assert "我推薦了一個人" in joined
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_fresh_voice_session_sends_started_prompt(monkeypatch):
    async def scenario():
        live, provider, socket, events, task = await run_scenario(
            monkeypatch,
        )
        try:
            await wait_until(lambda: bool(live.text))
            joined = "\n".join(live.text)
            assert "[VOICE_SESSION_STARTED]" in joined
            assert "主動打招呼" in joined
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_send_audio_failure_during_reconnect_does_not_close_session(
    monkeypatch,
):
    async def scenario():
        class BrokenAudioLive(FakeScriptLive):
            async def send_audio(self, data):
                raise RuntimeError("gemini_live_not_connected")

            async def receive_turn(self):
                while True:
                    await asyncio.sleep(0.02)
                    yield

        live = BrokenAudioLive()
        provider = FakeScriptProvider(live)
        socket = FakeScriptWebSocket()
        events = []

        async def send_event(event):
            events.append(event)

        task = asyncio.create_task(run_duplex_session(
            socket,
            provider=provider,
            limiter=FakeScriptLimiter(),
            identity="resume-test",
            initial_context={},
            max_session_seconds=60,
            send_event=send_event,
        ))
        try:
            await asyncio.sleep(0.05)
            for _ in range(10):
                await socket.incoming.put({
                    "type": "websocket.receive",
                    "bytes": silence(20),
                })
                await asyncio.sleep(0.02)
            assert task.done() is False
            errors = [
                event for event in events
                if event["type"] == "error"
            ]
            assert errors == []
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await asyncio.wait_for(task, timeout=5)
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            try:
                await asyncio.wait_for(task, timeout=5)
            except Exception:
                pass

    asyncio.run(scenario())