import asyncio
from types import SimpleNamespace

from app_voice_assistant.contracts import VoiceProposal
from app_voice_assistant.duplex_session import AppVoiceDuplexSession
from app_voice_assistant.provider import AppVoiceProvider
from app_voice_assistant.settings import AppVoiceSettings


def test_repeated_generic_tts_reply_uses_the_bounded_memory_cache():
    provider = AppVoiceProvider(AppVoiceSettings.from_env({}), ["test-key"])
    calls = []

    def synthesize(text):
        calls.append(text)
        return {"mime_type": "audio/wav", "base64": "dGVzdA=="}

    async def run_inline(function, *args):
        return function(*args)

    async def scenario():
        original = asyncio.to_thread
        asyncio.to_thread = run_inline
        try:
            first = await provider.synthesize("設定已更新。")
            second = await provider.synthesize("設定已更新。")
            return first, second
        finally:
            asyncio.to_thread = original

    provider._synthesize_sync = synthesize
    first, second = asyncio.run(scenario())

    assert first == second
    assert calls == ["設定已更新。"]


def test_non_blocking_tool_response_uses_when_idle_scheduling():
    class RawSession:
        def __init__(self):
            self.responses = []

        async def send_tool_response(self, *, function_responses):
            self.responses.append(function_responses)

    settings = AppVoiceSettings.from_env({
        "VOICE_APP_ASYNC_TOOLS_ENABLED": "on",
        "VOICE_APP_NON_BLOCKING_TOOLS": "read_weather,ask_app_ayue",
    })
    live = AppVoiceDuplexSession(settings, SimpleNamespace())
    raw = RawSession()
    live._session = raw

    asyncio.run(live.send_tool_response(
        call_id="weather-call",
        name="read_weather",
        response={"status": "ok"},
    ))
    weather = raw.responses[-1]
    assert weather.scheduling.value == "WHEN_IDLE"
    assert weather.will_continue is False

    asyncio.run(live.send_tool_response(
        call_id="write-call",
        name="write_app_action",
        response={"status": "confirmation_required"},
    ))
    write = raw.responses[-1]
    assert write.scheduling is None
    assert write.will_continue is None


def test_only_unresolved_natural_calendar_ranges_use_the_model():
    provider = AppVoiceProvider(AppVoiceSettings.from_env({}), ["test-key"])
    calls = []

    async def interpret(text, *, context, audio=None):
        calls.append(text)
        return VoiceProposal(
            "calendar.query",
            {"start_date": "2025-03-01", "end_date": "2025-05-31"},
            "我查看這段期間。",
            0,
        )

    provider._gemini_turn = interpret

    natural = asyncio.run(provider.interpret_text(
        "查去年三月到五月的行事曆",
        context={"revision": 0},
    ))
    preset = asyncio.run(provider.interpret_text(
        "查今天的行事曆",
        context={"revision": 0},
    ))

    assert natural is not None
    assert natural.arguments == {
        "start_date": "2025-03-01",
        "end_date": "2025-05-31",
        "source": "all",
    }
    assert preset is not None
    assert preset.arguments == {"source": "all", "range": "today"}
    assert calls == ["查去年三月到五月的行事曆"]
