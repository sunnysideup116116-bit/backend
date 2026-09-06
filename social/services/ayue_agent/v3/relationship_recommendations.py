"""Short-lived, room-scoped snapshots for relationship recommendations."""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from typing import Any


RECOMMENDATION_TTL_SECONDS = 30 * 60
_COLLECTION = None
_LOCK = threading.RLock()
_MEMORY: dict[tuple[str, str], dict[str, Any]] = {}


class RecommendationPersistenceError(RuntimeError):
    """Raised when a recommendation cannot be durably linked to a reply."""


def _test_mode() -> bool:
    return os.getenv("AYUE_TEST_MODE", "off").strip().lower() in {"1", "true", "on"}


def _collection() -> Any:
    global _COLLECTION
    if _test_mode() or os.getenv("AYUE_RELATIONSHIP_RECOMMENDATION_MONGO", "on").strip().lower() not in {"1", "true", "on"}:
        return None
    if _COLLECTION is None:
        try:
            from database import db
            _COLLECTION = db["v3_relationship_recommendations"]
        except Exception:
            return None
    return _COLLECTION


def ensure_indexes() -> None:
    collection = _collection()
    if collection is None:
        return
    try:
        collection.create_index(
            [("user_id", 1), ("room_id", 1), ("origin_run_id", 1)],
            unique=True,
            name="v3_relationship_recommendations_origin",
        )
        collection.create_index("expires_at", expireAfterSeconds=0)
        collection.create_index(
            [("user_id", 1), ("room_id", 1), ("published_at", -1)],
            name="v3_relationship_recommendations_recent",
        )
    except Exception:
        pass


def clear_runtime_state() -> None:
    with _LOCK:
        _MEMORY.clear()


def _safe_projection(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Keep only prompt-safe recommendation facts in the snapshot."""
    allowed = {
        "schema_version", "intent", "request", "candidate_refs", "candidate_pool",
        "activity", "evidence", "recommendations", "recommended_candidate_refs",
        "direct_evidence", "exploratory_evidence", "unavailable_refs", "unknowns",
        "status", "stop_reason",
    }
    result = {key: snapshot.get(key) for key in allowed if key in snapshot}
    result["candidate_pool"] = list(result.get("candidate_pool") or [])[:8]
    result["evidence"] = list(result.get("evidence") or [])[:8]
    result["recommendations"] = list(result.get("recommendations") or [])[:3]
    result["direct_evidence"] = list(result.get("direct_evidence") or [])[:3]
    result["exploratory_evidence"] = list(result.get("exploratory_evidence") or [])[:3]
    result["candidate_refs"] = [str(value)[:64] for value in (result.get("candidate_refs") or [])[:8]]
    result["recommended_candidate_refs"] = [
        str(value)[:64] for value in (result.get("recommended_candidate_refs") or [])[:3]
    ]
    result["unavailable_refs"] = [str(value)[:64] for value in (result.get("unavailable_refs") or [])[:3]]
    result["unknowns"] = [str(value)[:240] for value in (result.get("unknowns") or [])[:6]]
    return result


def save_snapshot(
    user_id: str,
    room_id: str,
    origin_run_id: str,
    source_message_id: str,
    snapshot: dict[str, Any],
    *,
    published_at: float | None = None,
) -> dict[str, Any]:
    if not user_id or not room_id or not origin_run_id or not source_message_id:
        raise RecommendationPersistenceError("recommendation snapshot requires durable identity")
    now = float(published_at or time.time())
    expires_at_epoch = now + RECOMMENDATION_TTL_SECONDS
    record = {
        "user_id": user_id,
        "room_id": room_id,
        "origin_run_id": origin_run_id,
        "source_message_id": source_message_id,
        "snapshot": _safe_projection(snapshot),
        "published_at": datetime.fromtimestamp(now, timezone.utc),
        "published_at_epoch": now,
        "expires_at": datetime.fromtimestamp(expires_at_epoch, timezone.utc),
        "expires_at_epoch": expires_at_epoch,
    }
    collection = _collection()
    if collection is not None:
        try:
            collection.update_one(
                {"user_id": user_id, "room_id": room_id, "origin_run_id": origin_run_id},
                {"$set": record},
                upsert=True,
            )
        except Exception as exc:
            raise RecommendationPersistenceError(type(exc).__name__) from exc
    with _LOCK:
        _MEMORY[(user_id, room_id)] = dict(record)
    return record


def get_snapshot(user_id: str, room_id: str) -> dict[str, Any] | None:
    now = time.time()
    record = None
    collection = _collection()
    if collection is not None:
        try:
            record = collection.find_one(
                {"user_id": user_id, "room_id": room_id},
                sort=[("published_at", -1)],
            )
        except Exception:
            record = None
    if not record:
        with _LOCK:
            record = dict(_MEMORY.get((user_id, room_id)) or {}) or None
    if not record or float(record.get("expires_at_epoch", 0) or 0) <= now:
        return None
    snapshot = dict(record.get("snapshot") or {})
    snapshot["source_message_id"] = str(record.get("source_message_id") or "")
    snapshot["origin_run_id"] = str(record.get("origin_run_id") or "")
    snapshot["expires_in_seconds"] = max(
        0, int(float(record.get("expires_at_epoch", now)) - now),
    )
    return snapshot


def public_projection(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {
        "schema_version": "relationship_recommendation.v1",
        "intent": str(snapshot.get("intent") or "recommend"),
        "request": str(snapshot.get("request") or "")[:240],
        "activity": str(snapshot.get("activity") or "")[:180],
        "candidate_refs": list(snapshot.get("candidate_refs") or [])[:8],
        "recommended_candidate_refs": list(snapshot.get("recommended_candidate_refs") or [])[:3],
        "candidate_pool": [
            {
                "contact_ref": str(item.get("contact_ref") or "")[:64],
                "display_name": str(item.get("display_name") or "對方")[:30],
                "recent_context": str(item.get("recent_context") or "")[:160],
                "initial_interest": str(item.get("initial_interest") or "")[:160],
                "personality_summary": str(item.get("personality_summary") or "")[:160],
                "evidence_fields": list(item.get("evidence_fields") or [])[:8],
            }
            for item in (snapshot.get("candidate_pool") or [])[:8]
            if isinstance(item, dict)
        ],
        "evidence": [
            {
                "contact_ref": str(item.get("contact_ref") or "")[:64],
                "display_name": str(item.get("display_name") or "對方")[:30],
                "recent_context": str(item.get("recent_context") or "")[:160],
                "initial_interest": str(item.get("initial_interest") or "")[:160],
                "personality_summary": str(item.get("personality_summary") or "")[:160],
                "verified_common_ground": list(item.get("verified_common_ground") or [])[:2],
                "evidence_fields": list(item.get("evidence_fields") or [])[:8],
            }
            for item in (snapshot.get("evidence") or [])[:8]
            if isinstance(item, dict)
        ],
        "recommendations": [
            {
                "contact_ref": str(item.get("contact_ref") or "")[:64],
                "display_name": str(item.get("display_name") or "對方")[:30],
                "classification": str(item.get("classification") or "exploratory"),
                "evidence_fields": list(item.get("evidence_fields") or [])[:6],
                "reason": str(item.get("reason") or "")[:240],
                "unknowns": [str(value)[:240] for value in (item.get("unknowns") or [])[:4]],
            }
            for item in (snapshot.get("recommendations") or [])[:3]
            if isinstance(item, dict)
        ],
        "unknowns": list(snapshot.get("unknowns") or [])[:4],
        "expires_in_seconds": int(snapshot.get("expires_in_seconds", 0) or 0),
    }
