"""Bounded, non-spam match-opportunity policy for public Ayue V2."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any

from database import matches_coll, profiles_coll
from services.match_state_service import load_match_state


GUIDANCE_COOLDOWN_SECONDS = 7 * 86400
GUIDANCE_DECLINE_SUPPRESSION_SECONDS = 30 * 86400
RECENT_DECLINE_SUPPRESSION_SECONDS = 86400
GUIDANCE_OFFER_TTL_SECONDS = 15 * 60
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
    if load_match_state(user_id)["search_blocked"]:
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


def record_guidance_shown(
    user_id: str, fingerprint: str, *, active_offer: bool = True,
) -> dict[str, Any]:
    now = time.time()
    payload = {
        "last_fingerprint": fingerprint,
        "last_shown_at": now,
    }
    if active_offer:
        payload.update({
            "active_offer_fingerprint": fingerprint,
            "active_offer_expires_at": now + GUIDANCE_OFFER_TTL_SECONDS,
        })
    return payload


def record_guidance_declined(user_id: str, fingerprint: str | None = None) -> dict[str, Any]:
    now = time.time()
    payload = {"suppressed_until": now + GUIDANCE_DECLINE_SUPPRESSION_SECONDS}
    if fingerprint:
        payload["last_fingerprint"] = fingerprint
    return payload


def claim_guidance_offer(user_id: str, fingerprint: str) -> bool:
    """Atomically claim one ambient offer for a user.

    This is guidance state only; it never authorizes or queues a match search.
    The atomic predicate prevents two browser tabs from showing the same offer
    and records the cooldown state that V3 previously forgot to persist.
    """
    now = time.time()
    try:
        current = profiles_coll.find_one(
            {"user_id": user_id},
            {"_id": 0, "match_guidance": 1},
        ) or {}
    except Exception:
        # Guidance is optional UX state.  A profile-store outage must not
        # turn an ordinary chat turn into a V3 runtime failure.
        return False
    guidance = current.get("match_guidance") or {}
    try:
        active_expires_at = float(guidance.get("active_offer_expires_at", 0) or 0)
    except (TypeError, ValueError):
        active_expires_at = 0.0
    if str(guidance.get("active_offer_fingerprint") or "") and active_expires_at > now:
        return False
    shown = record_guidance_shown(user_id, fingerprint, active_offer=True)
    try:
        result = profiles_coll.update_one(
            {
                "user_id": user_id,
                "$and": [
                    {"$or": [
                        {"match_guidance.last_fingerprint": {"$exists": False}},
                        {"match_guidance.last_fingerprint": {"$ne": fingerprint}},
                    ]},
                    {"$or": [
                        {"match_guidance.last_shown_at": {"$exists": False}},
                        {"match_guidance.last_shown_at": {"$lte": now - GUIDANCE_COOLDOWN_SECONDS}},
                    ]},
                ],
            },
            {"$set": {"match_guidance": shown}},
        )
    except Exception:
        return False
    return bool(getattr(result, "modified_count", 0))


def active_guidance_offer(profile: dict[str, Any]) -> dict[str, Any] | None:
    guidance = profile.get("match_guidance") or {}
    fingerprint = _text(guidance.get("active_offer_fingerprint"))
    try:
        expires_at = float(guidance.get("active_offer_expires_at", 0) or 0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if not fingerprint or expires_at <= time.time():
        return None
    return {
        "fingerprint": fingerprint,
        "expires_at": expires_at,
    }


def accept_guidance_offer(user_id: str, fingerprint: str) -> bool:
    """Consume an ambient offer before creating an explicit confirmation."""
    try:
        result = profiles_coll.update_one(
            {
                "user_id": user_id,
                "match_guidance.active_offer_fingerprint": fingerprint,
                "match_guidance.active_offer_expires_at": {"$gt": time.time()},
            },
            {"$unset": {
                "match_guidance.active_offer_fingerprint": "",
                "match_guidance.active_offer_expires_at": "",
            }},
        )
    except Exception:
        return False
    return bool(getattr(result, "modified_count", 0))


def decline_guidance_offer(user_id: str, fingerprint: str) -> bool:
    """Consume and suppress an ambient offer after an explicit decline."""
    payload = record_guidance_declined(user_id, fingerprint)
    try:
        result = profiles_coll.update_one(
            {
                "user_id": user_id,
                "match_guidance.active_offer_fingerprint": fingerprint,
            },
            {"$set": {"match_guidance": payload}},
        )
    except Exception:
        return False
    return bool(getattr(result, "modified_count", 0))


def missing_basis_question(assessment: MatchOpportunityAssessment) -> str:
    labels = {
        "recent_context": "你最近想認識什麼樣的人，或想一起做什麼？",
        "preferences": "你在意對方哪一個特質或生活習慣？",
        "values": "你在一段關係裡最重視什麼？",
        "personality": "你比較喜歡和什麼樣節奏的人相處？",
    }
    key = assessment.missing_basis[0] if assessment.missing_basis else "preferences"
    return labels[key]
