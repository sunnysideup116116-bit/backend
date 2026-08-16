"""Canonical compare-and-set transitions for cards and the public agent."""

from __future__ import annotations

import time
from typing import Callable

from fastapi import HTTPException
from bson.objectid import ObjectId
from pymongo import ReturnDocument

from database import matches_coll


LIVE_STATUSES = {"draft", "pending"}


def apply_match_decision(
    *, user_id: str, match_id: str, action: str, expected_status: str,
    expected_revision: int | None, explicit_reasons: list[str] | None = None,
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
    if idempotency_key and (current.get("last_decision") or {}).get("idempotency_key") == idempotency_key:
        return {"status": "success", "new_status": current.get("status"), "idempotent": True}
    current_revision = int(current.get("proposal_revision", 0))
    if expected_revision is not None and expected_revision != current_revision:
        return {"status": "stale", "stale": True, "current_status": current.get("status"), "current_revision": current_revision}
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
    if target not in LIVE_STATUSES:
        update["$unset"] = {"live_participants": ""}
    query = {"_id": oid, "status": expected_status,
             "from_user" if actor == current.get("from_user") else "to_user": user_id}
    if expected_revision is not None:
        # Missing revision is treated as legacy revision zero.
        query["$or"] = [{"proposal_revision": expected_revision}, {"proposal_revision": {"$exists": False}, "$expr": {"$eq": [expected_revision, 0]}}]
    updated = matches_coll.find_one_and_update(query, update, return_document=ReturnDocument.AFTER)
    if not updated:
        refreshed = matches_coll.find_one({"_id": oid}) or {}
        return {"status": "stale", "stale": True, "current_status": refreshed.get("status"), "current_revision": int(refreshed.get("proposal_revision", 0))}
    if after_transition:
        after_transition(updated, action, expected_status, explicit_reasons or [])
    return {"status": "success", "new_status": target, "idempotent": False, "proposal_revision": int(updated.get("proposal_revision", 0))}
