import asyncio

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
