"""Mongo source of truth for durable user preferences.

Neo4j keeps only the relation projection used by graph traversal. Evidence,
confidence, lifecycle state, and provenance live here.
"""

from __future__ import annotations

import time
from typing import Any

from pymongo import ASCENDING

from database import db
from matchmaker_agent.concept_identity import (
    canonicalize_concept,
    display_preference_label,
    has_mixed_preference_polarity,
    normalize_preference_text,
    PreferenceTextError,
    durable_memory_limit,
    split_compound_concept_label,
    split_explicit_preference_enumeration,
    normalize_fresh_preference_text,
    stored_concept_identity,
)


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
    identity = stored_concept_identity(item) if item.get("canonicalization_version") == "v2" else None
    if item.get("canonicalization_version") == "v2" and not identity:
        raise PreferenceTextError("invalid_preference_identity")
    label = identity.semantic_text if identity else normalize_fresh_preference_text(
        item.get("semantic_text") or item.get("label"))
    stance = str(item.get("stance") or "").strip().lower()
    if (
        has_mixed_preference_polarity(label)
        or split_explicit_preference_enumeration(label)
        or split_compound_concept_label(label)
    ):
        return None
    identity = identity or canonicalize_concept(
        label, item.get("key") or item.get("concept_key"),
    )
    if not identity or stance not in VALID_STANCES:
        return None
    return {
        **identity.as_dict(),
        "concept_key": identity.key,
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
    # Validate every item before the first index/write. A later oversized item
    # must not leave the accepted prefix of a batch in Mongo.
    if len(items) > durable_memory_limit():
        raise PreferenceTextError("too_many_preferences")
    cleaned = [_clean_item(item) for item in items]
    if any(item is None for item in cleaned):
        raise PreferenceTextError("invalid_atomic_preference")
    ensure_preference_indexes()
    now = time.time()
    saved: list[dict[str, Any]] = []
    for item in cleaned:
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
                    "semantic_text": item["semantic_text"],
                    "display_label": item["display_label"],
                    "canonical_key": item["canonical_key"],
                    "canonicalization_version": item["canonicalization_version"],
                    "semantic_input_hash": item["semantic_input_hash"],
                    "fidelity_status": item["fidelity_status"],
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
                **{name: item[name] for name in (
                    "semantic_text", "display_label", "canonical_key",
                    "canonicalization_version", "semantic_input_hash", "fidelity_status",
                )},
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
            "semantic_text": 1,
            "display_label": 1,
            "canonical_key": 1,
            "canonicalization_version": 1,
            "semantic_input_hash": 1,
            "fidelity_status": 1,
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
            "semantic_text": row.get("semantic_text"),
            "display_label": row.get("display_label") or display_preference_label(row.get("label")),
            "canonical_key": row.get("canonical_key") or row.get("concept_key"),
            "canonicalization_version": row.get("canonicalization_version") or "legacy_unknown",
            "semantic_input_hash": row.get("semantic_input_hash"),
            "fidelity_status": row.get("fidelity_status") or "legacy_unknown",
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
        # State changes are not identity rewrites. A caller requesting different
        # semantic content must use the explicit correction/create path.
        normalized = normalize_preference_text(label)
        existing = get_preference_fact(user_id, concept_key)
        if not existing or normalized != (existing.get("semantic_text") or existing.get("label")):
            raise PreferenceTextError("identity_change_requires_correction")
    PREFERENCE_FACTS.update_one(
        {"user_id": user_id, "concept_key": concept_key}, {"$set": updates}
    )
    return get_preference_fact(user_id, concept_key)
