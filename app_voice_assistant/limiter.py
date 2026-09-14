from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from .settings import AppVoiceSettings


@dataclass(frozen=True)
class AppVoiceTicket:
    identity: str
    user_id: str
    ip_fingerprint: str
    expires_at: float
    username: str = ""
    session_id: str = ""
    memory_older_summary: str = ""
    memory_recent_summary: str = ""


class AppVoiceTicketStore:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._tickets: dict[str, AppVoiceTicket] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        identity: str,
        user_id: str,
        ip_fingerprint: str,
        *,
        username: str = "",
        session_id: str = "",
        memory_older_summary: str = "",
        memory_recent_summary: str = "",
    ) -> tuple[str, int]:
        token = secrets.token_urlsafe(32)
        now = time.monotonic()
        with self._lock:
            self._cleanup(now)
            self._tickets[token] = AppVoiceTicket(
                identity=identity,
                user_id=user_id,
                ip_fingerprint=ip_fingerprint,
                expires_at=now + self._ttl,
                username=username,
                session_id=session_id,
                memory_older_summary=memory_older_summary,
                memory_recent_summary=memory_recent_summary,
            )
        return token, self._ttl

    def consume(self, token: str) -> AppVoiceTicket | None:
        now = time.monotonic()
        with self._lock:
            self._cleanup(now)
            value = self._tickets.pop(token, None)
        return value if value and value.expires_at >= now else None

    def _cleanup(self, now: float) -> None:
        expired = [key for key, value in self._tickets.items() if value.expires_at < now]
        for key in expired:
            self._tickets.pop(key, None)


class AppVoiceLimiter:
    def __init__(self, settings: AppVoiceSettings):
        self._settings = settings
        self._turns: dict[str, deque[float]] = defaultdict(deque)
        self._tts: dict[str, deque[float]] = defaultdict(deque)
        self._active = 0
        self._lock = threading.Lock()

    def allow_session(self, identity: str, *, now: float | None = None) -> bool:
        instant = time.time() if now is None else now
        with self._lock:
            values = self._turns[identity]
            while values and values[0] <= instant - 86400:
                values.popleft()
            recent = sum(item > instant - 600 for item in values)
            if recent >= self._settings.per_user_per_ten_minutes:
                return False
            if len(values) >= self._settings.per_user_per_day:
                return False
            values.append(instant)
            return True

    def allow_tts(self, identity: str, *, now: float | None = None) -> bool:
        instant = time.time() if now is None else now
        with self._lock:
            values = self._tts[identity]
            while values and values[0] <= instant - 86400:
                values.popleft()
            if len(values) >= self._settings.tts_per_user_per_day:
                return False
            values.append(instant)
            return True

    def acquire(self) -> bool:
        with self._lock:
            if self._active >= self._settings.global_concurrency:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)


def fingerprint(secret: bytes, *parts: str) -> str:
    value = "|".join(parts).encode("utf-8", "ignore")
    return hmac.new(secret, value, hashlib.sha256).hexdigest()


def new_process_secret() -> bytes:
    return secrets.token_bytes(32)
