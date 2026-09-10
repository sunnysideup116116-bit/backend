from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class KeyCandidate:
    key: str
    position: int

    def __repr__(self) -> str:  # Never reveal a secret in logs or test failures.
        return f"KeyCandidate(position={self.position})"


class GoogleApiKeyPool:
    """Registration voice key rotation with temporary key cooldowns."""

    def __init__(self, keys: list[str], *, cooldown_seconds: int = 60):
        self._keys = tuple(dict.fromkeys(key for key in keys if key))
        self._cooldown_seconds = max(1, cooldown_seconds)
        self._next_index = 0
        self._cooldown_until: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def size(self) -> int:
        return len(self._keys)

    def candidates(self, *, now: float | None = None) -> list[KeyCandidate]:
        instant = time.monotonic() if now is None else now
        with self._lock:
            if not self._keys:
                return []
            start = self._next_index
            self._next_index = (self._next_index + 1) % len(self._keys)
            ordered = [
                KeyCandidate(
                    key=self._keys[(start + offset) % len(self._keys)],
                    position=(start + offset) % len(self._keys),
                )
                for offset in range(len(self._keys))
            ]
            return [
                candidate
                for candidate in ordered
                if self._cooldown_until.get(candidate.key, 0.0) <= instant
            ]

    def mark_unavailable(self, key: str, *, now: float | None = None) -> None:
        instant = time.monotonic() if now is None else now
        with self._lock:
            if key in self._keys:
                self._cooldown_until[key] = instant + self._cooldown_seconds

    def mark_success(self, key: str) -> None:
        with self._lock:
            self._cooldown_until.pop(key, None)
