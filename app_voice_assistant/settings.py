from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from dotenv import load_dotenv


_TRUE = {"1", "true", "on", "yes"}
_SERVER_ENV = Path(__file__).resolve().parents[1] / ".env"


def _enabled(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in _TRUE


@dataclass(frozen=True)
class AppVoiceSettings:
    enabled: bool
    demo_only: bool
    gemini_fallback_enabled: bool
    text_model: str
    live_model: str
    tts_model: str
    tts_voice: str
    consent_version: str
    max_session_seconds: int
    ticket_ttl_seconds: int
    key_cooldown_seconds: int
    per_user_per_ten_minutes: int
    per_user_per_day: int
    global_concurrency: int
    tts_per_user_per_day: int
    test_user_ids: frozenset[str]

    @classmethod
    def from_env(
        cls, environ: Mapping[str, str] | None = None,
    ) -> "AppVoiceSettings":
        if environ is None:
            if os.getenv("AYUE_SKIP_DOTENV", "").strip().lower() not in _TRUE:
                load_dotenv(_SERVER_ENV, override=False)
            env: Mapping[str, str] = os.environ
        else:
            env = environ

        def bounded(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                parsed = int(env.get(name, str(default)))
            except (TypeError, ValueError):
                parsed = default
            return max(minimum, min(parsed, maximum))

        test_ids = frozenset(
            value.strip()
            for value in env.get("VOICE_APP_TEST_USER_IDS", "").split(",")
            if value.strip()
        )
        return cls(
            enabled=_enabled(env.get("VOICE_APP_ENABLED"), True),
            demo_only=_enabled(env.get("VOICE_APP_DEMO_ONLY"), True),
            gemini_fallback_enabled=_enabled(
                env.get("VOICE_APP_GEMINI_FALLBACK_ENABLED"), True,
            ),
            text_model=(
                env.get("VOICE_APP_TEXT_MODEL", "gemini-3.1-flash-lite").strip()
                or "gemini-3.1-flash-lite"
            ),
            live_model=(
                env.get(
                    "VOICE_APP_LIVE_MODEL", "gemini-3.1-flash-live-preview",
                ).strip()
                or "gemini-3.1-flash-live-preview"
            ),
            tts_model=(
                env.get("VOICE_APP_TTS_MODEL", "gemini-3.1-flash-tts-preview").strip()
                or "gemini-3.1-flash-tts-preview"
            ),
            tts_voice=(env.get("VOICE_APP_TTS_VOICE", "Achird").strip() or "Achird"),
            consent_version=(
                env.get("VOICE_APP_CONSENT_VERSION", "demo-free-gemini-live-v2").strip()
                or "demo-free-gemini-live-v2"
            ),
            max_session_seconds=bounded("VOICE_APP_MAX_SESSION_SECONDS", 600, 20, 600),
            ticket_ttl_seconds=bounded("VOICE_APP_TICKET_TTL_SECONDS", 60, 15, 300),
            key_cooldown_seconds=bounded("VOICE_APP_KEY_COOLDOWN_SECONDS", 60, 5, 3600),
            per_user_per_ten_minutes=bounded("VOICE_APP_PER_10_MINUTES", 6, 1, 1000),
            per_user_per_day=bounded("VOICE_APP_PER_DAY", 30, 1, 10000),
            global_concurrency=bounded("VOICE_APP_GLOBAL_CONCURRENCY", 4, 1, 1000),
            tts_per_user_per_day=bounded("VOICE_APP_TTS_PER_DAY", 60, 1, 1000),
            test_user_ids=test_ids,
        )

    def allows_user(self, user_id: str) -> bool:
        cleaned = str(user_id or "").strip()
        if not cleaned:
            return False
        if not self.demo_only:
            return True
        return cleaned in self.test_user_ids
