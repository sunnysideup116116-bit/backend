import asyncio
import json
import time
from types import SimpleNamespace

import pytest

from app_voice_assistant import duplex_runtime
from app_voice_assistant.duplex_runtime import (
    pcm_frame_rms,
    run_duplex_session,
    sanitize_script_lines,
)


def message(*, tool_calls=None, content=None):
    return SimpleNamespace(
        go_away=None,
        voice_activity=None,
        server_content=content,
        tool_call=(
            SimpleNamespace(function_calls=tool_calls)
            if tool_calls is not None
            else None
        ),
        tool_call_cancellation=None,
        session_resumption_update=None,
    )


class FakeScriptLive:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.tool_responses = []
        self.audio = []
        self.text = []
        self.closed = False

    def is_non_blocking_tool(self, name):
        return False

    async def connect(self):
        return False

    async def reconnect(self):
        return True

    async def receive_turn(self):
        while True:
            yield await self.incoming.get()

    async def send_audio(self, data):
        self.audio.append(data)

    async def send_text(self, text):
        self.text.append(text)

    async def send_screen_context(self, payload):
        pass

    async def send_tool_response(self, *, call_id, name, response):
        self.tool_responses.append((call_id, name, response))

    async def close(self):
        self.closed = True


class FakeScriptProvider:
    def __init__(self, live):
        self.live = live
        self.synthesized = []

    def create_duplex_session(self, **_options):
        return self.live

    async def stream_synthesize(self, text):
        self.synthesized.append(text)
        for index in range(2):
            yield (b"\x01\x02") * 8 if text else b"\x00\x00" * 8


class FakeScriptLimiter:
    def __init__(self, *, allow=True):
        self.allow = allow

    def allow_tts(self, identity):
        return self.allow


class FakeScriptWebSocket:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.closed = False

    async def receive(self):
        return await self.incoming.get()

    async def close(self, code=1000):
        self.closed = True


def silence(ms=20):
    return b"\x00" * ms


def loud(ms=20):
    value = 8000
    sample = value.to_bytes(2, "little", signed=True)
    sample_count = ms
    return (sample * (sample_count // 2)) + (sample * (sample_count % 2))


async def run_script_scenario(monkeypatch, lines):
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
        identity="script-test",
        initial_context={},
        max_session_seconds=60,
        send_event=send_event,
        initial_script_lines=lines,
    ))
    return live, provider, socket, events, task


async def send_frame(socket, payload):
    await socket.incoming.put({"type": "websocket.receive", "bytes": payload})
    await asyncio.sleep(0)


async def wait_for_event(events, event_type, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(event.get("type") == event_type for event in events):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"missing event {event_type}: {events}")


def test_sanitize_script_lines():
    assert sanitize_script_lines(None) == []
    assert sanitize_script_lines("abc") == []
    assert sanitize_script_lines(["  ", "你好"]) == ["你好"]
    assert sanitize_script_lines(["a" * 400]) == ["a" * 300]
    many = [f"第{i}句" for i in range(20)]
    assert len(sanitize_script_lines(many)) == 10


def test_pcm_frame_rms():
    assert pcm_frame_rms(b"") == 0.0
    assert pcm_frame_rms(silence(20)) == 0.0
    assert pcm_frame_rms(loud(20)) > 1000.0
    assert pcm_frame_rms(b"\x01") == 0.0


def test_script_mode_uses_synthesize_and_advances(monkeypatch):
    async def scenario():
        lines = ["第一句台詞", "第二句台詞", "第三句台詞"]
        live, provider, socket, events, task = await run_script_scenario(
            monkeypatch, lines,
        )
        try:
            await send_frame(socket, silence(20))
            await wait_for_event(events, "script_mode_started")
            await wait_for_event(events, "assistant_reply")
            reply = [
                event for event in events
                if event["type"] == "assistant_reply"
            ][0]
            assert reply["text"] == "第一句台詞"
            assert reply["code"] == "script"
            assert reply["can_interrupt"] is False
            await wait_for_event(events, "audio_complete")
            complete = [
                event for event in events
                if event["type"] == "audio_complete"
            ][0]
            assert complete["script_line"] == 0
            assert complete["script_lines"] == 3
            assert provider.synthesized == ["第一句台詞"]

            assert not live.audio

            async def send_speech_input():
                await asyncio.sleep(0.06)
                await send_frame(socket, loud(20))
                await asyncio.sleep(0.08)
                await send_frame(socket, silence(20))
                await asyncio.sleep(0.02)

            for expected_line in (1, 2):
                spoken_before = len(provider.synthesized)
                await send_speech_input()
                await wait_for_event(events, "audio_complete", timeout=8)
                latest_complete = [
                    event for event in events
                    if event["type"] == "audio_complete"
                ][-1]
                assert latest_complete["script_line"] == expected_line
                assert len(provider.synthesized) == spoken_before + 1
            await send_speech_input()
            await wait_for_event(events, "script_mode_finished", timeout=8)
            assert provider.synthesized == lines
            finished = [
                event for event in events
                if event["type"] == "script_mode_finished"
            ][0]
            assert finished["lines_spoken"] == 3
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_script_mode_ignores_gemini_output(monkeypatch):
    async def scenario():
        live, provider, socket, events, task = await run_script_scenario(
            monkeypatch, ["唯一一句台詞"],
        )
        try:
            await live.incoming.put(message(content=None, tool_calls=[SimpleNamespace(
                id="x",
                name="ask_matching_ayue",
                args={"question": "q"},
            )]))
            await send_frame(socket, silence(20))
            await wait_for_event(events, "audio_complete")
            assert not any(
                event["type"] in {
                    "action_proposal",
                    "assistant_transcript",
                    "confirmation_required",
                }
                for event in events
            )
            assert not live.audio
            assert not live.tool_responses
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_script_mode_silence_does_not_advance(monkeypatch):
    async def scenario():
        live, provider, socket, events, task = await run_script_scenario(
            monkeypatch, ["第一句台詞", "第二句台詞"],
        )
        try:
            await send_frame(socket, silence(20))
            await wait_for_event(events, "audio_complete")
            for _ in range(40):
                await send_frame(socket, silence(20))
            await asyncio.sleep(0.3)
            assert provider.synthesized == ["第一句台詞"]
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_script_mode_extra_line_speaks_without_advancing(monkeypatch):
    async def scenario():
        live, provider, socket, events, task = await run_script_scenario(
            monkeypatch, ["第一句台詞", "第二句台詞"],
        )
        try:
            await send_frame(socket, silence(20))
            await wait_for_event(events, "audio_complete")
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({
                    "type": "script_line_request",
                    "text": "配對後的加播台詞",
                }),
            })
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and len([
                event for event in events if event["type"] == "audio_complete"
            ]) < 2:
                await asyncio.sleep(0.01)
            extra_completes = [
                event for event in events
                if event["type"] == "audio_complete" and "script_line" not in event
            ]
            assert extra_completes, events
            assert "配對後的加播台詞" in provider.synthesized
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({
                    "type": "utterance",
                    "text": "換下一句",
                }),
            })
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and len([
                event for event in events
                if event["type"] == "audio_complete" and "script_line" in event
            ]) < 2:
                await asyncio.sleep(0.01)
            completes = [
                event for event in events
                if event["type"] == "audio_complete" and "script_line" in event
            ]
            assert [item["script_line"] for item in completes] == [0, 1]
            assert provider.synthesized == [
                "第一句台詞",
                "配對後的加播台詞",
                "第二句台詞",
            ]
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({
                    "type": "utterance",
                    "text": "劇本結束吧",
                }),
            })
            await wait_for_event(events, "script_mode_finished", timeout=8)
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_script_mode_typed_utterance_advances(monkeypatch):
    async def scenario():
        live, provider, socket, events, task = await run_script_scenario(
            monkeypatch, ["第一句台詞", "第二句台詞"],
        )
        try:
            await send_frame(socket, silence(20))
            await wait_for_event(events, "audio_complete")
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "utterance", "text": "好"}),
            })
            await wait_for_event(events, "user_transcript")
            await wait_for_event(events, "audio_complete", timeout=8)
            assert provider.synthesized == ["第一句台詞", "第二句台詞"]
        finally:
            await socket.incoming.put({
                "type": "websocket.receive",
                "text": json.dumps({"type": "stop"}),
            })
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())