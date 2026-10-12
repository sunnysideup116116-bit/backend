"""Project the owner's Recent Situation from Mongo into Neo4j.

Mongo stays authoritative. Every owner-data write enqueues one owner-scoped
outbox row; the worker retries ``pending`` rows and periodically reconciles
profiles whose outbox row is missing or stale, so a crash between the profile
update and the enqueue cannot cause a permanent miss. Expired or cleared
situations are projected as an explicit clear and then marked ``cleared`` so
the graph never keeps a stale situation and the worker does not loop.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import requests

from database import db, profiles_coll
from services.language_service import normalize_zh_tw
from services.profile_projection import (
    recent_context_is_active,
    safe_recent_context,
)


AGENT_URL = "http://127.0.0.1:9001"
CONTEXT_GRAPH_OUTBOX = db["context_graph_outbox"]
SITUATION_FIELDS = (
    "activity", "destination", "timing", "companion_intent", "temporal_status",
    "event", "feeling",
)
RECENT_SITUATION_TTL_SECONDS = 30 * 86400
RECONCILE_PROFILE_LIMIT = 200
_worker_thread: threading.Thread | None = None
_worker_stop = threading.Event()


def ensure_context_graph_indexes() -> None:
    try:
        CONTEXT_GRAPH_OUTBOX.create_index(
            "user_id", unique=True, name="one_context_projection_per_user",
        )
        CONTEXT_GRAPH_OUTBOX.create_index(
            [("status", 1), ("updated_at", 1)], name="pending_context_projections",
        )
    except Exception as exc:
        print(f"Context graph indexes skipped: {type(exc).__name__}")


def build_context_projection_payload(
    profile: dict[str, Any], *, now: float | None = None,
) -> dict[str, Any]:
    """Derive the owner-scoped Recent Situation projection from Mongo state."""
    reference = time.time() if now is None else float(now)
    active = recent_context_is_active(profile, now=reference)
    state = (profile.get("recent_context_state") or {}) if active else {}
    fields: dict[str, str] = {}
    for name in SITUATION_FIELDS:
        field = (state.get("fields") or {}).get(name) or {}
        label = normalize_zh_tw(str(field.get("value") or ""), max_length=40).strip()
        if label:
            fields[name] = label
    summary = safe_recent_context(profile.get("current_context"), "") if active else ""
    expires_at = (
        float(profile.get("recent_context_expires_at") or (reference + RECENT_SITUATION_TTL_SECONDS))
        if active else reference
    )
    return {
        "user_id": str(profile.get("user_id") or ""),
        "revision": int(profile.get("current_context_revision") or state.get("revision") or 0),
        "summary": summary,
        "fields": fields,
        "expires_at": expires_at,
        "state_updated_at": float(
            profile.get("recent_context_updated_at") or state.get("updated_at") or 0.0
        ),
    }


def enqueue_context_projection(user_id: str) -> dict[str, Any]:
    """Record a pending projection from the authoritative Mongo profile."""
    profile = profiles_coll.find_one(
        {"user_id": user_id},
        {
            "current_context": 1,
            "current_context_revision": 1,
            "recent_context_state": 1,
            "recent_context_expires_at": 1,
            "recent_context_updated_at": 1,
        },
    ) or {}
    payload = build_context_projection_payload({**profile, "user_id": user_id})
    now = time.time()
    CONTEXT_GRAPH_OUTBOX.update_one(
        {"user_id": user_id},
        {"$set": {**payload, "status": "pending", "updated_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    return payload


def sync_current_context_projection(user_id: str) -> dict[str, Any]:
    """Best-effort owner-scoped projection; Mongo remains authoritative."""
    payload = enqueue_context_projection(user_id)
    body = {
        "user_id": user_id,
        "revision": payload["revision"],
        "summary": payload["summary"],
        "fields": payload["fields"],
        "expires_at": payload["expires_at"],
        "updated_at": payload["state_updated_at"],
    }
    try:
        response = requests.post(
            f"{AGENT_URL}/api/context/situation", json=body, timeout=8,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") != "success":
            raise RuntimeError(str(result.get("message") or "projection_failed"))
        # An empty projection is an explicit clear, not an applied situation;
        # marking it keeps the reconcile scan from deleting it forever.
        status = "cleared" if not payload["summary"] and not payload["fields"] else "applied"
        CONTEXT_GRAPH_OUTBOX.update_one(
            {"user_id": user_id, "revision": payload["revision"]},
            {"$set": {"status": status, "applied_at": time.time()},
             "$unset": {"last_error": ""}},
        )
        return result
    except Exception as exc:
        CONTEXT_GRAPH_OUTBOX.update_one(
            {"user_id": user_id},
            {"$set": {"status": "pending", "last_error": type(exc).__name__}},
        )
        print(f"[CONTEXT_GRAPH] projection pending user={user_id} error={type(exc).__name__}")
        return {"status": "pending", "field_count": len(payload["fields"])}


def retry_pending_context_projections(limit: int = 3) -> dict[str, int]:
    user_ids = [
        row["user_id"]
        for row in CONTEXT_GRAPH_OUTBOX.find(
            {"status": "pending"}, {"_id": 0, "user_id": 1}
        ).sort("updated_at", 1).limit(max(1, min(limit, 100)))
        if row.get("user_id")
    ]
    applied = 0
    for user_id in user_ids:
        if sync_current_context_projection(user_id).get("status") == "success":
            applied += 1
    return {"attempted": len(user_ids), "applied": applied}


def reconcile_context_projections(limit: int = RECONCILE_PROFILE_LIMIT) -> dict[str, int]:
    """Re-enqueue missing/stale rows and clear graph state after expiry.

    A crash between the profile write and the outbox upsert would otherwise
    leave that owner unsynchronized forever; this bounded sweep closes the gap.
    """
    now = time.time()
    stats = {"expired": 0, "stale": 0, "repaired": 0}
    max_rows = max(1, min(int(limit or 1), 500))
    try:
        expired_rows = list(CONTEXT_GRAPH_OUTBOX.find(
            {"status": "applied", "expires_at": {"$lte": now}},
            {"_id": 0, "user_id": 1},
        ).sort("updated_at", 1).limit(max_rows))
    except Exception:
        return stats
    stats["expired"] = len(expired_rows)
    for row in expired_rows:
        user_id = str(row.get("user_id") or "")
        if user_id and sync_current_context_projection(user_id).get("status") == "success":
            stats["repaired"] += 1
    try:
        profiles = list(profiles_coll.find(
            {
                "user_id": {"$exists": True},
                "recent_context_state": {"$exists": True},
                "$or": [
                    {"recent_context_expires_at": {"$gt": now}},
                    {"recent_context_expires_at": {"$exists": False}},
                ],
            },
            {"_id": 0, "user_id": 1, "current_context_revision": 1},
        ).limit(max_rows))
    except Exception:
        return stats
    for profile in profiles:
        user_id = str(profile.get("user_id") or "")
        if not user_id:
            continue
        revision = int(profile.get("current_context_revision") or 0)
        try:
            row = CONTEXT_GRAPH_OUTBOX.find_one(
                {"user_id": user_id}, {"_id": 0, "revision": 1, "status": 1},
            )
        except Exception:
            continue
        fresh = bool(row) and int(row.get("revision") or 0) == revision and \
            str(row.get("status") or "") in {"applied", "cleared", "pending"}
        if fresh:
            continue
        stats["stale"] += 1
        if sync_current_context_projection(user_id).get("status") == "success":
            stats["repaired"] += 1
    return stats


def _projection_worker_loop(interval_seconds: float) -> None:
    while not _worker_stop.is_set():
        try:
            retry_pending_context_projections(limit=3)
            reconcile_context_projections()
        except Exception as exc:
            print(f"[CONTEXT_GRAPH] retry skipped error={type(exc).__name__}")
        _worker_stop.wait(max(5.0, interval_seconds))


def start_context_graph_worker(interval_seconds: float = 30.0) -> None:
    global _worker_thread
    if _worker_thread and _worker_thread.is_alive():
        return
    _worker_stop.clear()
    _worker_thread = threading.Thread(
        target=_projection_worker_loop,
        args=(interval_seconds,),
        name="context-graph-worker",
        daemon=True,
    )
    _worker_thread.start()


def stop_context_graph_worker() -> None:
    global _worker_thread
    _worker_stop.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=2.0)
    _worker_thread = None
