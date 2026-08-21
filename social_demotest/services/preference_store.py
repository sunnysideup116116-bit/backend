"""Mongo source of truth for durable user preferences.

Neo4j keeps only the relation projection used by graph traversal. Evidence,
confidence, lifecycle state, and provenance live here.
"""

from __future__ import annotations

import time
from typing import Any

from pymongo import ASCENDING

from database import db


PREFERENCE_FACTS = db["preference_facts"]
VALID_STANCES = {"like", "dislike", "require", "avoid"}


def ensure_preference_indexes() -> None:
    PREFERENCE_FACTS.create_index(
        [("user_id", ASCENDING), ("concept_key", ASCENDING)],
        unique=True,
        name="one_preference_per_user_concept",
    )
    PREFERENCE_FACTS.create_index(
        [("user_id", ASCENDING), ("active", ASCENDING), ("last_seen_at", ASCENDING)],
        name="active_preferences_by_user",
    )


def _clean_item(item: dict[str, Any]) -> dict[str, Any] | None:
    key = str(item.get("key") or item.get("concept_key") or "").strip().lower()
    label = str(item.get("label") or "").strip()[:40]
    stance = str(item.get("stance") or "").strip().lower()
    if not key or not label or stance not in VALID_STANCES:
        return None
    return {
        "concept_key": key,
        "label": label,
        "stance": stance,
        "category": str(item.get("category") or "lifestyle")[:30],
        "confidence": max(0.0, min(float(item.get("confidence", 0.7)), 1.0)),
        "reason": str(item.get("reason") or "").strip()[:240],
    }


def upsert_preference_facts(
    user_id: str,
    items: list[dict[str, Any]],
    *,
    source: str,
    message_id: str | None = None,
    match_id: str | None = None,
) -> list[dict[str, Any]]:
    ensure_preference_indexes()
    now = time.time()
    saved: list[dict[str, Any]] = []
    for raw_item in items:
        item = _clean_item(raw_item)
        if not item:
            continue
        identity = {"user_id": user_id, "concept_key": item["concept_key"]}
        PREFERENCE_FACTS.update_one(
            identity,
            {
                "$setOnInsert": {
                    "user_id": user_id,
                    "concept_key": item["concept_key"],
                    "first_seen_at": now,
                    "evidence_count": 0,
                    "evidence_ids": [],
                },
                "$set": {
                    "label": item["label"],
                    "stance": item["stance"],
                    "category": item["category"],
                    "active": True,
                    "last_seen_at": now,
                    "last_source": source,
                    "last_match_id": match_id,
                    "last_reason": item["reason"],
                },
                "$max": {"confidence": item["confidence"]},
            },
            upsert=True,
        )
        evidence_id = str(message_id or f"{source}:{match_id or '-'}:{now}")
        PREFERENCE_FACTS.update_one(
            {**identity, "evidence_ids": {"$ne": evidence_id}},
            {
                "$inc": {"evidence_count": 1},
                "$addToSet": {"evidence_ids": evidence_id},
            },
        )
        saved.append(
            {
                "key": item["concept_key"],
                "label": item["label"],
                "stance": item["stance"],
                "category": item["category"],
                "confidence": item["confidence"],
                "last_seen_at": now,
            }
        )
    return saved


def list_preference_facts(user_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = PREFERENCE_FACTS.find(
        {"user_id": user_id, "active": True},
        {
            "_id": 0,
            "concept_key": 1,
            "label": 1,
            "stance": 1,
            "category": 1,
            "confidence": 1,
            "last_seen_at": 1,
        },
    ).sort([("confidence", -1), ("last_seen_at", -1)]).limit(max(1, min(limit, 30)))
    return [
        {
            "key": row.get("concept_key"),
            "label": row.get("label"),
            "stance": row.get("stance"),
            "category": row.get("category"),
            "confidence": row.get("confidence", 0.7),
            "last_seen_at": row.get("last_seen_at", 0),
        }
        for row in rows
    ]


def get_preference_fact(user_id: str, concept_key: str) -> dict[str, Any] | None:
    return PREFERENCE_FACTS.find_one(
        {"user_id": user_id, "concept_key": concept_key},
        {"_id": 0},
    )


def set_preference_fact_state(
    user_id: str, concept_key: str, *, active: bool, label: str | None = None,
) -> dict[str, Any] | None:
    updates: dict[str, Any] = {"active": active, "last_seen_at": time.time()}
    if label:
        updates["label"] = label[:40]
    PREFERENCE_FACTS.update_one(
        {"user_id": user_id, "concept_key": concept_key}, {"$set": updates}
    )
    return get_preference_fact(user_id, concept_key)
