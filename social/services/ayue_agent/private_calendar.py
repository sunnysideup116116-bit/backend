"""Bounded Private calendar projections used by the current V2 runtime."""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone

from services.calendar_service import calendar_access_enabled, get_calendar_context, get_timezone


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


_DATE_MARKER_RE = re.compile(r"(?:\d{1,4}[/-])?\d{1,2}月\d{1,2}日?|\d{4}[/-]\d{1,2}[/-]\d{1,2}")


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def calendar_range_for_message(message: str, now: datetime | None = None) -> tuple[datetime, datetime, bool]:
    """Resolve a Taiwan-local availability range and cap it to 31 days."""
    zone = get_timezone("Asia/Taipei")
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    today = local_now.date()
    compact = _compact(message)
    start_day = today
    end_day = today + timedelta(days=31)

    if "本週" in compact or "這週" in compact:
        start_day = today - timedelta(days=today.weekday())
        end_day = start_day + timedelta(days=7)
    elif "下週" in compact:
        start_day = today - timedelta(days=today.weekday()) + timedelta(days=7)
        end_day = start_day + timedelta(days=7)
    elif "這個月" in compact or "本月" in compact:
        start_day = _month_start(today)
        end_day = _next_month(start_day)
    elif "下個月" in compact or "下月" in compact:
        start_day = _next_month(_month_start(today))
        end_day = _next_month(start_day)
    else:
        match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", compact)
        month_day = re.search(r"(\d{1,2})月(\d{1,2})日?", compact)
        try:
            if match:
                start_day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                end_day = start_day + timedelta(days=1)
            elif month_day:
                start_day = date(today.year, int(month_day.group(1)), int(month_day.group(2)))
                if start_day < today:
                    start_day = start_day.replace(year=start_day.year + 1)
                end_day = start_day + timedelta(days=1)
        except ValueError:
            start_day = today
            end_day = today + timedelta(days=31)

    truncated = end_day - start_day > timedelta(days=31)
    if truncated:
        end_day = start_day + timedelta(days=31)
    start = datetime.combine(start_day, datetime.min.time(), zone).astimezone(timezone.utc)
    end = datetime.combine(end_day, datetime.min.time(), zone).astimezone(timezone.utc)
    return start, end, truncated


def partner_busy(user_id: str, other_id: str, start: datetime, end: datetime) -> tuple[bool, list[dict[str, str]]]:
    """Return only busy intervals; no IDs or event metadata leave this facade."""
    if not calendar_access_enabled(other_id):
        return False, []
    events = get_calendar_context(user_id, other_id, start, end)
    busy = []
    for event in events.get("partner_busy", []):
        start_at, end_at = event.get("start_at"), event.get("end_at")
        if start_at and end_at:
            busy.append({"start_at": str(start_at), "end_at": str(end_at), "busy": "true"})
    return True, busy[:16]
