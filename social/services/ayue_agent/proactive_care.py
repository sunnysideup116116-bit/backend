"""Bounded proactive-care generation for Public Ayue.

Proactive care is a product surface, not a normal agent tool: it has no owner
turn and may not inspect match, calendar, or counterparty data.  This module
therefore exposes a small typed contract plus an atomic activity claim.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from bson.objectid import ObjectId
from pydantic import BaseModel, ConfigDict, Field

from database import messages_coll, profiles_coll
from services.ai_service import generate_chat_completion
from services.chat_service import generate_room_id
from services.ai_room_service import most_recent_ai_room
from services.profile_projection import safe_recent_context
from services.message_use_service import is_reusable_for_care
from services.ayue_agent.product_identity import AYUE_MISSION_SHORT, AYUE_VOICE_SHORT


class ProactiveCareDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=160)
    focus: Literal["latest_message", "recent_context", "follow_up"]
    grounding_span: str = Field(min_length=1, max_length=240)
    confidence: float = Field(ge=0, le=1)


class ProactiveCareContext(BaseModel):
    latest_owner_message: str = ""
    previous_assistant_message: str = ""
    recent_context: str = ""
    followup_topic: str = ""
    followup_question_goal: str = ""
    followup_grounding_span: str = ""
    relevant_memories: list[str] = Field(default_factory=list, max_length=8)
    tone: str = "friend"
    local_date: str
    local_period: str


_INTERNAL_TEXT_RE = re.compile(r"(?:seed_user_[\w-]+|demo_user|mongo|tool_call|visible_tools|prompt|系統限制|工具|函式)", re.IGNORECASE)
_ROLE_INVERSION_RE = re.compile(r"(?:欸|嗨|嘿)[，、\s]*阿月|阿月[，、\s]*(?:你|妳)", re.IGNORECASE)
_CONTROL_ONLY_MESSAGES = {"確認", "取消", "好", "好的", "可以", "不要", "不用", "ok", "yes", "no"}
_FREQUENCY_ALIASES = {
    "none": "none", "60": "60", "3600": "3600", "86400": "86400",
    # Preserve old settings while returning canonical values to the UI.
    "high": "60", "normal": "3600", "low": "86400",
}


def _clean(value: object, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _period(now: datetime) -> str:
    if now.hour < 11:
        return "早上"
    if now.hour < 17:
        return "下午"
    return "晚上"


def normalize_proactive_frequency(value: object) -> str:
    """Return the canonical persisted/UI value for care frequency."""
    return _FREQUENCY_ALIASES.get(str(value or "none").strip().lower(), "none")


def proactive_frequency_seconds(value: object) -> int | None:
    normalized = normalize_proactive_frequency(value)
    return None if normalized == "none" else int(normalized)


def schedule_proactive_care(user_id: str, frequency: object, *, last_activity: float, now: float | None = None) -> None:
    """Persist the due time so care does not depend on a browser polling."""
    now = now if now is not None else time.time()
    normalized = normalize_proactive_frequency(frequency)
    seconds = proactive_frequency_seconds(normalized)
    update: dict[str, object] = {
        "proactive_frequency": normalized,
        "last_user_activity_at": last_activity,
    }
    unset: dict[str, str] = {
        "proactive_care_claim_id": "",
        "proactive_care_claim_activity_at": "",
        "proactive_care_claimed_at": "",
        "proactive_care_retry_count": "",
        "proactive_care_retry_after": "",
    }
    if seconds is None or last_activity <= 0:
        unset["next_proactive_care_at"] = ""
    else:
        update["next_proactive_care_at"] = max(now, last_activity + seconds)
    profiles_coll.update_one({"user_id": user_id}, {"$set": update, "$unset": unset}, upsert=True)


def record_proactive_activity(user_id: str, *, now: float | None = None) -> float:
    """Compatibility alias for the candidate pipeline's activity marker."""
    from services.proactive_followup_service import record_owner_activity
    return record_owner_activity(user_id, now=now)


def _recent_context_sources_are_reusable(user_id: str, user_doc: dict) -> bool:
    """Require every active recent-context field to retain ordinary evidence."""
    state = user_doc.get("recent_context_state") or {}
    fields = state.get("fields") if isinstance(state, dict) else None
    if not isinstance(fields, dict) or not fields:
        return False
    evidence_ids: set[str] = set()
    for field in fields.values():
        if not isinstance(field, dict):
            return False
        message_id = str(field.get("evidence_message_id") or "")
        if not message_id:
            return False
        evidence_ids.add(message_id)
    for message_id in evidence_ids:
        try:
            source_id = ObjectId(message_id)
        except Exception:
            return False
        try:
            source = messages_coll.find_one(
                {"_id": source_id, "sender_id": user_id},
                {"metadata.message_use": 1},
            )
        except Exception:
            return False
        if not source or not is_reusable_for_care(source):
            return False
    return True


def build_proactive_care_context(
    user_id: str,
    user_doc: dict,
    *,
    now: datetime | None = None,
    room_id: str | None = None,
    candidate: dict | None = None,
) -> ProactiveCareContext:
    room_id = room_id or most_recent_ai_room(user_id)
    history = list(messages_coll.find(
        {
            "room_id": room_id,
        },
        {
            "_id": 0,
            "sender_id": 1,
            "content": 1,
            "metadata.message_use": 1,
        },
    ).sort([("timestamp", -1), ("_id", -1)]).limit(12))
    owner_messages = [
        item for item in history if item.get("sender_id") == user_id
    ]
    latest_owner_item = owner_messages[0] if owner_messages else None
    latest_owner = (
        _clean(latest_owner_item.get("content"), 240)
        if latest_owner_item and is_reusable_for_care(latest_owner_item)
        else ""
    )
    previous_assistant = next(
        (
            _clean(item.get("content"), 160)
            for item in history
            if item.get("sender_id") == "ai_assistant" and is_reusable_for_care(item)
        ),
        "",
    )
    safe_context = (
        safe_recent_context(user_doc.get("current_context"), "")
        if _recent_context_sources_are_reusable(user_id, user_doc)
        else ""
    )
    candidate_source = None
    if candidate:
        try:
            candidate_source = messages_coll.find_one(
                {
                    "_id": ObjectId(str(candidate.get("source_message_id") or "")),
                    "room_id": room_id,
                    "sender_id": user_id,
                },
                {"content": 1, "metadata.owner_raw_content": 1, "metadata.message_use": 1},
            )
        except Exception:
            candidate_source = None
        if not candidate_source or not is_reusable_for_care(candidate_source):
            candidate_source = None
    if (latest_owner_item is None or not is_reusable_for_care(latest_owner_item)) and not candidate_source:
        # A control/calendar turn must not wake care for an older topic merely
        # because that topic remains in the profile projection.
        safe_context = ""
    followup_grounding = (
        _clean((candidate or {}).get("evidence_span"), 240)
        if candidate_source
        else ""
    )
    if candidate_source:
        source_text = _clean(
            ((candidate_source.get("metadata") or {}).get("owner_raw_content"))
            or candidate_source.get("content"),
            800,
        )
        if followup_grounding not in source_text:
            followup_grounding = ""
    memories: list[str] = []
    stance_labels = {
        "like": "喜歡",
        "dislike": "不喜歡",
        "avoid": "避免",
        "require": "需要",
    }
    for item in (user_doc.get("profile_memory_preview") or [])[:8]:
        if isinstance(item, dict):
            label = _clean(item.get("label_zh_tw") or item.get("label"), 48)
            stance = stance_labels.get(str(item.get("stance") or "").lower(), "偏好")
        else:
            label = _clean(item, 48)
            stance = "偏好"
        memory_text = f"{stance}：{label}" if label else ""
        if memory_text and not _INTERNAL_TEXT_RE.search(memory_text) and memory_text not in memories:
            memories.append(memory_text)
    local_now = (now or datetime.now(ZoneInfo("Asia/Taipei"))).astimezone(ZoneInfo("Asia/Taipei"))
    tone = str(user_doc.get("mediator_tone") or "friend")
    if tone not in {"friend", "gentle", "enthusiastic"}:
        tone = "friend"
    return ProactiveCareContext(
        latest_owner_message=latest_owner,
        previous_assistant_message=previous_assistant,
        recent_context=safe_context,
        followup_topic=_clean((candidate or {}).get("topic"), 80) if candidate_source else "",
        followup_question_goal=_clean((candidate or {}).get("question_goal"), 160) if candidate_source else "",
        followup_grounding_span=followup_grounding,
        relevant_memories=memories,
        tone=tone,
        local_date=local_now.date().isoformat(),
        local_period=_period(local_now),
    )


def claim_proactive_care(user_id: str, last_activity: float, *, now: float | None = None, due_before: float | None = None) -> str | None:
    """Atomically reserve one care attempt for one owner activity timestamp."""
    if last_activity <= 0:
        return None
    claim_id = uuid.uuid4().hex
    claimed_at = now if now is not None else time.time()
    query = {
            "user_id": user_id,
            "last_user_activity_at": last_activity,
            "$or": [
                {"last_followup_activity_at": {"$lt": last_activity}},
                {"last_followup_activity_at": {"$exists": False}},
            ],
            "$and": [{
                "$or": [
                    {"proactive_care_claim_activity_at": {"$ne": last_activity}},
                    {"proactive_care_claim_activity_at": {"$exists": False}},
                ]
            }],
        }
    if due_before is not None:
        query["next_proactive_care_at"] = {"$lte": due_before}
    result = profiles_coll.find_one_and_update(
        query,
        {"$set": {
            "proactive_care_claim_id": claim_id,
            "proactive_care_claim_activity_at": last_activity,
            "proactive_care_claimed_at": claimed_at,
        }},
    )
    return claim_id if result else None


def finalize_proactive_care_claim(
    user_id: str, claim_id: str, last_activity: float, *, delivered: bool, now: float | None = None,
    delivery_marker: dict | None = None,
) -> bool:
    """Consume a claim even after provider failure, preventing polling storms."""
    update = {
        "$set": {
            "last_followup_activity_at": last_activity,
            "last_proactive_time": now if now is not None else time.time(),
        },
        "$unset": {
            "proactive_care_claim_id": "",
            "proactive_care_claim_activity_at": "",
            "proactive_care_claimed_at": "",
        },
    }
    if delivered:
        update["$set"]["ai_chat_locked"] = False
        update["$set"]["ai_chat_interaction_count"] = 0
        if delivery_marker:
            update["$set"]["proactive_care_delivery"] = delivery_marker
    result = profiles_coll.update_one(
        {"user_id": user_id, "proactive_care_claim_id": claim_id}, update,
    )
    return bool(getattr(result, "modified_count", 0))


def reschedule_proactive_care_claim(user_id: str, claim_id: str, last_activity: float, *, retry_after: float, retry_count: int) -> bool:
    """Release a failed provider attempt without consuming the owner activity."""
    result = profiles_coll.update_one(
        {"user_id": user_id, "proactive_care_claim_id": claim_id, "last_user_activity_at": last_activity},
        {"$set": {
            "next_proactive_care_at": retry_after,
            "proactive_care_retry_after": retry_after,
            "proactive_care_retry_count": retry_count,
        }, "$unset": {
            "proactive_care_claim_id": "",
            "proactive_care_claim_activity_at": "",
            "proactive_care_claimed_at": "",
        }},
    )
    return bool(getattr(result, "modified_count", 0))


def consume_proactive_delivery(user_id: str) -> dict | None:
    """Deliver a persisted care notice once; the actual chat message already exists."""
    doc = profiles_coll.find_one_and_update(
        {"user_id": user_id, "proactive_care_delivery.message": {"$exists": True}},
        {"$unset": {"proactive_care_delivery": ""}},
    )
    marker = (doc or {}).get("proactive_care_delivery") or {}
    return marker if isinstance(marker, dict) and marker.get("message") else None


def _valid_decision(raw: object, context: ProactiveCareContext) -> ProactiveCareDecision | None:
    try:
        decision = ProactiveCareDecision.model_validate(json.loads(str(raw)))
    except Exception:
        return None
    if decision.focus == "latest_message":
        source = context.latest_owner_message
    elif decision.focus == "recent_context":
        source = context.recent_context
    else:
        source = context.followup_grounding_span
    text = _clean(decision.message, 160)
    if (
        decision.confidence < 0.72
        or (context.followup_grounding_span and decision.focus != "follow_up")
        or not source
        or decision.grounding_span not in source
        or not text
        or text == context.previous_assistant_message
        or len(re.findall(r"[？?]", text)) > 1
        or len(re.findall(r"[。！？!?]", text)) > 2
        or _INTERNAL_TEXT_RE.search(text)
        or _ROLE_INVERSION_RE.search(text)
    ):
        return None
    decision.message = text
    return decision


def proactive_care_claim_is_current(user_id: str, claim_id: str, last_activity: float) -> bool:
    """Avoid saving stale care if a newer owner activity replaced the claim."""
    return bool(profiles_coll.find_one({
        "user_id": user_id,
        "proactive_care_claim_id": claim_id,
        "proactive_care_claim_activity_at": last_activity,
        "last_user_activity_at": last_activity,
    }, {"_id": 1}))


def generate_proactive_care_outcome(context: ProactiveCareContext) -> tuple[ProactiveCareDecision | None, str]:
    """Return a grounded care message, or None, after at most one repair call."""
    if not context.latest_owner_message and not context.recent_context and not context.followup_grounding_span:
        return None, "no_grounding"
    effective_context = context
    if not context.followup_grounding_span and context.latest_owner_message.strip().lower() in _CONTROL_ONLY_MESSAGES and context.recent_context:
        # Confirmation/cancellation tokens belong to a closed protocol and are
        # not a useful topic for a later care message.
        effective_context = context.model_copy(update={"latest_owner_message": ""})
    payload = effective_context.model_dump()
    base_prompt = f"""{AYUE_MISSION_SHORT}
{AYUE_VOICE_SHORT}

你是阿月，正在主動關心一位使用者。阿月是說話者，使用者是收話者；絕對不可把阿月叫成「欸阿月」，也不可角色顛倒。
只能根據安全 context 的 latest_owner_message、recent_context 或 follow-up 候選關心使用者。若有 follow-up 候選，focus 必須使用 follow_up，且只能確認候選的中性 question_goal，不得預設結果。不要讀取、提及或推銷配對、對方、行事曆、心理診斷或系統能力。若沒有具體可關心的內容，輸出空字串以外的 JSON 不可。
訊息必須一到兩句、最多一個自然問題、繁體中文且不重複 previous_assistant_message。grounding_span 必須是所選 focus 的原文連續子字串。
Memory 只可作為語氣背景，不可把記憶當成這次追問的 evidence。只輸出 JSON：{{"message":"...","focus":"latest_message|recent_context|follow_up","grounding_span":"原文子字串","confidence":0.0}}
安全 context：{json.dumps(payload, ensure_ascii=False)}"""
    provider_failed = False
    for attempt in range(2):
        prompt = base_prompt
        if attempt:
            prompt += "\n前一次輸出未通過格式或安全驗證。請重新產生一次，嚴格使用原文 grounding_span，不要輸出解釋。"
        try:
            provider_result = generate_chat_completion(
                prompt, temperature=0.5 if attempt == 0 else 0, json_output=True,
            )
            decision = _valid_decision(
                getattr(provider_result, "content", provider_result),
                effective_context,
            )
        except Exception:
            provider_failed = True
            decision = None
        if decision:
            return decision, "generated"
    return None, "provider_error" if provider_failed else "invalid_output"


def generate_proactive_care(context: ProactiveCareContext) -> ProactiveCareDecision | None:
    """Backward-compatible decision-only facade for callers and tests."""
    return generate_proactive_care_outcome(context)[0]
