from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


_TRUE_VALUES = {"1", "true", "on", "yes"}
_SERVER_ENV = Path(__file__).resolve().parents[1] / ".env"


def _load_server_environment() -> None:
    if os.getenv("AYUE_SKIP_DOTENV", "").strip().lower() not in _TRUE_VALUES:
        load_dotenv(_SERVER_ENV, override=False)


def _enabled(value: str | None, *, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in _TRUE_VALUES


def collect_google_api_keys(environ: Mapping[str, str] | None = None) -> list[str]:
    """Collect dedicated voice keys first, then the existing shared key pool.

    The project already uses both GOOGLE_API_KEYS1 and GOOGLE_API_KEY_1 style
    variables.  Supporting the same names lets the voice feature distribute new
    sessions without copying secrets into another file or into Flutter.
    """

    if environ is None:
        _load_server_environment()
    env = os.environ if environ is None else environ
    keys: list[str] = []

    def add(value: str | None) -> None:
        cleaned = (value or "").strip().strip("\"'")
        if cleaned and cleaned not in keys:
            keys.append(cleaned)

    for csv_name in ("VOICE_GOOGLE_API_KEYS", "GOOGLE_API_KEYS"):
        for value in (env.get(csv_name) or "").split(","):
            add(value)

    for prefix in (
        "VOICE_GOOGLE_API_KEY_",
        "VOICE_GOOGLE_API_KEYS",
        "GOOGLE_API_KEYS",
        "GOOGLE_API_KEY_",
    ):
        for index in range(1, 51):
            add(env.get(f"{prefix}{index}"))

    for name in (
        "VOICE_GOOGLE_API_KEY",
        "GOOGLE_AI_STUDIO_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ):
        add(env.get(name))

    # Social's config module owns dotenv loading.  Reuse its resolved pool when
    # this function is reading the live process environment; explicit mappings
    # used by tests remain fully isolated.
    if environ is None:
        try:
            from config import GOOGLE_API_KEYS as configured_keys
        except (ImportError, AttributeError):
            configured_keys = []
        for value in configured_keys:
            add(value)

    return keys


@dataclass(frozen=True)
class VoiceRegistrationSettings:
    enabled: bool
    local_rate_limit_enabled: bool
    model: str
    thinking_level: str
    silence_duration_ms: int
    max_session_seconds: int
    ticket_ttl_seconds: int
    key_cooldown_seconds: int
    per_identity_per_ten_minutes: int
    per_identity_per_day: int
    global_concurrency: int
    consent_version: str

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None,
    ) -> "VoiceRegistrationSettings":
        if environ is None:
            _load_server_environment()
        env = os.environ if environ is None else environ

        def bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(env.get(name, str(default)))
            except (TypeError, ValueError):
                value = default
            return max(minimum, min(value, maximum))

        return cls(
            enabled=_enabled(env.get("VOICE_REGISTRATION_ENABLED"), default=True),
            local_rate_limit_enabled=_enabled(
                env.get("VOICE_LOCAL_RATE_LIMIT_ENABLED"), default=False,
            ),
            model=(
                env.get("VOICE_REGISTRATION_MODEL", "gemini-3.1-flash-live-preview").strip()
                or "gemini-3.1-flash-live-preview"
            ),
            thinking_level=(
                env.get("VOICE_REGISTRATION_THINKING_LEVEL", "low").strip().lower()
                or "low"
            ),
            silence_duration_ms=bounded_int(
                "VOICE_SILENCE_DURATION_MS", 500, 200, 5000,
            ),
            max_session_seconds=bounded_int(
                "VOICE_MAX_SESSION_SECONDS", 120, 10, 600,
            ),
            ticket_ttl_seconds=bounded_int(
                "VOICE_TICKET_TTL_SECONDS", 60, 15, 300,
            ),
            key_cooldown_seconds=bounded_int(
                "VOICE_KEY_COOLDOWN_SECONDS", 60, 5, 3600,
            ),
            per_identity_per_ten_minutes=bounded_int(
                "VOICE_RATE_LIMIT_PER_10_MINUTES", 3, 1, 1000,
            ),
            per_identity_per_day=bounded_int(
                "VOICE_RATE_LIMIT_PER_DAY", 10, 1, 10000,
            ),
            global_concurrency=bounded_int(
                "VOICE_RATE_LIMIT_GLOBAL_CONCURRENCY", 4, 1, 1000,
            ),
            consent_version=(
                env.get("VOICE_CONSENT_VERSION", "free-tier-voice-v1").strip()
                or "free-tier-voice-v1"
            ),
        )
