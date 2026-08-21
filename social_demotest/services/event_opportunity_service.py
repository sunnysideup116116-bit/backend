"""Canonical Mongo workflow for a graph-discovered Event opportunity."""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from typing import Any

import requests
from pymongo.errors import DuplicateKeyError

from database import matches_coll, profiles_coll
from services.ayue_agent.public_relationship_projection import anonymize_counterparty_text
from services.match_reason_service import public_personality_phrase
from services.mediator_event_service import queue_mediator_event
from services.match_state_service import verified_accepted_match_query
from services.proposal_namespace import (
    EVENT_INVITATION_NAMESPACE,
    live_proposal_query,
    namespace_clause,
    participant_pair_key,
)


AGENT_EVENT_OPPORTUNITY_URL = "http://127.0.0.1:9001/api/proactive_event_match"
LIVE_STATUSES = {"draft", "pending"}
DEFAULT_AUTO_SCAN_MAX_PROPOSALS = 3
DEFAULT_PAIR_DECLINE_COOLDOWN_DAYS = 7

_scan_lock = threading.Lock()
_scan_requested = threading.Event()


def ensure_event_opportunity_indexes() -> None:
    try:
        matches_coll.create_index(
            "event_opportunity_key", unique=True, sparse=True,
            name="one_proposal_per_event_pair",
        )
        matches_coll.create_index(
            [("proposal_source", 1), ("created_at", -1)],
            name="event_opportunity_recent_scan",
        )
        matches_coll.create_index(
            [("participant_pair_key", 1), ("status", 1), ("declined_at", -1)],
            name="proposal_pair_decline_cooldown",
        )
    except Exception as exc:
        print(f"[event-opportunity] index setup skipped: {type(exc).__name__}")


def _live_query(user_id: str) -> dict[str, Any]:
    return live_proposal_query(user_id, EVENT_INVITATION_NAMESPACE)


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def _positive_timestamps(values: Any, limit: int = 8) -> list[float]:
    result = []
    for value in list(values or [])[:limit]:
        try:
            timestamp = float(value)
        except (TypeError, ValueError):
            continue
        if timestamp > 0 and timestamp not in result:
            result.append(timestamp)
    return sorted(result)


def _pair_decline_cooldown_seconds() -> int:
    days = _bounded_int(
        os.getenv(
            "EVENT_PAIR_DECLINE_COOLDOWN_DAYS",
            DEFAULT_PAIR_DECLINE_COOLDOWN_DAYS,
        ),
        DEFAULT_PAIR_DECLINE_COOLDOWN_DAYS, 0, 30,
    )
    return days * 86400


def _recent_decline_time_clause(now: float) -> dict[str, Any]:
    threshold = now - _pair_decline_cooldown_seconds()
    return {
        "$or": [
            {
                "declined_at": {"$gte": threshold},
            },
            {
                "declined_at": {"$exists": False},
                "updated_at": {"$gte": threshold},
            },
        ],
    }


def _pair_declined_recently(first_user: str, second_user: str, now: float) -> bool:
    if not _pair_decline_cooldown_seconds():
        return False
    pair_key = participant_pair_key(first_user, second_user)
    if not pair_key:
        return True
    return bool(matches_coll.count_documents({
        "$and": [
            {"status": "declined"},
            {"last_decision.action": "decline"},
            namespace_clause(EVENT_INVITATION_NAMESPACE),
            _recent_decline_time_clause(now),
            {
                "$or": [
                    {"participant_pair_key": pair_key},
                    {"from_user": first_user, "to_user": second_user},
                    {"from_user": second_user, "to_user": first_user},
                ]
            },
        ]
    }, limit=1))


def _recent_declined_counterparties(user_id: str, now: float) -> set[str]:
    if not _pair_decline_cooldown_seconds():
        return set()
    rows = matches_coll.find(
        {
            "status": "declined",
            "last_decision.action": "decline",
            "$and": [
                {"$or": [{"from_user": user_id}, {"to_user": user_id}]},
                namespace_clause(EVENT_INVITATION_NAMESPACE),
                _recent_decline_time_clause(now),
            ],
        },
        {"_id": 0, "from_user": 1, "to_user": 1},
    )
    result = set()
    for row in rows:
        other = row.get("to_user") if row.get("from_user") == user_id else row.get("from_user")
        if str(other or "").strip() and other != user_id:
            result.add(str(other))
    return result


def request_event_opportunity_scan() -> None:
    """Mark that new Event data should trigger one bounded scan when ready."""
    _scan_requested.set()


def _participant_ids(document: dict[str, Any]) -> set[str]:
    values = list(document.get("live_participants") or [])
    if not values:
        values = [document.get("from_user"), document.get("to_user")]
    return {str(value) for value in values if str(value or "").strip()}


def scan_event_opportunities(max_proposals: int | None = None) -> dict[str, Any]:
    """Create a small, cooldown-aware set of anonymous Event proposals."""
    limit = _bounded_int(
        max_proposals if max_proposals is not None else os.getenv(
            "EVENT_OPPORTUNITY_MAX_PROPOSALS_PER_SCAN", DEFAULT_AUTO_SCAN_MAX_PROPOSALS,
        ),
        DEFAULT_AUTO_SCAN_MAX_PROPOSALS, 1, 10,
    )
    max_users = _bounded_int(
        os.getenv("EVENT_OPPORTUNITY_MAX_USERS_PER_SCAN", "30"), 30, limit, 100,
    )
    if not _scan_lock.acquire(blocking=False):
        return {"status": "already_running", "created_count": 0, "max_proposals": limit}
    try:
        now = time.time()
        blocked_users: set[str] = set()
        for document in matches_coll.find(
            {
                "$and": [
                    {"status": {"$in": sorted(LIVE_STATUSES)}},
                    {
                        "$or": [
                            {"proposal_namespace": EVENT_INVITATION_NAMESPACE},
                            {
                                "proposal_namespace": {"$exists": False},
                                "proposal_source": "event_opportunity",
                            },
                        ]
                    },
                ]
            },
            {"_id": 0, "live_participants": 1, "from_user": 1, "to_user": 1},
        ):
            blocked_users.update(_participant_ids(document))

        user_ids = {
            str(row.get("user_id") or "").strip()
            for row in profiles_coll.find({"user_id": {"$exists": True}}, {"_id": 0, "user_id": 1})
            if str(row.get("user_id") or "").strip()
        }
        rotation = int(now // (7 * 86400))
        eligible = sorted(
            user_ids - blocked_users,
            key=lambda user_id: hashlib.sha256(
                f"{rotation}:{user_id}".encode("utf-8")
            ).hexdigest(),
        )[:max_users]

        status_counts: dict[str, int] = {}
        created_count = 0
        scanned_count = 0
        for user_id in eligible:
            if created_count >= limit:
                break
            scanned_count += 1
            try:
                result = create_event_opportunity(
                    user_id, excluded_user_ids=set(blocked_users),
                )
                status = str(result.get("status") or "error")
            except Exception as exc:
                print(f"[event-opportunity-scan] user failed error={type(exc).__name__}")
                status = "error"
            status_counts[status] = status_counts.get(status, 0) + 1
            if status == "created":
                created_count += 1
                created_document = matches_coll.find_one(
                    _live_query(user_id),
                    {"_id": 0, "live_participants": 1, "from_user": 1, "to_user": 1},
                ) or {}
                blocked_users.update(_participant_ids(created_document))

        return {
            "status": "success",
            "created_count": created_count,
            "scanned_count": scanned_count,
            "eligible_count": len(eligible),
            "skipped_user_count": len(user_ids & blocked_users),
            "max_proposals": limit,
            "status_counts": status_counts,
        }
    finally:
        _scan_lock.release()


def run_requested_event_opportunity_scan() -> dict[str, Any]:
    """Consume one discovery-triggered scan request when automatic scans are enabled."""
    if not _scan_requested.is_set():
        return {"status": "not_requested", "created_count": 0}
    if os.getenv("EVENT_OPPORTUNITY_AUTO_SCAN_ENABLED", "on").strip().lower() not in {
        "1", "true", "on",
    }:
        return {"status": "disabled", "created_count": 0}
    result = scan_event_opportunities()
    if result.get("status") != "already_running":
        _scan_requested.clear()
    return result


def _profile_snapshot(profile: dict[str, Any]) -> dict[str, Any]:
    return {
        "user_id": str(profile.get("user_id") or ""),
        "current_context": str(profile.get("current_context") or "")[:300],
        "public_personality": public_personality_phrase(profile),
        "context_revision": int(profile.get("current_context_revision", 0) or 0),
        "context_signals": profile.get("context_signals") or {},
    }


def _clean_hook(value: Any, other_id: str, other_profile: dict[str, Any]) -> str:
    other_name = str(
        other_profile.get("display_name") or other_profile.get("nickname")
        or other_profile.get("name") or ""
    ).strip()
    text = anonymize_counterparty_text(
        value, other_id, 220, counterparty_name=other_name,
    )
    return text or "阿月看到一個你可能會有興趣的活動，也想到一位能接上這個話題的人。要不要先讓我幫你問問？"


def _opportunity_key(event_id: str, first_user: str, second_user: str) -> str:
    raw = "|".join([event_id, *sorted([first_user, second_user])])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_event_opportunity(
    user_id: str, *, excluded_user_ids: set[str] | list[str] | None = None,
) -> dict[str, Any]:
    """Find one graph bridge and create an anonymous first-party draft."""
    safe_user_id = re.sub(r"\s+", "", str(user_id or ""))[:80]
    if not safe_user_id:
        return {"status": "invalid_user"}
    if matches_coll.count_documents(_live_query(safe_user_id), limit=1):
        return {"status": "already_active"}
    now = time.time()
    recent_declined = _recent_declined_counterparties(safe_user_id, now)
    response = requests.post(
        AGENT_EVENT_OPPORTUNITY_URL,
        json={
            "user_id": safe_user_id,
            "excluded_user_ids": sorted({
                str(value)[:80] for value in (excluded_user_ids or [])
                if str(value or "").strip() and str(value) != safe_user_id
            } | recent_declined)[:100],
        },
        timeout=(3, 90),
    )
    response.raise_for_status()
    agent_result = response.json()
    if agent_result.get("status") != "success":
        return {
            "status": str(agent_result.get("status") or "agent_error"),
            "message": str(agent_result.get("message") or "")[:160],
        }

    selected = agent_result.get("match") or {}
    target_id = str(selected.get("user_id") or "")
    candidate_id = str(selected.get("candidate_id") or "")
    first_user = str(agent_result.get("first_user_id") or "")
    second_user = str(agent_result.get("second_user_id") or "")
    event_id = str(selected.get("event_id") or "")
    if (
        target_id != safe_user_id
        or {first_user, second_user} != {target_id, candidate_id}
        or not event_id or not candidate_id or candidate_id == target_id
    ):
        return {"status": "invalid_agent_projection"}

    profiles = {
        str(row.get("user_id") or ""): row
        for row in profiles_coll.find(
            {"user_id": {"$in": [first_user, second_user]}},
            {
                "_id": 0, "user_id": 1, "display_name": 1, "nickname": 1,
                "name": 1, "current_context": 1, "current_context_revision": 1,
                "context_signals": 1, "big_five": 1,
            },
        )
    }
    if set(profiles) != {first_user, second_user}:
        return {"status": "profile_missing"}
    if matches_coll.count_documents(_live_query(candidate_id), limit=1):
        return {"status": "already_active"}
    pair_key = participant_pair_key(first_user, second_user)
    if _pair_declined_recently(first_user, second_user, now):
        return {"status": "pair_cooldown"}

    opportunity_key = _opportunity_key(event_id, first_user, second_user)
    existing = matches_coll.find_one(
        {"event_opportunity_key": opportunity_key}, {"_id": 1, "status": 1},
    )
    if existing:
        return {"status": "already_processed", "proposal_status": existing.get("status")}

    first_hook = _clean_hook(
        agent_result.get("first_hook") or agent_result.get("hook"),
        second_user,
        profiles[second_user],
    )
    second_hook = _clean_hook(
        agent_result.get("second_hook"), first_user, profiles[first_user],
    )
    session_starts = _positive_timestamps(selected.get("session_starts"))
    session_ends = _positive_timestamps(selected.get("session_ends"))
    event_snapshot = {
        "event_id": event_id[:80],
        "title": str(selected.get("event_name") or "")[:160],
        "summary": str(selected.get("event_description") or "")[:300],
        "venue": str(selected.get("event_location") or "")[:120],
        "region": str(selected.get("event_region") or "")[:40],
        "category": str(selected.get("event_category") or "")[:30],
        "starts_at": selected.get("starts_at"),
        "ends_at": selected.get("ends_at"),
        "time_precision": str(selected.get("time_precision") or "date")[:20],
        "session_starts": session_starts,
        "session_ends": session_ends,
        "session_precisions": [
            "datetime" if str(value or "") == "datetime" else "date"
            for value in list(selected.get("session_precisions") or [])[:8]
        ],
        "session_count": len(session_starts) or 1,
        "actionable_until": max(session_starts or _positive_timestamps([selected.get("starts_at")]) or [0]),
        "source_url": str(selected.get("source_url") or "")[:500],
    }
    match_doc = {
        "from_user": first_user,
        "to_user": second_user,
        "live_participants": [first_user, second_user],
        "status": "draft",
        "proposal_revision": 0,
        "delivery_channel": "mediator_chat",
        "proposal_namespace": EVENT_INVITATION_NAMESPACE,
        "proposal_source": "event_opportunity",
        "participant_pair_key": pair_key,
        "event_opportunity_key": opportunity_key,
        "event_snapshot": event_snapshot,
        "reason": first_hook,
        "receiver_reason": second_hook,
        "reason_version": "event_opportunity_v1",
        "directional_reason_v3": [
            {"viewer_id": first_user, "counterparty_id": second_user, "viewer_text": first_hook},
            {"viewer_id": second_user, "counterparty_id": first_user, "viewer_text": second_hook},
        ],
        "recommendation_tier": "event_grounded",
        "distinctive_tags": list(dict.fromkeys(
            list(selected.get("target_links") or [])
            + list(selected.get("candidate_links") or [])
        ))[:6],
        "reason_items": [{
            "kind": "event_bridge",
            "text": str(event_snapshot.get("title") or "近期活動")[:120],
            "target_evidence_ids": [],
            "candidate_evidence_ids": [],
        }],
        "receiver_reason_items": [],
        "match_context_snapshot": {
            "target": _profile_snapshot(profiles[first_user]),
            "candidate": _profile_snapshot(profiles[second_user]),
        },
        "created_at": now,
        "updated_at": now,
        "state_history": [{
            "from": None, "to": "draft", "actor": "system",
            "action": "event_opportunity_created", "at": now,
        }],
        "relationship_establishing": not bool(matches_coll.find_one(
            verified_accepted_match_query(first_user, second_user), {"_id": 1},
        )),
    }
    try:
        inserted = matches_coll.insert_one(match_doc)
    except DuplicateKeyError:
        return {"status": "already_active"}

    match_id = str(inserted.inserted_id)
    queued = queue_mediator_event(
        first_user,
        "阿月看到一個活動，也想到一位可能接得上你的人。",
        "match_proposal",
        event_key=f"event-opportunity:{opportunity_key}:first",
        match_id=match_id,
        proposal_role="initiator",
        proposal_namespace=EVENT_INVITATION_NAMESPACE,
    )
    return {
        "status": "created",
        "first_party": "requester" if first_user == safe_user_id else "other",
        "event_title": event_snapshot["title"],
        "queued": bool(queued),
    }
