"""Durable, owner-private follow-ups after confirmed shared dates."""

from __future__ import annotations

import hashlib
import threading
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from bson.objectid import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import calendar_events_coll, db, matches_coll, profiles_coll
from services.chat_service import save_system_message_once
from services.match_state_service import verified_accepted_match_query
from services.proactive_followup_service import (
    FOLLOWUP_ACTIVE_CHAT_GRACE_SECONDS,
    followup_mode_for_user,
    is_proactive_care_enabled,
    next_quiet_end,
    owner_busy_until,
)
from services.relationship_engagement_service import (
    generate_mediator_private_room_id,
    participant_role,
)
from services.risk_block_service import risk_block_service


POST_DATE_FOLLOWUPS = db["post_date_followups"]
POST_DATE_FOLLOWUP_CONTROL = db["post_date_followup_control"]
DATE_FOLLOWUP_DELAY_SECONDS = 3600
DATE_FOLLOWUP_TTL_SECONDS = 48 * 3600
AUTO_PROMPT_SPACING_SECONDS = 3600

_STOP_EVENT = threading.Event()
_THREAD: threading.Thread | None = None


def ensure_indexes() -> None:
    try:
        POST_DATE_FOLLOWUPS.create_index(
            [("event_id", 1), ("recipient_id", 1)], unique=True,
        )
        POST_DATE_FOLLOWUPS.create_index([("status", 1), ("available_at", 1)])
    except Exception as exc:
        print(f"Post-date follow-up index setup skipped: {type(exc).__name__}")


def _as_timestamp(value) -> float:
    if isinstance(value, datetime):
        instant = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return instant.timestamp()
    return float(value or 0)


def _rollout_started_at(now: float) -> float | None:
    if followup_mode_for_user("") != "on":
        return None
    row = POST_DATE_FOLLOWUP_CONTROL.find_one_and_update(
        {"_id": "rollout"},
        {"$setOnInsert": {"activated_at": now}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    ) or {}
    return float(row.get("activated_at", now) or now)


def _event_key(event_id: str, recipient_id: str) -> str:
    digest = hashlib.sha256(f"{event_id}\x00{recipient_id}".encode()).hexdigest()
    return "post-date-followup:" + digest


def _question(end_at: float, now: float) -> str:
    zone = ZoneInfo("Asia/Taipei")
    days = (datetime.fromtimestamp(now, zone).date() - datetime.fromtimestamp(end_at, zone).date()).days
    prefix = "今天" if days <= 0 else "昨天" if days == 1 else "前天" if days == 2 else "那次"
    return f"{prefix}原本安排的約會，後來情況怎麼樣？有想跟我聊聊的嗎？"


def _valid_match(event: dict, recipient_id: str, other_id: str) -> dict | None:
    try:
        query = verified_accepted_match_query(recipient_id, other_id)
        event_match_id = str(event.get("match_id") or "")
        if event_match_id:
            query = {"$and": [query, {"_id": ObjectId(event_match_id)}]}
        match = matches_coll.find_one(query)
        if not match or risk_block_service.is_pair_blocked(recipient_id, other_id):
            return None
        coordination = match.get("date_coordination") or {}
        if (
            str(coordination.get("coordination_id") or "") != str(event.get("coordination_id") or "")
            or coordination.get("status") != "completed"
        ):
            return None
        return match
    except Exception:
        return None


def _reconcile_pending_records(now: float) -> None:
    """Cancel removed dates and move unsent records with the latest event revision."""
    try:
        rows = list(POST_DATE_FOLLOWUPS.find(
            {"status": {"$in": ["pending", "processing"]}},
        ).limit(200))
    except Exception:
        return
    for row in rows:
        event = calendar_events_coll.find_one({"event_id": row.get("event_id")})
        if not event or event.get("status") != "confirmed":
            POST_DATE_FOLLOWUPS.update_one(
                {"_id": row.get("_id"), "status": {"$in": ["pending", "processing"]}},
                {"$set": {"status": "cancelled", "updated_at": now}, "$unset": {"lease_token": "", "lease_until": ""}},
            )
            continue
        end_at = _as_timestamp(event.get("end_at"))
        POST_DATE_FOLLOWUPS.update_one(
            {"_id": row.get("_id"), "status": "pending"},
            {"$set": {
                "event_revision": event.get("revision"),
                "available_at": end_at + DATE_FOLLOWUP_DELAY_SECONDS,
                "expires_at": end_at + DATE_FOLLOWUP_TTL_SECONDS,
                "updated_at": now,
            }},
        )


def run_post_date_followups_once(*, now: float | None = None, limit: int = 50) -> dict[str, int]:
    current = time.time() if now is None else float(now)
    stats = {"scanned": 0, "shadowed": 0, "delivered": 0, "deferred": 0, "expired": 0, "skipped": 0}
    mode = followup_mode_for_user("")
    _reconcile_pending_records(current)
    rollout_at = _rollout_started_at(current) if mode == "on" else None
    earliest = datetime.fromtimestamp(current - DATE_FOLLOWUP_TTL_SECONDS, timezone.utc)
    latest = datetime.fromtimestamp(current - DATE_FOLLOWUP_DELAY_SECONDS, timezone.utc)
    try:
        events = list(calendar_events_coll.find({
            "source_type": "date",
            "status": "confirmed",
            "end_at": {"$gt": earliest, "$lte": latest},
            "participants.1": {"$exists": True},
        }).sort("end_at", 1).limit(max(1, min(limit, 200))))
    except Exception:
        return stats
    for event in events:
        end_at = _as_timestamp(event.get("end_at"))
        event_id = str(event.get("event_id") or event.get("_id") or "")
        participants = [str(item) for item in event.get("participants", []) if str(item)]
        if not event_id or len(set(participants)) != 2:
            continue
        for recipient_id in dict.fromkeys(participants):
            other_id = next(item for item in participants if item != recipient_id)
            stats["scanned"] += 1
            key = _event_key(event_id, recipient_id)
            existing = POST_DATE_FOLLOWUPS.find_one({"_id": key}) or {}
            if existing.get("status") in {"delivered", "expired", "cancelled"}:
                continue
            if existing.get("status") == "shadowed":
                rescheduled_after_rollout = (
                    mode == "on"
                    and rollout_at is not None
                    and end_at >= rollout_at
                    and existing.get("event_revision") != event.get("revision")
                )
                if not rescheduled_after_rollout:
                    continue
                POST_DATE_FOLLOWUPS.update_one(
                    {"_id": key, "status": "shadowed"},
                    {"$set": {"status": "pending", "updated_at": current}},
                )
                existing["status"] = "pending"
            if mode == "off":
                stats["skipped"] += 1
                continue
            if mode == "shadow":
                POST_DATE_FOLLOWUPS.update_one(
                    {"_id": key},
                    {"$setOnInsert": {
                        "event_id": event_id, "recipient_id": recipient_id,
                        "other_id": other_id, "event_revision": event.get("revision"),
                        "status": "shadowed", "available_at": end_at + DATE_FOLLOWUP_DELAY_SECONDS,
                        "expires_at": end_at + DATE_FOLLOWUP_TTL_SECONDS, "created_at": current,
                    }},
                    upsert=True,
                )
                stats["shadowed"] += 1
                continue
            if rollout_at is None or end_at < rollout_at:
                stats["skipped"] += 1
                continue
            profile = profiles_coll.find_one({"user_id": recipient_id}) or {}
            match = _valid_match(event, recipient_id, other_id)
            if not match or not is_proactive_care_enabled(profile):
                POST_DATE_FOLLOWUPS.update_one(
                    {"_id": key},
                    {"$set": {"status": "cancelled", "updated_at": current}, "$setOnInsert": {"created_at": current}},
                    upsert=True,
                )
                stats["skipped"] += 1
                continue
            expires_at = end_at + DATE_FOLLOWUP_TTL_SECONDS
            if current >= expires_at:
                POST_DATE_FOLLOWUPS.update_one(
                    {"_id": key}, {"$set": {"status": "expired", "updated_at": current}}, upsert=True,
                )
                stats["expired"] += 1
                continue
            defer_until = 0.0
            quiet_end = next_quiet_end(current)
            if quiet_end:
                defer_until = max(defer_until, quiet_end)
            last_activity = float(profile.get("last_user_activity_at", 0) or 0)
            if last_activity and current - last_activity < FOLLOWUP_ACTIVE_CHAT_GRACE_SECONDS:
                defer_until = max(defer_until, last_activity + FOLLOWUP_ACTIVE_CHAT_GRACE_SECONDS)
            last_prompt = float(profile.get("last_automatic_relationship_prompt_at", 0) or 0)
            if last_prompt and current - last_prompt < AUTO_PROMPT_SPACING_SECONDS:
                defer_until = max(defer_until, last_prompt + AUTO_PROMPT_SPACING_SECONDS)
            if bool(profile.get("mediator_calendar_access", True)):
                busy_until, calendar_ok = owner_busy_until(recipient_id, now=current)
                if not calendar_ok:
                    defer_until = max(defer_until, current + 30 * 60)
                elif busy_until and busy_until > current:
                    defer_until = max(defer_until, busy_until)
            if defer_until > current:
                POST_DATE_FOLLOWUPS.update_one(
                    {"_id": key},
                    {"$set": {"status": "pending", "available_at": defer_until, "updated_at": current}, "$setOnInsert": {"created_at": current}},
                    upsert=True,
                )
                stats["deferred"] += 1
                continue
            token = hashlib.sha256(f"{key}:{event.get('revision')}".encode()).hexdigest()[:24]
            try:
                claimed = POST_DATE_FOLLOWUPS.find_one_and_update(
                    {"_id": key, "$or": [
                        {"status": {"$exists": False}},
                        {"status": "pending"},
                        {"status": "processing", "lease_until": {"$lte": current}},
                    ]},
                    {"$set": {
                        "event_id": event_id, "recipient_id": recipient_id, "other_id": other_id,
                        "event_revision": event.get("revision"), "status": "processing",
                        "lease_token": token, "lease_until": current + 90,
                        "available_at": end_at + DATE_FOLLOWUP_DELAY_SECONDS,
                        "expires_at": expires_at, "updated_at": current,
                    }, "$setOnInsert": {"created_at": current}},
                    upsert=not bool(existing), return_document=ReturnDocument.AFTER,
                )
            except DuplicateKeyError:
                claimed = None
            if not claimed:
                continue
            latest_profile = profiles_coll.find_one(
                {"user_id": recipient_id},
                {"proactive_care_enabled": 1, "proactive_frequency": 1},
            ) or {}
            if not is_proactive_care_enabled(latest_profile):
                POST_DATE_FOLLOWUPS.update_one(
                    {"_id": key, "lease_token": token},
                    {"$set": {"status": "cancelled", "updated_at": current}, "$unset": {"lease_token": "", "lease_until": ""}},
                )
                stats["skipped"] += 1
                continue
            room_id = generate_mediator_private_room_id(recipient_id, other_id)
            saved = save_system_message_once(
                room_id, _question(end_at, current),
                metadata={
                    "event_type": "post_date_followup", "date_event_id": event_id,
                    "relationship_id": str(match.get("_id")), "notification_eligible": True,
                },
                event_key=key,
            )
            message_id = str(saved.get("message_id") or saved.get("_id") or "")
            if saved.get("created"):
                unread_field = f"private_unread.{participant_role(match, recipient_id)}"
                matches_coll.update_one({"_id": match["_id"]}, {"$inc": {unread_field: 1}})
            profile_query = {"user_id": recipient_id}
            if "proactive_care_enabled" in profile:
                profile_query["proactive_care_enabled"] = True
            profiles_coll.update_one(
                profile_query,
                {"$set": {
                    "last_automatic_relationship_prompt_at": current,
                    "pending_post_date_feedback": {
                        "event_id": event_id, "other_id": other_id,
                        "relationship_id": str(match.get("_id")), "question_message_id": message_id,
                        "asked_at": current, "expires_at": expires_at,
                    },
                    "post_date_followup_delivery": {
                        "event_id": event_id, "other_id": other_id,
                        "message": saved.get("content") or _question(end_at, current),
                        "message_id": message_id, "created_at": current,
                    },
                }},
            )
            POST_DATE_FOLLOWUPS.update_one(
                {"_id": key, "lease_token": token},
                {"$set": {"status": "delivered", "message_id": message_id, "delivered_at": current}, "$unset": {"lease_token": "", "lease_until": ""}},
            )
            stats["delivered"] += 1
    return stats


def _worker_loop() -> None:
    while not _STOP_EVENT.wait(30):
        try:
            run_post_date_followups_once()
        except Exception as exc:
            print(f"Post-date follow-up scheduler skipped: {type(exc).__name__}")


def start_post_date_followup_worker() -> None:
    global _THREAD
    if _THREAD and _THREAD.is_alive():
        return
    _STOP_EVENT.clear()
    _THREAD = threading.Thread(target=_worker_loop, name="post-date-followup", daemon=True)
    _THREAD.start()


def stop_post_date_followup_worker() -> None:
    _STOP_EVENT.set()
