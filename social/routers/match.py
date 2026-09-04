import time
import requests
import json
import re
import os
import uuid
from typing import Callable
from fastapi import APIRouter, HTTPException, BackgroundTasks
from models import (
    MatchRequest, AcceptRequest, MatchDecisionRequest, ProactiveEventRequest,
    EventDiscoveryRequest, EventOpportunityScanRequest,
)
from database import profiles_coll, matches_coll
from services.ai_service import get_embedding, generate_chat_completion
from services.memory_service import get_user_graph_memories
from services.mediator_event_service import queue_mediator_event
from services.match_state_service import (
    get_match_status_snapshot, has_verified_acceptance, reconcile_live_match,
)
from services.match_reason_service import (
    FRIEND_COPY_VERSION,
    MATCH_PROPOSAL_FEW_SHOTS,
    MATCH_PROPOSAL_STYLE_IDS,
    PRIVATE_OPENING_FEW_SHOTS,
    V4_REASON_VERSION,
    build_v4_snapshot_fallback,
    friend_intro_fallback,
    match_reason_style_id,
    public_personality_phrase,
    reason_for_viewer,
    short_public_text as reason_public_text,
    valid_accepted_opening_text,
    valid_friend_intro_text,
)
from services.match_action_service import (
    decide_active_proposal as decide_active_proposal_action,
    decide_match as decide_match_action,
    start_match_search,
)
from services.match_search_job_service import (
    MatchSearchPipelineError,
    cancel_match_search,
    public_match_search_status,
    register_match_search_pipeline,
)
from services.event_discovery_job_service import (
    enqueue_event_discovery_job, event_discovery_job_snapshot,
)
from services.event_relevance_service import rebuild_all_event_relevance
from services.event_opportunity_service import create_event_opportunity, scan_event_opportunities
from services.event_lifecycle_service import run_event_lifecycle_once
from services.event_card_projection import public_event_card
from services.proposal_namespace import (
    EVENT_INVITATION_NAMESPACE,
    RELATIONSHIP_MATCH_NAMESPACE,
    live_proposal_query,
    namespace_for_document,
    participant_pair_key,
)
from services.profile_projection import safe_recent_context
from services.match_card_projection import (
    proposal_card_state, proposal_counterparty_nickname,
)
from services.public_nickname_service import proposal_display_name
from services.language_service import normalize_zh_tw
from services.risk_block_service import (
    RiskBlockServiceUnavailable,
    risk_block_service,
)
from services.ayue_agent.public_relationship_projection import (
    anonymize_counterparty_text,
    display_name as public_display_name,
)
from bson.objectid import ObjectId
from bson.errors import InvalidId
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

router = APIRouter(prefix="/api/match", tags=["Match"])
DRAFT_TTL_SECONDS = 24 * 3600
PENDING_TTL_SECONDS = 72 * 3600
SEARCH_LOCK_TTL_SECONDS = 5 * 60
LIVE_MATCH_STATUSES = {"draft", "pending"}
MATCH_CANDIDATE_POOL_SIZE = 20
MATCH_MAX_CANDIDATE_BATCHES = 3
MATCH_SELECTION_TIMEOUT_SECONDS = 120.0


def ensure_match_indexes():
    """Keep one unresolved proposal per participant in each namespace."""
    try:
        matches_coll.create_index(
            [("proposal_namespace", 1), ("live_participants", 1)],
            unique=True,
            partialFilterExpression={"status": {"$in": ["draft", "pending"]}},
            name="one_live_proposal_per_namespace_participant",
        )
    except Exception as exc:
        # A legacy duplicate must be migrated before Atlas can create this index.
        print(f"[match] unable to create live-match index: {exc}")


def agent_candidate_limit() -> int:
    try:
        value = int(os.getenv("MATCH_AGENT_CANDIDATE_LIMIT", "3"))
    except (TypeError, ValueError):
        value = 3
    return max(1, min(value, 5))


def vector_qualification_minimum() -> float:
    try:
        value = float(os.getenv("MATCH_VECTOR_QUALIFICATION_MIN", "0.55"))
    except (TypeError, ValueError):
        value = 0.55
    return max(0.0, min(value, 1.0))


def _set_match_search(user_id: str, status: str, source: str, **extra):
    payload = {"status": status, "source": source, "updated_at": time.time(), **extra}
    profiles_coll.update_one(
        {"user_id": user_id},
        {"$set": {"match_search": payload}},
        upsert=True,
    )


def reconcile_match_state(user_id: str):
    """Compatibility facade; canonical reconciliation lives in the state service."""
    return reconcile_live_match(user_id)

def derive_match_stage(match_doc: dict, user_id: str) -> str:
    if not match_doc:
        return "idle"
    status = match_doc.get("status")
    if status == "draft" and match_doc.get("from_user") == user_id:
        return "waiting_user"
    if status == "pending" and match_doc.get("from_user") == user_id:
        return "waiting_other"
    if status == "pending" and match_doc.get("to_user") == user_id:
        return "incoming_decision"
    if status == "accepted":
        return "completed"
    return status or "idle"


def build_active_proposal_card(match_doc: dict, user_id: str):
    stage = derive_match_stage(match_doc, user_id)
    if stage not in {"waiting_user", "waiting_other", "incoming_decision"}:
        return None
    is_initiator = match_doc.get("from_user") == user_id
    other_id = match_doc.get("to_user") if is_initiator else match_doc.get("from_user")
    other_name = public_display_name(other_id)
    initiator_id = match_doc.get("from_user")
    receiver_id = match_doc.get("to_user")
    viewer_reason = reason_for_viewer(match_doc, user_id)
    snapshot = match_doc.get("match_context_snapshot", {}) or {}
    own_snapshot = snapshot.get("target" if is_initiator else "candidate", {}) or {}
    other_snapshot = snapshot.get("candidate" if is_initiator else "target", {}) or {}
    is_v4 = match_doc.get("reason_version") == V4_REASON_VERSION
    reason_items = [] if is_v4 else (
        match_doc.get("reason_items", [])
        if is_initiator else match_doc.get("receiver_reason_items", [])
    )
    reasons = [
        anonymize_counterparty_text(item.get("text"), other_id, 120, counterparty_name=other_name)
        for item in reason_items
        if item.get("kind") in {"shared_graph", "shared_context", "shared_value"} and item.get("text")
    ][:2]
    if not reasons:
        reasons = [anonymize_counterparty_text(
            reason_for_viewer(match_doc, user_id) or "目前沒有明確共同點，適合先從公開近況聊起。",
            other_id, 180, counterparty_name=other_name,
        )]
    score = (
        match_doc.get("score_breakdown", {})
        if is_initiator else match_doc.get("receiver_score_breakdown", {})
    )
    card = {
        "match_id": str(match_doc["_id"]),
        # Keep shared/model-facing projections anonymous. The HTTP adapter
        # adds a public nickname separately for the proposal UI only.
        "other_label": "對方",
        "stage": stage,
        "event_type": "match_proposal" if is_initiator else "incoming_match_interest",
        "proposal_role": "initiator" if is_initiator else "receiver",
        "opening": (
            "欸，我找到一位可能適合你的人，想先問問你的感覺。"
            if is_initiator else "欸，有位人選想認識你，我先來問你本人。"
        ),
        "your_context": reason_public_text(
            safe_recent_context(own_snapshot.get("current_context"), "尚無近期情境"), 80,
        ),
        "other_context": anonymize_counterparty_text(
            reason_public_text(
                safe_recent_context(other_snapshot.get("current_context"), "尚無近期情境"), 80,
            ), other_id, 80,
            counterparty_name=other_name,
        ),
        "reasons": reasons,
        "score": round(float(score.get("total", 0) or 0)),
        "viewer_reason": anonymize_counterparty_text(
            viewer_reason, other_id, 180, counterparty_name=other_name,
        ),
        "reason_version": str(match_doc.get("reason_version") or "legacy"),
        "proposal_namespace": namespace_for_document(match_doc),
        "proposal_revision": int(match_doc.get("proposal_revision", 0) or 0),
    }
    event_card = public_event_card(match_doc)
    if event_card:
        card["event"] = event_card
    # The old distinctive_tags field describes the candidate, not both viewers.
    # Publish a separate, role-bound list for the optional feedback dialog. It
    # contains only saved public proposal facts, never live/private preferences.
    options = []
    if is_initiator and namespace_for_document(match_doc) != EVENT_INVITATION_NAMESPACE:
        tags = match_doc.get("distinctive_tags")
        if isinstance(tags, list):
            options.extend(value for value in tags if isinstance(value, str))
    if other_snapshot.get("user_id") == other_id:
        signals = other_snapshot.get("context_signals") or {}
        options.extend([
            other_snapshot.get("public_personality"),
            signals.get("activity") if isinstance(signals, dict) else None,
            safe_recent_context(other_snapshot.get("current_context"), ""),
        ])
    if event_card and event_card.get("category"):
        options.insert(0, event_card["category"])
    card["decline_reason_options"] = list(dict.fromkeys(
        text for value in options if isinstance(value, str)
        if (text := anonymize_counterparty_text(
            value, other_id, 120, counterparty_name=other_name,
        ).strip())
    ))[:6]
    # V4 exposes exactly one reason: the projection bound to this viewer.
    # Keep legacy aliases only for old records whose API shape cannot change.
    if not is_v4:
        card.update({
            "proposal_revision": int(match_doc.get("proposal_revision", 0)),
            "other_id": other_id,
            "recommendation_reason": anonymize_counterparty_text(
                reason_for_viewer(match_doc, initiator_id), other_id, 180, counterparty_name=other_name,
            ),
            "receiver_reason": anonymize_counterparty_text(
                reason_for_viewer(match_doc, receiver_id), other_id, 180, counterparty_name=other_name,
            ),
        })
    return card


def build_status_proposal_card(match_doc: dict, user_id: str):
    """Safe card for polling only; decision identifiers arrive via mediator events."""
    card = build_active_proposal_card(match_doc, user_id)
    if not card:
        return None
    return {
        key: card[key]
        for key in (
            "other_label", "stage", "event_type", "opening", "your_context",
            "other_context", "reasons", "score", "viewer_reason",
            "event", "proposal_namespace", "proposal_revision",
        )
        if key in card
    }


def _single_live_namespace_proposal(user_id: str, namespace: str) -> dict | None:
    """Return one live namespace slot, failing closed on legacy duplicates."""
    rows = list(matches_coll.find(
        live_proposal_query(user_id, namespace),
    ).sort([("created_at", -1)]).limit(2))
    return rows[0] if len(rows) == 1 else None
def _short_text(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", normalize_zh_tw(str(value or ""))).strip()
    return text[:limit].rstrip()


def _short_list(value, item_limit: int = 3, char_limit: int = 24) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    clean = []
    for item in items:
        text = _short_text(item, char_limit)
        if text and text not in clean:
            clean.append(text)
        if len(clean) >= item_limit:
            break
    return clean


def strip_agent_payload(doc):
    """Return a compact profile for the LLM agent."""
    if not isinstance(doc, dict):
        return doc
    big_five = doc.get("big_five") or {}
    deep_profile = doc.get("deep_profile") or {}
    compact = {
        "user_id": doc.get("user_id"),
        "initial_interest": _short_text(doc.get("initial_interest"), 80),
        "current_context": _short_text(doc.get("current_context"), 110),
        "profile_memory_summary": _short_text(doc.get("profile_memory_summary"), 90),
        "context_signals": {
            key: _short_text(value, 36)
            for key, value in (doc.get("context_signals") or {}).items()
            if value
        },
        "big_five": {
            "O": big_five.get("O"),
            "C": big_five.get("C"),
            "E": big_five.get("E"),
            "A": big_five.get("A"),
            "N": big_five.get("N"),
            "summary": _short_text(big_five.get("summary"), 90),
        },
        "deep_profile": {
            "summary": _short_text(deep_profile.get("summary"), 120),
            "values": _short_list(deep_profile.get("values"), 3, 24),
            "life_goals": _short_list(deep_profile.get("life_goals"), 3, 24),
            "relationship_needs": _short_list(deep_profile.get("relationship_needs"), 3, 24),
            "stress_coping": _short_text(deep_profile.get("stress_coping"), 70),
            "ideal_future": _short_text(deep_profile.get("ideal_future"), 90),
        },
    }
    compact["big_five"] = {
        key: value for key, value in compact["big_five"].items()
        if value is not None and value != ""
    }
    compact["deep_profile"] = {
        key: value for key, value in compact["deep_profile"].items()
        if value not in ("", [], None)
    }
    return {
        key: value for key, value in compact.items()
        if value not in ("", {}, [], None)
    }


def _text_values(value):
    if isinstance(value, list):
        return {str(item).strip() for item in value if str(item).strip()}
    if isinstance(value, str) and value.strip():
        return {value.strip()}
    return set()


def _deep_profile_values(profile):
    values = set()
    for key, value in (profile or {}).items():
        if key == "summary":
            continue
        values.update(_text_values(value))
    return values


def _trait_stances(user_id: str) -> dict[str, set[str]]:
    stances: dict[str, set[str]] = {}
    for item in get_user_graph_memories(user_id, 20):
        key = str(item.get("key") or "").strip()
        stance = str(item.get("stance") or "").strip()
        if key and stance:
            stances.setdefault(key, set()).add(stance)
    return stances


def candidate_qualification(
    target: dict, candidate: dict, *,
    target_stances: dict[str, set[str]] | None = None,
    candidate_stances: dict[str, set[str]] | None = None,
    vector_score: float = 0.0,
) -> dict:
    """Require a reciprocal safety check plus one strong, owner-grounded link."""
    target_stances = target_stances if target_stances is not None else _trait_stances(target.get("user_id"))
    candidate_stances = candidate_stances if candidate_stances is not None else _trait_stances(candidate.get("user_id"))
    hard_conflicts = []
    for left, right in ((target_stances, candidate_stances), (candidate_stances, target_stances)):
        for key, stances in left.items():
            if {"dislike", "avoid"} & stances and {"like", "require"} & right.get(key, set()):
                hard_conflicts.append(key)
    shared_persistent_preferences = sorted(
        key for key in target_stances.keys() & candidate_stances.keys()
        if ({"like", "require"} & target_stances[key])
        and ({"like", "require"} & candidate_stances[key])
    )
    target_values = _deep_profile_values(target.get("deep_profile", {}))
    candidate_values = _deep_profile_values(candidate.get("deep_profile", {}))
    shared_values = sorted(target_values & candidate_values)
    target_activity = str((target.get("context_signals") or {}).get("activity") or "").strip()
    candidate_activity = str((candidate.get("context_signals") or {}).get("activity") or "").strip()
    same_activity = bool(target_activity and candidate_activity and target_activity == candidate_activity)
    strong_reason_codes = []
    if same_activity:
        strong_reason_codes.append("shared_activity")
    if shared_persistent_preferences:
        strong_reason_codes.append("shared_persistent_preference")
    if shared_values:
        strong_reason_codes.append("shared_value")
    target_context = str(target.get("current_context") or "").strip()
    candidate_context = str(
        candidate.get("current_context")
        or candidate.get("initial_interest")
        or (candidate.get("context_signals") or {}).get("activity")
        or ""
    ).strip()
    if (
        target_context
        and candidate_context
        and float(vector_score or 0) >= vector_qualification_minimum()
    ):
        strong_reason_codes.append("semantic_context_similarity")
    return {
        "eligible": not hard_conflicts and bool(strong_reason_codes),
        "hard_conflict_keys": sorted(set(hard_conflicts)),
        "strong_reason_codes": strong_reason_codes,
    }


def validated_distinctive_tags(candidate: dict) -> list[str]:
    """Build proposal tags only from fields the candidate already owns."""
    deep = candidate.get("deep_profile") or {}
    values = [
        (candidate.get("context_signals") or {}).get("activity"),
        candidate.get("initial_interest"),
        *((deep.get("values") or []) if isinstance(deep.get("values"), list) else [deep.get("values")]),
        (candidate.get("big_five") or {}).get("summary"),
    ]
    tags = []
    for value in values:
        text = _short_text(value, 28)
        if text and text not in tags:
            tags.append(text)
        if len(tags) >= 4:
            break
    return tags


def build_validated_match_explanation(target: dict, candidate: dict, vector_score: float):
    """Build user-visible scores and reasons only from owner-bound facts."""
    target_id, candidate_id = target.get("user_id"), candidate.get("user_id")
    target_graph = get_user_graph_memories(target_id, 20)
    candidate_graph = get_user_graph_memories(candidate_id, 20)
    target_traits = {
        (item.get("key"), item.get("stance")): item for item in target_graph if item.get("key")
    }
    candidate_traits = {
        (item.get("key"), item.get("stance")): item for item in candidate_graph if item.get("key")
    }
    shared_traits = [
        (target_traits[key], candidate_traits[key])
        for key in target_traits.keys() & candidate_traits.keys()
        if key[1] not in {"dislike", "avoid"}
    ]
    conflicts = [
        key for key, item in target_traits.items()
        if key[1] in {"dislike", "avoid"}
        and any(candidate_key == key[0] and stance in {"like", "require"}
                for candidate_key, stance in candidate_traits)
    ]

    target_deep = _deep_profile_values(target.get("deep_profile", {}))
    candidate_deep = _deep_profile_values(candidate.get("deep_profile", {}))
    shared_values = sorted(target_deep & candidate_deep)
    union_values = target_deep | candidate_deep

    target_signals = target.get("context_signals", {}) or {}
    candidate_signals = candidate.get("context_signals", {}) or {}
    shared_context = []
    for key in ("activity", "timing", "preference", "companion_intent"):
        left, right = target_signals.get(key), candidate_signals.get(key)
        if left and right and str(left).strip() == str(right).strip():
            shared_context.append((key, str(left).strip()))

    target_bf, candidate_bf = target.get("big_five", {}) or {}, candidate.get("big_five", {}) or {}
    numeric_traits = [
        key for key in ("O", "C", "E", "A", "N")
        if isinstance(target_bf.get(key), (int, float))
        and isinstance(candidate_bf.get(key), (int, float))
    ]
    if numeric_traits:
        average_distance = sum(
            abs(float(target_bf[key]) - float(candidate_bf[key])) for key in numeric_traits
        ) / len(numeric_traits)
        personality_score = round(max(0, 15 * (1 - average_distance / 10)))
    else:
        personality_score = 0

    context_score = round(max(0, min(1, float(vector_score or 0))) * 30)
    graph_score = min(25, len(shared_traits) * 6)
    graph_score = max(0, graph_score - len(conflicts) * 8)
    values_score = round(20 * len(shared_values) / len(union_values)) if union_values else 0
    score_breakdown = {
        "context": context_score,
        "graph": graph_score,
        "values": values_score,
        "personality": personality_score,
        "conversation": 0,
    }
    score_breakdown["total"] = sum(score_breakdown.values())

    reason_items = []
    for left, right in shared_traits[:2]:
        reason_items.append({
            "kind": "shared_graph",
            "text": f"你們都偏好{left.get('label')}",
            "target_evidence_ids": [f"graph:{target_id}:{left.get('key')}"],
            "candidate_evidence_ids": [f"graph:{candidate_id}:{right.get('key')}"],
        })
    for key, value in shared_context[:1]:
        reason_items.append({
            "kind": "shared_context",
            "text": f"你們近期都提到{value}",
            "target_evidence_ids": [f"profile:{target_id}:context_signals:{key}"],
            "candidate_evidence_ids": [f"profile:{candidate_id}:context_signals:{key}"],
        })
    for value in shared_values[:1]:
        reason_items.append({
            "kind": "shared_value",
            "text": f"你們都重視{value}",
            "target_evidence_ids": [f"profile:{target_id}:deep_profile"],
            "candidate_evidence_ids": [f"profile:{candidate_id}:deep_profile"],
        })
    shared_kinds = {"shared_graph", "shared_context", "shared_value"}
    shared_reasons = [item["text"] for item in reason_items if item.get("kind") in shared_kinds]
    candidate_context = str(candidate.get("current_context") or "").strip()
    if shared_reasons:
        tier = "grounded"
        reason_items.append({
            "kind": "public_context",
            "text": f"對方公開近況：{candidate_context}" if candidate_context else "",
            "target_evidence_ids": [],
            "candidate_evidence_ids": [f"profile:{candidate_id}:current_context"] if candidate_context else [],
        })
        top_reasons = shared_reasons[:2]
        recommendation_reason = "已確認共同點：" + "；".join(top_reasons)
        if candidate_context:
            recommendation_reason += f"。對方公開近況：{candidate_context}。"
        recommendation_reason += "可以先從這些共同點聊起。"
    else:
        tier = "exploratory"
        reason_items.append({
            "kind": "exploratory_notice",
            "text": "目前尚未找到明確共同點；這次依整體情境與個性資料排序，適合先聊聊看。",
            "target_evidence_ids": [f"profile:{target_id}:current_context", f"profile:{target_id}:big_five"],
            "candidate_evidence_ids": [f"profile:{candidate_id}:current_context", f"profile:{candidate_id}:big_five"],
        })
        if candidate_context:
            reason_items.append({
                "kind": "public_context",
                "text": f"對方公開近況：{candidate_context}",
                "target_evidence_ids": [],
                "candidate_evidence_ids": [f"profile:{candidate_id}:current_context"],
            })
        top_reasons = ["目前尚未找到明確共同點"]
        recommendation_reason = "探索型推薦：目前尚未找到明確共同點。"
        if candidate_context:
            recommendation_reason += f"對方公開近況：{candidate_context}。"
        recommendation_reason += "這次依整體情境與個性資料排序，適合先聊聊看；不代表對方已同意你的特定行程。"
    reason_items = [item for item in reason_items if item.get("text")]
    reason_items.append({
        "kind": "recommendation_tier",
        "text": tier,
        "target_evidence_ids": [],
        "candidate_evidence_ids": [],
    })
    return score_breakdown, reason_items, top_reasons, recommendation_reason


def _public_personality_phrase(profile: dict) -> str:
    return public_personality_phrase(profile)


def _directional_reason_fallback(viewer: dict, other: dict, tier: str) -> dict:
    return friend_intro_fallback(viewer, other, tier)


def _valid_directional_reason(
    value: object, fallback: dict, *, required_context: str = "",
    required_introduced_personality: str = "", required_viewer_personality: str = "",
    forbidden_viewer_context: str = "",
) -> dict:
    """Allow only short, person-first hypotheses; fall back on any provider drift."""
    if not isinstance(value, dict):
        return fallback
    # V7 asks the provider for role-separated fragments.  Compose them here,
    # after checking each fragment against the correct person's evidence, so a
    # model cannot silently move the other person's recent context onto the
    # viewer (or vice versa).  Keep accepting the V6 viewer_text shape for
    # immutable in-flight proposals and existing deterministic tests.
    if any(key in value for key in ("other_sentence", "viewer_bridge_sentence", "ask_sentence")):
        other_sentence = _short_text(value.get("other_sentence"), 100)
        viewer_bridge = _short_text(value.get("viewer_bridge_sentence"), 100)
        ask_sentence = _short_text(value.get("ask_sentence"), 100)
        if (
            not other_sentence or not viewer_bridge or not ask_sentence
            or (required_context and required_context not in other_sentence)
            or (required_introduced_personality and required_introduced_personality not in other_sentence)
            or (required_viewer_personality and required_viewer_personality not in viewer_bridge)
            or (
                forbidden_viewer_context
                and forbidden_viewer_context != required_context
                and forbidden_viewer_context in other_sentence
            )
            or (required_context and required_context in viewer_bridge)
            or "你" not in viewer_bridge
            or not ask_sentence.endswith(("？", "?"))
        ):
            return fallback
        # A viewer's activity/personality must not be presented as the
        # counterparty's evidence.  The current context is optional, so only
        # reject it when it is explicitly available and distinct.
        text = f"{other_sentence}{viewer_bridge}{ask_sentence}"
        validated = valid_friend_intro_text(
            text,
            required_context=required_context,
            introduced_personality=required_introduced_personality,
            viewer_personality=required_viewer_personality,
            role_bound=True,
        )
        if not validated:
            return fallback
        starter = _short_text(value.get("conversation_starter"), 72)
        accepted_opening = valid_accepted_opening_text(
            value.get("accepted_opening"),
            required_context=required_context,
            introduced_personality=required_introduced_personality,
            viewer_personality=required_viewer_personality,
        )
        return {
            **fallback,
            "viewer_text": validated,
            "conversation_starter": starter or fallback["conversation_starter"],
            "accepted_opening": accepted_opening or fallback["accepted_opening"],
            "role_segments": {
                "other_sentence": other_sentence,
                "viewer_bridge_sentence": viewer_bridge,
                "ask_sentence": ask_sentence,
            },
        }
    text = valid_friend_intro_text(
        value.get("viewer_text"), required_context=required_context,
        introduced_personality=required_introduced_personality,
        viewer_personality=required_viewer_personality,
    )
    starter = _short_text(value.get("conversation_starter"), 72)
    accepted_opening = valid_accepted_opening_text(
        value.get("accepted_opening"), required_context=required_context,
        introduced_personality=required_introduced_personality,
        viewer_personality=required_viewer_personality,
    )
    if not text:
        return fallback
    result = {**fallback, "viewer_text": text, "conversation_starter": starter or fallback["conversation_starter"]}
    if accepted_opening:
        result["accepted_opening"] = accepted_opening
    return result


def _refine_directional_reason(viewer: dict, other: dict, tier: str, fallback: dict) -> dict:
    """Phrase one bound direction at a time so a provider cannot swap roles."""
    other_context = reason_public_text(other.get("current_context"), 56)
    if not other_context:
        return fallback
    style_id = str(fallback.get("style_id") or match_reason_style_id(viewer, other))
    try:
        style_index = MATCH_PROPOSAL_STYLE_IDS.index(style_id)
    except ValueError:
        style_index = 0
    payload = {
        "style_id": style_id,
        "recommendation_tier": tier,
        "person_being_introduced_recent_context": other_context,
        "person_being_introduced_personality": _public_personality_phrase(other),
        "message_receiver_personality": _public_personality_phrase(viewer),
    }
    prompt = f"""你是交友軟體裡像朋友一樣牽線的阿月。只寫給一位收件人，不要產生雙向欄位。
用自然、有變化的繁體中文寫 2 到 3 句：先介紹另一位人選最近想做的事與個性，再用假設語氣說明兩人的個性在那個情境裡可能有什麼舒服、有趣的互動，最後真誠問收件人是否想認識對方或一起參加。
必須原樣保留「person_being_introduced_recent_context」文字，避免角色顛倒。不同活動不可說成共同興趣；不可說對方已答應、已同意或一定合適；不可補名字、地點、活動、個性或其他事實；禁止「物件」、ID、資料庫與技術詞。不要使用固定標題或條列。
只參考這一種指定語氣，不要讀取或重現其他示例：{json.dumps(MATCH_PROPOSAL_FEW_SHOTS[style_index], ensure_ascii=False)}
為避免把舊版固定開頭當成唯一模板，請不要固定使用「欸，我想到一個你可能會想認識的人」；本回合以 style_id 為準。
同時寫一則只會在配對成功後才使用的 accepted_opening：1 到 3 句、像朋友幫忙遞第一句話、須保留 {{{{counterparty}}}} 這個 placeholder 一次，且須包含對方近期情境與雙方公開性格；不可使用真實姓名或補充其他事實。其語氣參考：{json.dumps(PRIVATE_OPENING_FEW_SHOTS, ensure_ascii=False)}
只輸出 JSON：{{"other_sentence":"","viewer_bridge_sentence":"","ask_sentence":"","conversation_starter":"","accepted_opening":""}}
其中 other_sentence 只能描述被介紹者的近期情境與個性，viewer_bridge_sentence 只能描述收件人的個性與可能的互動，ask_sentence 才能提出是否想認識的問題；三段都必須使用假設語氣。
資料：{json.dumps(payload, ensure_ascii=False)}"""
    try:
        raw = json.loads(generate_chat_completion(prompt, temperature=0.55, json_output=True).content)
        return _valid_directional_reason(
            raw, fallback, required_context=other_context,
            required_introduced_personality=_public_personality_phrase(other),
            required_viewer_personality=_public_personality_phrase(viewer),
            forbidden_viewer_context=reason_public_text(viewer.get("current_context"), 56),
        )
    except Exception:
        return fallback


def build_directional_match_explanations(target: dict, candidate: dict, vector_score: float, *, refine: bool = False) -> tuple[dict, dict]:
    """Generate the same pairing from each viewer's perspective without changing ranking."""
    _, items, _, _ = build_validated_match_explanation(target, candidate, vector_score)
    tier = next((item.get("text") for item in items if item.get("kind") == "recommendation_tier"), "exploratory")
    target_fallback = _directional_reason_fallback(target, candidate, tier)
    candidate_fallback = _directional_reason_fallback(candidate, target, tier)
    if not refine:
        return target_fallback, candidate_fallback
    payload = {
        "tier": tier,
        "target_view": {
            "other_recent_context": _short_text(candidate.get("current_context"), 56),
            "viewer_personality": _public_personality_phrase(target),
            "other_personality": _public_personality_phrase(candidate),
        },
        "candidate_view": {
            "other_recent_context": _short_text(target.get("current_context"), 56),
            "viewer_personality": _public_personality_phrase(candidate),
            "other_personality": _public_personality_phrase(target),
        },
    }
    prompt = f"""你在為交友配對撰寫雙向介紹。只使用提供的公開資料。
各方向各寫最多兩句繁體中文：提到『對方』近期情境，再把雙方互動節奏帶入，使用「可能、或許、可以」等假設語氣，最後給一個自然開場方向。
不同活動不是共同興趣；不可說對方已答應、已同意、想找人同行；不可輸出 ID、資料庫、物件、技術詞。
只輸出 JSON：{{"target":{{"viewer_text":"","conversation_starter":""}},"candidate":{{"viewer_text":"","conversation_starter":""}}}}
資料：{json.dumps(payload, ensure_ascii=False)}"""
    try:
        raw = json.loads(generate_chat_completion(prompt, temperature=0.35, json_output=True).content)
        return _valid_directional_reason(raw.get("target"), target_fallback), _valid_directional_reason(raw.get("candidate"), candidate_fallback)
    except Exception:
        return target_fallback, candidate_fallback


def _role_bound_reason_entries(target: dict, candidate: dict, tier: str, *, refine: bool = False) -> list[dict]:
    """Build two role-bound reasons without ever asking a model to map roles.

    The older v2 prompt returned both directions in one JSON object. A model
    could swap those sibling fields and the old validator had no way to detect
    it. V3 constructs each direction from one viewer and one counterparty,
    then persists those bindings with the text.
    """
    entries: list[dict] = []
    for viewer, other in ((target, candidate), (candidate, target)):
        viewer_id, other_id = str(viewer.get("user_id") or ""), str(other.get("user_id") or "")
        if not viewer_id or not other_id or viewer_id == other_id:
            continue
        fallback = _directional_reason_fallback(viewer, other, tier)
        reason = _refine_directional_reason(viewer, other, tier, fallback) if refine else fallback
        text = str(reason.get("viewer_text") or "")
        if not text or "seed_user" in text.lower() or "物件" in text:
            continue
        entries.append({
            "viewer_id": viewer_id,
            "counterparty_id": other_id,
            "counterparty_context_snapshot": _short_text(other.get("current_context"), 56),
            "viewer_text": text,
            "interaction_hypothesis": _short_text(text.split("。", 1)[-1], 76),
            "conversation_starter": _short_text(reason.get("conversation_starter"), 72),
            "used_evidence_keys": list(reason.get("used_evidence_keys") or []),
            "tier": tier,
        })
    return entries


def build_directional_reason_v3(
    target: dict, candidate: dict, vector_score: float, *, refine: bool = False,
) -> list[dict]:
    _, items, _, _ = build_validated_match_explanation(target, candidate, vector_score)
    tier = next((item.get("text") for item in items if item.get("kind") == "recommendation_tier"), "exploratory")
    return _role_bound_reason_entries(target, candidate, tier, refine=refine)


def build_directional_reason_v3_from_snapshot(match_doc: dict) -> list[dict]:
    """Repair/read old proposals from their creation-time data only."""
    snapshot = match_doc.get("match_context_snapshot") or {}
    target = dict(snapshot.get("target") or {}) if isinstance(snapshot, dict) else {}
    candidate = dict(snapshot.get("candidate") or {}) if isinstance(snapshot, dict) else {}
    if (
        target.get("user_id") != match_doc.get("from_user")
        or candidate.get("user_id") != match_doc.get("to_user")
    ):
        return []
    return _role_bound_reason_entries(
        target, candidate,
        str(match_doc.get("recommendation_tier") or "exploratory") if str(match_doc.get("recommendation_tier") or "") in {"grounded", "exploratory"} else "exploratory",
    )


def _friend_intro_entry(viewer: dict, other: dict, tier: str, *, refine: bool) -> dict:
    """Create one V4 projection from one recipient's point of view.

    The identifiers are stored only with the proposal so the runtime can prove
    which direction a projection belongs to.  They are never returned by any
    public card/event projection.
    """
    viewer_id = str(viewer.get("user_id") or "")
    other_id = str(other.get("user_id") or "")
    if not viewer_id or not other_id or viewer_id == other_id:
        return {}
    context_revision = str(
        other.get("context_revision")
        or other.get("profile_revision")
        or other.get("updated_at")
        or ""
    )
    style_id = match_reason_style_id(viewer, other, context_revision=context_revision)
    fallback = friend_intro_fallback(viewer, other, tier, style_id=style_id)
    reason = _refine_directional_reason(viewer, other, tier, fallback) if refine else fallback
    text = _short_text(reason.get("viewer_text"), 220)
    if not text or any(token in text.lower() for token in ("seed_user", "user_id", "mongo", "資料庫", "物件")):
        reason = fallback
        text = _short_text(reason.get("viewer_text"), 110)
    return {
        "copy_version": FRIEND_COPY_VERSION,
        # Internal only: public card projections deliberately drop this field.
        "style_id": style_id,
        "viewer_id": viewer_id,
        "counterparty_id": other_id,
        "counterparty_context_snapshot": reason_public_text(other.get("current_context"), 56),
        "counterparty_public_personality": _public_personality_phrase(other),
        "viewer_public_personality": _public_personality_phrase(viewer),
        "viewer_text": text,
        "conversation_starter": _short_text(reason.get("conversation_starter"), 72),
        "accepted_opening": _short_text(reason.get("accepted_opening"), 220),
        "tier": tier,
    }


def build_friend_intro_v4(
    initiator: dict, receiver: dict, vector_score: float, *, refine: bool = False,
) -> dict:
    """Create the two immutable V4 friend-introduction projections.

    Calls are intentionally sequential and role-local: a model writes one
    invitation to the initiator and a separate one to the receiver.  It never
    gets a bidirectional JSON shape that it could accidentally swap.
    """
    _, items, _, _ = build_validated_match_explanation(initiator, receiver, vector_score)
    tier = next((item.get("text") for item in items if item.get("kind") == "recommendation_tier"), "exploratory")
    if tier not in {"grounded", "exploratory"}:
        tier = "exploratory"
    return {
        "initiator_preview": _friend_intro_entry(initiator, receiver, tier, refine=refine),
        "receiver_invitation": _friend_intro_entry(receiver, initiator, tier, refine=refine),
    }


def build_friend_intro_v4_from_snapshot(match_doc: dict) -> dict:
    """Recover a malformed V4 live projection from immutable creation data."""
    return build_v4_snapshot_fallback(match_doc)


def _existing_job_match_result(match_doc: dict, user_id: str, user_doc: dict) -> dict:
    """Rebuild the bounded pipeline result after a worker lease handoff."""
    matched_id = (
        match_doc.get("to_user")
        if match_doc.get("from_user") == user_id else match_doc.get("from_user")
    )
    candidate = profiles_coll.find_one(
        {"user_id": matched_id}, {"_id": 0, "big_five": 1, "current_context": 1},
    ) or {}
    return {
        "status": "success",
        "matches": [{
            "match_id": str(match_doc.get("_id")),
            "matched_user_id": matched_id,
            "contrast_label": match_doc.get("contrast_label", ""),
            "distinctive_tags": match_doc.get("distinctive_tags", []),
            "score_breakdown": match_doc.get("score_breakdown", {}),
            "top_reasons": match_doc.get("top_reasons", []),
            "reason_items": match_doc.get("reason_items", []),
            "recommendation_reason": reason_for_viewer(match_doc, user_id),
            "viewer_reason": reason_for_viewer(match_doc, user_id),
            "reason_version": match_doc.get("reason_version", "v3"),
            "big_five": candidate.get("big_five", {}),
            "current_context": candidate.get("current_context", ""),
            "target_context": user_doc.get("current_context", ""),
        }],
        "debug_info": [],
    }

def _request_matchmaker_selection(payload: dict, *, timeout: float) -> list[dict]:
    """Evaluate one qualified batch. An empty result must be explicit, never a failure."""
    try:
        response = requests.post("http://127.0.0.1:9001/api/match", json=payload, timeout=timeout)
    except requests.Timeout:
        raise MatchSearchPipelineError("matchmaker_timeout", "matchmaker_request") from None
    except requests.RequestException:
        raise MatchSearchPipelineError("matchmaker_unavailable", "matchmaker_request") from None
    try:
        response.raise_for_status()
    except requests.HTTPError:
        code = "matchmaker_http_error"
        try:
            detail = response.json().get("detail")
            if isinstance(detail, dict) and detail.get("code") in {
                "matchmaker_provider_error", "matchmaker_invalid_response", "matchmaker_timeout",
                "matchmaker_graph_timeout", "matchmaker_graph_unavailable",
                "matchmaker_empty_response", "matchmaker_output_truncated",
            }:
                code = detail["code"]
        except (ValueError, AttributeError):
            pass
        stage = "matchmaker_request" if code in {
            "matchmaker_timeout", "matchmaker_graph_timeout", "matchmaker_graph_unavailable", "matchmaker_provider_error",
        } else "matchmaker_response"
        raise MatchSearchPipelineError(code, stage) from None
    try:
        data = response.json()
    except ValueError:
        raise MatchSearchPipelineError("matchmaker_invalid_response", "matchmaker_response") from None
    if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
        raise MatchSearchPipelineError("matchmaker_invalid_response", "matchmaker_response")
    selected = data["matches"]
    if data.get("outcome") == "no_suitable_candidate" and not selected:
        return []
    # The model can only select from this batch, not a rejected/blocked person
    # or an unreviewed later batch. Public copy is generated after selection.
    allowed_ids = {candidate["user_id"] for candidate in payload["candidates"]}
    if (data.get("outcome") not in {None, "selected"} or len(selected) != 1
            or not isinstance(selected[0], dict)
            or not isinstance(selected[0].get("matched_user_id"), str)
            or selected[0]["matched_user_id"] not in allowed_ids):
        raise MatchSearchPipelineError("matchmaker_invalid_response", "matchmaker_response")
    return selected


def generate_matches_for_user(
    user_id: str, source: str = "manual", *,
    report_progress: Callable[[str], bool] | None = None,
    can_commit: Callable[[], bool] | None = None,
    search_job_id: str = "",
):
    """Run the existing matching pipeline for either a manual or proactive request."""
    req = MatchRequest(user_id=user_id)
    candidate_limit = agent_candidate_limit()
    total_start = time.perf_counter()
    print(f"[TIMING][V1 /api/match] start user={req.user_id} candidate_limit={candidate_limit}")

    step_start = time.perf_counter()
    def report(step: str, **extra) -> bool:
        if report_progress is not None:
            return bool(report_progress(step))
        _set_match_search(user_id, step, source, **extra)
        return True

    if not report("loading_profile"):
        return {"status": "stale", "matches": [], "debug_info": []}
    user_doc = profiles_coll.find_one({"user_id": req.user_id}, {"_id": 0})
    print(f"[TIMING][V1 /api/match] load user profile: {time.perf_counter() - step_start:.3f}s")
    if not user_doc:
         raise HTTPException(status_code=400, detail="User context not found.")

    if search_job_id:
        existing_job_match = matches_coll.find_one({
            "search_job_id": search_job_id,
            "from_user": req.user_id,
            "status": {"$in": list(LIVE_MATCH_STATUSES)},
        })
        if existing_job_match:
            return _existing_job_match_result(existing_job_match, req.user_id, user_doc)
         
    stored_context_value = user_doc.get("current_context")
    stored_context = str(stored_context_value or "")
    normalized_context = safe_recent_context(stored_context, "交朋友")
    user_doc["current_context"] = normalized_context
    user_embedding = user_doc.get("context_embedding", [])
    if not user_embedding or normalized_context != stored_context:
        step_start = time.perf_counter()
        user_embedding = get_embedding(normalized_context)
        # Embedding refresh is derived from the profile version loaded above.
        # Never let a slow search overwrite a newer profile/context update.
        profiles_coll.update_one(
            {"user_id": req.user_id, "current_context": stored_context_value},
            {"$set": {"context_embedding": user_embedding}},
        )
        print(f"[TIMING][V1 /api/match] create missing embedding: {time.perf_counter() - step_start:.3f}s")
    
    step_start = time.perf_counter()
    existing_matches = list(matches_coll.find({"$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]}))
    try:
        block_exclusions = risk_block_service.excluded_user_ids(req.user_id)
    except RiskBlockServiceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="安全關係狀態暫時無法確認，配對搜尋未執行",
        ) from exc
    excluded_users = {req.user_id, *block_exclusions}
    current_revision = int(user_doc.get("current_context_revision", 0))
    for m in existing_matches:
        status = m.get("status")
        age = time.time() - float(m.get("created_at", 0))
        should_exclude = status in {"draft", "pending"} or (
            status == "accepted" and has_verified_acceptance(m)
        )
        if status == "declined":
            should_exclude = age < 30 * 86400 or int(m.get("context_revision", 0)) == current_revision
        if should_exclude:
            excluded_users.add(m["from_user"])
            excluded_users.add(m["to_user"])
    print(f"[TIMING][V1 /api/match] load existing matches: {time.perf_counter() - step_start:.3f}s count={len(existing_matches)}")
    
    pipeline = [
        {
            "$vectorSearch": {
                "index": "vector_index",
                "path": "context_embedding",
                "queryVector": user_embedding,
                "numCandidates": 50,
                "limit": MATCH_CANDIDATE_POOL_SIZE
            }
        },
        {
            "$match": {
                "user_id": { "$nin": list(excluded_users) }
            }
        },
        {
            "$addFields": {
                "score": { "$meta": "vectorSearchScore" }
            }
        },
        {
            "$project": {
                "_id": 0
            }
        }
    ]
    
    try:
        if not report("vector_search"):
            return {"status": "stale", "matches": [], "debug_info": []}
        step_start = time.perf_counter()
        raw_candidates = list(profiles_coll.aggregate(pipeline))
        print(f"[TIMING][V1 /api/match] Mongo vector search: {time.perf_counter() - step_start:.3f}s raw_candidates={len(raw_candidates)}")
    except Exception:
        print(f"[TIMING][V1 /api/match] Mongo vector search failed after {time.perf_counter() - step_start:.3f}s")
        raise MatchSearchPipelineError("vector_search_unavailable", "vector_search")

    top_5_candidates = []
    seen_candidates = set(excluded_users)
    for c in raw_candidates:
        candidate_id = c.get("user_id")
        if not candidate_id or candidate_id in seen_candidates:
            continue
        seen_candidates.add(candidate_id)
        score = c.get("score", 0.0)
        top_5_candidates.append((score, c))
        if len(top_5_candidates) >= MATCH_CANDIDATE_POOL_SIZE:
            break
    
    if not top_5_candidates:
        return {"status": "no_suitable_candidate", "matches": [], "debug_info": []}

    # Only candidates with a reciprocal safety check and a strong owner-grounded
    # link are eligible for the LLM ranking step.  The model never receives weak
    # or conflicting candidates and therefore cannot "pick the least bad" one.
    clean_candidates = [c[1] if isinstance(c, tuple) else c for c in top_5_candidates]
    for candidate in clean_candidates:
        candidate["current_context"] = safe_recent_context(
            candidate.get("current_context"), ""
        )
    
    # 取得 target_user 的 deep_profile
    target_deep_profile = user_doc.get("deep_profile", {})
    
    # 取得每位 candidate 的 deep_profile
    step_start = time.perf_counter()
    for c in clean_candidates:
        c_doc = profiles_coll.find_one({"user_id": c.get("user_id")}, {"deep_profile": 1, "_id": 0})
        if c_doc and c_doc.get("deep_profile"):
            c["deep_profile"] = c_doc["deep_profile"]
    print(f"[TIMING][V1 /api/match] hydrate candidate deep_profile: {time.perf_counter() - step_start:.3f}s candidates={len(clean_candidates)}")

    vector_scores = {candidate.get("user_id"): score for score, candidate in top_5_candidates}
    target_stances = _trait_stances(user_doc.get("user_id"))
    if not report("candidate_qualification", candidate_count=len(clean_candidates)):
        return {"status": "stale", "matches": [], "debug_info": []}
    qualification_by_id = {}
    for candidate in clean_candidates:
        candidate_id = candidate.get("user_id")
        if not candidate_id:
            continue
        qualification_by_id[candidate_id] = candidate_qualification(
            user_doc,
            candidate,
            target_stances=target_stances,
            candidate_stances=_trait_stances(candidate_id),
            vector_score=vector_scores.get(candidate_id, 0),
        )
    qualified_candidates = [
        candidate for candidate in clean_candidates
        if qualification_by_id.get(candidate.get("user_id"), {}).get("eligible")
        and not reconcile_match_state(candidate["user_id"])
    ]
    if not qualified_candidates:
        return {
            "status": "no_suitable_candidate", "matches": [],
            "debug_info": [{
                "user_id": candidate.get("user_id"),
                "score": round(float(vector_scores.get(candidate.get("user_id"), 0) or 0) * 100, 2),
                "eligible": False,
                "reason_codes": qualification_by_id.get(candidate.get("user_id"), {}).get("strong_reason_codes", []),
            } for candidate in clean_candidates],
        }
    
    agent_user_doc = strip_agent_payload(user_doc)
    selection_deadline = time.monotonic() + MATCH_SELECTION_TIMEOUT_SECONDS
    agent_matches = []
    for batch_index in range(MATCH_MAX_CANDIDATE_BATCHES):
        batch = qualified_candidates[batch_index * candidate_limit:(batch_index + 1) * candidate_limit]
        if not batch:
            break
        if not report("matchmaker_request", candidate_count=len(batch), batch=batch_index + 1):
            return {"status": "stale", "matches": [], "debug_info": []}
        remaining = selection_deadline - time.monotonic()
        if remaining <= 0:
            raise MatchSearchPipelineError("matchmaker_timeout", "matchmaker_request")
        payload = {
            "target_user": agent_user_doc,
            "candidates": [strip_agent_payload(c) for c in batch],
            "target_deep_profile": target_deep_profile,
        }
        step_start = time.perf_counter()
        agent_matches = _request_matchmaker_selection(payload, timeout=remaining)
        print(f"[match-selection] batch={batch_index + 1} candidates={len(batch)} "
              f"selected={len(agent_matches)} elapsed={time.perf_counter() - step_start:.3f}s")
        if agent_matches:
            break
    if not report("matchmaker_response"):
        return {"status": "stale", "matches": [], "debug_info": []}
    if not agent_matches:
        return {"status": "no_suitable_candidate", "matches": [], "debug_info": []}
    
    # 阿月一次只牽一條線，避免同時丟出候選人清單。
    if not report("proposal_write"):
        return {"status": "stale", "matches": [], "debug_info": []}
    step_start = time.perf_counter()
    result_matches = []
    for m in agent_matches[:1]:
        if can_commit is not None and not can_commit():
            return {"status": "stale", "matches": [], "debug_info": []}
        matched_id = m.get("matched_user_id")
        
        if not matched_id or not qualification_by_id.get(matched_id, {}).get("eligible"):
            continue
        if reconcile_match_state(matched_id):
            continue

        candidate_doc = next(
            (candidate for candidate in qualified_candidates if candidate.get("user_id") == matched_id),
            profiles_coll.find_one({"user_id": matched_id}, {"_id": 0}) or {},
        )
        contrast_label = _short_text((candidate_doc.get("big_five") or {}).get("summary"), 16)
        distinctive_tags = validated_distinctive_tags(candidate_doc)
        score_breakdown, reason_items, top_reasons, reason = build_validated_match_explanation(
            user_doc, candidate_doc, vector_scores.get(matched_id, 0)
        )
        receiver_breakdown, receiver_items, _, receiver_reason = build_validated_match_explanation(
            candidate_doc, user_doc, vector_scores.get(matched_id, 0)
        )
        # V4 persists two role-bound friend introductions.  They are created
        # with two separate model calls and never exposed as a two-way public
        # payload, so a role swap cannot leak into the recipient's card.
        friend_intro_v4 = build_friend_intro_v4(
            user_doc, candidate_doc, vector_scores.get(matched_id, 0), refine=True,
        )
        ai_recommendation_reason = str(
            (friend_intro_v4.get("initiator_preview") or {}).get("viewer_text") or reason
        )
        ai_receiver_reason = str(
            (friend_intro_v4.get("receiver_invitation") or {}).get("viewer_text") or receiver_reason
        )
        recommendation_tier = next(
            (item.get("text") for item in reason_items if item.get("kind") == "recommendation_tier"),
            "exploratory",
        )
        
        match_doc = {
            "from_user": req.user_id,
            "to_user": matched_id,
            "live_participants": [req.user_id, matched_id],
            "reason": ai_recommendation_reason,
            "receiver_reason": ai_receiver_reason,
            "reason_version": V4_REASON_VERSION,
            "proposal_namespace": RELATIONSHIP_MATCH_NAMESPACE,
            "reason_copy_version": FRIEND_COPY_VERSION,
            "friend_intro_v4": friend_intro_v4,
            "contrast_label": contrast_label,
            "distinctive_tags": distinctive_tags,
            "score_breakdown": score_breakdown,
            "top_reasons": top_reasons,
            "reason_items": reason_items,
            "recommendation_tier": recommendation_tier,
            "receiver_reason_items": receiver_items,
            "receiver_score_breakdown": receiver_breakdown,
            "match_context_snapshot": {
                "target": {
                    "user_id": req.user_id,
                    "current_context": user_doc.get("current_context"),
                    "public_personality": _public_personality_phrase(user_doc),
                    "context_revision": int(user_doc.get("current_context_revision", 0)),
                    "context_signals": user_doc.get("context_signals", {}),
                },
                "candidate": {
                    "user_id": matched_id,
                    "current_context": candidate_doc.get("current_context"),
                    "public_personality": _public_personality_phrase(candidate_doc),
                    "context_revision": int(candidate_doc.get("current_context_revision", 0)),
                    "context_signals": candidate_doc.get("context_signals", {}),
                },
            },
            "status": "draft",
            "delivery_channel": "mediator_chat",
            "proposal_source": str(source or "manual")[:40],
            "participant_pair_key": participant_pair_key(req.user_id, matched_id),
            "relationship_establishing": True,
            "context_revision": int(user_doc.get("current_context_revision", 0)),
            "proposal_revision": 0,
            "created_at": time.time(),
            "state_history": [{"from": None, "to": "draft", "actor": req.user_id, "action": "created", "at": time.time()}]
        }
        if search_job_id:
            match_doc["search_job_id"] = search_job_id
        if can_commit is not None and not can_commit():
            return {"status": "stale", "matches": [], "debug_info": []}
        try:
            insert_result = matches_coll.insert_one(match_doc)
        except DuplicateKeyError:
            # Another worker/user established a live proposal after our final
            # read. The unique participant index is the authoritative guard.
            return {"status": "stale", "matches": [], "debug_info": []}
        
        # 查詢候選人的 profile 供前端渲染
        to_doc = profiles_coll.find_one({"user_id": matched_id}, {"_id": 0})
        
        result_matches.append({
            "match_id": str(insert_result.inserted_id),
            "matched_user_id": matched_id,
            "contrast_label": contrast_label,
            "distinctive_tags": distinctive_tags,
            "score_breakdown": score_breakdown,
            "top_reasons": top_reasons,
            "reason_items": reason_items,
            "recommendation_reason": ai_recommendation_reason,
            "viewer_reason": ai_recommendation_reason,
            "reason_version": V4_REASON_VERSION,
            "big_five": to_doc.get("big_five", {}) if to_doc else {},
            "current_context": to_doc.get("current_context", "") if to_doc else "",
            "target_context": user_doc.get("current_context", ""),
        })
        print(f"  ✅ 建立 draft 配對: {req.user_id} → {matched_id} [{contrast_label}]")
    
    print(f"[TIMING][V1 /api/match] persist draft matches and load result profiles: {time.perf_counter() - step_start:.3f}s result_matches={len(result_matches)}")

    debug_candidates = []
    for score, doc in top_5_candidates:
        debug_candidates.append({
            "user_id": doc.get("user_id"),
            "score": round(score * 100, 2),
            "context": doc.get("current_context"),
            "big_five_summary": doc.get("big_five", {}).get("summary", "")
        })
    
    print(f"[TIMING][V1 /api/match] total: {time.perf_counter() - total_start:.3f}s user={req.user_id}")
    return {
        "status": "success" if result_matches else "no_suitable_candidate",
        "matches": result_matches,
        "debug_info": debug_candidates
    }

def _queue_match_event(user_id: str, event_type: str, message: str, **extra):
    return queue_mediator_event(user_id, message, event_type, **extra)


def create_proactive_match_proposal(user_id: str, source: str = "automatic", force_new: bool = False):
    """Retired compatibility facade; matching now requires an explicit action."""
    return {"status": "disabled", "reason_code": "explicit_match_action_required"}


# Candidate ranking is still this module's legacy implementation. The durable
# job service owns when it runs, leases, cancellation and public status.
register_match_search_pipeline(generate_matches_for_user)

@router.post("/request")
def request_next_match(req: MatchRequest, background_tasks: BackgroundTasks):
    active = reconcile_match_state(req.user_id)
    if active:
        stage = derive_match_stage(active, req.user_id)
        other_id = active["to_user"] if active["from_user"] == req.user_id else active["from_user"]
        _set_match_search(
            req.user_id, stage, req.source, match_id=str(active["_id"]), other_id=other_id
        )
        return {"status": "already_active", "stage": stage}
    if not req.confirmed:
        _set_match_search(req.user_id, "awaiting_confirmation", req.source)
        return {
            "status": "awaiting_confirmation",
            "message": "要我現在幫你翻翻名單嗎？你點開始，我才會真的去找。",
        }
    return start_match_search(
        req.user_id, source=req.source, force_new=req.force_new,
        idempotency_key=f"match-request:{req.user_id}:{uuid.uuid4().hex}",
    )


@router.post("/cancel")
def cancel_match_request(req: MatchRequest):
    return cancel_match_search(req.user_id, source=req.source)

@router.get("/status")
def get_match_status(user_id: str):
    relationship_active = reconcile_match_state(user_id)
    event_active = _single_live_namespace_proposal(
        user_id, EVENT_INVITATION_NAMESPACE,
    )
    relationship_card = (
        build_status_proposal_card(relationship_active, user_id)
        if relationship_active else None
    )
    event_card = (
        build_status_proposal_card(event_active, user_id)
        if event_active else None
    )
    search = public_match_search_status(user_id)
    snapshot = get_match_status_snapshot(user_id)
    public_snapshot = {
        key: snapshot.get(key)
        for key in ("state", "scope", "is_terminal", "chat_opened", "counterparty", "reason_code")
    }
    return {
        "search": search,
        "match_search": search,
        "active_proposal_card": relationship_card,
        "active_proposals": {
            RELATIONSHIP_MATCH_NAMESPACE: relationship_card,
            EVENT_INVITATION_NAMESPACE: event_card,
        },
        "status_snapshot": public_snapshot,
        "search_reason_code": search.get("reason_code") or None,
    }

def _accepted_chat_target(match_doc: dict | None, user_id: str) -> dict:
    """Expose navigation identity only to participants after mutual consent."""
    if not match_doc or match_doc.get("status") != "accepted":
        return {}
    first, second = match_doc.get("from_user"), match_doc.get("to_user")
    if user_id not in {first, second}:
        return {}
    # Old imports can contain bare terminal rows without mutual consent.
    # Share the same evidence policy as the established-contact read model.
    try:
        if not has_verified_acceptance(match_doc):
            return {}
    except (AttributeError, TypeError):
        return {}
    other_id = second if user_id == first else first
    if not isinstance(other_id, str) or not other_id.strip() or other_id == user_id:
        return {}
    return {"other_id": other_id}


@router.get("/state")
def get_single_match_state(user_id: str, match_id: str):
    try:
        match_doc = matches_coll.find_one({"_id": ObjectId(match_id)})
    except Exception:
        match_doc = None
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")
    if user_id not in {match_doc.get("from_user"), match_doc.get("to_user")}:
        raise HTTPException(status_code=403, detail="Only a match participant may read this state")
    proposal_namespace = namespace_for_document(match_doc)
    response = {
        "match_id": match_id,
        **proposal_card_state(match_doc, user_id),
        "proposal_namespace": proposal_namespace,
        "chat_reused": bool(
            match_doc.get("status") == "accepted"
            and proposal_namespace == EVENT_INVITATION_NAMESPACE
            and match_doc.get("relationship_establishing") is False
        ),
        **_accepted_chat_target(match_doc, user_id),
    }
    # While a proposal is still actionable, hydrate its saved chat card from
    # the canonical viewer-bound projection.  This lets old cards receive copy
    # compatibility fixes without editing message history.
    active_card = build_active_proposal_card(match_doc, user_id)
    if active_card:
        response["viewer_reason"] = active_card.get("viewer_reason", "")
        response["decline_reason_options"] = active_card.get("decline_reason_options", [])
        if active_card.get("event"):
            response["event"] = active_card["event"]
    response["counterparty_nickname"] = proposal_counterparty_nickname(
        match_doc, user_id,
        lambda uid: proposal_display_name(uid, fallback_lookup=public_display_name),
    )
    return response

@router.post("")
def match_endpoint(req: MatchRequest):
    return start_match_search(
        req.user_id, source=req.source, force_new=req.force_new,
        idempotency_key=f"match-endpoint:{req.user_id}:{uuid.uuid4().hex}",
    )

def _apply_match_decision(req: MatchDecisionRequest, background_tasks: BackgroundTasks):
    result = decide_match_action(
        user_id=req.user_id, match_id=req.match_id, action=req.action,
        expected_status=req.expected_status, expected_revision=req.expected_revision,
        expected_namespace=req.proposal_namespace,
        explicit_reasons=req.explicit_reasons,
        schedule_task=background_tasks.add_task,
    )
    if result.get("stale"):
        raise HTTPException(status_code=409, detail={
            "message": "配對狀態已變更",
            "current_status": result.get("current_status"),
            "current_revision": result.get("current_revision"),
            "current_namespace": result.get("current_namespace"),
        })
    response = dict(result)
    # Navigation is an HTTP-only projection, not a new tool observation or
    # another state transition. Never trust a client-supplied counterparty ID.
    response.pop("other_id", None)
    response.pop("other_name", None)
    if result.get("status") == "success" and result.get("new_status") == "accepted":
        try:
            accepted = matches_coll.find_one({
                "_id": ObjectId(req.match_id),
                "status": "accepted",
                "$or": [{"from_user": req.user_id}, {"to_user": req.user_id}],
            })
        except (InvalidId, PyMongoError) as exc:
            # Consent is already committed. A failed navigation read must not
            # make the successful decision look like a failed write.
            print(f"[match] accepted navigation unavailable: {type(exc).__name__}")
            accepted = None
        response.update(_accepted_chat_target(accepted, req.user_id))
    return response


def decide_active_proposal_for_agent(user_id: str, decision: str, revision: int, idempotency_key: str) -> dict:
    """Agent facade: server injects current proposal identity/status, never the model."""
    return decide_active_proposal_action(
        user_id=user_id,
        decision=decision,
        expected_revision=revision,
        idempotency_key=idempotency_key,
    )


@router.post("/decision")
def decide_match(req: MatchDecisionRequest, background_tasks: BackgroundTasks):
    return _apply_match_decision(req, background_tasks)


@router.post("/proactive_event")
@router.post("/proactive-event", include_in_schema=False)
def proactive_event_match(req: ProactiveEventRequest):
    try:
        return create_event_opportunity(req.user_id)
    except HTTPException:
        raise
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=503,
            detail=f"主動活動媒合 Agent (port 9001) 無法連線: {type(exc).__name__}",
        ) from exc
    except Exception as exc:
        print(f"[event-opportunity] create failed: {type(exc).__name__}")
        raise HTTPException(status_code=503, detail="主動活動媒合暫時無法建立") from exc


@router.post("/events/discover")
def discover_public_events(req: EventDiscoveryRequest):
    """Queue manual discovery; only the Event worker executes the long pipeline."""
    try:
        return enqueue_event_discovery_job(
            region=req.region, window_days=req.window_days,
            categories=req.categories or [], source="api", job_kind="discovery",
        )
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail={
            "code": "event_queue_unavailable",
            "message": "活動搜尋暫時無法排入佇列，請稍後再試。",
        }) from exc


@router.get("/events/discover/status")
def get_public_event_discovery_status():
    """Read the same bounded, secret-free singleton snapshot as the Demo UI."""
    snapshot = event_discovery_job_snapshot()
    if snapshot.get("state") == "unavailable":
        raise HTTPException(status_code=503, detail={
            "code": "event_queue_unavailable",
            "message": "暫時無法讀取活動搜尋進度，請稍後再試。",
        })
    return {"status": "success", **snapshot}


@router.post("/events/relevance/rebuild")
def rebuild_public_event_relevance():
    """Manual demo backfill; internal event/user identifiers are never returned."""
    result = rebuild_all_event_relevance(limit=20)
    if result.get("status") == "error":
        raise HTTPException(status_code=503, detail=result)
    return {
        "status": "success",
        "event_count": int(result.get("event_count", 0) or 0),
        "relevance_count": int(result.get("relevance_count", 0) or 0),
        "avoidance_count": int(result.get("avoidance_count", 0) or 0),
        "link_count": int(result.get("link_count", 0) or 0),
        "embedded_count": int(result.get("embedded_count", 0) or 0),
        "pending_count": int(result.get("pending_count", 0) or 0),
        "deferred": result.get("status") == "deferred",
        "retry_after": result.get("retry_after"),
    }


@router.post("/events/opportunities/scan")
def scan_public_event_opportunities(req: EventOpportunityScanRequest):
    """Run the bounded opportunity scan used after discovery."""
    return scan_event_opportunities(max_proposals=req.max_proposals)


@router.post("/events/lifecycle/run")
def run_public_event_lifecycle():
    """Run bounded Event and unresolved-proposal cleanup."""
    return run_event_lifecycle_once()


@router.post("/accept")
def accept_match(req: AcceptRequest, background_tasks: BackgroundTasks):
    match_doc = matches_coll.find_one({"_id": ObjectId(req.match_id)})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")
    return _apply_match_decision(MatchDecisionRequest(
        **req.model_dump(), action="accept", expected_status=match_doc.get("status", ""),
        proposal_namespace=namespace_for_document(match_doc),
    ), background_tasks)


@router.post("/decline")
def decline_match(req: AcceptRequest, background_tasks: BackgroundTasks):
    match_doc = matches_coll.find_one({"_id": ObjectId(req.match_id)})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")
    action = "cancel" if match_doc.get("status") == "pending" and match_doc.get("from_user") == req.user_id else "decline"
    return _apply_match_decision(MatchDecisionRequest(
        **req.model_dump(), action=action, expected_status=match_doc.get("status", ""),
        proposal_namespace=namespace_for_document(match_doc),
    ), background_tasks)
