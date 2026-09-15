import asyncio

from app_voice_assistant.contracts import VoiceProposal
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
