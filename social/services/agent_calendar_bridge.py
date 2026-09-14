"""Ephemeral Google Calendar projections for existing calendar-capable agents."""

from __future__ import annotations

import re
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Iterable

from services.calendar_service import as_utc, calendar_access_enabled, get_timezone
from services.google_calendar_service import GoogleCalendarError, get_google_calendar_service


class AgentCalendarUnavailable(RuntimeError):
    """A connected external calendar could not be read safely for this turn."""


def _google_datetime(value: object, *, all_day: bool, timezone_name: str) -> datetime:
    if all_day:
        local_date = date.fromisoformat(str(value or "")[:10])
        return datetime.combine(
            local_date,
            clock_time.min,
            get_timezone(timezone_name),
        ).astimezone(timezone.utc)
    parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timed Google event has no timezone")
    return parsed.astimezone(timezone.utc)


def _agent_event(owner_id: str, event: dict) -> dict | None:
    all_day = bool(event.get("all_day"))
    timezone_name = str(event.get("timezone") or "Asia/Taipei")
    try:
        start_at = _google_datetime(
            event.get("start_at"),
            all_day=all_day,
            timezone_name=timezone_name,
        )
        end_at = _google_datetime(
            event.get("end_at"),
            all_day=all_day,
            timezone_name=timezone_name,
        )
    except (TypeError, ValueError):
        return None
    if end_at <= start_at:
        return None
    return {
        "event_id": str(event.get("event_id") or ""),
        "source_type": "google",
        "provider_read_only": True,
        "participants": [owner_id],
        "status": str(event.get("status") or "confirmed"),
        "title": str(event.get("title") or "Google 行程")[:160],
        "activity": str(event.get("title") or "Google 行程")[:160],
        "start_at": start_at,
        "end_at": end_at,
        "all_day": all_day,
        "timezone": timezone_name,
        "location": str(event.get("location") or "")[:300],
        "notes": "",
        "revision": 0,
    }


def google_events_for_agent(
    owner_id: str,
    start: datetime,
    end: datetime,
    *,
    authorized: bool,
) -> list[dict]:
    """Read Google events only with request identity and the user's live opt-in.

    Event bodies remain process-local and are never written to MongoDB.
    """
    if not authorized:
        return []
    service = get_google_calendar_service()
    try:
        status = service.status(owner_id)
    except Exception:
        # An optional integration that cannot establish a connection state must
        # not break the existing internal-calendar read path.
        return []
    if status.get("state") != "connected":
        return []
    if not calendar_access_enabled(owner_id):
        return []

    cursor = as_utc(start)
    boundary = as_utc(end)
    projected: dict[str, dict] = {}
    try:
        while cursor < boundary:
            chunk_end = min(cursor + timedelta(days=92), boundary)
            page = service.list_events(owner_id, cursor, chunk_end)
            for raw in page.get("events", []):
                if not isinstance(raw, dict):
                    continue
                event = _agent_event(owner_id, raw)
                if event and event["event_id"]:
                    projected[event["event_id"]] = event
            cursor = chunk_end
    except GoogleCalendarError as exc:
        if exc.code in {
            "google_calendar_not_connected",
            "google_calendar_reauth_required",
            "google_calendar_account_not_allowed",
        }:
            return []
        raise AgentCalendarUnavailable(exc.code) from exc
    except Exception as exc:
        raise AgentCalendarUnavailable("google_calendar_agent_read_failed") from exc
    return sorted(
        projected.values(),
        key=lambda item: (as_utc(item["start_at"]), item["event_id"]),
    )[:256]


def merge_google_events(
    owner_id: str,
    internal_events: Iterable[dict],
    start: datetime,
    end: datetime,
    *,
    authorized: bool,
) -> list[dict]:
    events = [dict(event) for event in internal_events if isinstance(event, dict)]
    events.extend(
        google_events_for_agent(owner_id, start, end, authorized=authorized)
    )
    deduplicated = {
        str(event.get("event_id") or f"internal-{index}"): event
        for index, event in enumerate(events)
    }
    def sort_start(item: dict) -> datetime:
        value = item.get("start_at")
        if isinstance(value, datetime):
            return as_utc(value)
        try:
            parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
            return as_utc(parsed)
        except ValueError:
            return datetime.max.replace(tzinfo=timezone.utc)

    return sorted(
        deduplicated.values(),
        key=lambda item: (
            sort_start(item),
            str(item.get("event_id") or ""),
        ),
    )


def find_google_events(
    owner_id: str,
    event_hint: str,
    *,
    date_hint: str = "",
    authorized: bool,
    limit: int = 10,
) -> list[dict]:
    if not authorized:
        return []
    zone = get_timezone("Asia/Taipei")
    today = datetime.now(timezone.utc).astimezone(zone).date()
    try:
        target = date.fromisoformat(date_hint) if date_hint else None
    except ValueError:
        target = None
    start_day = target or today - timedelta(days=31)
    end_day = (target + timedelta(days=1)) if target else today + timedelta(days=91)
    start = datetime.combine(start_day, clock_time.min, zone).astimezone(timezone.utc)
    end = datetime.combine(end_day, clock_time.min, zone).astimezone(timezone.utc)
    events = google_events_for_agent(owner_id, start, end, authorized=True)
    tokens = [
        token
        for token in re.sub(r"\s+", " ", str(event_hint or "")).lower().split(" ")
        if token
    ]
    matches: list[dict] = []
    for event in events:
        zone = get_timezone(str(event.get("timezone") or "Asia/Taipei"))
        local_date = as_utc(event["start_at"]).astimezone(zone).date().isoformat()
        if target and local_date != target.isoformat():
            continue
        haystack = re.sub(
            r"\s+",
            " ",
            f"{event.get('title', '')} {event.get('location', '')}",
        ).lower()
        if tokens and not all(token in haystack for token in tokens):
            continue
        matches.append(event)
        if len(matches) >= max(1, min(limit, 30)):
            break
    return matches


def google_busy_until(owner_id: str, *, now: float) -> tuple[float | None, bool]:
    """Return the current Google busy boundary for proactive-care gating."""
    current = datetime.fromtimestamp(now, tz=timezone.utc)
    try:
        events = google_events_for_agent(
            owner_id,
            current - timedelta(seconds=1),
            current + timedelta(days=1),
            authorized=True,
        )
    except AgentCalendarUnavailable:
        return None, False
    active = [
        as_utc(event["end_at"])
        for event in events
        if as_utc(event["start_at"]) <= current < as_utc(event["end_at"])
    ]
    return (max(active).timestamp() if active else None), True
