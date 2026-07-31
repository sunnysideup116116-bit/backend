"""Shared, minimal public projections for accepted relationships.

This module is deliberately the only place that turns an accepted-match record
and a profile into data the public Ayue runtime may describe.  It never returns
database identifiers or private memory/calendar fields.
"""

from __future__ import annotations

import re
from typing import Any

from database import matches_coll, profiles_coll
from services.match_state_service import verified_accepted_match_query


MAX_MENTIONED_CONTACTS = 3
_INTERNAL_REFERENCE_RE = re.compile(r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)", re.IGNORECASE)
_PUBLIC_REASON_KINDS = frozenset({"shared_graph", "shared_context", "shared_value"})


def public_text(value: Any, limit: int = 160) -> str:
    text = _INTERNAL_REFERENCE_RE.sub("對方", str(value or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def display_name(user_id: str | None) -> str:
    if not user_id:
        return "對方"
    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"_id": 0, "display_name": 1, "nickname": 1, "name": 1},
    ) or {}
    value = str(profile.get("display_name") or profile.get("nickname") or profile.get("name") or "").strip()
    if not value or value == user_id or value.startswith(("seed_user_", "demo_user", "user_")):
        return "對方"
    return public_text(value, 30) or "對方"


def other_id(match: dict[str, Any], user_id: str) -> str | None:
    return match.get("to_user") if match.get("from_user") == user_id else match.get("from_user")


def safe_public_profile(other_user_id: str | None) -> dict[str, str]:
    profile = profiles_coll.find_one(
        {"user_id": other_user_id},
        {
            "_id": 0, "current_context": 1, "initial_interest": 1,
            "big_five.summary": 1,
        },
    ) or {}
    return {
        "recent_context": public_text(profile.get("current_context"), 100),
        "initial_interest": public_text(profile.get("initial_interest"), 80),
        "personality_summary": public_text((profile.get("big_five") or {}).get("summary"), 100),
    }


def safe_match_reason(match: dict[str, Any], user_id: str) -> str:
    is_initiator = match.get("from_user") == user_id
    directional = (match.get("directional_reason_v2") or {}).get("target" if is_initiator else "candidate") or {}
    text = public_text(directional.get("viewer_text"), 140) if isinstance(directional, dict) else ""
    if text:
        return text
    parts: list[str] = []
    for item in match.get("reason_items" if is_initiator else "receiver_reason_items") or []:
        if not isinstance(item, dict) or item.get("kind") not in _PUBLIC_REASON_KINDS:
            continue
        text = public_text(item.get("text"), 90)
        if text and text not in parts:
            parts.append(text)
        if len(parts) == 2:
            break
    return "；".join(parts)


def verified_common_ground(match: dict[str, Any], user_id: str) -> list[str]:
    is_initiator = match.get("from_user") == user_id
    values: list[str] = []
    for item in match.get("reason_items" if is_initiator else "receiver_reason_items") or []:
        if not isinstance(item, dict) or item.get("kind") not in _PUBLIC_REASON_KINDS:
            continue
        text = public_text(item.get("text"), 90)
        if text and text not in values:
            values.append(text)
    return values[:2]


def validated_mentioned_contact_ids(user_id: str, candidate_ids: list[str] | None) -> tuple[list[str], bool]:
    """Validate client mention bindings against canonical accepted contacts.

    The returned IDs stay executor-side.  ``overflow`` intentionally remains
    true when more than three valid contacts were supplied, so the planner can
    ask the user to narrow the request rather than silently inspecting a subset.
    """
    valid: list[str] = []
    for other_user_id in candidate_ids or []:
        if not isinstance(other_user_id, str) or not other_user_id or other_user_id == user_id:
            continue
        if other_user_id in valid:
            continue
        try:
            accepted = matches_coll.find_one(verified_accepted_match_query(user_id, other_user_id), {"_id": 1})
        except Exception:
            # Entity binding must fail closed; an unavailable canonical read is
            # never a reason to expose a client-supplied profile target.
            accepted = None
        if accepted:
            valid.append(other_user_id)
    return valid[:MAX_MENTIONED_CONTACTS], len(valid) > MAX_MENTIONED_CONTACTS


def mentioned_contact_refs(user_id: str, other_user_ids: list[str]) -> list[dict[str, str]]:
    """Build prompt-safe entity references after server-side validation."""
    return [{"display_name": display_name(other_user_id)} for other_user_id in other_user_ids[:MAX_MENTIONED_CONTACTS]]


def mentioned_contact_summary(user_id: str, other_user_ids: list[str]) -> list[dict[str, Any]]:
    """Return public summaries only for verified accepted mentioned contacts."""
    contacts: list[dict[str, Any]] = []
    projection = {
        "_id": 0, "from_user": 1, "to_user": 1, "reason_items": 1,
        "receiver_reason_items": 1, "directional_reason_v2": 1,
        "distinctive_tags": 1, "recommendation_tier": 1,
    }
    for other_user_id in other_user_ids[:MAX_MENTIONED_CONTACTS]:
        match = matches_coll.find_one(verified_accepted_match_query(user_id, other_user_id), projection)
        if not match:
            continue
        public_profile = safe_public_profile(other_user_id)
        tags = [public_text(value, 30) for value in (match.get("distinctive_tags") or [])]
        contacts.append({
            "display_name": display_name(other_user_id),
            **public_profile,
            "safe_match_reason": safe_match_reason(match, user_id),
            "verified_common_ground": verified_common_ground(match, user_id),
            "distinctive_tags": [tag for tag in tags if tag][:4],
        })
    return contacts
