"""Canonical temporal state for Events and Event proposal snapshots.

Discovery ingest already admits an Event while its interval still overlaps
`now` (already started but not ended), and the Matchmaker keeps loading active
graph Events until `expires_at`. The public card lifecycle must judge the same
Event with the same temporal facts: an ongoing Event with a trustworthy end
stays live until that end, and only an ended Event (or one without a reliable
end that has already started) may expire as "started past its window".

These helpers read the authoritative fields the Event already carries and
never invent a second time source: Unix-second `starts_at` / `ends_at` plus
the bounded `session_starts` / `session_ends` pair arrays.
"""

from __future__ import annotations

import math
from typing import Any

UPCOMING = "upcoming"
ONGOING = "ongoing"
ENDED = "ended"
UNKNOWN = "unknown"

LIVE_STATES = frozenset({UPCOMING, ONGOING})
_SESSION_BOUND = 8


def _timestamp(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def event_start(event: dict[str, Any] | None) -> float | None:
    return _timestamp((event or {}).get("starts_at"))


def event_end(event: dict[str, Any] | None) -> float | None:
    """Latest trustworthy end instant, or None when no reliable end exists.

    `ends_at` is the primary field; bounded session ends extend it. Only
    explicit, non-reversed ends count: a session without an end provides no
    end information, and an end earlier than its own start is dropped instead
    of being trusted.
    """
    data = event or {}
    start = event_start(data)
    candidates: list[float] = []
    end = _timestamp(data.get("ends_at"))
    if end is not None and (start is None or end >= start):
        candidates.append(end)
    starts = list(data.get("session_starts") or [])[:_SESSION_BOUND]
    ends = list(data.get("session_ends") or [])[:_SESSION_BOUND]
    for index, raw_start in enumerate(starts):
        session_start = _timestamp(raw_start)
        session_end = _timestamp(ends[index]) if index < len(ends) else None
        if session_end is not None and (session_start is None or session_end >= session_start):
            candidates.append(session_end)
    return max(candidates) if candidates else None


def event_live_until(event: dict[str, Any] | None) -> float:
    """Deadline for an unresolved invitation made from this Event.

    A trustworthy end is the deadline. Without one the conservative legacy
    start-based deadline is kept so an already-started Event with no reliable
    end does not stay actionable indefinitely.
    """
    data = event or {}
    end = event_end(data)
    if end is not None:
        return end
    legacy = [_timestamp(value) for value in list(data.get("session_starts") or [])[:_SESSION_BOUND]]
    legacy.append(event_start(data))
    positive = [value for value in legacy if value is not None]
    return max(positive) if positive else 0.0


def event_temporal_state(event: dict[str, Any] | None, *, now: float) -> str:
    """Classify an Event (or snapshot) as upcoming/ongoing/ended/unknown.

    `unknown` means no trustworthy end could be established; callers must
    keep their conservative fail-closed behavior for it.
    """
    current = float(now)
    start = event_start(event)
    end = event_end(event)
    if end is not None:
        if start is not None and current < start:
            return UPCOMING
        return ONGOING if current < end else ENDED
    if start is not None and current < start:
        return UPCOMING
    return UNKNOWN


def is_temporally_eligible(event: dict[str, Any] | None, *, now: float) -> bool:
    """True when the Event may still carry a live invitation at `now`."""
    return event_temporal_state(event, now=now) in LIVE_STATES
