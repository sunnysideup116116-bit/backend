import time
import requests
import json
import re
import os
import uuid
from fastapi import APIRouter, HTTPException, BackgroundTasks
from models import MatchRequest, AcceptRequest, MatchDecisionRequest
from database import profiles_coll, matches_coll
from services.ai_service import get_embedding, generate_chat_completion
from services.memory_service import get_user_graph_memories
from services.mediator_event_service import queue_mediator_event
from services.match_state_service import get_match_status_snapshot, has_verified_acceptance
from services.match_action_service import (
    decide_active_proposal as decide_active_proposal_action,
    decide_match as decide_match_action,
    register_match_search_executor,
    start_match_search,
)
from services.profile_projection import safe_recent_context
from services.ayue_agent.public_relationship_projection import display_name as public_display_name
from bson.objectid import ObjectId
from pymongo import ReturnDocument

router = APIRouter(prefix="/api/match", tags=["Match"])
DRAFT_TTL_SECONDS = 24 * 3600
PENDING_TTL_SECONDS = 72 * 3600
SEARCH_LOCK_TTL_SECONDS = 5 * 60
LIVE_MATCH_STATUSES = {"draft", "pending"}


def ensure_match_indexes():
    """Keep one unresolved proposal per participant at the database boundary."""
    try:
        matches_coll.create_index(
            [("live_participants", 1)],
            unique=True,
            partialFilterExpression={"status": {"$in": ["draft", "pending"]}},
            name="one_live_match_per_participant",
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
    """Expire stale proposals; matches are the canonical live-match state."""
    now = time.time()
    for status, ttl in (("draft", DRAFT_TTL_SECONDS), ("pending", PENDING_TTL_SECONDS)):
        matches_coll.update_many(
            {"status": status, "created_at": {"$lt": now - ttl}, "live_participants": user_id},
            {"$set": {"status": "expired", "expired_at": now, "expired_reason": f"{status}_timeout"},
             "$unset": {"live_participants": ""}},
        )
    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"matchmaking_in_progress": 1, "matchmaking_started_at": 1},
    ) or {}
    if profile.get("matchmaking_in_progress") and float(profile.get("matchmaking_started_at", 0)) < now - SEARCH_LOCK_TTL_SECONDS:
        profiles_coll.update_one({"user_id": user_id}, {
            "$set": {"matchmaking_in_progress": False}, "$unset": {"matchmaking_started_at": ""}
        })
    return matches_coll.find_one(
        {"status": {"$in": list(LIVE_MATCH_STATUSES)}, "$or": [{"live_participants": user_id}, {"live_participants": {"$exists": False}, "$or": [{"from_user": user_id}, {"to_user": user_id}]}]},
        sort=[("created_at", -1)],
    )

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
    initiator_id = match_doc.get("from_user")
    receiver_id = match_doc.get("to_user")
    viewer_reason = reason_for_viewer(match_doc, user_id)
    snapshot = match_doc.get("match_context_snapshot", {}) or {}
    own_snapshot = snapshot.get("target" if is_initiator else "candidate", {}) or {}
    other_snapshot = snapshot.get("candidate" if is_initiator else "target", {}) or {}
    reason_items = (
        match_doc.get("reason_items", [])
        if is_initiator else match_doc.get("receiver_reason_items", [])
    )
    reasons = [
        item.get("text")
        for item in reason_items
        if item.get("kind") in {"shared_graph", "shared_context", "shared_value"} and item.get("text")
    ][:2]
    if not reasons:
        reasons = [reason_for_viewer(match_doc, user_id) or "目前沒有明確共同點，適合先從公開近況聊起。"]
    score = (
        match_doc.get("score_breakdown", {})
        if is_initiator else match_doc.get("receiver_score_breakdown", {})
    )
    return {
        "match_id": str(match_doc["_id"]),
        "proposal_revision": int(match_doc.get("proposal_revision", 0)),
        "other_id": other_id,
        "other_label": public_display_name(other_id),
        "stage": stage,
        "event_type": "match_proposal" if is_initiator else "incoming_match_interest",
        "proposal_role": "initiator" if is_initiator else "receiver",
        "opening": (
            f"欸，我一看到 {public_display_name(other_id)} 就想到你。"
            if is_initiator else f"欸，{public_display_name(other_id)} 想認識你，我先來問你本人。"
        ),
        "your_context": own_snapshot.get("current_context") or "尚無近期情境",
        "other_context": other_snapshot.get("current_context") or "尚無近期情境",
        "reasons": reasons,
        "score": round(float(score.get("total", 0) or 0)),
        # Keep these two fields permanently tied to the match roles.  UI
        # callers must render viewer_reason instead of reinterpreting roles.
        "recommendation_reason": reason_for_viewer(match_doc, initiator_id),
        "receiver_reason": reason_for_viewer(match_doc, receiver_id),
        "viewer_reason": viewer_reason,
    }
def _short_text(value, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
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
    """A compact, non-clinical descriptor grounded only in public Big Five values."""
    traits = profile.get("big_five") or {}
    options = []
    value = traits.get("E")
    if isinstance(value, (int, float)):
        options.append("比較外向、容易帶起話題" if value >= 6 else "偏安靜、重視舒服節奏" if value <= 4 else "互動節奏自然")
    value = traits.get("O")
    if isinstance(value, (int, float)) and value >= 7:
        options.append("願意嘗試新點子")
    value = traits.get("A")
    if isinstance(value, (int, float)) and value >= 7:
        options.append("願意傾聽")
    summary = _short_text(traits.get("summary"), 32)
    return options[0] if options else summary


def _directional_reason_fallback(viewer: dict, other: dict, tier: str) -> dict:
    """Viewer-specific, privacy-safe explanation when LLM wording is unavailable."""
    other_context = _short_text(other.get("current_context"), 56)
    viewer_context = _short_text(viewer.get("current_context"), 56)
    viewer_trait = _public_personality_phrase(viewer)
    other_trait = _public_personality_phrase(other)
    lines = []
    if other_context:
        lines.append(f"對方最近提到「{other_context}」。")
    if viewer_trait and other_trait:
        lines.append(f"你{viewer_trait}，對方{other_trait}；如果聊起這件事，或許能形成舒服的互補。")
    elif viewer_trait and other_context:
        lines.append(f"你{viewer_trait}，或許能讓這個話題更容易延伸。")
    elif viewer_context and other_context:
        lines.append("你們最近在意的事情不同，可以先交換各自想做的事，看看節奏合不合。")
    elif tier == "grounded":
        lines.append("你們有已確認的共同點，可以先從那裡自然聊起。")
    else:
        lines.append("這次是依整體情境與個性推薦，還沒有已確認的共同興趣。")
    text = "".join(lines[:2])[:110]
    starter = f"可以先問他最近那件「{other_context}」最吸引他的地方。" if other_context else "可以先從最近想做的事聊起。"
    return {
        "tier": tier,
        "viewer_text": text,
        "scenario_bridge": other_context,
        "personality_dynamic": "互補" if viewer_trait and other_trait else "",
        "conversation_starter": starter,
        "used_evidence_keys": [key for key, value in (
            ("other.current_context", other_context),
            ("viewer.big_five", viewer_trait),
            ("other.big_five", other_trait),
        ) if value],
    }


def _valid_directional_reason(value: object, fallback: dict) -> dict:
    """Allow only short, person-first hypotheses; fall back on any provider drift."""
    if not isinstance(value, dict):
        return fallback
    text = _short_text(value.get("viewer_text"), 110)
    starter = _short_text(value.get("conversation_starter"), 72)
    banned = ("seed_user", "user_id", "mongo", "資料庫", "物件", "已答應", "已同意")
    if not text or any(token.lower() in text.lower() for token in banned):
        return fallback
    if text.count("。") > 2:
        return fallback
    return {**fallback, "viewer_text": text, "conversation_starter": starter or fallback["conversation_starter"]}


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
        raw = json.loads(generate_chat_completion(prompt, temperature=0.35, json_output=True))
        return _valid_directional_reason(raw.get("target"), target_fallback), _valid_directional_reason(raw.get("candidate"), candidate_fallback)
    except Exception:
        return target_fallback, candidate_fallback


def _role_bound_reason_entries(target: dict, candidate: dict, tier: str) -> list[dict]:
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
        text = str(fallback.get("viewer_text") or "")
        if not text or "seed_user" in text.lower() or "物件" in text:
            continue
        entries.append({
            "viewer_id": viewer_id,
            "counterparty_id": other_id,
            "counterparty_context_snapshot": _short_text(other.get("current_context"), 56),
            "viewer_text": text,
            "interaction_hypothesis": _short_text(text.split("。", 1)[-1], 76),
            "conversation_starter": _short_text(fallback.get("conversation_starter"), 72),
            "used_evidence_keys": list(fallback.get("used_evidence_keys") or []),
            "tier": tier,
        })
    return entries


def build_directional_reason_v3(target: dict, candidate: dict, vector_score: float) -> list[dict]:
    _, items, _, _ = build_validated_match_explanation(target, candidate, vector_score)
    tier = next((item.get("text") for item in items if item.get("kind") == "recommendation_tier"), "exploratory")
    return _role_bound_reason_entries(target, candidate, tier)


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


def reason_for_viewer(match_doc: dict, user_id: str) -> str:
    """One canonical viewer projection for cards, status and mediator replies."""
    from_user, to_user = str(match_doc.get("from_user") or ""), str(match_doc.get("to_user") or "")
    expected_other = to_user if user_id == from_user else from_user if user_id == to_user else ""
    v3 = match_doc.get("directional_reason_v3") or (
        build_directional_reason_v3_from_snapshot(match_doc)
        if match_doc.get("status") in {"draft", "pending"} else []
    )
    if isinstance(v3, list) and expected_other:
        matching = [
            item for item in v3 if isinstance(item, dict)
            and item.get("viewer_id") == user_id
            and item.get("counterparty_id") == expected_other
        ]
        if len(matching) == 1:
            text = _short_text(matching[0].get("viewer_text"), 110)
            if text and not any(token in text.lower() for token in ("seed_user", "user_id", "mongo", "資料庫", "物件")):
                return text
    directional = match_doc.get("directional_reason_v2") or {}
    key = "target" if match_doc.get("from_user") == user_id else "candidate"
    candidate = directional.get(key) if isinstance(directional, dict) else None
    if isinstance(candidate, dict) and candidate.get("viewer_text"):
        return str(candidate["viewer_text"])
    return str(match_doc.get("reason") if key == "target" else match_doc.get("receiver_reason") or match_doc.get("reason") or "")

def generate_matches_for_user(user_id: str, source: str = "manual"):
    """Run the existing matching pipeline for either a manual or proactive request."""
    req = MatchRequest(user_id=user_id)
    candidate_limit = agent_candidate_limit()
    total_start = time.perf_counter()
    print(f"[TIMING][V1 /api/match] start user={req.user_id} candidate_limit={candidate_limit}")

    step_start = time.perf_counter()
    _set_match_search(user_id, "loading_profile", source)
    user_doc = profiles_coll.find_one({"user_id": req.user_id}, {"_id": 0})
    print(f"[TIMING][V1 /api/match] load user profile: {time.perf_counter() - step_start:.3f}s")
    if not user_doc:
         raise HTTPException(status_code=400, detail="User context not found.")
         
    stored_context = str(user_doc.get("current_context") or "")
    normalized_context = safe_recent_context(stored_context, "交朋友")
    user_doc["current_context"] = normalized_context
    user_embedding = user_doc.get("context_embedding", [])
    if not user_embedding or normalized_context != stored_context:
        step_start = time.perf_counter()
        user_embedding = get_embedding(normalized_context)
        profiles_coll.update_one(
            {"user_id": req.user_id},
            {"$set": {"context_embedding": user_embedding, "current_context": normalized_context}},
        )
        print(f"[TIMING][V1 /api/match] create missing embedding: {time.perf_counter() - step_start:.3f}s")
    
    step_start = time.perf_counter()
    existing_matches = list(matches_coll.find({"$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]}))
    excluded_users = {req.user_id}
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
                "limit": 20
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
            "$limit": candidate_limit
        },
        {
            "$project": {
                "_id": 0
            }
        }
    ]
    
    try:
        _set_match_search(user_id, "vector_search", source)
        step_start = time.perf_counter()
        raw_candidates = list(profiles_coll.aggregate(pipeline))
        print(f"[TIMING][V1 /api/match] Mongo vector search: {time.perf_counter() - step_start:.3f}s raw_candidates={len(raw_candidates)}")
    except Exception as e:
        print(f"[TIMING][V1 /api/match] Mongo vector search failed after {time.perf_counter() - step_start:.3f}s")
        print(f"Vector search failed: {e}")
        raise HTTPException(status_code=500, detail="Vector search failed. 請確認已在 MongoDB Atlas 建立 vector_index 且具備 context_embedding 欄位。")

    top_5_candidates = []
    for c in raw_candidates:
        score = c.get("score", 0.0)
        top_5_candidates.append((score, c))
    
    if not top_5_candidates:
        raise HTTPException(status_code=404, detail="Not enough candidates.")

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
    agent_candidates = [strip_agent_payload(c) for c in qualified_candidates]
    payload = {
        "target_user": agent_user_doc,
        "candidates": agent_candidates,
        "target_deep_profile": target_deep_profile
    }
    try:
        original_payload_chars = len(json.dumps({
            "target_user": user_doc,
            "candidates": qualified_candidates,
            "target_deep_profile": target_deep_profile
        }, ensure_ascii=False, default=str))
        stripped_payload_chars = len(json.dumps(payload, ensure_ascii=False, default=str))
        print(
            "[TIMING][V1 /api/match] Agent payload stripped "
            f"context_embedding original_chars={original_payload_chars} "
            f"stripped_chars={stripped_payload_chars} "
            f"saved_chars={original_payload_chars - stripped_payload_chars}"
        )
    except Exception as e:
        print(f"[TIMING][V1 /api/match] Agent payload size logging failed: {e}")
    
    try:
        _set_match_search(user_id, "graph_check", source, candidate_count=len(qualified_candidates))
        print("📞 正在打電話給 9001 港口的媒婆 Agent...")
        step_start = time.perf_counter()
        agent_resp = requests.post("http://127.0.0.1:9001/api/match", json=payload, timeout=120)
        print(f"[TIMING][V1 /api/match] 9001 Agent HTTP roundtrip: {time.perf_counter() - step_start:.3f}s status={agent_resp.status_code}")
        agent_resp.raise_for_status()
        
        step_start = time.perf_counter()
        agent_data = agent_resp.json()
        print(f"[TIMING][V1 /api/match] parse Agent response JSON: {time.perf_counter() - step_start:.3f}s")
        # 🥚 雙黃蛋：解析 matches 陣列
        if agent_data.get("outcome") == "no_suitable_candidate":
            return {"status": "no_suitable_candidate", "matches": [], "debug_info": []}
        agent_matches = agent_data.get("matches", [])
        if not agent_matches:
            return {"status": "no_suitable_candidate", "matches": [], "debug_info": []}
        print(f"✅ Agent 回應: {len(agent_matches)} 位候選人")
    except requests.RequestException as e:
        print(f"❌ 無法連線到 9001 Agent: {e}")
        raise HTTPException(status_code=503, detail=f"配對 Agent (port 9001) 無法連線: {e}")
    
    # 阿月一次只牽一條線，避免同時丟出候選人清單。
    _set_match_search(user_id, "writing_reason", source)
    step_start = time.perf_counter()
    result_matches = []
    for m in agent_matches[:1]:
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
        # V3 binds each explanation to the actual viewer/counterparty IDs.
        # Keep V2 fields as a compatibility projection, but do not ask an LLM
        # to generate two role-labelled paragraphs in one response.
        directional_target, directional_candidate = build_directional_match_explanations(
            user_doc, candidate_doc, vector_scores.get(matched_id, 0), refine=False,
        )
        directional_v3 = build_directional_reason_v3(
            user_doc, candidate_doc, vector_scores.get(matched_id, 0),
        )
        
        # Persist only explanations assembled from owner-bound evidence.  The
        # ranking model may select an eligible candidate, but its free-form
        # prose is never a trusted source of facts shown to either person.
        by_viewer = {item.get("viewer_id"): item for item in directional_v3 if isinstance(item, dict)}
        ai_recommendation_reason = str((by_viewer.get(req.user_id) or {}).get("viewer_text") or directional_target["viewer_text"] or reason)
        ai_receiver_reason = str((by_viewer.get(matched_id) or {}).get("viewer_text") or directional_candidate["viewer_text"] or receiver_reason)
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
            "reason_version": "v3",
            "directional_reason_v2": {
                "target": directional_target,
                "candidate": directional_candidate,
            },
            "directional_reason_v3": directional_v3,
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
                    "context_revision": int(user_doc.get("current_context_revision", 0)),
                    "context_signals": user_doc.get("context_signals", {}),
                },
                "candidate": {
                    "user_id": matched_id,
                    "current_context": candidate_doc.get("current_context"),
                    "context_revision": int(candidate_doc.get("current_context_revision", 0)),
                    "context_signals": candidate_doc.get("context_signals", {}),
                },
            },
            "status": "draft",
            "delivery_channel": "mediator_chat",
            "context_revision": int(user_doc.get("current_context_revision", 0)),
            "proposal_revision": 0,
            "created_at": time.time(),
            "state_history": [{"from": None, "to": "draft", "actor": req.user_id, "action": "created", "at": time.time()}]
        }
        insert_result = matches_coll.insert_one(match_doc)
        
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
            "receiver_reason": ai_receiver_reason,
            "reason_version": "v3",
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
    """Create one proposal and always surface success or failure to Ayue's chat."""
    now = time.time()
    request_id = uuid.uuid4().hex
    active = reconcile_match_state(user_id)
    if active:
        stage = derive_match_stage(active, user_id)
        _set_match_search(
            user_id, stage,
            source, match_id=str(active["_id"]),
            other_id=active["to_user"] if active["from_user"] == user_id else active["from_user"],
        )
        _queue_match_event(user_id, "match_search_blocked", "手上這條線還沒確認完，先把它處理好，我再幫你看下一位。")
        return {"status": "already_active"}
    claimed = profiles_coll.find_one_and_update(
        {"user_id": user_id,
         "$or": [{"matchmaking_in_progress": {"$ne": True}}, {"matchmaking_started_at": {"$lt": now - 300}}],
         "user_id": user_id},
        {"$set": {"matchmaking_in_progress": True, "matchmaking_started_at": now,
                  "matchmaking_request_id": request_id,
                  "match_search": {"status": "searching", "source": source, "started_at": now,
                                   "request_id": request_id}}},
        return_document=True
    )
    if not claimed:
        return {"status": "already_searching"}
    request_context_revision = int((claimed or {}).get("current_context_revision", 0))
    profiles_coll.update_one(
        {"user_id": user_id, "match_search.request_id": request_id},
        {"$set": {"match_search.context_revision": request_context_revision}},
    )
    try:
        result = generate_matches_for_user(user_id, source)
        suggestions = result.get("matches", [])[:1]
        if not suggestions:
            raise HTTPException(status_code=404, detail="目前沒有新的候選人")
        first = suggestions[0]
        candidate_id = first.get("matched_user_id", "這個人")
        first_match_id = first.get("match_id")
        tone = (profiles_coll.find_one({"user_id": user_id}, {"mediator_tone": 1}) or {}).get("mediator_tone", "friend")
        proposal_message = {
            "friend": "我翻到一位可以介紹給你的人，先看這張阿月牽線提案。",
            "gentle": "我幫你留意到一位可能合拍的人，先看看這個提案。",
            "enthusiastic": "我找到一位有機會聊起來的人，快看看這張牽線提案！",
        }.get(tone, "我翻到一位可以介紹給你的人，先看這張阿月牽線提案。")
        current = profiles_coll.find_one(
            {"user_id": user_id}, {"current_context_revision": 1, "matchmaking_request_id": 1},
        ) or {}
        current_revision = int(current.get("current_context_revision", 0))
        is_stale = (
            current.get("matchmaking_request_id") != request_id
            or current_revision != request_context_revision
        )
        if is_stale:
            profiles_coll.update_one(
                {"user_id": user_id, "matchmaking_request_id": request_id},
                {"$set": {"match_search.status": "stale", "match_search.completed_at": time.time()}},
            )
            return {"status": "stale"}
        profiles_coll.update_one({"user_id": user_id}, {"$set": {
            "last_auto_match_revision": current_revision,
            "match_search": {"status": "idle", "source": source, "completed_at": time.time()}}})
        queue_mediator_event(
            user_id, proposal_message, "match_proposal",
            matches=suggestions, match_id=first_match_id, other_id=candidate_id,
            proposal_role="initiator", debug_info=result.get("debug_info", []),
        )
        return {"status": "queued", "other_id": candidate_id}
    except Exception as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        no_candidates = "candidate" in detail.lower() or "候選" in detail or getattr(exc, "status_code", 0) == 404
        message = "這輪我暫時沒看到適合的新對象，等資料多一點我再幫你看。" if no_candidates else "我剛剛找人的路上卡了一下，沒有假裝成功；晚點可以再叫我試一次。"
        _queue_match_event(user_id, "match_search_empty" if no_candidates else "match_search_failed", message, error=detail[:200])
        profiles_coll.update_one({"user_id": user_id}, {"$set": {"match_search": {
            "status": "no_candidates" if no_candidates else "failed", "source": source,
            "error": detail[:200], "completed_at": time.time()}}})
        print(f"Proactive matchmaking failed for {user_id}: {exc}")
        return {"status": "no_candidates" if no_candidates else "failed", "detail": detail}
    finally:
        profiles_coll.update_one(
            {"user_id": user_id},
            {"$set": {"matchmaking_in_progress": False},
             "$unset": {"matchmaking_started_at": "", "matchmaking_request_id": ""}},
        )


# Candidate ranking lives in this router for now, but every public caller
# reaches it through the shared match-action boundary.
register_match_search_executor(create_proactive_match_proposal)

@router.post("/request")
def request_next_match(req: MatchRequest, background_tasks: BackgroundTasks):
    active = reconcile_match_state(req.user_id)
    if active:
        stage = derive_match_stage(active, req.user_id)
        other_id = active["to_user"] if active["from_user"] == req.user_id else active["from_user"]
        _set_match_search(
            req.user_id, stage, req.source, match_id=str(active["_id"]), other_id=other_id
        )
        return {"status": "already_active", "stage": stage, "match_id": str(active["_id"])}
    profile = profiles_coll.find_one({"user_id": req.user_id}) or {}
    if profile.get("matchmaking_in_progress") and profile.get("matchmaking_started_at", 0) > time.time() - 300:
        return {"status": "already_searching"}
    if not req.confirmed:
        _set_match_search(req.user_id, "awaiting_confirmation", req.source)
        return {
            "status": "awaiting_confirmation",
            "message": "要我現在幫你翻翻名單嗎？你點開始，我才會真的去找。",
        }
    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
        "status": "queued", "source": req.source, "requested_at": time.time()}}}, upsert=True)
    background_tasks.add_task(start_match_search, req.user_id, source=req.source, force_new=req.force_new)
    return {"status": "queued"}


@router.post("/cancel")
def cancel_match_request(req: MatchRequest):
    active = reconcile_match_state(req.user_id)
    if active:
        return {"status": "already_active", "match_id": str(active["_id"])}
    _set_match_search(req.user_id, "cancelled", req.source)
    return {"status": "cancelled"}

@router.get("/status")
def get_match_status(user_id: str):
    active = reconcile_match_state(user_id)
    profile = profiles_coll.find_one({"user_id": user_id}, {"match_search": 1, "matchmaking_in_progress": 1}) or {}
    search = profile.get("match_search", {"status": "idle"})
    if active and search.get("status") in {"idle", "completed", "cancelled"}:
        search = {
            "status": derive_match_stage(active, user_id),
            "source": "reconciled", "match_id": str(active["_id"]),
            "other_id": active["to_user"] if active["from_user"] == user_id else active["from_user"],
            "updated_at": time.time(),
        }
    elif active:
        search = {
            **search,
            "status": derive_match_stage(active, user_id),
            "match_id": str(active["_id"]),
            "other_id": active["to_user"] if active["from_user"] == user_id else active["from_user"],
        }
    if not active and search.get("status") in {"waiting_user", "waiting_other", "incoming_decision", "found", "completed"}:
        search = {"status": "idle", "source": "reconciled", "updated_at": time.time()}
        profiles_coll.update_one(
            {"user_id": user_id, "match_search.status": {"$in": ["waiting_user", "waiting_other", "incoming_decision", "found", "completed"]}},
            {"$set": {"match_search": search}},
        )
    live_match = ({"match_id": str(active["_id"]), "status": active.get("status"), "other_id": active["to_user"] if active["from_user"] == user_id else active["from_user"]} if active else None)
    return {"search": search, "live_match": live_match, "match_search": search,
            "matchmaking_in_progress": bool(profile.get("matchmaking_in_progress")),
            "active_proposal_card": build_active_proposal_card(active, user_id) if active else None,
            "active_proposal": ({
                "match_id": str(active["_id"]),
                "status": active.get("status"),
                "other_id": active["to_user"] if active["from_user"] == user_id else active["from_user"],
            } if active else None),
            "status_snapshot": get_match_status_snapshot(user_id)}

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
    other_id = match_doc.get("to_user") if match_doc.get("from_user") == user_id else match_doc.get("from_user")
    return {"match_id": match_id, "status": match_doc.get("status"),
            "stage": derive_match_stage(match_doc, user_id), "other_id": other_id}

@router.post("")
def match_endpoint(req: MatchRequest):
    return generate_matches_for_user(req.user_id)

def _apply_match_decision(req: MatchDecisionRequest, background_tasks: BackgroundTasks):
    result = decide_match_action(
        user_id=req.user_id, match_id=req.match_id, action=req.action,
        expected_status=req.expected_status, expected_revision=req.expected_revision,
        explicit_reasons=req.explicit_reasons,
        schedule_task=background_tasks.add_task,
    )
    if result.get("stale"):
        raise HTTPException(status_code=409, detail={"message": "配對狀態已變更", "current_status": result.get("current_status"), "current_revision": result.get("current_revision")})
    return result


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


@router.post("/accept")
def accept_match(req: AcceptRequest, background_tasks: BackgroundTasks):
    match_doc = matches_coll.find_one({"_id": ObjectId(req.match_id)})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")
    return _apply_match_decision(MatchDecisionRequest(
        **req.model_dump(), action="accept", expected_status=match_doc.get("status", "")
    ), background_tasks)


@router.post("/decline")
def decline_match(req: AcceptRequest, background_tasks: BackgroundTasks):
    match_doc = matches_coll.find_one({"_id": ObjectId(req.match_id)})
    if not match_doc:
        raise HTTPException(status_code=404, detail="Match not found")
    action = "cancel" if match_doc.get("status") == "pending" and match_doc.get("from_user") == req.user_id else "decline"
    return _apply_match_decision(MatchDecisionRequest(
        **req.model_dump(), action=action, expected_status=match_doc.get("status", "")
    ), background_tasks)
