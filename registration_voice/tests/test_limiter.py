from dataclasses import replace

from registration_voice.limiter import LocalVoiceLimiter
from registration_voice.settings import VoiceRegistrationSettings


def test_local_rate_limiter_is_a_noop_while_env_switch_is_off():
    settings = VoiceRegistrationSettings.from_env({
        "VOICE_LOCAL_RATE_LIMIT_ENABLED": "off",
        "VOICE_RATE_LIMIT_PER_10_MINUTES": "1",
        "VOICE_RATE_LIMIT_GLOBAL_CONCURRENCY": "1",
    })
    limiter = LocalVoiceLimiter(settings)

    assert all(limiter.allow_start("same", now=100) for _ in range(5))
    assert limiter.acquire() is True
    assert limiter.acquire() is True


def test_local_rate_limiter_enforces_config_when_switched_on():
    settings = replace(
        VoiceRegistrationSettings.from_env({}),
        local_rate_limit_enabled=True,
        per_identity_per_ten_minutes=2,
        per_identity_per_day=3,
        global_concurrency=1,
    )
    limiter = LocalVoiceLimiter(settings)

    assert limiter.allow_start("same", now=100) is True
    assert limiter.allow_start("same", now=101) is True
    assert limiter.allow_start("same", now=102) is False
    assert limiter.acquire() is True
    assert limiter.acquire() is False
    limiter.release()
    assert limiter.acquire() is True
