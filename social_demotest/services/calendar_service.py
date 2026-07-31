"""Canonical calendar storage, validation, privacy filtering, and conflict checks."""

from __future__ import annotations

from datetime import date as date_value, datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from uuid import uuid4

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from database import calendar_events_coll, profiles_coll


ACTIVE_EVENT_STATUSES = {"confirmed", "pending_reconfirmation"}


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
            "coordination_id", unique=True, sparse=True,
            partialFilterExpression={"source_type": "date"},
        )
        calendar_events_coll.create_index([("participants", 1), ("start_at", 1), ("status", 1)])
        calendar_events_coll.create_index("agent_action_key", unique=True, sparse=True)
    except Exception as exc:  # Database may be unavailable during local startup.
        print(f"Calendar index setup skipped: {exc}")


def _parse_local_interval(form: dict) -> tuple[datetime, datetime, str]:
    form = normalize_form(form)
    required = ("date", "start_time", "end_time")
    missing = [key for key in required if not str(form.get(key, "")).strip()]
    if missing:
        labels = {"date": "日期", "start_time": "開始時間", "end_time": "結束時間"}
        raise HTTPException(status_code=400, detail=f"請填寫：{'、'.join(labels[key] for key in missing)}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", form["date"]):
        raise HTTPException(status_code=400, detail="日期格式需為 YYYY-MM-DD")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", form["start_time"]):
        raise HTTPException(status_code=400, detail="開始時間格式需為 HH:MM")
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", form["end_time"]):
        raise HTTPException(status_code=400, detail="結束時間格式需為 HH:MM")
    zone_name = form.get("timezone") or "Asia/Taipei"
    try:
        date_value.fromisoformat(form["date"])
        zone = get_timezone(zone_name)
        start = datetime.fromisoformat(f"{form['date']}T{form['start_time']}").replace(tzinfo=zone)
        end = datetime.fromisoformat(f"{form['date']}T{form['end_time']}").replace(tzinfo=zone)
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

    return {
        # ``title`` is the canonical label for personal events.  Retain it
        # through normalization so an agent confirmation can accurately show
        # both a new event and an edited event without falling back to an
        # unrelated activity field.
        "title": str(form.get("title") or form.get("activity") or "").strip(),
        "date": raw_date,
        "start_time": normalize_time(form.get("start_time")),
        "end_time": normalize_time(form.get("end_time")),
        "timezone": str(form.get("timezone") or "Asia/Taipei").strip(),
        "activity": str(form.get("activity") or "").strip(),
        "location": str(form.get("location") or "").strip(),
        "budget": str(form.get("budget") or "").strip(),
        "notes": str(form.get("notes") or "").strip(),
    }


def serialize_event(event: dict, viewer_id: str | None = None, include_private: bool = True) -> dict:
    result = {
        "event_id": event["event_id"],
        "source_type": event.get("source_type"),
        "participants": event.get("participants", []),
        "status": event.get("status"),
        "title": event.get("title", ""),
        "start_at": iso_utc(event.get("start_at")),
        "end_at": iso_utc(event.get("end_at")),
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
        "title": payload["title"].strip(),
        "start_at": start_at,
        "end_at": end_at,
        "timezone": zone_name,
        "location": payload.get("location", "").strip(),
        "notes": payload.get("notes", "").strip(),
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
    merged = {
        "date": as_utc(event["start_at"]).astimezone(zone).date().isoformat(),
        "start_time": as_utc(event["start_at"]).astimezone(zone).strftime("%H:%M"),
        "end_time": as_utc(event["end_at"]).astimezone(zone).strftime("%H:%M"),
        "timezone": event.get("timezone", "Asia/Taipei"),
    }
    merged.update({key: value for key, value in changes.items() if value is not None})
    form = normalize_form(merged)
    start_at, end_at, zone_name = _parse_local_interval(form)
    update = {
        "start_at": start_at, "end_at": end_at, "timezone": zone_name,
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


def _event_matches_hint(event: dict, event_hint: str) -> bool:
    hint = re.sub(r"\s+", "", str(event_hint or "").lower())
    if not hint:
        return False
    haystack = " ".join(str(event.get(key) or "") for key in ("title", "activity", "location"))
    try:
        zone = get_timezone(event.get("timezone") or "Asia/Taipei")
        local_start = as_utc(event["start_at"]).astimezone(zone)
        haystack += f" {local_start:%Y-%m-%d} {local_start.month}/{local_start.day}"
    except Exception:
        pass
    return hint in re.sub(r"\s+", "", haystack.lower())


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


def resolve_owned_event(user_id: str, event_hint: str) -> tuple[dict | None, str | None]:
    """Resolve either a personal event or a shared date visible to this owner."""
    return _resolve_event(user_id, event_hint)


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
                partner_busy.append(serialize_event(event, viewer_id, include_private=False))
    return {"viewer_events": viewer_events, "partner_busy": partner_busy}


def calendar_access_enabled(user_id: str) -> bool:
    profile = profiles_coll.find_one({"user_id": user_id}, {"mediator_calendar_access": 1}) or {}
    return profile.get("mediator_calendar_access", True)
