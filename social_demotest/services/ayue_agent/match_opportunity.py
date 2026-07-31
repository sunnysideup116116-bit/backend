"""Bounded, non-spam match-opportunity policy for public Ayue V2."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from database import matches_coll


GUIDANCE_COOLDOWN_SECONDS = 7 * 86400
GUIDANCE_DECLINE_SUPPRESSION_SECONDS = 30 * 86400
RECENT_DECLINE_SUPPRESSION_SECONDS = 86400
# Only an unresolved proposal blocks another search.  An accepted match is a
# completed connection: it remains excluded from future candidate selection,
# but it must not prevent an explicit request to meet somebody else.
LIVE_MATCH_STATUSES = {"draft", "pending"}


@dataclass(frozen=True)
class MatchOpportunityAssessment:
    state: str
    reason_codes: tuple[str, ...] = ()
    fingerprint: str = ""
    profile_basis_count: int = 0
    missing_basis: tuple[str, ...] = ()


def _text(value: Any) -> str:
    return str(value or "").strip()


def _has_non_placeholder_context(profile: dict[str, Any]) -> bool:
    return _text(profile.get("current_context")) not in {"", "交朋友", "尚無近期情境"}


def _has_preferences(profile: dict[str, Any]) -> bool:
    return bool(profile.get("profile_memory_preview")) or bool(_text(profile.get("profile_memory_summary")))


def _has_values(profile: dict[str, Any]) -> bool:
    deep = profile.get("deep_profile") or {}
    return bool(deep.get("values") or deep.get("relationship_needs") or deep.get("life_goals"))


def _has_personality(profile: dict[str, Any]) -> bool:
    big_five = profile.get("big_five") or {}
    return bool(_text(big_five.get("summary"))) or sum(
        1 for key in ("O", "C", "E", "A", "N") if isinstance(big_five.get(key), (int, float))
    ) >= 3


def _profile_basis(profile: dict[str, Any]) -> dict[str, bool]:
    return {
        "recent_context": _has_non_placeholder_context(profile),
        "preferences": _has_preferences(profile),
        "values": _has_values(profile),
        "personality": _has_personality(profile),
    }


def opportunity_fingerprint(profile: dict[str, Any]) -> str:
    """Hash only the minimal readiness projection; never put it in public prompts."""
    basis = _profile_basis(profile)
    preferences = []
    for item in (profile.get("profile_memory_preview") or [])[:8]:
        if isinstance(item, dict):
            preferences.append({
                "key": _text(item.get("key")),
                "label": _text(item.get("label") or item.get("label_zh_tw")),
                "stance": _text(item.get("stance")),
            })
        else:
            preferences.append(_text(item))
    deep = profile.get("deep_profile") or {}
    projection = {
        "context_revision": int(profile.get("current_context_revision", 0) or 0),
        "current_context": _text(profile.get("current_context")),
        "basis": basis,
        "preferences": preferences,
        "deep_profile": {
            key: deep.get(key)
            for key in ("values", "relationship_needs", "life_goals", "stress_coping", "ideal_future")
            if deep.get(key) not in (None, "", [], {})
        },
    }
    return hashlib.sha256(
        json.dumps(projection, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()[:16]


def assess_match_opportunity(profile: dict[str, Any], user_id: str, *, explicit_search: bool = False) -> MatchOpportunityAssessment:
    """Assess whether a gentle invitation is appropriate; never authorise a search."""
    guidance = profile.get("match_guidance") or {}
    now = time.time()
    fingerprint = opportunity_fingerprint(profile)
    active = matches_coll.find_one({
        "status": {"$in": list(LIVE_MATCH_STATUSES)},
        "$or": [{"from_user": user_id}, {"to_user": user_id}],
    }, {"_id": 1})
    if active or bool(profile.get("matchmaking_in_progress")) or _text((profile.get("match_search") or {}).get("status")) in {"searching", "queued"}:
        return MatchOpportunityAssessment("active_match_blocked", ("active_match",), fingerprint)
    recent_decline = matches_coll.find_one({
        "status": "declined",
        "$or": [{"from_user": user_id}, {"to_user": user_id}],
        "updated_at": {"$gte": now - RECENT_DECLINE_SUPPRESSION_SECONDS},
    }, {"_id": 1})
    if recent_decline and not explicit_search:
        return MatchOpportunityAssessment("suppressed", ("recent_decline",), fingerprint)
    if not explicit_search:
        if float(guidance.get("suppressed_until", 0) or 0) > now:
            return MatchOpportunityAssessment("suppressed", ("user_declined",), fingerprint)
        if guidance.get("last_fingerprint") == fingerprint:
            return MatchOpportunityAssessment("suppressed", ("same_fingerprint",), fingerprint)
        if now - float(guidance.get("last_shown_at", 0) or 0) < GUIDANCE_COOLDOWN_SECONDS:
            return MatchOpportunityAssessment("suppressed", ("cooldown",), fingerprint)
    basis = _profile_basis(profile)
    strong_basis = basis["recent_context"] or basis["preferences"] or basis["values"]
    count = sum(basis.values())
    if count < 2 or not strong_basis:
        missing = tuple(name for name, available in basis.items() if not available)
        return MatchOpportunityAssessment("not_ready", ("profile_basis_insufficient",), fingerprint, count, missing)
    return MatchOpportunityAssessment("ready", (), fingerprint, count)


def record_guidance_shown(user_id: str, fingerprint: str) -> dict[str, Any]:
    return {
        "last_fingerprint": fingerprint,
        "last_shown_at": time.time(),
    }


def record_guidance_declined(user_id: str, fingerprint: str | None = None) -> dict[str, Any]:
    now = time.time()
    payload = {"suppressed_until": now + GUIDANCE_DECLINE_SUPPRESSION_SECONDS}
    if fingerprint:
        payload["last_fingerprint"] = fingerprint
    return payload


def missing_basis_question(assessment: MatchOpportunityAssessment) -> str:
    labels = {
        "recent_context": "你最近想認識什麼樣的人，或想一起做什麼？",
        "preferences": "你在意對方哪一個特質或生活習慣？",
        "values": "你在一段關係裡最重視什麼？",
        "personality": "你比較喜歡和什麼樣節奏的人相處？",
    }
    key = assessment.missing_basis[0] if assessment.missing_basis else "preferences"
    return labels[key]
