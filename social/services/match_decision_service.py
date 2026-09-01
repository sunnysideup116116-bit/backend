"""Canonical compare-and-set transitions for cards and the public agent."""

from __future__ import annotations

import time
from typing import Callable

from fastapi import HTTPException
from bson.objectid import ObjectId
from pymongo import ReturnDocument

from database import matches_coll
from services.match_state_service import verified_accepted_match_query
from services.proposal_namespace import (
    EVENT_INVITATION_NAMESPACE,
    namespace_clause,
    namespace_for_document,
)


LIVE_STATUSES = {"draft", "pending"}


def expire_event_proposals(
    *, now: float | None = None, event_ids: list[str] | None = None,
    lead_seconds: int = 0, limit: int = 500, expire_all: bool = False,
) -> dict[str, int | str]:
    """Atomically expire unresolved Event proposals whose activity is no longer actionable."""
    current_time = float(now if now is not None else time.time())
    safe_lead = max(0, min(int(lead_seconds or 0), 7 * 86400))
    safe_limit = max(1, min(int(limit or 500), 1000))
    safe_event_ids = list(dict.fromkeys(
        str(value or "")[:80] for value in (event_ids or [])
        if str(value or "").strip()
    ))[:500]
    due_conditions: list[dict] = [
        {"event_snapshot.actionable_until": {"$gt": 0, "$lte": current_time + safe_lead}},
        {
            "event_snapshot.actionable_until": {"$exists": False},
            "event_snapshot.starts_at": {"$gt": 0, "$lte": current_time + safe_lead},
        },
    ]
    if safe_event_ids:
        due_conditions.append({"event_snapshot.event_id": {"$in": safe_event_ids}})
    query_conditions: list[dict] = [
        {"status": {"$in": sorted(LIVE_STATUSES)}},
        namespace_clause(EVENT_INVITATION_NAMESPACE),
    ]
    if not expire_all:
        query_conditions.append({"$or": due_conditions})
    rows = list(matches_coll.find(
        {"$and": query_conditions},
        {
            "_id": 1, "status": 1, "proposal_revision": 1,
            "event_snapshot.event_id": 1, "event_snapshot.starts_at": 1,
            "event_snapshot.actionable_until": 1,
        },
    ).sort([("created_at", 1)]).limit(safe_limit))

    expired_count = 0
    stale_count = 0
    removed_ids = set(safe_event_ids)
    for row in rows:
        status = str(row.get("status") or "")
        revision = int(row.get("proposal_revision", 0) or 0)
        event_snapshot = row.get("event_snapshot") or {}
        event_id = str(event_snapshot.get("event_id") or "")
        reason = (
            "inventory_refreshed" if expire_all
            else "event_removed" if event_id in removed_ids
            else "event_started"
        )
        transition = {
            "from": status, "to": "expired", "actor": "system",
            "action": "event_expired", "reason": reason, "at": current_time,
        }
        query: dict = {"_id": row.get("_id"), "status": status}
        if "proposal_revision" in row:
            query["proposal_revision"] = revision
        else:
            query["proposal_revision"] = {"$exists": False}
        updated = matches_coll.find_one_and_update(
            query,
            {
                "$set": {
                    "status": "expired", "updated_at": current_time,
                    "expired_at": current_time, "expired_reason": reason,
                    "last_decision": transition,
                },
                "$inc": {"proposal_revision": 1},
                "$push": {"state_history": transition},
                "$unset": {"live_participants": ""},
            },
            return_document=ReturnDocument.AFTER,
        )
        if updated:
            expired_count += 1
        else:
            stale_count += 1
    return {
        "status": "success", "checked_count": len(rows),
        "expired_count": expired_count, "stale_count": stale_count,
    }


def apply_match_decision(
    *, user_id: str, match_id: str, action: str, expected_status: str,
    expected_revision: int | None, explicit_reasons: list[str] | None = None,
    expected_namespace: str | None = None,
    idempotency_key: str | None = None, after_transition: Callable[[dict, str, str, list[str]], None] | None = None,
) -> dict:
    if action not in {"accept", "decline", "cancel"} or expected_status not in LIVE_STATUSES:
        raise HTTPException(status_code=400, detail="Invalid match decision")
    try:
        oid = ObjectId(match_id)
    except Exception:
        raise HTTPException(status_code=404, detail="Match not found")
    current = matches_coll.find_one({"_id": oid})
    if not current:
        raise HTTPException(status_code=404, detail="Match not found")
    if user_id not in {current.get("from_user"), current.get("to_user")}:
        raise HTTPException(status_code=403, detail="Only a match participant may decide")
    current_namespace = namespace_for_document(current)
    if expected_namespace and expected_namespace != current_namespace:
        return {
            "status": "stale", "stale": True,
            "current_status": current.get("status"),
            "current_revision": int(current.get("proposal_revision", 0) or 0),
            "current_namespace": current_namespace,
        }
    if idempotency_key and (current.get("last_decision") or {}).get("idempotency_key") == idempotency_key:
        return {
            "status": "success", "new_status": current.get("status"),
            "idempotent": True,
            "proposal_revision": int(current.get("proposal_revision", 0) or 0),
            "proposal_namespace": current_namespace,
            "chat_reused": bool(
                current.get("status") == "accepted"
                and current_namespace == EVENT_INVITATION_NAMESPACE
                and current.get("relationship_establishing") is False
            ),
        }
    current_revision = int(current.get("proposal_revision", 0))
    if expected_revision is not None and expected_revision != current_revision:
        return {
            "status": "stale", "stale": True,
            "current_status": current.get("status"),
            "current_revision": current_revision,
            "current_namespace": current_namespace,
        }
    if action == "accept" and expected_status == "draft":
        actor, target = current.get("from_user"), "pending"
    elif action == "accept" and expected_status == "pending":
        actor, target = current.get("to_user"), "accepted"
    elif action == "decline" and expected_status in LIVE_STATUSES:
        actor, target = (current.get("from_user") if expected_status == "draft" else current.get("to_user")), "declined"
    elif action == "cancel" and expected_status == "pending":
        actor, target = current.get("from_user"), "declined"
    else:
        raise HTTPException(status_code=400, detail="This action is not valid for the expected status")
    if user_id != actor:
        raise HTTPException(status_code=403, detail="This decision is not available to this participant")
    now = time.time()
    transition = {"from": expected_status, "to": target, "actor": user_id, "action": action, "at": now}
    if idempotency_key:
        transition["idempotency_key"] = idempotency_key
    update = {
        "$set": {"status": target, "updated_at": now, "last_decision": transition},
        "$inc": {"proposal_revision": 1}, "$push": {"state_history": transition},
    }
    update["$set"]["proposal_namespace"] = current_namespace
    if action == "decline":
        update["$set"]["declined_at"] = now
    elif action == "cancel":
        update["$set"]["cancelled_at"] = now
    if target == "accepted":
        other_accepted_query = {
            "$and": [
                verified_accepted_match_query(
                    str(current.get("from_user") or ""),
                    str(current.get("to_user") or ""),
                ),
                {"_id": {"$ne": oid}},
            ]
        }
        if matches_coll.count_documents(other_accepted_query, limit=1):
            update["$set"]["relationship_establishing"] = False
    if target not in LIVE_STATUSES:
        update["$unset"] = {"live_participants": ""}
    query = {"_id": oid, "status": expected_status,
             "from_user" if actor == current.get("from_user") else "to_user": user_id}
    if "proposal_namespace" in current:
        query["proposal_namespace"] = current_namespace
    else:
        query["proposal_namespace"] = {"$exists": False}
    if expected_revision is not None:
        # Missing revision is treated as legacy revision zero.
        query["$or"] = [{"proposal_revision": expected_revision}, {"proposal_revision": {"$exists": False}, "$expr": {"$eq": [expected_revision, 0]}}]
    updated = matches_coll.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
    if not updated:
        refreshed = matches_coll.find_one({"_id": oid}) or {}
        return {
            "status": "stale", "stale": True,
            "current_status": refreshed.get("status"),
            "current_revision": int(refreshed.get("proposal_revision", 0)),
            "current_namespace": namespace_for_document(refreshed),
        }
    if after_transition:
        after_transition(updated, action, expected_status, explicit_reasons or [])
    return {
        "status": "success", "new_status": target, "idempotent": False,
        "proposal_revision": int(updated.get("proposal_revision", 0)),
        "proposal_namespace": namespace_for_document(updated),
        "chat_reused": bool(
            target == "accepted"
            and namespace_for_document(updated) == EVENT_INVITATION_NAMESPACE
            and updated.get("relationship_establishing") is False
        ),
    }
