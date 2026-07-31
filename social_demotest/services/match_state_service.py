"""Canonical, read-only match status projection for public surfaces."""

from __future__ import annotations

import time
from typing import Any

from database import matches_coll, profiles_coll


LIVE_MATCH_STATUSES = {"draft", "pending"}
SEARCHING_STATUSES = {"queued", "searching", "loading_profile", "vector_search", "graph_check", "writing_reason"}
SEARCH_RESULT_STATUSES = {"no_candidates", "failed", "cancelled"}
TERMINAL_MATCH_STATUSES = {"accepted", "declined", "expired"}
DRAFT_TTL_SECONDS = 24 * 3600
PENDING_TTL_SECONDS = 72 * 3600
SEARCH_LOCK_TTL_SECONDS = 5 * 60


def has_verified_acceptance(match_doc: dict[str, Any]) -> bool:
    """Return whether a loaded document proves both-party acceptance."""
    decision = match_doc.get("last_decision") or {}
    if (
        decision.get("from") == "pending"
        and decision.get("to") == "accepted"
        and decision.get("action") == "accept"
    ):
        return True
    return any(
        isinstance(item, dict)
        and item.get("from") == "pending"
        and item.get("to") == "accepted"
        and item.get("action") == "accept"
        for item in (match_doc.get("state_history") or [])
    )


def verified_accepted_match_query(
    user_id: str, other_id: str | None = None,
) -> dict[str, Any]:
    """Match accepted relationships created by the canonical state machine.

    A bare ``status=accepted`` is not sufficient evidence that both people
    accepted.  Old demo imports and one-off fixtures used to write terminal
    rows directly, which made every such row appear as a real contact after a
    refresh.  Canonical acceptance always records the pending -> accepted
    transition in either ``state_history`` or ``last_decision``.
    """
    if other_id:
        participants: dict[str, Any] = {
            "$or": [
                {"from_user": user_id, "to_user": other_id},
                {"from_user": other_id, "to_user": user_id},
            ]
        }
    else:
        participants = {
            "$or": [{"from_user": user_id}, {"to_user": user_id}]
        }
    acceptance_evidence = {
        "$or": [
            {
                "state_history": {
                    "$elemMatch": {
                        "from": "pending",
                        "to": "accepted",
                        "action": "accept",
                    }
                }
            },
            {
                "last_decision.from": "pending",
                "last_decision.to": "accepted",
                "last_decision.action": "accept",
            },
        ]
    }
    return {
        "$and": [
            {"status": "accepted"},
            participants,
            acceptance_evidence,
        ]
    }


def _other_id(match: dict[str, Any], user_id: str) -> str | None:
    return match.get("to_user") if match.get("from_user") == user_id else match.get("from_user")


def _display_name(user_id: str | None) -> str:
    if not user_id:
        return "對方"
    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"_id": 0, "display_name": 1, "nickname": 1, "name": 1}
    ) or {}
    value = str(profile.get("display_name") or profile.get("nickname") or profile.get("name") or "").strip()
    if not value or value == user_id or value.startswith(("seed_user_", "demo_user", "user_")):
        return "對方"
    return value[:30]


def _live_match_query(user_id: str, status: str | None = None) -> dict[str, Any]:
    query: dict[str, Any] = {
        "status": status or {"$in": list(LIVE_MATCH_STATUSES)},
        "$or": [
            {"live_participants": user_id},
            {
                "live_participants": {"$exists": False},
                "$or": [{"from_user": user_id}, {"to_user": user_id}],
            },
        ],
    }
    return query


def reconcile_live_match(user_id: str) -> dict[str, Any] | None:
    """Expire stale live records and return the one allowed live proposal."""
    now = time.time()
    for status, ttl in (("draft", DRAFT_TTL_SECONDS), ("pending", PENDING_TTL_SECONDS)):
        expiry_query = _live_match_query(user_id, status)
        expiry_query["created_at"] = {"$lt": now - ttl}
        matches_coll.update_many(
            expiry_query,
            {"$set": {"status": "expired", "expired_at": now, "expired_reason": f"{status}_timeout"},
             "$unset": {"live_participants": ""}},
        )
    profiles_coll.update_many(
        {"user_id": user_id, "matchmaking_in_progress": True,
         "matchmaking_started_at": {"$lt": now - SEARCH_LOCK_TTL_SECONDS}},
        {"$set": {"matchmaking_in_progress": False}, "$unset": {"matchmaking_started_at": ""}},
    )
    live = list(matches_coll.find(
        _live_match_query(user_id),
        {"_id": 1, "from_user": 1, "to_user": 1, "status": 1, "proposal_revision": 1, "updated_at": 1, "created_at": 1},
    ).sort([("created_at", -1)]).limit(2))
    return live[0] if len(live) == 1 else None


def derive_match_stage(match_doc: dict[str, Any] | None, user_id: str) -> str:
    if not match_doc:
        return "idle"
    status = match_doc.get("status")
    if status == "draft" and match_doc.get("from_user") == user_id:
        return "waiting_user"
    if status == "pending" and match_doc.get("from_user") == user_id:
        return "waiting_other"
    if status == "pending" and match_doc.get("to_user") == user_id:
        return "incoming_decision"
    if status == "accepted":
        return "accepted"
    return str(status or "idle")


def get_counterparty_match_source(user_id: str) -> dict[str, Any]:
    """Select the sole effective proposal/accepted match using canonical expiry rules.

    This is an internal domain projection. Callers must still remove identifiers
    and expose only privacy-safe fields.
    """
    live = reconcile_live_match(user_id)
    live_count = matches_coll.count_documents(_live_match_query(user_id))
    if live_count > 1:
        return {"ambiguous": True, "match": None}
    projection = {
        "_id": 0, "from_user": 1, "to_user": 1, "status": 1,
        "reason_items": 1, "receiver_reason_items": 1,
        "directional_reason_v2": 1,
        "distinctive_tags": 1, "recommendation_tier": 1,
        "updated_at": 1, "created_at": 1,
    }
    if live:
        match = matches_coll.find_one({"_id": live.get("_id")}, projection)
    else:
        match = matches_coll.find_one(
            verified_accepted_match_query(user_id),
            projection,
            sort=[("updated_at", -1), ("created_at", -1)],
        )
    return {"ambiguous": False, "match": match}


def get_match_status_snapshot(user_id: str) -> dict[str, Any]:
    """Return the only public projection of a user's match status.

    A completed proposal remains visible as the latest result even though there
    is no longer a live card.  This is the distinction the old active-state
    endpoint lost when it reconciled completed searches back to ``idle``.
    """
    # Reconcile expiry before deciding whether live state is ambiguous. Without
    # this order, two expired legacy rows can mask the actual latest outcome.
    live = reconcile_live_match(user_id)
    live_count = matches_coll.count_documents(_live_match_query(user_id))
    if live_count > 1:
        return {"state": "failed", "scope": "live_match", "is_terminal": False,
                "chat_opened": False,
                "counterparty": "對方", "revision": None, "updated_at": None,
                "reason_code": "ambiguous_live_match"}
    if live:
        return {
            "state": derive_match_stage(live, user_id), "scope": "live_match", "is_terminal": False,
            "chat_opened": False,
            "counterparty": _display_name(_other_id(live, user_id)),
            "revision": int(live.get("proposal_revision", 0)),
            "updated_at": live.get("updated_at") or live.get("created_at"), "reason_code": None,
        }

    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"_id": 0, "match_search": 1, "matchmaking_in_progress": 1}
    ) or {}
    search = profile.get("match_search") or {}
    search_status = str(search.get("status") or "idle")
    if profile.get("matchmaking_in_progress") or search_status in SEARCHING_STATUSES:
        return {"state": "searching", "scope": "search", "is_terminal": False,
                "chat_opened": False,
                "counterparty": "對方", "revision": None, "updated_at": search.get("updated_at") or search.get("started_at"),
                "reason_code": None}

    participant_query = {"$or": [{"from_user": user_id}, {"to_user": user_id}]}
    latest = matches_coll.find_one(
        {
            "$and": [
                participant_query,
                {
                    "$or": [
                        {"status": {"$in": ["declined", "expired"]}},
                        verified_accepted_match_query(user_id),
                    ]
                },
            ]
        },
        {"_id": 1, "from_user": 1, "to_user": 1, "status": 1, "proposal_revision": 1, "updated_at": 1, "created_at": 1},
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if search_status in SEARCH_RESULT_STATUSES:
        search_updated = search.get("completed_at") or search.get("updated_at")
        latest_updated = (latest or {}).get("updated_at") or (latest or {}).get("created_at") or 0
        if not latest or float(search_updated or 0) >= float(latest_updated or 0):
            return {"state": search_status, "scope": "search", "is_terminal": True,
                    "chat_opened": False,
                    "counterparty": "對方", "revision": None,
                    "updated_at": search_updated,
                    "reason_code": str(search.get("error") or search_status)[:80]}
    if latest:
        state = derive_match_stage(latest, user_id)
        return {
            "state": state, "scope": "latest_match", "is_terminal": True,
            "chat_opened": state == "accepted",
            "counterparty": _display_name(_other_id(latest, user_id)),
            "revision": int(latest.get("proposal_revision", 0)),
            "updated_at": latest.get("updated_at") or latest.get("created_at"), "reason_code": None,
        }
    return {"state": "idle", "scope": "none", "is_terminal": False,
            "chat_opened": False,
            "counterparty": "對方", "revision": None, "updated_at": None, "reason_code": None}
