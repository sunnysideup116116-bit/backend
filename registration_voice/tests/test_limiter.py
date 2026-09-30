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


def test_local_rate_limiter_is_enabled_when_the_switch_is_unspecified():
    assert VoiceRegistrationSettings.from_env({}).local_rate_limit_enabled is True


def test_changing_installations_does_not_reset_the_ip_budget():
    configured = replace(
        VoiceRegistrationSettings.from_env({}),
        per_identity_per_ten_minutes=2,
        per_identity_per_day=3,
    )
    limiter = LocalVoiceLimiter(configured)

    assert limiter.allow_start("installation-a", client_ip_identity="ip-a", now=100)
    assert limiter.allow_start("installation-b", client_ip_identity="ip-a", now=101)
    assert not limiter.allow_start("installation-c", client_ip_identity="ip-a", now=102)
    # A denied shared-IP claim must not consume the installation's own budget.
    assert limiter.allow_start("installation-c", client_ip_identity="ip-b", now=103)
    assert limiter.allow_start("installation-d", client_ip_identity="ip-a", now=702)
    assert not limiter.allow_start("installation-e", client_ip_identity="ip-a", now=703)
    assert limiter.allow_start("installation-e", client_ip_identity="ip-a", now=86500)


def test_rejected_installation_rotation_does_not_allocate_buckets():
    configured = replace(
        VoiceRegistrationSettings.from_env({}),
        per_identity_per_ten_minutes=1,
    )
    limiter = LocalVoiceLimiter(configured)
    assert limiter.allow_start("first", client_ip_identity="same-ip", now=100)
    original_keys = set(limiter._events)

    for index in range(10_000):
        assert not limiter.allow_start(
            f"rotation-{index}", client_ip_identity="same-ip", now=101,
        )
    assert set(limiter._events) == original_keys


def test_bucket_capacity_fails_closed_without_evicting_or_charging_live_quotas():
    configured = replace(
        VoiceRegistrationSettings.from_env({}),
        per_identity_per_ten_minutes=2,
    )
    limiter = LocalVoiceLimiter(configured, max_bucket_keys=4)
    assert limiter.allow_start("first", client_ip_identity="ip-a", now=100)
    assert limiter.allow_start("second", client_ip_identity="ip-b", now=101)
    original_keys = set(limiter._events)

    for index in range(1_000):
        assert not limiter.allow_start(
            f"new-{index}", client_ip_identity=f"new-ip-{index}", now=102,
        )
    # A failed new-IP reservation must not consume the existing identity quota.
    assert not limiter.allow_start("first", client_ip_identity="new-ip", now=103)
    assert limiter.allow_start("first", client_ip_identity="ip-a", now=104)
    assert not limiter.allow_start("first", client_ip_identity="ip-a", now=105)
    assert set(limiter._events) == original_keys


def test_expired_buckets_are_reclaimed_before_admitting_new_keys():
    limiter = LocalVoiceLimiter(VoiceRegistrationSettings.from_env({}), max_bucket_keys=2)
    assert limiter.allow_start("old", client_ip_identity="old-ip", now=100)
    assert not limiter.allow_start("new", client_ip_identity="new-ip", now=101)

    assert limiter.allow_start("new", client_ip_identity="new-ip", now=86500)
    assert set(limiter._events) == {"identity:new", "ip:new-ip"}
    assert len(limiter._events) == 2
