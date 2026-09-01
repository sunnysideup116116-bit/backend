"""Shared proposal namespace and unordered-pair helpers.

``proposal_namespace`` identifies which live proposal slot a document uses.
``proposal_source`` remains provenance and must not be used as the long-term
state boundary.  Legacy documents are classified without being rewritten by
read paths; the explicit migration script performs the durable backfill.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any


RELATIONSHIP_MATCH_NAMESPACE = "relationship_match"
EVENT_INVITATION_NAMESPACE = "event_invitation"
PROPOSAL_NAMESPACES = frozenset({
    RELATIONSHIP_MATCH_NAMESPACE,
    EVENT_INVITATION_NAMESPACE,
})
LIVE_PROPOSAL_STATUSES = frozenset({"draft", "pending"})
TERMINAL_PROPOSAL_STATUSES = frozenset({"accepted", "declined", "expired"})


def namespace_for_document(document: dict[str, Any] | None) -> str:
    document = document or {}
    namespace = str(document.get("proposal_namespace") or "")
    if namespace in PROPOSAL_NAMESPACES:
        return namespace
    if str(document.get("proposal_source") or "") == "event_opportunity":
        return EVENT_INVITATION_NAMESPACE
    return RELATIONSHIP_MATCH_NAMESPACE


def namespace_clause(namespace: str) -> dict[str, Any]:
    """Return a migration-safe Mongo clause for one proposal namespace."""
    if namespace == EVENT_INVITATION_NAMESPACE:
        return {
            "$or": [
                {"proposal_namespace": EVENT_INVITATION_NAMESPACE},
                {
                    "proposal_namespace": {"$exists": False},
                    "proposal_source": "event_opportunity",
                },
            ]
        }
    if namespace != RELATIONSHIP_MATCH_NAMESPACE:
        raise ValueError("Unsupported proposal namespace")
    return {
        "$or": [
            {"proposal_namespace": RELATIONSHIP_MATCH_NAMESPACE},
            {
                "proposal_namespace": {"$exists": False},
                "proposal_source": {"$ne": "event_opportunity"},
            },
        ]
    }


def participant_clause(user_id: str) -> dict[str, Any]:
    return {
        "$or": [
            {"live_participants": user_id},
            {
                "live_participants": {"$exists": False},
                "$or": [{"from_user": user_id}, {"to_user": user_id}],
            },
        ]
    }


def live_proposal_query(
    user_id: str, namespace: str, status: str | None = None,
) -> dict[str, Any]:
    return {
        "$and": [
            {"status": status or {"$in": sorted(LIVE_PROPOSAL_STATUSES)}},
            namespace_clause(namespace),
            participant_clause(user_id),
        ]
    }


def participant_pair_key(first_user: Any, second_user: Any) -> str:
    first = str(first_user or "").strip()
    second = str(second_user or "").strip()
    if not first or not second or first == second:
        return ""
    raw = "|".join(sorted((first, second)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalized_live_participants(document: dict[str, Any] | None) -> list[str]:
    """Return the canonical two participants used by the live unique index."""
    document = document or {}
    first = str(document.get("from_user") or "").strip()
    second = str(document.get("to_user") or "").strip()
    if not first or not second or first == second:
        return []
    return [first, second]


def live_namespace_conflict_count(documents: list[dict[str, Any]]) -> int:
    """Count namespace/participant slots claimed by more than one live row."""
    claims: dict[tuple[str, str], int] = {}
    conflicts = 0
    for document in documents:
        if str(document.get("status") or "") not in LIVE_PROPOSAL_STATUSES:
            continue
        namespace = namespace_for_document(document)
        for user_id in normalized_live_participants(document):
            key = (namespace, user_id)
            claims[key] = claims.get(key, 0) + 1
            if claims[key] == 2:
                conflicts += 1
    return conflicts


def extend_match_status_validator(validator: dict[str, Any] | None) -> tuple[dict, bool, bool]:
    """Add ``expired`` to an existing proposal status enum without replacing its schema."""
    updated = deepcopy(validator or {})
    found = False
    changed = False

    def visit(value: Any) -> None:
        nonlocal found, changed
        if isinstance(value, dict):
            properties = value.get("properties")
            status_schema = properties.get("status") if isinstance(properties, dict) else None
            if isinstance(status_schema, dict) and isinstance(status_schema.get("enum"), list):
                statuses = status_schema["enum"]
                known = set(statuses)
                if {"draft", "pending", "accepted", "declined"}.issubset(known):
                    found = True
                    if "expired" not in known:
                        statuses.append("expired")
                        changed = True
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(updated)
    return updated, changed, found
