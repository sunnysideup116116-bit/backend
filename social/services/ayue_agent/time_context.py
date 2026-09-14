"""One authoritative clock for a public-Ayue turn."""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .contracts import TurnClockV1


_WEEKDAYS_ZH_TW = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
_WEEKDAY_SUFFIXES = {
    "一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5,
    "日": 6, "天": 6,
}
_RELATIVE_WEEK_PREFIXES = {
    "下下週": 14,
    "下下星期": 14,
    "下下禮拜": 14,
    "下下周": 14,
    "下週": 7,
    "下星期": 7,
    "下禮拜": 7,
    "下周": 7,
    "這週": 0,
    "本週": 0,
    "這星期": 0,
    "本星期": 0,
    "這禮拜": 0,
    "本禮拜": 0,
    "這周": 0,
    "本周": 0,
}


def _timezone(name: str):
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=8), name="Asia/Taipei")


def resolve_temporal_references(message: str, local_now: datetime) -> dict[str, str]:
    """Resolve only bounded relative terms actually written in the message.

    Weekday phrases are resolved here, once, from the authoritative turn clock.
    In particular, ``下週三`` is not allowed to inherit the date for the
    shorter ``下週`` token.
    """
    text = message or ""
    today = local_now.date()
    values = {
        "今天": today,
        "明天": today + timedelta(days=1),
        "後天": today + timedelta(days=2),
        "本週": today - timedelta(days=today.weekday()),
        "這週": today - timedelta(days=today.weekday()),
        "下週": today - timedelta(days=today.weekday()) + timedelta(days=7),
        "本周": today - timedelta(days=today.weekday()),
        "這周": today - timedelta(days=today.weekday()),
        "下周": today - timedelta(days=today.weekday()) + timedelta(days=7),
        "下下週": today - timedelta(days=today.weekday()) + timedelta(days=14),
        "下下星期": today - timedelta(days=today.weekday()) + timedelta(days=14),
        "下下禮拜": today - timedelta(days=today.weekday()) + timedelta(days=14),
        "下下周": today - timedelta(days=today.weekday()) + timedelta(days=14),
        "本月": today.replace(day=1),
        "下個月": (today.replace(day=28) + timedelta(days=4)).replace(day=1),
    }
    current_monday = today - timedelta(days=today.weekday())
    for prefix, week_offset in _RELATIVE_WEEK_PREFIXES.items():
        for suffix, weekday in _WEEKDAY_SUFFIXES.items():
            term = f"{prefix}{suffix}"
            if term in text:
                values[term] = current_monday + timedelta(days=week_offset + weekday)

    # Bare weekday forms are useful context when the user says "星期三".
    # Do not add them for a longer relative phrase; the exact phrase above is
    # the only authoritative match in that case.
    for prefix in ("週", "星期", "禮拜"):
        for suffix in _WEEKDAY_SUFFIXES:
            term = f"{prefix}{suffix}"
            if term in text and not any(
                f"{relative_prefix}{term}" in text
                for relative_prefix in _RELATIVE_WEEK_PREFIXES
            ):
                values[term] = current_monday + timedelta(days=_WEEKDAY_SUFFIXES[suffix])
    present = {term: value.isoformat() for term, value in values.items() if term in text}
    # Chinese relative expressions overlap by prefix (``下週`` is contained
    # in ``下下週四``).  Keep only the longest written term so downstream
    # consumers cannot accidentally use the shorter Monday reference.
    for term in list(present):
        if any(term != other and term in other and other in present for other in present):
            present.pop(term, None)
    return present


def resolve_temporal_candidates(message: str, local_now: datetime) -> list[dict[str, object]]:
    """Return bounded, planner-only candidate dates for written relative terms.

    The authoritative legacy resolver above intentionally keeps calendar-week
    semantics.  This additive projection exposes a future occurrence only when
    that stated weekday has already passed, leaving the semantic choice to the
    Planner while keeping every concrete date server-derived.
    """
    references = resolve_temporal_references(message, local_now)
    today = local_now.date()
    output: list[dict[str, object]] = []
    for source_text, raw_date in references.items():
        try:
            stated = datetime.fromisoformat(raw_date).date()
        except (TypeError, ValueError):
            continue
        relation = "past" if stated < today else "today" if stated == today else "future"
        candidates: list[dict[str, str]] = [{
            "id": "stated_period",
            "date": stated.isoformat(),
            "relation": relation,
        }]
        is_weekday_reference = any(
            source_text.endswith(suffix) for suffix in _WEEKDAY_SUFFIXES
        ) and any(token in source_text for token in ("週", "周", "星期", "禮拜"))
        if is_weekday_reference and stated < today:
            next_occurrence = stated + timedelta(days=7)
            candidates.append({
                "id": "next_occurrence",
                "date": next_occurrence.isoformat(),
                "relation": "future",
            })
        output.append({"source_text": source_text, "candidates": candidates})
    return output


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
