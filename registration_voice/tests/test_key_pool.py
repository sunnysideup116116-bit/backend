from registration_voice.key_pool import GoogleApiKeyPool
from registration_voice.settings import collect_google_api_keys


def test_collect_google_api_keys_supports_existing_env_naming_and_deduplicates():
    keys = collect_google_api_keys({
        "GOOGLE_API_KEYS1": "alpha",
        "GOOGLE_API_KEYS2": "beta",
        "GOOGLE_API_KEY_1": "alpha",
        "GOOGLE_AI_STUDIO_API_KEY": "gamma",
    })

    assert keys == ["alpha", "beta", "gamma"]


def test_key_pool_rotates_new_sessions_and_skips_cooling_key():
    pool = GoogleApiKeyPool(["alpha", "beta", "gamma"], cooldown_seconds=60)

    assert [candidate.position for candidate in pool.candidates(now=10)] == [0, 1, 2]
    assert [candidate.position for candidate in pool.candidates(now=10)] == [1, 2, 0]

    pool.mark_unavailable("beta", now=10)
    assert [candidate.position for candidate in pool.candidates(now=20)] == [2, 0]
    assert [candidate.position for candidate in pool.candidates(now=71)] == [0, 1, 2]


def test_key_candidate_repr_never_exposes_secret():
    candidate = GoogleApiKeyPool(["top-secret-key"]).candidates(now=0)[0]

    assert "top-secret-key" not in repr(candidate)
