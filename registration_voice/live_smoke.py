"""Opt-in synthetic-audio smoke test for the real Gemini Live endpoint.

Run from Server with VOICE_LIVE_SMOKE=1. The script never prints or
persists API keys or audio and uses only synthetic registration data.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from array import array

from google import genai

from .contracts import safe_form, validate_function_patch
from .provider import GeminiLiveProvider
from .settings import VoiceRegistrationSettings, collect_google_api_keys


SYNTHETIC_TEXT = (
    "我的暱稱是小歌，不是小哥。歌是歌曲的歌，哥字加上欠字。"
    "我今年二十五歲，住在台東縣，"
    "平常喜歡看電影和跟朋友打球。"
)


def _resample_pcm16_mono(data: bytes, source_rate: int, target_rate: int) -> bytes:
    samples = array("h")
    samples.frombytes(data[: len(data) - (len(data) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples or source_rate == target_rate:
        return data

    output_length = max(1, int(len(samples) * target_rate / source_rate))
    result = array("h")
    for output_index in range(output_length):
        position = output_index * source_rate / target_rate
        lower = min(int(position), len(samples) - 1)
        upper = min(lower + 1, len(samples) - 1)
        fraction = position - lower
        value = round(samples[lower] * (1 - fraction) + samples[upper] * fraction)
        result.append(max(-32768, min(value, 32767)))
    if sys.byteorder != "little":
        result.byteswap()
    return result.tobytes()


def _generate_synthetic_audio(key: str) -> bytes:
    client = genai.Client(api_key=key)
    try:
        interaction = client.interactions.create(
            model="gemini-3.1-flash-tts-preview",
            input=f"請用自然清楚的台灣繁體中文，只朗讀以下內容：{SYNTHETIC_TEXT}",
            response_format={"type": "audio"},
            generation_config={"speech_config": [{"voice": "Kore"}]},
        )
        output_audio = getattr(interaction, "output_audio", None)
        encoded = getattr(output_audio, "data", None)
        if encoded is None:
            raise RuntimeError("TTS response did not contain audio")
        pcm_24k = base64.b64decode(encoded) if isinstance(encoded, str) else bytes(encoded)
        return _resample_pcm16_mono(pcm_24k, 24000, 16000)
    finally:
        client.close()


async def _run() -> dict:
    if os.getenv("VOICE_LIVE_SMOKE", "").strip().lower() not in {"1", "true", "on"}:
        raise RuntimeError("Set VOICE_LIVE_SMOKE=1 to authorize the live smoke test")
    keys = collect_google_api_keys()
    if not keys:
        raise RuntimeError("No Google API key is configured")

    audio = await asyncio.to_thread(_generate_synthetic_audio, keys[0])
    settings = VoiceRegistrationSettings.from_env()
    provider = GeminiLiveProvider(settings)
    form = safe_form({})
    provider_key = keys[1] if len(keys) > 1 else keys[0]

    async with provider.connect(provider_key, form, 0) as connection:
        for offset in range(0, len(audio), 3200):
            await connection.send_audio(audio[offset : offset + 3200])
            await asyncio.sleep(0.08)
        await connection.send_audio_stream_end()

        async with asyncio.timeout(35):
            async for event in connection.events():
                if event.type != "tool_calls":
                    continue
                for call in event.tool_calls:
                    if call.name != "propose_registration_patch":
                        continue
                    decision = validate_function_patch(call.args, current_revision=0)
                    await connection.send_tool_result(call, {
                        "applied": bool(decision.changes),
                        "current_revision": 1 if decision.changes else 0,
                    })
                    checks = {
                        "nickname": decision.changes.get("nickname") == "小歌",
                        "age": decision.changes.get("age") == 25,
                        "region": decision.changes.get("region") == "台東縣",
                        "interest": all(
                            word in str(decision.changes.get("interest") or "")
                            for word in ("看電影", "打球")
                        ),
                    }
                    return {
                        "success": all(checks.values()),
                        "checks": checks,
                        "fields": sorted(decision.changes),
                        "changes": decision.changes,
                        "rejected": list(decision.rejected),
                        "warnings": list(decision.warnings),
                    }
    raise RuntimeError("Gemini Live returned no registration tool call")


def main() -> int:
    result = asyncio.run(_run())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
