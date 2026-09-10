from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from .settings import VoiceRegistrationSettings


@dataclass(frozen=True)
class Ticket:
    identity: str
    client_ip_fingerprint: str
    expires_at: float


class TicketStore:
    def __init__(self, ttl_seconds: int):
        self._ttl_seconds = ttl_seconds
        self._tickets: dict[str, Ticket] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        identity: str,
        client_ip_fingerprint: str,
        *,
        now: float | None = None,
    ) -> tuple[str, int]:
        instant = time.monotonic() if now is None else now
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._cleanup(instant)
            self._tickets[token] = Ticket(
                identity,
                client_ip_fingerprint,
                instant + self._ttl_seconds,
            )
        return token, self._ttl_seconds

    def consume(self, token: str, *, now: float | None = None) -> Ticket | None:
        instant = time.monotonic() if now is None else now
        with self._lock:
            self._cleanup(instant)
            ticket = self._tickets.pop(token, None)
        return ticket if ticket and ticket.expires_at >= instant else None

    def _cleanup(self, now: float) -> None:
        expired = [token for token, ticket in self._tickets.items() if ticket.expires_at < now]
        for token in expired:
            self._tickets.pop(token, None)


class LocalVoiceLimiter:
    """Server-local limiter retained behind VOICE_LOCAL_RATE_LIMIT_ENABLED."""

    def __init__(self, settings: VoiceRegistrationSettings):
        self._settings = settings
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._active = 0
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._settings.local_rate_limit_enabled

    def allow_start(self, identity: str, *, now: float | None = None) -> bool:
        if not self.enabled:
            return True
        instant = time.time() if now is None else now
        with self._lock:
            events = self._events[identity]
            while events and events[0] <= instant - 86400:
                events.popleft()
            ten_minute_count = sum(ts > instant - 600 for ts in events)
            if (
                len(events) >= self._settings.per_identity_per_day
                or ten_minute_count >= self._settings.per_identity_per_ten_minutes
            ):
                return False
            events.append(instant)
            return True

    def acquire(self) -> bool:
        if not self.enabled:
            return True
        with self._lock:
            if self._active >= self._settings.global_concurrency:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            self._active = max(0, self._active - 1)


def anonymous_identity(installation_id: str, client_ip: str, secret: bytes) -> str:
    payload = f"{client_ip}|{installation_id}".encode("utf-8", "ignore")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def process_identity_secret() -> bytes:
    return secrets.token_bytes(32)
