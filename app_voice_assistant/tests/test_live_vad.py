import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from google import genai

from app_voice_assistant.duplex_session import AppVoiceDuplexSession
from app_voice_assistant.settings import AppVoiceSettings


@pytest.mark.parametrize("value, expected", [
    (None, 700), ("450", 450), ("1100", 1100), ("invalid", 700),
    ("", 700), ("-1", 200), ("99999", 2000),
])
def test_live_vad_environment_is_bounded(value, expected):
    env = {} if value is None else {"VOICE_APP_VAD_SILENCE_MS": value}
    assert AppVoiceSettings.from_env(env).vad_silence_duration_ms == expected


@pytest.mark.parametrize("silence_ms", [450, 700, 1100])
def test_live_handshake_and_reconnect_use_configured_pause_without_changing_audio(monkeypatch, silence_ms):
    configs = []

    @asynccontextmanager
    async def connect(*, model, config):
        configs.append(config)
        yield SimpleNamespace()

    monkeypatch.setattr(genai, "Client", lambda **kwargs: SimpleNamespace(
        aio=SimpleNamespace(live=SimpleNamespace(connect=connect)), close=lambda: None,
    ))
    settings = AppVoiceSettings.from_env({"VOICE_APP_VAD_SILENCE_MS": str(silence_ms)})
    keys = SimpleNamespace(
        candidates=lambda: [SimpleNamespace(key="synthetic-key")],
        mark_success=lambda key: None,
    )

    async def scenario():
        live = AppVoiceDuplexSession(settings, keys, routing_mode="template")
        await live.connect()
        await live.reconnect()
        await live.close()

    asyncio.run(scenario())
    assert len(configs) == 2
    for config in configs:
        realtime = config.realtime_input_config
        assert realtime.automatic_activity_detection.silence_duration_ms == silence_ms
        assert realtime.automatic_activity_detection.prefix_padding_ms == 120
        assert realtime.activity_handling.value == "START_OF_ACTIVITY_INTERRUPTS"
        assert config.response_modalities[0].value == "AUDIO"
        assert len(config.tools[0].function_declarations) == 21
