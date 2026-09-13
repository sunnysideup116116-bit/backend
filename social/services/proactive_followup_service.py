"""Owner-scoped candidates for natural, delayed Public Ayue check-ins.

The profile extractor proposes a small typed follow-up intent. This module
owns persistence, timing, eligibility, and delivery claims; it never stores a
raw transcript and it never sends Calendar details to a model.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from bson.objectid import ObjectId
from pymongo.errors import DuplicateKeyError

from database import calendar_events_coll, db, messages_coll, profiles_coll
from services.agent_calendar_bridge import google_busy_until
from services.ayue_agent.time_context import resolve_temporal_references
from services.calendar_service import ACTIVE_EVENT_STATUSES
from services.message_use_service import is_reusable_for_care, is_reusable_for_profile
from services.profile_projection import INTERNAL_ID_RE, PROTECTED_CONTENT_RE
from services.public_ai_room_scope import is_owned_public_ai_room


PROACTIVE_FOLLOWUPS = db["proactive_followups"]
FOLLOWUP_POLICY_VERSION = "proactive-followup-v1"
FOLLOWUP_MODE_ENV = "AYUE_PROACTIVE_FOLLOWUP_MODE"
FOLLOWUP_MAX_CANDIDATES = 3
FOLLOWUP_MAX_ATTEMPTS = 3
FOLLOWUP_RETRY_DELAYS = (60, 300, 900)
FOLLOWUP_MIN_CARE_INTERVAL_SECONDS = 48 * 3600
FOLLOWUP_WEEK_WINDOW_SECONDS = 7 * 86400
FOLLOWUP_WEEKLY_LIMIT = 3
FOLLOWUP_UNANSWERED_COOLDOWN_SECONDS = 7 * 86400
FOLLOWUP_ACTIVE_CHAT_GRACE_SECONDS = 20 * 60
FOLLOWUP_BUSY_GRACE_SECONDS = 30 * 60
FOLLOWUP_QUIET_START_HOUR = 22
FOLLOWUP_QUIET_END_HOUR = 9
FOLLOWUP_TIMEZONE = "Asia/Taipei"
FOLLOWUP_DEFAULT_TTL_SECONDS = 7 * 86400
FOLLOWUP_EXPLICIT_TTL_SECONDS = 3 * 86400

ACTIVE_FOLLOWUP_STATUSES = ("pending", "processing", "asked")
_FORBIDDEN_FOLLOWUP_TEXT = re.compile(
    r"行事曆|日曆|calendar|配對|媒合|約會邀請|對方私人|工具|系統|"
    r"seed_user_|demo_user|mongo|prompt|tool_call",
    re.IGNORECASE,
)


def followup_mode_for_user(_user_id: str) -> str:
    mode = os.getenv(FOLLOWUP_MODE_ENV, "off").strip().lower()
    return mode if mode in {"off", "shadow", "on"} else "off"


def is_proactive_care_enabled(user_doc: dict[str, Any] | None) -> bool:
    """Resolve the new boolean setting with a safe legacy fallback."""
    profile = user_doc or {}
    if "proactive_care_enabled" in profile:
        return profile.get("proactive_care_enabled") is True
    # A missing legacy field is a new profile and defaults to enabled. Once an
    # old value exists, only the known values can opt in; malformed values fail
    # closed instead of silently turning care back on.
    if "proactive_frequency" not in profile:
        return True
    legacy = str(profile.get("proactive_frequency") or "").strip().lower()
    if legacy in {"none", "off", "false", "0", ""}:
        return False
    return legacy in {"60", "3600", "86400", "high", "normal", "low", "on", "true", "1"}


def ensure_indexes() -> None:
    try:
        expire_stale_followups(now=time.time())
        _backfill_active_slots()
        PROACTIVE_FOLLOWUPS.create_index(
            [("user_id", 1), ("status", 1), ("available_at", 1)],
            name="proactive_followup_due",
        )
        PROACTIVE_FOLLOWUPS.create_index(
            [("user_id", 1), ("room_id", 1), ("source_message_id", 1)],
            unique=True,
            name="proactive_followup_source",
        )
        PROACTIVE_FOLLOWUPS.create_index(
            [("lease_until", 1), ("status", 1)],
            name="proactive_followup_lease",
        )
        PROACTIVE_FOLLOWUPS.create_index(
            [("user_id", 1), ("active_slot", 1)],
            unique=True,
            partialFilterExpression={"active_slot": {"$exists": True}},
            name="proactive_followup_active_slot",
        )
    except Exception as exc:
        print(f"Proactive follow-up index setup skipped: {type(exc).__name__}")


def _backfill_active_slots() -> None:
    """Give pre-slot active candidates one of three durable user slots."""
    rows = list(PROACTIVE_FOLLOWUPS.find(
        {
            "status": {"$in": list(ACTIVE_FOLLOWUP_STATUSES)},
            "active_slot": {"$exists": False},
        },
        {"_id": 1, "user_id": 1, "created_at": 1},
    ).sort([("user_id", 1), ("created_at", 1), ("_id", 1)]))
    slots_by_user: dict[str, set[int]] = {}
    for row in rows:
        user_id = str(row.get("user_id") or "")
        if not user_id:
            continue
        if user_id not in slots_by_user:
            slots_by_user[user_id] = {
                int(value)
                for value in PROACTIVE_FOLLOWUPS.distinct(
                    "active_slot",
                    {
                        "user_id": user_id,
                        "status": {"$in": list(ACTIVE_FOLLOWUP_STATUSES)},
                        "active_slot": {"$in": list(range(1, FOLLOWUP_MAX_CANDIDATES + 1))},
                    },
                )
                if isinstance(value, int)
            }
        free_slot = next(
            (slot for slot in range(1, FOLLOWUP_MAX_CANDIDATES + 1) if slot not in slots_by_user[user_id]),
            None,
        )
        if free_slot is None:
            PROACTIVE_FOLLOWUPS.update_one(
                {"_id": row.get("_id"), "status": {"$in": list(ACTIVE_FOLLOWUP_STATUSES)}},
                {
                    "$set": {
                        "status": "suppressed",
                        "closed_reason": "migration_capacity",
                        "updated_at": time.time(),
                    },
                    "$unset": {"lease_token": "", "lease_until": "", "next_attempt_at": ""},
                },
            )
            continue
        result = PROACTIVE_FOLLOWUPS.update_one(
            {
                "_id": row.get("_id"),
                "status": {"$in": list(ACTIVE_FOLLOWUP_STATUSES)},
                "active_slot": {"$exists": False},
            },
            {"$set": {"active_slot": free_slot, "updated_at": time.time()}},
        )
        if getattr(result, "modified_count", 0):
            slots_by_user[user_id].add(free_slot)


def _safe_text(value: Any, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()[:limit]
    if INTERNAL_ID_RE.search(text) or PROTECTED_CONTENT_RE.search(text):
        return ""
    return text


def _candidate_id(user_id: str, room_id: str, source_message_id: str) -> str:
    digest = hashlib.sha256(
        f"{user_id}\x00{room_id}\x00{source_message_id}".encode("utf-8")
    ).hexdigest()
    return f"proactive-followup:{digest}"


def _source_id(value: Any) -> ObjectId | None:
    try:
        return ObjectId(str(value or ""))
    except Exception:
        return None


def _source_is_reusable(
    user_id: str, room_id: str, source_message_id: str,
) -> tuple[bool, dict[str, Any] | None]:
    source_id = _source_id(source_message_id)
    if source_id is None:
        return False, None
    try:
        source = messages_coll.find_one(
            {"_id": source_id, "room_id": room_id, "sender_id": user_id},
            {
                "content": 1,
                "metadata.owner_raw_content": 1,
                "metadata.message_use": 1,
                "timestamp": 1,
            },
        )
    except Exception:
        return False, None
    return bool(source and is_reusable_for_profile(source)), source


def _safe_candidate_query(
    user_id: str,
    room_id: str | None = None,
    statuses: tuple[str, ...] = ACTIVE_FOLLOWUP_STATUSES,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    query: dict[str, Any] = {"user_id": user_id, "status": {"$in": list(statuses)}}
    if room_id:
        query["room_id"] = room_id
    if now is not None:
        query["expires_at"] = {"$gt": float(now)}
    return query


def list_followup_candidates(
    user_id: str,
    room_id: str | None = None,
    *,
    statuses: tuple[str, ...] = ACTIVE_FOLLOWUP_STATUSES,
    now: float | None = None,
    limit: int = FOLLOWUP_MAX_CANDIDATES,
) -> list[dict[str, Any]]:
    try:
        cursor = PROACTIVE_FOLLOWUPS.find(
            _safe_candidate_query(user_id, room_id, statuses, now=now),
        ).sort([("priority", -1), ("available_at", 1), ("created_at", 1)])
        return list(cursor.limit(max(1, min(int(limit or 1), FOLLOWUP_MAX_CANDIDATES))))
    except Exception:
        return []


def followup_candidate_snapshot(
    user_id: str, room_id: str, *, now: float | None = None,
) -> list[dict[str, Any]]:
    """Return one stable internal binding plus the prompt-safe fields."""
    rows = list_followup_candidates(user_id, room_id, now=now)
    return [
        {
            "slot": slot,
            "_candidate_id": row.get("_id"),
            "_revision": int(row.get("revision", 1) or 1),
            "topic": _safe_text(row.get("topic"), 80),
            "question_goal": _safe_text(row.get("question_goal"), 120),
            "timing": str(row.get("timing") or "after_days")[:24],
            "status": str(row.get("status") or "pending")[:24],
        }
        for slot, row in enumerate(rows, start=1)
    ]


def followup_candidate_summaries(
    user_id: str, room_id: str, *, now: float | None = None,
) -> list[dict[str, Any]]:
    """Return the slot-based projection the extractor may see."""
    return [
        {key: value for key, value in item.items() if not key.startswith("_")}
        for item in followup_candidate_snapshot(user_id, room_id, now=now)
    ]


def validate_followup_proposal(
    proposal: Any,
    owner_message: str,
    recent_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Validate model output without turning it into an authority-bearing ID."""
    if proposal is None:
        return None
    try:
        raw = proposal.model_dump() if hasattr(proposal, "model_dump") else dict(proposal)
    except Exception:
        return None
    action = str(raw.get("action") or "none")
    if action == "none":
        return None
    if action not in {"create", "update", "close"}:
        return None
    context = recent_context or {}
    if action == "create" and context.get("message_kind") != "real_world_update":
        return None
    try:
        confidence = float(raw.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        return None
    evidence_span = _safe_text(raw.get("evidence_span"), 240)
    if confidence < 0.90 or raw.get("subject") != "owner" or not evidence_span:
        return None
    if evidence_span not in str(owner_message or ""):
        return None
    topic = _safe_text(raw.get("topic"), 80)
    question_goal = _safe_text(raw.get("question_goal"), 160)
    if _FORBIDDEN_FOLLOWUP_TEXT.search(topic) or _FORBIDDEN_FOLLOWUP_TEXT.search(question_goal):
        return None
    if action in {"create", "update"} and (not topic or not question_goal):
        return None
    try:
        slot = int(raw["existing_slot"]) if raw.get("existing_slot") is not None else None
    except (TypeError, ValueError):
        slot = None
    if action in {"update", "close"} and slot not in {1, 2, 3}:
        return None
    timing = str(raw.get("timing") or "none")
    if timing not in {"after_days", "after_activity", "none"}:
        timing = "none"
    try:
        wait_days = max(1, min(int(raw.get("wait_days", 3) or 3), 7))
    except (TypeError, ValueError):
        wait_days = 3
    return {
        "action": action,
        "existing_slot": slot,
        "topic": topic,
        "question_goal": question_goal,
        "timing": timing,
        "wait_days": wait_days,
        "evidence_span": evidence_span,
        "confidence": min(confidence, 1.0),
        "subject": "owner",
        "reason_code": _safe_text(raw.get("reason_code"), 60) or "accepted",
    }


def _event_date_details(
    owner_message: str, source_timestamp: float, timing: str,
) -> tuple[date | None, str]:
    try:
        local_now = datetime.fromtimestamp(
            float(source_timestamp), tz=ZoneInfo(FOLLOWUP_TIMEZONE),
        )
        resolved = resolve_temporal_references(owner_message, local_now)
        # Prefer the exact typed timing. If the profile extractor kept a
        # coarse value (for example ``下週``) while the source says ``下週五``,
        # use the longest resolved phrase so the scheduler still waits until
        # after the actual day.
        value = resolved.get(timing)
        resolved_term = timing if value is not None else ""
        if value is None and resolved:
            resolved_term = max(resolved, key=len)
            value = resolved[resolved_term]
        if value:
            return date.fromisoformat(value), resolved_term
        numeric = re.search(
            r"(?<!\d)(?:(?P<year>20\d{2})[年/-])?(?P<month>\d{1,2})[月/-](?P<day>\d{1,2})日?",
            owner_message,
        )
        if not numeric:
            return None, ""
        local_date = local_now.date()
        year = int(numeric.group("year") or local_date.year)
        parsed = date(year, int(numeric.group("month")), int(numeric.group("day")))
        if not numeric.group("year") and parsed < local_date:
            parsed = date(year + 1, parsed.month, parsed.day)
        return parsed, numeric.group(0)
    except Exception:
        return None, ""


def _event_date_from_context(
    owner_message: str, source_timestamp: float, timing: str,
) -> date | None:
    return _event_date_details(owner_message, source_timestamp, timing)[0]


def _schedule_window(
    owner_message: str,
    source_timestamp: float,
    recent_context: dict[str, Any] | None,
    proposal: dict[str, Any],
    *,
    now: float,
) -> tuple[float, float]:
    timing = str((recent_context or {}).get("timing") or "")
    event_date, resolved_term = _event_date_details(owner_message, source_timestamp, timing)
    if event_date is not None:
        if resolved_term in {"本週", "下週"}:
            event_date += timedelta(days=6)
        local_available = datetime.combine(
            event_date + timedelta(days=1), clock_time(10, 0),
            tzinfo=ZoneInfo(FOLLOWUP_TIMEZONE),
        )
        available_at = max(local_available.timestamp(), now)
        return available_at, available_at + FOLLOWUP_EXPLICIT_TTL_SECONDS
    try:
        wait_days = max(2, min(int(proposal.get("wait_days", 3) or 3), 7))
    except (TypeError, ValueError):
        wait_days = 3
    available_at = max(float(source_timestamp) + wait_days * 86400, now)
    return available_at, float(source_timestamp) + FOLLOWUP_DEFAULT_TTL_SECONDS


def expire_stale_followups(user_id: str | None = None, *, now: float | None = None) -> int:
    current = time.time() if now is None else float(now)
    query: dict[str, Any] = {
        "status": {"$in": list(ACTIVE_FOLLOWUP_STATUSES)},
        "expires_at": {"$lte": current},
        "$or": [
            {"delivery_claim_token": {"$exists": False}},
            {"delivery_finalized_at": {"$exists": True}},
        ],
    }
    if user_id:
        query["user_id"] = user_id
    try:
        result = PROACTIVE_FOLLOWUPS.update_many(
            query,
            {
                "$set": {"status": "expired", "updated_at": current},
                "$unset": {
                    "active_slot": "",
                    "lease_token": "",
                    "lease_until": "",
                    "next_attempt_at": "",
                },
                "$inc": {"revision": 1},
            },
        )
        return int(getattr(result, "modified_count", 0) or 0)
    except Exception:
        return 0


def apply_followup_proposal(
    user_id: str,
    room_id: str,
    source_message_id: str,
    owner_message: str,
    source_timestamp: float,
    proposal: Any,
    recent_context: dict[str, Any] | None = None,
    *,
    now: float | None = None,
    mode: str | None = None,
    candidate_snapshot: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    current = time.time() if now is None else float(now)
    effective_mode = mode or followup_mode_for_user(user_id)
    if effective_mode == "off":
        return {"status": "disabled"}
    if not is_owned_public_ai_room(user_id, room_id):
        return {"status": "invalid_scope"}
    reusable, source = _source_is_reusable(user_id, room_id, source_message_id)
    if not reusable or not source:
        return {"status": "excluded", "reason": "source_not_reusable"}
    normalized = validate_followup_proposal(proposal, owner_message, recent_context)
    if not normalized:
        return {"status": "noop"}
    expire_stale_followups(user_id, now=current)
    action = normalized["action"]
    if action in {"update", "close"}:
        slot = normalized.get("existing_slot")
        bindings = candidate_snapshot
        if bindings is None:
            bindings = followup_candidate_snapshot(user_id, room_id, now=current)
        binding = bindings[int(slot) - 1] if slot and int(slot) <= len(bindings) else None
        if not isinstance(binding, dict) or binding.get("_candidate_id") is None:
            return {"status": "stale_slot"}
        candidate_id = binding.get("_candidate_id")
        candidate_revision = int(binding.get("_revision", 1) or 1)
        if action == "close":
            update = {
                "$set": {
                    "status": "completed",
                    "closed_reason": normalized["reason_code"],
                    "closed_source_message_id": source_message_id,
                    "closed_evidence_span": normalized["evidence_span"],
                    "closed_at": current,
                    "updated_at": current,
                },
                "$unset": {
                    "active_slot": "",
                    "lease_token": "",
                    "lease_until": "",
                    "next_attempt_at": "",
                },
                "$inc": {"revision": 1},
            }
        else:
            available_at, expires_at = _schedule_window(
                owner_message, source_timestamp, recent_context, normalized, now=current,
            )
            update_unset = {"lease_token": "", "lease_until": ""}
            if expires_at <= current:
                update_unset["active_slot"] = ""
            update = {
                "$set": {
                    "topic": normalized["topic"],
                    "question_goal": normalized["question_goal"],
                    "evidence_span": normalized["evidence_span"],
                    "source_message_id": source_message_id,
                    "source_timestamp": float(source_timestamp),
                    "available_at": available_at,
                    "expires_at": expires_at,
                    "next_attempt_at": available_at,
                    "status": "pending" if expires_at > current else "expired",
                    "mode": effective_mode,
                    "updated_at": current,
                },
                "$unset": update_unset,
                "$inc": {"revision": 1},
            }
        try:
            result = PROACTIVE_FOLLOWUPS.update_one(
                {
                    "_id": candidate_id,
                    "user_id": user_id,
                    "room_id": room_id,
                    "revision": candidate_revision,
                    "status": {"$in": list(ACTIVE_FOLLOWUP_STATUSES)},
                },
                update,
            )
        except Exception:
            return {"status": "storage_unavailable"}
        return {"status": "updated" if getattr(result, "modified_count", 0) else "stale"}

    all_active = list_followup_candidates(user_id, None, now=current)
    if len(all_active) >= FOLLOWUP_MAX_CANDIDATES:
        return {"status": "at_capacity"}
    available_at, expires_at = _schedule_window(
        owner_message, source_timestamp, recent_context, normalized, now=current,
    )
    if expires_at <= current:
        return {"status": "expired"}
    candidate = {
        "_id": _candidate_id(user_id, room_id, source_message_id),
        "version": FOLLOWUP_POLICY_VERSION,
        "user_id": user_id,
        "room_id": room_id,
        "source_message_id": source_message_id,
        "source_timestamp": float(source_timestamp),
        "evidence_span": normalized["evidence_span"],
        "topic": normalized["topic"],
        "question_goal": normalized["question_goal"],
        "timing": normalized["timing"],
        "available_at": available_at,
        "expires_at": expires_at,
        "next_attempt_at": available_at,
        "priority": round(float(normalized["confidence"]), 4),
        "status": "pending",
        "mode": effective_mode,
        "revision": 1,
        "attempt_count": 0,
        "no_response_count": 0,
        "created_at": current,
        "updated_at": current,
    }
    for active_slot in range(1, FOLLOWUP_MAX_CANDIDATES + 1):
        candidate_with_slot = {**candidate, "active_slot": active_slot}
        try:
            PROACTIVE_FOLLOWUPS.insert_one(candidate_with_slot)
            return {"status": "created", "candidate_id": candidate["_id"]}
        except DuplicateKeyError:
            try:
                existing = PROACTIVE_FOLLOWUPS.find_one(
                    {"_id": candidate["_id"], "user_id": user_id, "room_id": room_id},
                    {"status": 1},
                )
            except Exception:
                return {"status": "storage_unavailable"}
            if existing:
                # A source is idempotent for its entire lifecycle. A retrying
                # extractor must not resurrect a terminal candidate.
                return {"status": "already_exists"}
            continue
        except Exception:
            return {"status": "storage_unavailable"}
    return {"status": "at_capacity"}


def cancel_pending_followups(
    user_id: str, *, room_id: str | None = None, reason: str = "cancelled",
) -> int:
    query: dict[str, Any] = {
        "user_id": user_id,
        "status": {"$in": list(ACTIVE_FOLLOWUP_STATUSES)},
        "$or": [
            {"delivery_claim_token": {"$exists": False}},
            {"delivery_finalized_at": {"$exists": True}},
        ],
    }
    if room_id:
        query["room_id"] = room_id
    try:
        result = PROACTIVE_FOLLOWUPS.update_many(
            query,
            {
                "$set": {"status": "cancelled", "closed_reason": reason, "updated_at": time.time()},
                "$unset": {
                    "active_slot": "",
                    "lease_token": "",
                    "lease_until": "",
                    "next_attempt_at": "",
                    "delivery_claim_token": "",
                },
                "$inc": {"revision": 1},
            },
        )
        return int(getattr(result, "modified_count", 0) or 0)
    except Exception:
        return 0


def expire_followup_candidate(candidate: dict[str, Any], *, now: float | None = None) -> bool:
    current = time.time() if now is None else float(now)
    try:
        result = PROACTIVE_FOLLOWUPS.update_one(
            {
                "_id": candidate.get("_id"),
                "revision": int(candidate.get("revision", 1) or 1),
                "status": {"$in": list(ACTIVE_FOLLOWUP_STATUSES)},
            },
            {
                "$set": {"status": "expired", "updated_at": current},
                "$inc": {"revision": 1},
                "$unset": {
                    "active_slot": "",
                    "lease_token": "",
                    "lease_until": "",
                    "next_attempt_at": "",
                    "delivery_claim_token": "",
                },
            },
        )
        return bool(getattr(result, "modified_count", 0))
    except Exception:
        return False


def claim_followup_candidate(
    candidate: dict[str, Any], *, now: float | None = None,
) -> tuple[str, dict[str, Any]] | None:
    current = time.time() if now is None else float(now)
    token = uuid.uuid4().hex
    try:
        result = PROACTIVE_FOLLOWUPS.find_one_and_update(
            {
                "_id": candidate.get("_id"),
                "expires_at": {"$gt": current},
                "$or": [
                    {
                        "status": "pending",
                        "available_at": {"$lte": current},
                        "$or": [
                            {"next_attempt_at": {"$lte": current}},
                            {"next_attempt_at": {"$exists": False}},
                        ],
                    },
                    {
                        "status": "processing",
                        "lease_until": {"$lte": current},
                    },
                ],
            },
            {
                "$set": {
                    "status": "processing",
                    "lease_token": token,
                    "lease_until": current + 90,
                    "updated_at": current,
                },
                "$inc": {"attempt_count": 1},
            },
            return_document=True,
        )
    except Exception:
        return None
    if not result:
        return None
    return token, dict(result)


def release_followup_candidate(
    candidate: dict[str, Any], token: str, *, now: float, retry: bool,
) -> None:
    attempts = max(1, int(candidate.get("attempt_count", 1) or 1))
    if retry and attempts < FOLLOWUP_MAX_ATTEMPTS:
        next_attempt_at = now + FOLLOWUP_RETRY_DELAYS[
            min(attempts - 1, len(FOLLOWUP_RETRY_DELAYS) - 1)
        ]
        update = {
            "$set": {
                "status": "pending",
                "next_attempt_at": next_attempt_at,
                "updated_at": now,
            },
            "$unset": {"lease_token": "", "lease_until": ""},
        }
    else:
        update = {
            "$set": {"status": "suppressed", "updated_at": now},
            "$unset": {
                "active_slot": "",
                "lease_token": "",
                "lease_until": "",
                "next_attempt_at": "",
            },
        }
    try:
        PROACTIVE_FOLLOWUPS.update_one(
            {"_id": candidate.get("_id"), "status": "processing", "lease_token": token},
            update,
        )
    except Exception:
        pass


def mark_followup_asked(
    candidate: dict[str, Any], token: str, *, now: float, message_id: str,
) -> bool:
    try:
        result = PROACTIVE_FOLLOWUPS.update_one(
            {
                "_id": candidate.get("_id"),
                "status": "processing",
                "lease_token": token,
            },
            {
                "$set": {
                    "status": "asked",
                    "last_asked_at": now,
                    "delivery_message_id": message_id,
                    "updated_at": now,
                },
                "$unset": {"lease_token": "", "lease_until": ""},
                "$inc": {"revision": 1},
            },
        )
        if getattr(result, "modified_count", 0):
            return True
    except Exception:
        pass
    try:
        existing = PROACTIVE_FOLLOWUPS.find_one(
            {
                "_id": candidate.get("_id"),
                "status": "asked",
                "delivery_message_id": message_id,
            },
            {"_id": 1},
        )
        return bool(existing)
    except Exception:
        return False


def _delivery_times(profile: dict[str, Any], now: float) -> list[float]:
    raw = profile.get("proactive_care_delivery_times") or []
    result: list[float] = []
    for value in raw:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            continue
        if timestamp > now - FOLLOWUP_WEEK_WINDOW_SECONDS:
            result.append(timestamp)
    return sorted(result)


def unanswered_followup_until(
    user_id: str, *, now: float | None = None,
) -> tuple[float | None, bool]:
    """Return the latest seven-day cooldown end across unanswered care."""
    current = time.time() if now is None else float(now)
    try:
        asked_candidates = list(PROACTIVE_FOLLOWUPS.find(
            {
                "user_id": user_id,
                "last_asked_at": {
                    "$gt": current - FOLLOWUP_UNANSWERED_COOLDOWN_SECONDS,
                    "$lte": current,
                },
            },
            {"room_id": 1, "last_asked_at": 1},
        ).sort([("last_asked_at", -1)]).limit(FOLLOWUP_MAX_CANDIDATES))
        profile = profiles_coll.find_one(
            {"user_id": user_id},
            {"proactive_care_delivery_history": 1},
        ) or {}
        delivery_history = profile.get("proactive_care_delivery_history") or []
        asked_records = [
            {
                "room_id": item.get("room_id"),
                "last_asked_at": item.get("asked_at"),
            }
            for item in delivery_history[-12:]
            if isinstance(item, dict)
            and current - FOLLOWUP_UNANSWERED_COOLDOWN_SECONDS
            < float(item.get("asked_at", 0) or 0)
            <= current
        ]
        asked_records.extend(asked_candidates)
        if not asked_records:
            return None, True
        cooldown_ends: list[float] = []
        seen: set[tuple[str, float]] = set()
        for asked in sorted(
            asked_records,
            key=lambda item: float(item.get("last_asked_at", 0) or 0),
            reverse=True,
        ):
            asked_at = float(asked.get("last_asked_at", 0) or 0)
            room_id = str(asked.get("room_id") or "")
            if not asked_at or not room_id:
                return None, False
            record_key = (room_id, asked_at)
            if record_key in seen:
                continue
            seen.add(record_key)
            owner_response = messages_coll.find_one(
                {
                    "room_id": room_id,
                    "sender_id": user_id,
                    "timestamp": {"$gt": asked_at},
                },
                {"_id": 1},
                sort=[("timestamp", 1), ("_id", 1)],
            )
            if not owner_response:
                cooldown_ends.append(
                    asked_at + FOLLOWUP_UNANSWERED_COOLDOWN_SECONDS,
                )
    except Exception:
        return None, False
    if not cooldown_ends:
        return None, True
    return max(cooldown_ends), True


def followup_delivery_claim_is_current(
    user_id: str,
    candidate_id: Any,
    candidate_token: str,
    delivery_token: str,
    *,
    now: float | None = None,
) -> bool:
    """Recheck consent and both leases immediately before message persistence."""
    current = time.time() if now is None else float(now)
    try:
        candidate = PROACTIVE_FOLLOWUPS.find_one(
            {
                "_id": candidate_id,
                "user_id": user_id,
                "status": "processing",
                "lease_token": candidate_token,
                "lease_until": {"$gt": current},
                "expires_at": {"$gt": current},
            },
            {"_id": 1},
        )
        profile = profiles_coll.find_one(
            {
                "user_id": user_id,
                "proactive_care_delivery_claim_id": delivery_token,
                "proactive_care_delivery_claim_candidate_id": str(candidate_id or ""),
                "proactive_care_delivery_claim_until": {"$gt": current},
            },
            {"proactive_care_enabled": 1, "proactive_frequency": 1},
        )
    except Exception:
        return False
    return bool(candidate and profile and is_proactive_care_enabled(profile))


def claim_delivery_slot(user_id: str, candidate_id: str, *, now: float | None = None) -> str | None:
    current = time.time() if now is None else float(now)
    try:
        profile = profiles_coll.find_one(
            {"user_id": user_id},
            {
                "proactive_care_enabled": 1,
                "proactive_frequency": 1,
                "proactive_care_last_sent_at": 1,
                "proactive_care_delivery_times": 1,
                "proactive_care_delivery_claim_until": 1,
                "proactive_care_pending_delivery": 1,
            },
        ) or {}
    except Exception:
        return None
    if not is_proactive_care_enabled(profile):
        return None
    pending = profile.get("proactive_care_pending_delivery")
    if isinstance(pending, dict) and pending.get("event_key"):
        return None
    last_sent = float(profile.get("proactive_care_last_sent_at", 0) or 0)
    if last_sent and current - last_sent < FOLLOWUP_MIN_CARE_INTERVAL_SECONDS:
        return None
    if len(_delivery_times(profile, current)) >= FOLLOWUP_WEEKLY_LIMIT:
        return None
    unanswered_until, unanswered_available = unanswered_followup_until(
        user_id, now=current,
    )
    if not unanswered_available or (unanswered_until and unanswered_until > current):
        return None
    token = uuid.uuid4().hex
    query: dict[str, Any] = {
        "user_id": user_id,
        "proactive_care_pending_delivery": {"$exists": False},
        "$or": [
            {"proactive_care_delivery_claim_until": {"$exists": False}},
            {"proactive_care_delivery_claim_until": {"$lte": current}},
        ],
    }
    # Re-check the new consent bit inside the atomic claim when it exists, so
    # a pause racing the scheduler cannot still reserve a delivery slot.
    if "proactive_care_enabled" in profile:
        query["proactive_care_enabled"] = True
    if last_sent:
        query["proactive_care_last_sent_at"] = last_sent
    try:
        result = profiles_coll.find_one_and_update(
            query,
            {
                "$set": {
                    "proactive_care_delivery_claim_id": token,
                    "proactive_care_delivery_claim_candidate_id": candidate_id,
                    "proactive_care_delivery_claim_until": current + 90,
                },
            },
        )
    except Exception:
        return None
    return token if result else None


def stage_delivery_slot(
    user_id: str,
    token: str,
    *,
    candidate_id: str,
    candidate_token: str,
    now: float,
    event_key: str,
    message: str,
    origin_room_id: str,
    require_enabled_flag: bool = False,
) -> dict[str, Any] | None:
    """Persist generated care independently from its mutable candidate."""
    pending = {
        "event_key": event_key,
        "candidate_id": candidate_id,
        "candidate_token": candidate_token,
        "delivery_token": token,
        "message": message,
        "origin_room_id": origin_room_id,
        "asked_at": now,
        "staged_at": now,
    }
    query: dict[str, Any] = {
        "user_id": user_id,
        "proactive_care_delivery_claim_id": token,
        "proactive_care_delivery_claim_candidate_id": candidate_id,
        "$or": [
            {"proactive_care_pending_delivery": {"$exists": False}},
            {"proactive_care_pending_delivery.event_key": event_key},
        ],
    }
    if require_enabled_flag:
        query["proactive_care_enabled"] = True
    try:
        result = profiles_coll.update_one(
            query,
            {"$set": {"proactive_care_pending_delivery": pending}},
        )
        if getattr(result, "modified_count", 0) or getattr(result, "matched_count", 0):
            return pending
    except Exception:
        pass
    try:
        profile = profiles_coll.find_one(
            {
                "user_id": user_id,
                "proactive_care_pending_delivery.event_key": event_key,
            },
            {"proactive_care_pending_delivery": 1},
        )
        existing = (profile or {}).get("proactive_care_pending_delivery")
        return dict(existing) if isinstance(existing, dict) else None
    except Exception:
        return None


def list_pending_delivery_slots(*, limit: int = 40) -> list[dict[str, Any]]:
    try:
        return list(profiles_coll.find(
            {"proactive_care_pending_delivery.event_key": {"$exists": True}},
            {"user_id": 1, "proactive_care_enabled": 1, "proactive_frequency": 1,
             "proactive_care_pending_delivery": 1},
        ).sort("proactive_care_pending_delivery.staged_at", 1).limit(
            max(1, min(int(limit or 1), 100)),
        ))
    except Exception:
        return []


def commit_delivery_slot(
    user_id: str,
    *,
    event_key: str,
    now: float,
    message_id: str,
    message: str,
    origin_room_id: str,
    require_enabled_flag: bool = False,
) -> bool:
    """Atomically publish a poll marker and account for one delivered message."""
    query: dict[str, Any] = {
        "user_id": user_id,
        "proactive_care_pending_delivery.event_key": event_key,
        "proactive_care_last_delivery_message_id": {"$ne": message_id},
        "proactive_care_delivery_history.message_id": {"$ne": message_id},
    }
    if require_enabled_flag:
        query["proactive_care_enabled"] = True
    try:
        result = profiles_coll.update_one(
            query,
            {
                "$set": {
                    "proactive_care_last_sent_at": now,
                    "proactive_care_delivery_updated_at": now,
                    "proactive_care_last_delivery_message_id": message_id,
                    "proactive_care_delivery": {
                        "message": message,
                        "message_id": message_id,
                        "created_at": now,
                        "origin_room_id": origin_room_id,
                    },
                },
                "$push": {
                    "proactive_care_delivery_times": {
                        "$each": [now],
                        "$slice": -12,
                    },
                    "proactive_care_delivery_history": {
                        "$each": [{
                            "message_id": message_id,
                            "room_id": origin_room_id,
                            "asked_at": now,
                        }],
                        "$slice": -12,
                    },
                },
                "$unset": {
                    "proactive_care_pending_delivery": "",
                    "proactive_care_delivery_claim_id": "",
                    "proactive_care_delivery_claim_candidate_id": "",
                    "proactive_care_delivery_claim_until": "",
                },
            },
        )
        if getattr(result, "modified_count", 0) or getattr(result, "matched_count", 0):
            return True
    except Exception:
        pass
    try:
        existing = profiles_coll.find_one(
            {
                "user_id": user_id,
                "$or": [
                    {"proactive_care_last_delivery_message_id": message_id},
                    {"proactive_care_delivery_history.message_id": message_id},
                ],
            },
            {"_id": 1, "proactive_care_pending_delivery": 1},
        )
        if existing:
            # A candidate write may have failed after this event was delivered.
            # Re-staging that event must not leave a permanent pending lock or
            # recreate a consumed notice. Clear only this event's pending slot.
            pending = existing.get("proactive_care_pending_delivery") or {}
            if pending.get("event_key") != event_key:
                return True
            profiles_coll.update_one(
                {
                    "user_id": user_id,
                    "proactive_care_pending_delivery.event_key": event_key,
                    "$or": [
                        {"proactive_care_last_delivery_message_id": message_id},
                        {"proactive_care_delivery_history.message_id": message_id},
                    ],
                },
                {"$unset": {
                    "proactive_care_pending_delivery": "",
                    "proactive_care_delivery_claim_id": "",
                    "proactive_care_delivery_claim_candidate_id": "",
                    "proactive_care_delivery_claim_until": "",
                }},
            )
            return True
        return False
    except Exception:
        return False


def cancel_pending_delivery_slot(user_id: str, *, event_key: str | None = None) -> None:
    query: dict[str, Any] = {"user_id": user_id}
    if event_key is not None:
        query["proactive_care_pending_delivery.event_key"] = event_key
    try:
        profiles_coll.update_one(
            query,
            {"$unset": {
                "proactive_care_pending_delivery": "",
                "proactive_care_delivery_claim_id": "",
                "proactive_care_delivery_claim_candidate_id": "",
                "proactive_care_delivery_claim_until": "",
            }},
        )
    except Exception:
        pass


def authorize_pending_delivery_slot(
    user_id: str, event_key: str, profile: dict[str, Any], *, now: float,
) -> bool:
    """Atomically check the live opt-in and pending event before first save."""
    query: dict[str, Any] = {
        "user_id": user_id,
        "proactive_care_pending_delivery.event_key": event_key,
        "last_user_activity_at": profile.get("last_user_activity_at", {"$exists": False}),
        "mediator_calendar_access": profile.get("mediator_calendar_access", {"$exists": False}),
    }
    if "proactive_care_enabled" in profile:
        query["proactive_care_enabled"] = True
    else:
        query["proactive_care_enabled"] = {"$exists": False}
        query["proactive_frequency"] = profile.get("proactive_frequency", {"$exists": False})
    try:
        result = profiles_coll.update_one(
            query,
            {"$set": {"proactive_care_pending_delivery.authorized_at": now}},
        )
        return bool(getattr(result, "matched_count", 0) or getattr(result, "modified_count", 0))
    except Exception:
        return False


def release_delivery_slot(user_id: str, token: str, *, candidate_id: str) -> None:
    try:
        profiles_coll.update_one(
            {
                "user_id": user_id,
                "proactive_care_delivery_claim_id": token,
                "proactive_care_delivery_claim_candidate_id": candidate_id,
            },
            {
                "$unset": {
                    "proactive_care_delivery_claim_id": "",
                    "proactive_care_delivery_claim_candidate_id": "",
                    "proactive_care_delivery_claim_until": "",
                },
            },
        )
    except Exception:
        pass


def next_quiet_end(now: float, *, timezone_name: str = FOLLOWUP_TIMEZONE) -> float | None:
    try:
        zone = ZoneInfo(timezone_name)
        local = datetime.fromtimestamp(now, tz=zone)
        if local.hour >= FOLLOWUP_QUIET_START_HOUR:
            target_date = local.date() + timedelta(days=1)
        elif local.hour < FOLLOWUP_QUIET_END_HOUR:
            target_date = local.date()
        else:
            return None
        return datetime.combine(
            target_date,
            clock_time(FOLLOWUP_QUIET_END_HOUR, 0),
            tzinfo=zone,
        ).timestamp()
    except Exception:
        return now + 3600


def owner_busy_until(user_id: str, *, now: float) -> tuple[float | None, bool]:
    """Return only busy-until state; False means the read was unavailable."""
    try:
        event = calendar_events_coll.find_one(
            {
                "participants": user_id,
                "status": {"$in": list(ACTIVE_EVENT_STATUSES)},
                "start_at": {"$lte": datetime.fromtimestamp(now, tz=timezone.utc)},
                "end_at": {"$gt": datetime.fromtimestamp(now, tz=timezone.utc)},
            },
            {"end_at": 1, "_id": 0},
        )
    except Exception:
        return None, False
    internal_until: float | None = None
    if event:
        end_at = event.get("end_at")
        if isinstance(end_at, datetime):
            if end_at.tzinfo is None:
                end_at = end_at.replace(tzinfo=timezone.utc)
            internal_until = end_at.timestamp()
        else:
            try:
                internal_until = float(end_at)
            except (TypeError, ValueError):
                return None, False
    try:
        external_until, external_ok = google_busy_until(user_id, now=now)
    except Exception:
        return None, False
    if not external_ok:
        return None, False
    candidates = [value for value in (internal_until, external_until) if value]
    return (max(candidates) if candidates else None), True


def defer_followup_candidate(candidate: dict[str, Any], *, until: float, now: float) -> None:
    try:
        PROACTIVE_FOLLOWUPS.update_one(
            {
                "_id": candidate.get("_id"),
                "status": {"$in": list(ACTIVE_FOLLOWUP_STATUSES)},
            },
            {
                "$set": {
                    "available_at": float(until),
                    "next_attempt_at": float(until),
                    "updated_at": now,
                },
            },
        )
    except Exception:
        pass


def record_owner_activity(user_id: str, *, now: float | None = None) -> float:
    current = time.time() if now is None else float(now)
    try:
        profiles_coll.update_one(
            {"user_id": user_id},
            {"$set": {"last_user_activity_at": current}},
            upsert=True,
        )
    except Exception:
        pass
    return current


__all__ = [
    "ACTIVE_FOLLOWUP_STATUSES",
    "FOLLOWUP_DEFAULT_TTL_SECONDS",
    "FOLLOWUP_EXPLICIT_TTL_SECONDS",
    "FOLLOWUP_MAX_ATTEMPTS",
    "FOLLOWUP_MAX_CANDIDATES",
    "FOLLOWUP_MIN_CARE_INTERVAL_SECONDS",
    "FOLLOWUP_POLICY_VERSION",
    "FOLLOWUP_RETRY_DELAYS",
    "FOLLOWUP_UNANSWERED_COOLDOWN_SECONDS",
    "FOLLOWUP_WEEKLY_LIMIT",
    "PROACTIVE_FOLLOWUPS",
    "apply_followup_proposal",
    "authorize_pending_delivery_slot",
    "cancel_pending_delivery_slot",
    "cancel_pending_followups",
    "claim_delivery_slot",
    "claim_followup_candidate",
    "commit_delivery_slot",
    "defer_followup_candidate",
    "ensure_indexes",
    "expire_followup_candidate",
    "expire_stale_followups",
    "followup_candidate_snapshot",
    "followup_candidate_summaries",
    "followup_delivery_claim_is_current",
    "followup_mode_for_user",
    "is_proactive_care_enabled",
    "list_followup_candidates",
    "list_pending_delivery_slots",
    "mark_followup_asked",
    "next_quiet_end",
    "owner_busy_until",
    "record_owner_activity",
    "release_delivery_slot",
    "release_followup_candidate",
    "stage_delivery_slot",
    "unanswered_followup_until",
    "validate_followup_proposal",
]
