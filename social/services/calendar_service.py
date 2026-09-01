"""Canonical calendar storage, validation, privacy filtering, and conflict checks."""

from __future__ import annotations

from datetime import date as date_value, datetime, timedelta, timezone
from difflib import SequenceMatcher
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import uuid4

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from database import calendar_events_coll, profiles_coll


ACTIVE_EVENT_STATUSES = {"confirmed", "pending_reconfirmation"}
_RESOLUTION_CANDIDATES: dict[tuple[str, str], list[dict]] = {}
_RESOLUTION_KIND: dict[tuple[str, str], str] = {}


def get_timezone(zone_name: str):
    """Use IANA data when available and keep the Taiwan demo working on Windows."""
    try:
        return ZoneInfo(zone_name)
    except ZoneInfoNotFoundError as exc:
        if zone_name == "Asia/Taipei":
            return timezone(timedelta(hours=8), name="Asia/Taipei")
        raise HTTPException(status_code=400, detail=f"不支援的時區：{zone_name}") from exc


def as_utc(value: datetime) -> datetime:
    """MongoDB returns BSON datetimes without tzinfo even though they are UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_utc(value: datetime | None) -> str | None:
    return as_utc(value).isoformat() if value else None


def ensure_calendar_indexes() -> None:
    """Safe to call repeatedly; failures are non-fatal for an offline demo."""
    try:
        calendar_events_coll.create_index("event_id", unique=True)
        calendar_events_coll.create_index(
            "coordination_id", unique=True,
            partialFilterExpression={
                "source_type": "date",
                "coordination_id": {"$type": "string"},
            },
        )
        calendar_events_coll.create_index([("participants", 1), ("start_at", 1), ("status", 1)])
        calendar_events_coll.create_index("agent_action_key", unique=True, sparse=True)
    except Exception as exc:  # Database may be unavailable during local startup.
        print(f"Calendar index setup skipped: {exc}")


def _parse_local_interval(form: dict) -> tuple[datetime, datetime, str]:
    form = normalize_form(form)
    all_day = bool(form.get("all_day"))
    required = ("date",) if all_day else ("date", "start_time", "end_time")
    missing = [key for key in required if not str(form.get(key, "")).strip()]
    if missing:
        labels = {"date": "日期", "start_time": "開始時間", "end_time": "結束時間"}
        raise HTTPException(status_code=400, detail=f"請填寫：{'、'.join(labels[key] for key in missing)}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", form["date"]):
        raise HTTPException(status_code=400, detail="日期格式需為 YYYY-MM-DD")
    end_date_text = str(form.get("end_date") or form["date"]).strip()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end_date_text):
        raise HTTPException(status_code=400, detail="結束日期格式需為 YYYY-MM-DD")
    if not all_day:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", form["start_time"]):
            raise HTTPException(status_code=400, detail="開始時間格式需為 HH:MM")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", form["end_time"]):
            raise HTTPException(status_code=400, detail="結束時間格式需為 HH:MM")
    zone_name = form.get("timezone") or "Asia/Taipei"
    try:
        start_date = date_value.fromisoformat(form["date"])
        end_date = date_value.fromisoformat(end_date_text)
        if end_date < start_date:
            raise ValueError("end date precedes start date")
        if (end_date - start_date).days > 366:
            raise HTTPException(status_code=400, detail="行程日期範圍不能超過 367 天")
        zone = get_timezone(zone_name)
        if all_day:
            start = datetime.combine(start_date, datetime.min.time(), zone)
            # ``end_date`` is inclusive in the natural-language/API contract;
            # storage uses the standard exclusive boundary.
            end = datetime.combine(end_date + timedelta(days=1), datetime.min.time(), zone)
        else:
            start = datetime.fromisoformat(f"{start_date.isoformat()}T{form['start_time']}").replace(tzinfo=zone)
            end = datetime.fromisoformat(f"{end_date.isoformat()}T{form['end_time']}").replace(tzinfo=zone)
    except HTTPException:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise HTTPException(status_code=400, detail="日期或時間格式不正確") from exc
    if end <= start:
        raise HTTPException(status_code=400, detail="結束時間必須晚於開始時間")
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc), zone_name


def normalize_form(form: dict) -> dict:
    """Keep old cards readable while requiring the new precise time model to confirm."""
    form = dict(form or {})
    if not form.get("start_time") and form.get("time"):
        form["start_time"] = form["time"]

    raw_date = str(form.get("date") or "").strip()
    raw_date = raw_date.split("T", 1)[0].replace("/", "-").replace(".", "-")
    date_match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw_date)
    if date_match:
        raw_date = f"{int(date_match.group(1)):04d}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}"
    raw_end_date = str(form.get("end_date") or "").strip()
    raw_end_date = raw_end_date.split("T", 1)[0].replace("/", "-").replace(".", "-")
    end_date_match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", raw_end_date)
    if end_date_match:
        raw_end_date = (
            f"{int(end_date_match.group(1)):04d}-"
            f"{int(end_date_match.group(2)):02d}-"
            f"{int(end_date_match.group(3)):02d}"
        )
    all_day = form.get("all_day") is True

    def normalize_time(value: object) -> str:
        raw = str(value or "").strip()
        colon_match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", raw)
        chinese_match = re.fullmatch(r"(\d{1,2})\s*點(?:\s*(\d{1,2})\s*分?)?", raw)
        match = colon_match or chinese_match
        if not match:
            return raw
        hour = int(match.group(1))
        minute = int(match.group(2) or 0)
        if hour > 23 or minute > 59:
            return raw
        return f"{hour:02d}:{minute:02d}"

    normalized = {
        # ``title`` is the canonical label for personal events.  Retain it
        # through normalization so an agent confirmation can accurately show
        # both a new event and an edited event without falling back to an
        # unrelated activity field.
        "title": str(form.get("title") or form.get("activity") or "").strip(),
        "date": raw_date,
        "start_time": "" if all_day else normalize_time(form.get("start_time")),
        "end_time": "" if all_day else normalize_time(form.get("end_time")),
        "timezone": str(form.get("timezone") or "Asia/Taipei").strip(),
        "activity": str(form.get("activity") or "").strip(),
        "location": str(form.get("location") or "").strip(),
        "budget": str(form.get("budget") or "").strip(),
        "notes": str(form.get("notes") or "").strip(),
    }
    if raw_end_date:
        normalized["end_date"] = raw_end_date
    if "all_day" in form:
        normalized["all_day"] = all_day
    return normalized


def serialize_event(event: dict, viewer_id: str | None = None, include_private: bool = True) -> dict:
    result = {
        "event_id": event["event_id"],
        "source_type": event.get("source_type"),
        "participants": event.get("participants", []),
        "status": event.get("status"),
        "title": event.get("title", ""),
        "start_at": iso_utc(event.get("start_at")),
        "end_at": iso_utc(event.get("end_at")),
        "all_day": bool(event.get("all_day", False)),
        "timezone": event.get("timezone", "Asia/Taipei"),
        "location": event.get("location", ""),
        "notes": event.get("notes", ""),
        "activity": event.get("activity") or event.get("title", ""),
        "budget": event.get("budget", ""),
        "revision": event.get("revision", 1),
        "match_id": event.get("match_id"),
        "coordination_id": event.get("coordination_id"),
        "pending_change": event.get("pending_change"),
    }
    if not include_private:
        return {
            "event_id": event["event_id"], "status": event.get("status"),
            "start_at": result["start_at"], "end_at": result["end_at"], "busy": True,
        }
    return result


def list_events(user_id: str, start: datetime, end: datetime, include_cancelled: bool = False) -> list[dict]:
    query = {
        "participants": user_id,
        "start_at": {"$lt": end},
        "end_at": {"$gt": start},
    }
    if not include_cancelled:
        query["status"] = {"$ne": "cancelled"}
    return [serialize_event(event, user_id) for event in calendar_events_coll.find(query).sort("start_at", 1)]


def get_next_event(user_id: str, start: datetime, end: datetime) -> dict | None:
    """Return the owner's nearest active event in a bounded time window."""
    query = {
        "participants": user_id,
        "status": {"$in": list(ACTIVE_EVENT_STATUSES)},
        "start_at": {"$gte": as_utc(start), "$lt": as_utc(end)},
    }
    event = calendar_events_coll.find_one(query, sort=[("start_at", 1), ("event_id", 1)])
    return serialize_event(event, user_id) if event else None


def find_conflicts(participant_ids: list[str], start_at: datetime, end_at: datetime, exclude_event_id: str | None = None) -> list[dict]:
    query: dict = {
        "participants": {"$in": participant_ids},
        "status": {"$in": list(ACTIVE_EVENT_STATUSES)},
        "start_at": {"$lt": end_at},
        "end_at": {"$gt": start_at},
    }
    if exclude_event_id:
        query["event_id"] = {"$ne": exclude_event_id}
    return list(calendar_events_coll.find(query))


def conflicts_for_viewer(viewer_id: str, participant_ids: list[str], start_at: datetime, end_at: datetime, exclude_event_id: str | None = None) -> list[dict]:
    conflicts = []
    for event in find_conflicts(participant_ids, start_at, end_at, exclude_event_id):
        owner_is_viewer = viewer_id in event.get("participants", [])
        is_shared_with_viewer = event.get("source_type") == "date" and viewer_id in event.get("participants", [])
        conflicts.append(serialize_event(event, viewer_id, include_private=owner_is_viewer or is_shared_with_viewer))
    return conflicts


def create_personal_event(user_id: str, payload: dict, *, agent_action_key: str | None = None) -> dict:
    if agent_action_key:
        prior = calendar_events_coll.find_one({"agent_action_key": agent_action_key})
        if prior:
            return serialize_event(prior, user_id)
    form = normalize_form(payload)
    start_at, end_at, zone_name = _parse_local_interval(form)
    now = datetime.now(timezone.utc)
    event = {
        "event_id": uuid4().hex,
        "source_type": "personal",
        "participants": [user_id],
        "title": form["title"],
        "start_at": start_at,
        "end_at": end_at,
        "all_day": bool(form.get("all_day")),
        "timezone": zone_name,
        "location": form.get("location", ""),
        "notes": form.get("notes", ""),
        "status": "confirmed",
        "revision": 1,
        "created_at": now,
        "updated_at": now,
    }
    if agent_action_key:
        event["agent_action_key"] = agent_action_key
    try:
        calendar_events_coll.insert_one(event)
    except DuplicateKeyError:
        prior = calendar_events_coll.find_one({"agent_action_key": agent_action_key}) if agent_action_key else None
        if prior:
            return serialize_event(prior, user_id)
        raise
    return serialize_event(event, user_id)


def update_personal_event(
    user_id: str, event_id: str, changes: dict, *, expected_revision: int | None = None,
    agent_action_key: str | None = None,
) -> dict:
    event = calendar_events_coll.find_one({
        "event_id": event_id, "source_type": "personal", "participants": user_id,
        "status": {"$ne": "cancelled"},
    })
    if not event:
        raise HTTPException(status_code=404, detail="找不到私人行程")
    if agent_action_key and event.get("last_agent_action_key") == agent_action_key:
        return serialize_event(event, user_id)
    zone = get_timezone(event.get("timezone", "Asia/Taipei"))
    local_start = as_utc(event["start_at"]).astimezone(zone)
    local_end = as_utc(event["end_at"]).astimezone(zone)
    event_all_day = bool(event.get("all_day", False))
    merged = {
        "date": local_start.date().isoformat(),
        "end_date": (
            (local_end.date() - timedelta(days=1)).isoformat()
            if event_all_day else local_end.date().isoformat()
        ),
        "all_day": event_all_day,
        "start_time": "" if event_all_day else local_start.strftime("%H:%M"),
        "end_time": "" if event_all_day else local_end.strftime("%H:%M"),
        "timezone": event.get("timezone", "Asia/Taipei"),
    }
    if changes.get("date") is not None and changes.get("end_date") is None:
        try:
            normalized_new_date = normalize_form({"date": changes["date"]})["date"]
            current_start_date = date_value.fromisoformat(merged["date"])
            current_end_date = date_value.fromisoformat(merged["end_date"])
            new_start_date = date_value.fromisoformat(normalized_new_date)
            merged["end_date"] = (
                new_start_date + (current_end_date - current_start_date)
            ).isoformat()
        except (KeyError, TypeError, ValueError):
            # The canonical parser below returns the public validation error.
            pass
    merged.update({key: value for key, value in changes.items() if value is not None})
    form = normalize_form(merged)
    start_at, end_at, zone_name = _parse_local_interval(form)
    update = {
        "start_at": start_at, "end_at": end_at, "timezone": zone_name,
        "all_day": bool(form.get("all_day")),
        "updated_at": datetime.now(timezone.utc), "revision": int(event.get("revision", 1)) + 1,
    }
    for key in ("title", "location", "notes"):
        if changes.get(key) is not None:
            update[key] = changes[key].strip()
    if agent_action_key:
        update["last_agent_action_key"] = agent_action_key
    query = {"_id": event["_id"]}
    if expected_revision is not None:
        query["revision"] = expected_revision
    result = calendar_events_coll.update_one(query, {"$set": update})
    if not result.matched_count:
        latest = calendar_events_coll.find_one({"_id": event["_id"]}) or {}
        if agent_action_key and latest.get("last_agent_action_key") == agent_action_key:
            return serialize_event(latest, user_id)
        raise HTTPException(status_code=409, detail="行程剛剛已變更，請重新確認")
    return serialize_event(calendar_events_coll.find_one({"_id": event["_id"]}), user_id)


def cancel_event(
    user_id: str, event_id: str, *, personal_only: bool = False,
    expected_revision: int | None = None, agent_action_key: str | None = None,
) -> dict:
    query = {"event_id": event_id, "participants": user_id}
    if personal_only:
        query["source_type"] = "personal"
    event = calendar_events_coll.find_one(query)
    if not event:
        raise HTTPException(status_code=404, detail="找不到行程")
    if agent_action_key and event.get("last_agent_action_key") == agent_action_key:
        return serialize_event(event, user_id)
    if event.get("status") == "cancelled":
        # A confirmation may have been waiting while somebody else cancelled
        # the event.  It is not this agent action's idempotent retry.
        if expected_revision is not None:
            raise HTTPException(status_code=409, detail="行程剛剛已變更，請重新確認")
        return serialize_event(event, user_id)
    now = datetime.now(timezone.utc)
    update = {"status": "cancelled", "cancelled_by": user_id, "cancelled_at": now, "updated_at": now}
    if agent_action_key:
        update["last_agent_action_key"] = agent_action_key
    revision_query = {"_id": event["_id"]}
    if expected_revision is not None:
        revision_query["revision"] = expected_revision
    result = calendar_events_coll.update_one(revision_query, {"$set": update, "$inc": {"revision": 1}})
    if not result.matched_count:
        latest = calendar_events_coll.find_one({"_id": event["_id"]}) or {}
        if agent_action_key and latest.get("last_agent_action_key") == agent_action_key:
            return serialize_event(latest, user_id)
        raise HTTPException(status_code=409, detail="行程剛剛已變更，請重新確認")
    return serialize_event(calendar_events_coll.find_one({"_id": event["_id"]}), user_id)


# 模型回覆常見的全形/特殊標點 → 半形對照，比對前統一正規化避免「09:00–10:00」vs「09:00-10:00」漏抓
_HINT_NORMALIZE_TABLE = str.maketrans({
    "\u2013": "-", "\u2014": "-",   # en dash / em dash
    "\uff5e": "-",                  # 全形波浪號（當作連字號）
    "\u3000": " ",                  # 全形空白
    "\uff0c": " ",                  # 全形逗號
    "：": ":",                       # 全形冒號
    "０": "0", "１": "1", "２": "2", "３": "3", "４": "4",
    "５": "5", "６": "6", "７": "7", "８": "8", "９": "9",
})


def _normalize_chinese_temporal(text: str) -> str:
    """Convert common Chinese date/time phrases into haystack-compatible forms.

    The model often echoes the owner's original wording back into an
    ``event_hint`` (e.g. ``8月25日下午5點到8點與簡的雞排約會``), so before
    matching we translate:
    - ``2026年8月25日`` → ``2026-08-25`` and ``8月25日`` → ``8/25``
    - ``下午5點到8點`` → ``17:00-20:00`` (the 上午/下午/晚上 prefix carries to
      the end of the range), ``5點半`` → ``05:30``, ``8點5分`` → ``08:05``
    - relative weekdays (``下禮拜三`` / ``下週三`` / ``下星期三``) are kept as
      a bare weekday token (``星期三``) because the haystack carries the event's
      Chinese weekday; the week offset itself cannot be resolved without a
      reference date, so it is dropped rather than compared literally.
    ISO dates and half-width times are left untouched.
    """
    if not text:
        return text

    def _hour_with_prefix(prefix: str, hour: int) -> int:
        if prefix in {"下午", "傍晚", "晚上", "中午"}:
            return hour if hour >= 12 else hour + 12
        if prefix == "凌晨":
            return hour if hour < 12 else hour - 12
        return hour  # 早上／上午／無前綴：依原數字

    # 相對星期：上/本/下下/下（禮拜|週|星期|周）X → 星期三
    text = re.sub(
        r"(?:上|這|本|下下|下)\s*(?:禮拜|週|星期|周)\s*([一二三四五六日天])",
        r" \1 ", text,
    )
    text = re.sub(
        r"(?:禮拜|星期|周)\s*([一二三四五六日天])",
        r" \1 ", text,
    )

    # 日期：2026年8月25日 → 2026-08-25；8月25日 → 8/25（對齊 haystack 的 m/d 格式）
    text = re.sub(
        r"(?P<year>(?:19|20)\d{2})年(?P<month>\d{1,2})月(?P<day>\d{1,2})(?:日|號)",
        lambda m: f" {int(m.group('year')):04d}-{int(m.group('month')):02d}-{int(m.group('day')):02d} ",
        text,
    )
    text = re.sub(
        r"(?<![\d/])(?P<month>\d{1,2})月(?P<day>\d{1,2})(?:日|號)",
        lambda m: f" {int(m.group('month'))}/{int(m.group('day'))} ",
        text,
    )

    # 時間（含範圍）：下午5點到8點 → 17:00-20:00；5點半 → 05:30
    time_pattern = re.compile(
        r"(?P<p1>凌晨|早上|上午|中午|下午|傍晚|晚上)?(?P<h1>\d{1,2})\s*點"
        r"(?:(?:(?P<m1>\d{1,2})\s*分)|(?P<hm1>半))?"
        r"(?:\s*(?:到|至|[~～\-–—])\s*"
        r"(?P<p2>凌晨|早上|上午|中午|下午|傍晚|晚上)?(?P<h2>\d{1,2})\s*點"
        r"(?:(?:(?P<m2>\d{1,2})\s*分)|(?P<hm2>半))?)?"
    )

    # 半形時間範圍：14:02到14:05 → 14:02-14:05（對齊 haystack 的 HH:MM-HH:MM）
    text = re.sub(
        r"(?P<t1>\d{1,2}:\d{2})\s*(?:到|至|[~～\-–—])\s*(?P<t2>\d{1,2}:\d{2})",
        lambda m: f" {m.group('t1')}-{m.group('t2')} ",
        text,
    )

    def _fraction(m: re.Match, tag: str) -> int:
        minutes = m.group(f"m{tag}")
        if minutes is not None:
            return int(minutes)
        return 30 if m.group(f"hm{tag}") else 0

    def _time_repl(m: re.Match) -> str:
        h1 = _hour_with_prefix(m.group("p1") or "", int(m.group("h1")))
        first = f"{h1:02d}:{_fraction(m, '1'):02d}"
        if m.group("h2") is None:
            return f" {first} "
        h2 = _hour_with_prefix(m.group("p2") or m.group("p1") or "", int(m.group("h2")))
        return f" {first}-{h2:02d}:{_fraction(m, '2'):02d} "

    return time_pattern.sub(_time_repl, text)


def _normalize_hint_text(s: str) -> str:
    text = _normalize_chinese_temporal(str(s or ""))
    return re.sub(r"\s+", " ", text.translate(_HINT_NORMALIZE_TABLE)).strip()


def _clean_event_hint(event_hint: str) -> str:
    """Apply only structural normalization before event identity matching."""
    text = _normalize_hint_text(event_hint)
    if not text:
        return ""
    # Keep punctuation handling structural; semantic wrappers stay in the
    # query and therefore cannot silently become an event identity.
    text = re.sub(r"[，。！？!?、：:；;（）()\[\]{}<>「」『』]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _event_matches_hint(event: dict, event_hint: str) -> bool:
    raw = _clean_event_hint(str(event_hint or ""))
    if not raw:
        return False
    # 「的」不整顆剝離（名稱如「與簡的雞排約會」需要它），但作為 segment 拆分符號，
    # 避免「14:05的接小孩上學」黏成一段而比對失敗。
    segments = [s for s in re.split(r"[\s,，、]+|的", raw) if s.strip()]
    if not segments:
        return False
    haystack = " ".join(str(event.get(key) or "") for key in ("title", "activity", "location"))
    try:
        zone = get_timezone(event.get("timezone") or "Asia/Taipei")
        local_start = as_utc(event["start_at"]).astimezone(zone)
        local_end = as_utc(event["end_at"]).astimezone(zone)
        haystack += (
            f" {local_start:%Y-%m-%d} {local_start.month}/{local_start.day}"
            f" {local_start:%H:%M}-{local_end:%H:%M}"
            f" {local_start.strftime('%A')}"
        )
        # 中文星期（一二三四五六日）讓「下禮拜三」類 hint 可比對
        weekday_zh = "一二三四五六日"[local_start.weekday()]
        haystack += f" {weekday_zh} {local_start:%A}"
    except Exception:
        pass
    haystack_compact = _normalize_hint_text(haystack).lower().replace(" ", "")
    # 每個段落都要出現在 haystack（順序不拘），避免 hint 與 event 欄位順序相反時漏抓
    return all(re.sub(r"\s+", "", seg.lower()) in haystack_compact for seg in segments)


def _resolve_event(user_id: str, event_hint: str, *, source_type: str | None = None) -> tuple[dict | None, str | None]:
    """Resolve one owner-visible event without giving event IDs to the planner."""
    if not re.sub(r"\s+", "", str(event_hint or "")):
        return None, "not_found"
    query: dict = {
        "participants": user_id,
        "status": {"$in": list(ACTIVE_EVENT_STATUSES)},
    }
    if source_type:
        query["source_type"] = source_type
    events = list(calendar_events_coll.find(query).sort("start_at", 1))
    matches = [event for event in events if _event_matches_hint(event, event_hint)]
    if len(matches) == 1:
        return matches[0], None
    return None, "ambiguous" if len(matches) > 1 else "not_found"


def _event_matches_explicit_temporal(event: dict, hint: str, temporal_references: dict[str, str] | None = None) -> bool:
    """Apply only explicit date/time constraints before fuzzy title matching."""
    raw_hint = str(hint or "")
    normalized = _normalize_hint_text(raw_hint)
    date_tokens: list[str] = []
    for term, resolved in (temporal_references or {}).items():
        if term and resolved and str(term) in raw_hint:
            # ``_normalize_chinese_temporal`` intentionally turns weekday
            # phrases into a short display token, so preserve the authoritative
            # date from the turn clock before that normalization loses the term.
            date_tokens.append(str(resolved)[:10])
        if term and resolved:
            normalized = normalized.replace(str(term), str(resolved))
    date_tokens.extend(re.findall(r"(?:\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}\b)", normalized))
    time_tokens = re.findall(r"\b\d{1,2}:\d{2}\b", normalized)
    if not date_tokens and not time_tokens:
        return True
    try:
        zone = get_timezone(event.get("timezone") or "Asia/Taipei")
        start = as_utc(event["start_at"]).astimezone(zone)
        end = as_utc(event["end_at"]).astimezone(zone)
    except Exception:
        return False
    if date_tokens:
        date_ok = False
        for token in date_tokens:
            if "-" in token:
                date_ok = token == start.date().isoformat()
            else:
                month, day = (int(part) for part in token.split("/"))
                date_ok = (month, day) == (start.month, start.day)
            if date_ok:
                break
        if not date_ok:
            return False
    if time_tokens:
        event_times = {start.strftime("%H:%M"), end.strftime("%H:%M")}
        if not all(f"{int(token[:2]):02d}:{token[3:]}" in event_times for token in time_tokens):
            return False
    return True


def _target_selector_value(selector: object, field_name: str) -> str:
    """Read a typed selector without importing the V3 command model."""
    if selector is None:
        return ""
    if isinstance(selector, dict):
        return str(selector.get(field_name) or "").strip()
    value = getattr(selector, field_name, None)
    return str(value or "").strip()


def _event_matches_target_selector(event: dict, target_selector: object | None) -> bool:
    """Apply exact local date/start/end filters before identity matching."""
    if target_selector is None:
        return True
    selector_date = _target_selector_value(target_selector, "date")
    selector_start = _target_selector_value(target_selector, "start_time")
    selector_end = _target_selector_value(target_selector, "end_time")
    if not any((selector_date, selector_start, selector_end)):
        return True
    try:
        zone = get_timezone(event.get("timezone") or "Asia/Taipei")
        start = as_utc(event["start_at"]).astimezone(zone)
        end = as_utc(event["end_at"]).astimezone(zone)
    except Exception:
        return False
    if selector_date and selector_date != start.date().isoformat():
        return False
    if selector_start and selector_start != start.strftime("%H:%M"):
        return False
    if selector_end and selector_end != end.strftime("%H:%M"):
        return False
    return True


def resolve_owned_event_with_candidates(
    user_id: str,
    event_hint: str,
    *,
    source_type: str | None = None,
    temporal_references: dict[str, str] | None = None,
    target_selector: object | None = None,
    limit: int = 3,
) -> tuple[dict | None, str | None, list[dict]]:
    """Resolve an event once, returning bounded server-side fuzzy candidates.

    Exact matching remains the compatibility path.  Fuzzy matching is only a
    suggestion path: explicit date/time constraints are hard filters, short or
    generic hints never trigger a fuzzy write, and a close second candidate is
    surfaced as ambiguity instead of being silently selected.
    """
    hint = str(event_hint or "").strip()
    if not hint:
        return None, "not_found", []
    query: dict = {"participants": user_id, "status": {"$in": list(ACTIVE_EVENT_STATUSES)}}
    if source_type:
        query["source_type"] = source_type
    events = list(calendar_events_coll.find(query).sort("start_at", 1))
    events = [event for event in events if _event_matches_target_selector(event, target_selector)]
    exact = [event for event in events if _event_matches_hint(event, hint)]
    if len(exact) == 1:
        return exact[0], "exact", []
    if len(exact) > 1:
        return None, "ambiguous", exact[:limit]

    cleaned = _clean_event_hint(hint)
    query_identity = re.sub(r"[\d:/\-\s]+", "", cleaned).strip().lower()
    has_temporal_constraint = bool(re.findall(r"(?:\b\d{4}-\d{1,2}-\d{1,2}\b|\b\d{1,2}/\d{1,2}\b|\b\d{1,2}:\d{2}\b)", _normalize_hint_text(hint)))
    if len(query_identity) < 2 and not has_temporal_constraint:
        return None, "not_found", []
    scored: list[tuple[float, dict]] = []
    for event in events:
        if not _event_matches_explicit_temporal(event, hint, temporal_references):
            continue
        identity = " ".join(str(event.get(key) or "") for key in ("title", "activity", "location"))
        identity = re.sub(r"\s+", "", _normalize_hint_text(identity)).lower()
        if not identity:
            continue
        score = SequenceMatcher(None, query_identity, identity).ratio()
        if len(query_identity) <= len(identity):
            score = max(
                score,
                max(
                    SequenceMatcher(None, query_identity, identity[offset:offset + len(query_identity)]).ratio()
                    for offset in range(len(identity) - len(query_identity) + 1)
                ),
            )
        # A substring match is stronger than a whole-string typo comparison.
        if query_identity in identity:
            score = max(score, 0.9)
        if len(query_identity) >= 3 and score < 0.66:
            continue
        if len(query_identity) == 2 and score < 0.5:
            continue
        scored.append((score, event))
    scored.sort(key=lambda item: item[0], reverse=True)
    if not scored:
        return None, "not_found", []
    if len(query_identity) < 3 and len(scored) != 1:
        return None, "ambiguous" if len(scored) > 1 else "not_found", [event for _score, event in scored[:limit]]
    top_score = scored[0][0]
    candidates = [event for _score, event in scored[:limit]]
    if len(scored) > 1 and top_score - scored[1][0] < 0.15:
        return None, "ambiguous", candidates
    return scored[0][1], "fuzzy_suggestion", candidates[:1]


def resolve_owned_event(
    user_id: str,
    event_hint: str,
    *,
    temporal_references: dict[str, str] | None = None,
    target_selector: object | None = None,
) -> tuple[dict | None, str | None]:
    """Resolve either a personal event or a shared date visible to this owner."""
    event, resolution, candidates = resolve_owned_event_with_candidates(
        user_id, event_hint, temporal_references=temporal_references,
        target_selector=target_selector,
    )
    _RESOLUTION_CANDIDATES[(user_id, str(event_hint or ""))] = list(candidates)
    _RESOLUTION_KIND[(user_id, str(event_hint or ""))] = str(resolution or "")
    # Keep the legacy success shape (event, None) for callers that only need
    # exact matching; preflight also accepts the richer status values.
    return event, None if resolution in {"exact", "fuzzy_suggestion"} else resolution


def get_owned_event_resolution_candidates(user_id: str, event_hint: str) -> list[dict]:
    key = (user_id, str(event_hint or ""))
    candidates = list(_RESOLUTION_CANDIDATES.pop(key, []))
    return candidates


def get_owned_event_resolution_kind(user_id: str, event_hint: str) -> str:
    key = (user_id, str(event_hint or ""))
    return str(_RESOLUTION_KIND.pop(key, ""))


def resolve_owned_event_reference(
    user_id: str, reference: dict[str, object],
) -> tuple[dict | None, str | None]:
    """Load a previously server-selected event without natural-language matching.

    ``reference`` is created by a trusted calendar read/preflight path.  The
    caller may pass only the opaque server record; ownership, active status and
    the snapshot revision are checked here before a mutation plan is built.
    """
    event_id = str(reference.get("event_id") or "")
    if not event_id:
        return None, "not_found"
    event = calendar_events_coll.find_one({
        "event_id": event_id,
        "participants": user_id,
        "status": {"$in": list(ACTIVE_EVENT_STATUSES)},
    })
    if not event:
        return None, "stale_revision" if reference.get("revision") else "not_found"
    expected_revision = reference.get("revision")
    if expected_revision is not None:
        try:
            if int(event.get("revision", 0) or 0) != int(expected_revision):
                return None, "stale_revision"
        except (TypeError, ValueError):
            return None, "stale_revision"
    expected_source = str(reference.get("source_type") or "")
    if expected_source and str(event.get("source_type") or "") != expected_source:
        return None, "stale_revision"
    return event, None


def get_owned_event_by_id(
    user_id: str,
    event_id: str,
    *,
    include_cancelled: bool = False,
    source_type: str | None = None,
) -> dict | None:
    """Load one owner-visible event for server-side verification.

    This is intentionally an executor/domain-service seam.  Callers must
    already possess the opaque server reference; it is never exposed to an
    LLM or accepted as a planner argument.
    """
    event_id = str(event_id or "").strip()
    if not event_id:
        return None
    query: dict[str, object] = {"event_id": event_id, "participants": user_id}
    if not include_cancelled:
        query["status"] = {"$in": list(ACTIVE_EVENT_STATUSES)}
    if source_type:
        query["source_type"] = source_type
    return calendar_events_coll.find_one(query)


def resolve_owned_events_for_cancel(
    user_id: str, *, mode: str, event_hints: list[str] | None = None, limit: int = 10,
) -> tuple[list[dict], str | None]:
    """Resolve a bounded cancellation set without exposing IDs to the planner.

    ``selected`` requires each human description to resolve uniquely.  ``all_upcoming``
    deliberately includes only active events that have not started yet, so a broad
    natural-language request cannot erase calendar history.
    """
    hints = [str(value or "").strip() for value in (event_hints or []) if str(value or "").strip()]
    if mode == "selected":
        if not 2 <= len(hints) <= limit or len(set(hints)) != len(hints):
            return [], "invalid_selection"
        events: list[dict] = []
        seen_ids: set[str] = set()
        for hint in hints:
            event, resolution = resolve_owned_event(user_id, hint)
            resolution_kind = get_owned_event_resolution_kind(user_id, hint)
            if resolution_kind == "fuzzy_suggestion":
                # A fuzzy suggestion is safe for candidate retrieval but not
                # for a bounded destructive batch without an explicit choice.
                return [], "ambiguous"
            if resolution:
                return [], resolution
            if not event or str(event.get("event_id") or "") in seen_ids:
                return [], "ambiguous"
            seen_ids.add(str(event.get("event_id") or ""))
            events.append(event)
        return events, None
    if mode == "all_upcoming":
        if hints:
            return [], "invalid_selection"
        now = datetime.now(timezone.utc)
        query = {
            "participants": user_id,
            "status": {"$in": list(ACTIVE_EVENT_STATUSES)},
            "start_at": {"$gte": now},
        }
        events = list(calendar_events_coll.find(query).sort("start_at", 1).limit(limit + 1))
        if len(events) > limit:
            return [], "too_many"
        return events, None
    return [], "invalid_selection"


def cancel_targets_are_current(user_id: str, targets: list[dict]) -> bool:
    """Preflight a batch confirmation before changing any event.

    This protects a confirmed batch from partially applying after another actor
    has edited or cancelled one of its targets.  Actual writes still include
    the same revision check for the race between this read and the write.
    """
    if not targets:
        return False
    for target in targets:
        event_id = str(target.get("event_id") or "")
        revision = int(target.get("event_revision", 0) or 0)
        source_type = str(target.get("event_source_type") or "")
        if not event_id or not revision or source_type not in {"personal", "date"}:
            return False
        event = calendar_events_coll.find_one({
            "event_id": event_id,
            "participants": user_id,
            "source_type": source_type,
            "status": {"$in": list(ACTIVE_EVENT_STATUSES)},
            "revision": revision,
        })
        if not event:
            return False
    return True


def find_owned_events(
    user_id: str, event_hint: str, *, date_hint: str = "", companion_user_id: str | None = None, limit: int = 10,
) -> list[dict]:
    """Return owner-visible event candidates without making the caller use IDs.

    This is intentionally a domain lookup, rather than a planner-side filter.
    ``date_hint`` is optional because a user can naturally say just "看電影";
    callers must ask a clarification when multiple events remain.
    """
    hint = str(event_hint or "").strip()
    if not hint:
        return []
    query: dict = {
        "participants": user_id,
        "status": {"$in": list(ACTIVE_EVENT_STATUSES)},
    }
    if companion_user_id:
        # This ID is executor-owned and comes only from a verified accepted
        # relationship, never from the planner or a client request.
        query.update({"source_type": "date", "participants": {"$all": [user_id, companion_user_id]}})
    candidates = []
    for event in calendar_events_coll.find(query).sort("start_at", 1):
        if not _event_matches_hint(event, hint):
            continue
        if date_hint:
            try:
                zone = get_timezone(event.get("timezone") or "Asia/Taipei")
                event_date = as_utc(event["start_at"]).astimezone(zone).date().isoformat()
            except Exception:
                continue
            if event_date != date_hint:
                continue
        candidates.append(event)
        if len(candidates) >= limit:
            break
    return candidates


def resolve_personal_event(user_id: str, event_hint: str) -> tuple[dict | None, str | None]:
    """Compatibility wrapper for callers that explicitly require a private event."""
    return _resolve_event(user_id, event_hint, source_type="personal")


def get_calendar_context(viewer_id: str, partner_id: str | None, start: datetime, end: datetime) -> dict:
    viewer_events = list_events(viewer_id, start, end)
    partner_busy = []
    if partner_id:
        query = {
            "participants": partner_id,
            "status": {"$in": list(ACTIVE_EVENT_STATUSES)},
            "start_at": {"$lt": end}, "end_at": {"$gt": start},
        }
        for event in calendar_events_coll.find(query).sort("start_at", 1):
            if viewer_id in event.get("participants", []) and event.get("source_type") == "date":
                partner_busy.append(serialize_event(event, viewer_id))
            else:
                # 對方私人行程只回 busy 投影；serialize_event 的 non-private 分支仍會帶 event_id，
                # 在此剝掉，避免對方行程內部編號外洩給 viewer。
                projection = serialize_event(event, viewer_id, include_private=False)
                projection.pop("event_id", None)
                partner_busy.append(projection)
    return {"viewer_events": viewer_events, "partner_busy": partner_busy}


def calendar_access_enabled(user_id: str) -> bool:
    profile = profiles_coll.find_one({"user_id": user_id}, {"mediator_calendar_access": 1}) or {}
    return profile.get("mediator_calendar_access", True)
