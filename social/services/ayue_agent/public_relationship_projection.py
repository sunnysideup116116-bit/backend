"""Shared, minimal public projections for accepted relationships.

This module is deliberately the only place that turns an accepted-match record
and a profile into data the public Ayue runtime may describe.  It never returns
database identifiers or private memory/calendar fields.
"""

from __future__ import annotations

import re
import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Any

from database import matches_coll, profiles_coll
from pypinyin import Style, lazy_pinyin
from services.language_service import normalize_zh_tw
from services.match_state_service import verified_accepted_match_query
from services.match_reason_service import reason_for_viewer
from services.public_nickname_service import contact_display_name, warm_public_nicknames


MAX_MENTIONED_CONTACTS = 3
MAX_LISTED_ACCEPTED_CONTACTS = 8
MAX_CONTACT_RESOLUTION_CANDIDATES = 50
_INTERNAL_REFERENCE_RE = re.compile(r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)", re.IGNORECASE)
_PUBLIC_REASON_KINDS = frozenset({"shared_graph", "shared_context", "shared_value"})
CONTACT_REF_PREFIX = "relref_"


def contact_reference(
    user_id: str, other_user_id: str, reference_scope: str = "legacy",
) -> str:
    """Return an opaque reference scoped to one owner turn."""
    digest = hashlib.sha256(
        f"{user_id}:{reference_scope}:{other_user_id}".encode("utf-8")
    ).hexdigest()
    return f"{CONTACT_REF_PREFIX}{digest[:24]}"


def public_text(value: Any, limit: int = 160) -> str:
    text = _INTERNAL_REFERENCE_RE.sub("對方", normalize_zh_tw(str(value or "")))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def anonymize_counterparty_text(
    value: Any, other_user_id: str | None, limit: int = 160,
    *, counterparty_name: str | None = None,
) -> str:
    """Remove the executor-known identity while retaining safe profile meaning."""
    text = public_text(value, limit)
    tokens = [str(other_user_id or "").strip(), str(counterparty_name or "").strip()]
    for token in tokens:
        if len(token) >= 2 and token != "對方":
            text = re.sub(re.escape(token), "對方", text, flags=re.IGNORECASE)
    return text[:limit]


def anonymize_counterparty_payload(
    value: Any, other_user_id: str | None, *, counterparty_name: str | None = None,
):
    """Recursively anonymize human-facing proposal/profile values, not keys."""
    if isinstance(value, str):
        return anonymize_counterparty_text(
            value, other_user_id, 500, counterparty_name=counterparty_name,
        )
    if isinstance(value, list):
        return [
            anonymize_counterparty_payload(item, other_user_id, counterparty_name=counterparty_name)
            for item in value
        ]
    if isinstance(value, dict):
        return {
            key: anonymize_counterparty_payload(item, other_user_id, counterparty_name=counterparty_name)
            for key, item in value.items()
        }
    return value


def display_name(user_id: str | None, *, profile: dict[str, Any] | None = None) -> str:
    if not user_id:
        return "對方"
    if profile is None:
        profile = profiles_coll.find_one(
            {"user_id": user_id}, {"_id": 0, "display_name": 1, "nickname": 1, "name": 1},
        ) or {}
    return contact_display_name(user_id, profile) or "對方"


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
    projected = public_text(reason_for_viewer(match, user_id), 220)
    if projected or match.get("reason_version") == "v4_friend_intro":
        return projected
    # Compatibility for historical accepted records that predate directional
    # text and only stored verified public reason items.
    is_initiator = match.get("from_user") == user_id
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


def accepted_contact_ids_by_display_name(user_id: str, name_hint: str) -> list[str]:
    """Resolve a public name only inside the owner's accepted relationships.

    The returned IDs are executor-only.  Exact normalized-name matching keeps
    the model from broadening a human label into an arbitrary profile lookup;
    duplicate public names intentionally remain multiple candidates.
    """
    target = re.sub(r"\s+", "", public_text(name_hint, 30)).casefold()
    if not target:
        return []
    resolved: list[str] = []
    try:
        matches = matches_coll.find(
            verified_accepted_match_query(user_id),
            {"_id": 0, "from_user": 1, "to_user": 1},
        )
        matches = list(matches)
        warm_public_nicknames([other_id(match, user_id) for match in matches])
        for match in matches:
            other_user_id = other_id(match, user_id)
            if not isinstance(other_user_id, str) or other_user_id in resolved:
                continue
            label = display_name(other_user_id)
            normalized_label = re.sub(r"\s+", "", label).casefold()
            if label != "對方" and normalized_label == target:
                resolved.append(other_user_id)
    except Exception:
        # Relationship lookup is an authorization boundary and must fail closed.
        return []
    return resolved


@dataclass(frozen=True)
class ContactNameResolution:
    status: str
    other_id: str | None = None
    display_name: str = ""
    candidates: tuple[str, ...] = ()
    kind: str = ""


def _normalized_contact_label(value: Any) -> str:
    text = unicodedata.normalize("NFKC", normalize_zh_tw(str(value or ""))).casefold()
    return "".join(
        char for char in text
        if not unicodedata.category(char).startswith(("P", "Z"))
    ).lstrip("@")[:30]


def _phonetic_contact_label(value: Any) -> str:
    """Return a bounded, tone-free pinyin key for Chinese display names."""
    normalized = _normalized_contact_label(value)
    if not normalized or not any("\u3400" <= char <= "\u9fff" for char in normalized):
        return ""
    return "".join(
        lazy_pinyin(
            normalized,
            style=Style.NORMAL,
            strict=False,
            errors=lambda chars: list(chars),
        ),
    ).casefold()


def _levenshtein(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    if not right:
        return len(left)
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            current.append(min(
                current[-1] + 1,
                previous[right_index] + 1,
                previous[right_index - 1] + (left_char != right_char),
            ))
        previous = current
    return previous[-1]


def _fuzzy_limit(length: int) -> tuple[int, float]:
    if length <= 1:
        return 0, 1.0
    if length <= 4:
        return 1, 0.0
    if length <= 8:
        return 2, 0.70
    return max(1, int(length * 0.20)), 0.0


def resolve_accepted_contact_name(user_id: str, name_hint: str) -> ContactNameResolution:
    """Resolve a bounded public label only among the owner's accepted contacts."""
    target = _normalized_contact_label(name_hint)
    if not target:
        return ContactNameResolution("not_found")
    try:
        cursor = matches_coll.find(
            verified_accepted_match_query(user_id),
            {"_id": 0, "from_user": 1, "to_user": 1},
        )
        if hasattr(cursor, "limit"):
            cursor = cursor.limit(MAX_CONTACT_RESOLUTION_CANDIDATES + 1)
        matches = list(cursor)[:MAX_CONTACT_RESOLUTION_CANDIDATES + 1]
    except Exception:
        return ContactNameResolution("unavailable")
    if len(matches) > MAX_CONTACT_RESOLUTION_CANDIDATES:
        return ContactNameResolution("too_many")

    ids: list[str] = []
    for match in matches:
        candidate = other_id(match, user_id)
        if isinstance(candidate, str) and candidate and candidate not in ids:
            ids.append(candidate)
    if not ids:
        return ContactNameResolution("not_found")
    try:
        profiles = list(profiles_coll.find(
            {"user_id": {"$in": ids}},
            {"_id": 0, "user_id": 1, "display_name": 1, "nickname": 1, "name": 1},
        ))
    except Exception:
        return ContactNameResolution("unavailable")
    labels: list[tuple[str, str, str, str]] = []
    profile_by_id = {str(item.get("user_id")): item for item in profiles}
    warm_public_nicknames(ids)
    for candidate in ids:
        profile = profile_by_id.get(candidate) or {}
        label = contact_display_name(candidate, profile)
        normalized = _normalized_contact_label(label)
        if label and label != "對方" and normalized:
            labels.append((candidate, label, normalized, _phonetic_contact_label(label)))
    if len(labels) != len(ids):
        # Unknown labels are not proof that a named accepted contact is absent;
        # they also prevent safely ruling out duplicate names.
        return ContactNameResolution("unavailable")
    exact = [(candidate, label) for candidate, label, normalized, _phonetic in labels if normalized == target]
    if len(exact) == 1:
        return ContactNameResolution("resolved_exact", exact[0][0], exact[0][1], kind="exact")
    if len(exact) > 1:
        return ContactNameResolution("ambiguous", candidates=tuple(label for _, label in exact[:3]))

    phonetic_target = _phonetic_contact_label(target)
    if phonetic_target:
        phonetic_matches = [
            (candidate, label)
            for candidate, label, _normalized, phonetic in labels
            if phonetic and phonetic == phonetic_target
        ]
        if len(phonetic_matches) == 1:
            return ContactNameResolution(
                "resolved_phonetic",
                phonetic_matches[0][0],
                phonetic_matches[0][1],
                kind="phonetic",
            )
        if len(phonetic_matches) > 1:
            return ContactNameResolution(
                "ambiguous",
                candidates=tuple(label for _, label in phonetic_matches[:3]),
            )

    maximum_distance, minimum_similarity = _fuzzy_limit(max(len(target), 1))
    scored: list[tuple[int, float, str, str]] = []
    for candidate, label, normalized, _phonetic in labels:
        length = max(len(target), len(normalized))
        distance = _levenshtein(target, normalized)
        similarity = 1.0 - (distance / length if length else 1.0)
        candidate_limit, candidate_minimum_similarity = _fuzzy_limit(length)
        if distance <= min(maximum_distance, candidate_limit) and similarity >= max(minimum_similarity, candidate_minimum_similarity):
            scored.append((distance, similarity, candidate, label))
    if not scored:
        return ContactNameResolution("not_found")
    scored.sort(key=lambda item: (item[0], -item[1], item[3], item[2]))
    best = scored[0]
    tied = [item for item in scored if item[0] == best[0] and item[1] == best[1]]
    if len(tied) > 1 or (len(scored) > 1 and best[1] - scored[1][1] < 0.20):
        return ContactNameResolution("ambiguous", candidates=tuple(item[3] for item in scored[:3]))
    return ContactNameResolution("resolved_fuzzy", best[2], best[3], kind="fuzzy")


def mentioned_contact_summary(user_id: str, other_user_ids: list[str]) -> list[dict[str, Any]]:
    """Return public summaries only for verified accepted mentioned contacts."""
    contacts: list[dict[str, Any]] = []
    projection = {
        "_id": 0, "from_user": 1, "to_user": 1, "reason_items": 1,
        "receiver_reason_items": 1, "directional_reason_v2": 1,
        "reason_version": 1, "friend_intro_v4": 1,
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


def accepted_contact_summaries(
    user_id: str, limit: int = MAX_LISTED_ACCEPTED_CONTACTS,
    *, reference_scope: str = "legacy",
) -> tuple[list[dict[str, Any]], bool]:
    """Return a bounded public projection of the owner's accepted contacts only."""
    safe_limit = max(1, min(int(limit), MAX_LISTED_ACCEPTED_CONTACTS))
    projection = {
        "_id": 0, "from_user": 1, "to_user": 1, "reason_items": 1,
        "receiver_reason_items": 1, "directional_reason_v2": 1,
        "reason_version": 1, "friend_intro_v4": 1,
        "distinctive_tags": 1, "recommendation_tier": 1, "updated_at": 1,
    }
    matches = list(matches_coll.find(verified_accepted_match_query(user_id), projection))
    matches.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    truncated = len(matches) > safe_limit
    contacts: list[dict[str, Any]] = []
    warm_public_nicknames([other_id(match, user_id) for match in matches[:safe_limit]])
    for match in matches[:safe_limit]:
        other_user_id = other_id(match, user_id)
        if not other_user_id:
            continue
        public_profile = safe_public_profile(other_user_id)
        tags = [public_text(value, 30) for value in (match.get("distinctive_tags") or [])]
        contacts.append({
            "contact_ref": contact_reference(user_id, other_user_id, reference_scope),
            "display_name": display_name(other_user_id),
            **public_profile,
            "safe_match_reason": safe_match_reason(match, user_id),
            "verified_common_ground": verified_common_ground(match, user_id),
            "distinctive_tags": [tag for tag in tags if tag][:4],
            "evidence_available": bool(
                public_profile.get("recent_context")
                or public_profile.get("initial_interest")
                or public_profile.get("personality_summary")
                or safe_match_reason(match, user_id)
            ),
        })
    return contacts, truncated


def contact_evidence_by_refs(
    user_id: str, contact_refs: list[str], *, reference_scope: str = "legacy",
    limit: int = 3,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve only current accepted contacts represented by opaque refs."""
    wanted = [str(ref).strip() for ref in contact_refs[:limit] if str(ref).strip()]
    if not wanted:
        return [], []
    matches = list(matches_coll.find(verified_accepted_match_query(user_id)))
    matches.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
    matches = matches[:MAX_LISTED_ACCEPTED_CONTACTS]
    result: list[dict[str, Any]] = []
    found: set[str] = set()
    for match in matches:
        other_user_id = other_id(match, user_id)
        if not other_user_id:
            continue
        ref = contact_reference(user_id, other_user_id, reference_scope)
        if ref not in wanted or ref in found:
            continue
        found.add(ref)
        profile = profiles_coll.find_one(
            {"user_id": other_user_id},
            {"_id": 0, "current_context": 1, "initial_interest": 1, "big_five.summary": 1},
        ) or {}
        values = {
            "recent_context": public_text(profile.get("current_context"), 400),
            "initial_interest": public_text(profile.get("initial_interest"), 400),
            "personality_summary": public_text((profile.get("big_five") or {}).get("summary"), 400),
            "safe_match_reason": public_text(reason_for_viewer(match, user_id), 400),
            "verified_common_ground": verified_common_ground(match, user_id),
            "distinctive_tags": [public_text(value, 80) for value in (match.get("distinctive_tags") or [])[:4]],
        }
        fields = [key for key, value in values.items() if value]
        result.append({
            "contact_ref": ref,
            "display_name": display_name(other_user_id),
            **values,
            "evidence_fields": fields,
            "truncated": any(len(str(value)) >= 400 for value in values.values() if isinstance(value, str)),
        })
    return result, [ref for ref in wanted if ref not in found]
