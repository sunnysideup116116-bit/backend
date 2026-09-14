"""Project structured recent context from Mongo into the Neo4j intent index."""

from __future__ import annotations

import hashlib
import re
import threading
import time
from typing import Any

import requests

from database import db, profiles_coll
from services.language_service import normalize_zh_tw


AGENT_URL = "http://127.0.0.1:9001"
CONTEXT_GRAPH_OUTBOX = db["context_graph_outbox"]
PROJECTED_FIELDS = ("activity", "destination")
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


def _concept_key(label: str) -> str:
    normalized = re.sub(r"\s+", "", label.strip().lower())
    return "concept_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def _projection_concepts(state: dict[str, Any]) -> list[dict[str, str]]:
    fields = state.get("fields") or {}
    concepts: list[dict[str, str]] = []
    seen: set[str] = set()
    for field_name in PROJECTED_FIELDS:
        field = fields.get(field_name) or {}
        label = normalize_zh_tw(str(field.get("value") or ""), max_length=40).strip()
        if not label:
            continue
        normalized = re.sub(r"\s+", "", label.lower())
        if normalized in seen:
            continue
        seen.add(normalized)
        concepts.append({"key": _concept_key(label), "label": label})
    return concepts


def sync_current_context_projection(user_id: str) -> dict[str, Any]:
    """Best-effort projection; Mongo remains authoritative on every failure."""
    profile = profiles_coll.find_one(
        {"user_id": user_id},
        {
            "recent_context_state": 1,
            "recent_context_expires_at": 1,
            "current_context_revision": 1,
        },
    ) or {}
    state = profile.get("recent_context_state") or {}
    expires_at = float(profile.get("recent_context_expires_at") or (time.time() + 30 * 86400))
    payload = {
        "user_id": user_id,
        "concepts": _projection_concepts(state),
        "expires_at": expires_at,
        "revision": int(profile.get("current_context_revision") or state.get("revision") or 0),
    }
    now = time.time()
    CONTEXT_GRAPH_OUTBOX.update_one(
        {"user_id": user_id},
        {"$set": {**payload, "status": "pending", "updated_at": now},
         "$setOnInsert": {"created_at": now}},
        upsert=True,
    )
    try:
        response = requests.post(
            f"{AGENT_URL}/api/context/project", json=payload, timeout=8,
        )
        response.raise_for_status()
        result = response.json()
        if result.get("status") != "success":
            raise RuntimeError(str(result.get("message") or "projection_failed"))
        CONTEXT_GRAPH_OUTBOX.update_one(
            {"user_id": user_id, "revision": payload["revision"]},
            {"$set": {"status": "applied", "applied_at": time.time()},
             "$unset": {"last_error": ""}},
        )
        return result
    except Exception as exc:
        CONTEXT_GRAPH_OUTBOX.update_one(
            {"user_id": user_id, "revision": payload["revision"]},
            {"$set": {"status": "pending", "last_error": type(exc).__name__}},
        )
        print(f"[CONTEXT_GRAPH] projection pending user={user_id} error={type(exc).__name__}")
        return {"status": "pending", "concept_count": len(payload["concepts"])}


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


def _projection_worker_loop(interval_seconds: float) -> None:
    while not _worker_stop.is_set():
        try:
            retry_pending_context_projections(limit=3)
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
