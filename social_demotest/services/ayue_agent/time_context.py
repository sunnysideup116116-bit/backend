"""One authoritative clock for a public-Ayue turn."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .contracts import TurnClockV1


_WEEKDAYS_ZH_TW = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=8), name="Asia/Taipei")


def resolve_temporal_references(message: str, local_now: datetime) -> dict[str, str]:
    """Resolve only relative terms actually written in the owner's message."""
    text = message or ""
    today = local_now.date()
    values = {
        "今天": today,
        "明天": today + timedelta(days=1),
        "後天": today + timedelta(days=2),
        "本週": today - timedelta(days=today.weekday()),
        "這週": today - timedelta(days=today.weekday()),
        "下週": today - timedelta(days=today.weekday()) + timedelta(days=7),
        "本月": today.replace(day=1),
        "下個月": (today.replace(day=28) + timedelta(days=4)).replace(day=1),
    }
    return {term: value.isoformat() for term, value in values.items() if term in text}


def build_turn_clock(message: str, now_utc: datetime | None = None) -> TurnClockV1:
    timezone_name = os.getenv("AYUE_DEFAULT_TIMEZONE", "Asia/Taipei").strip() or "Asia/Taipei"
    utc_now = now_utc or datetime.now(timezone.utc)
    if utc_now.tzinfo is None:
        utc_now = utc_now.replace(tzinfo=timezone.utc)
    utc_now = utc_now.astimezone(timezone.utc)
    local_now = utc_now.astimezone(_timezone(timezone_name))
    return TurnClockV1(
        timezone=timezone_name,
        utc_iso=utc_now.isoformat(),
        local_iso=local_now.isoformat(),
        local_date=local_now.date().isoformat(),
        local_time=local_now.strftime("%H:%M"),
        weekday_zh_tw=_WEEKDAYS_ZH_TW[local_now.weekday()],
        temporal_references=resolve_temporal_references(message, local_now),
    )


def clock_utc(clock: TurnClockV1) -> datetime:
    return datetime.fromisoformat(clock.utc_iso).astimezone(timezone.utc)
