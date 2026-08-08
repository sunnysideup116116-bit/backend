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

from pydantic import BaseModel, ConfigDict, Field

from database import messages_coll, profiles_coll
from services.ai_service import generate_chat_completion
from services.chat_service import generate_room_id
from services.profile_projection import safe_recent_context
from services.ayue_agent.product_identity import AYUE_MISSION_SHORT, AYUE_VOICE_SHORT


class ProactiveCareDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(min_length=1, max_length=160)
    focus: Literal["latest_message", "recent_context"]
    grounding_span: str = Field(min_length=1, max_length=240)
    confidence: float = Field(ge=0, le=1)


class ProactiveCareContext(BaseModel):
    latest_owner_message: str = ""
    previous_assistant_message: str = ""
    recent_context: str = ""
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
    """Schedule exactly one new care opportunity for a newly saved owner turn."""
    activity_at = now if now is not None else time.time()
    profile = profiles_coll.find_one({"user_id": user_id}, {"proactive_frequency": 1}) or {}
    schedule_proactive_care(user_id, profile.get("proactive_frequency", "3600"), last_activity=activity_at, now=activity_at)
    return activity_at


def build_proactive_care_context(user_id: str, user_doc: dict, *, now: datetime | None = None) -> ProactiveCareContext:
    room_id = generate_room_id(user_id, "ai_assistant")
    history = list(messages_coll.find(
        {"room_id": room_id}, {"_id": 0, "sender_id": 1, "content": 1},
    ).sort("timestamp", -1).limit(12))
    latest_owner = next((_clean(item.get("content"), 240) for item in history if item.get("sender_id") == user_id), "")
    previous_assistant = next((_clean(item.get("content"), 160) for item in history if item.get("sender_id") == "ai_assistant"), "")
    local_now = (now or datetime.now(ZoneInfo("Asia/Taipei"))).astimezone(ZoneInfo("Asia/Taipei"))
    tone = str(user_doc.get("mediator_tone") or "friend")
    if tone not in {"friend", "gentle", "enthusiastic"}:
        tone = "friend"
    return ProactiveCareContext(
        latest_owner_message=latest_owner,
        previous_assistant_message=previous_assistant,
        recent_context=safe_recent_context(user_doc.get("current_context"), ""),
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
    source = context.latest_owner_message if decision.focus == "latest_message" else context.recent_context
    text = _clean(decision.message, 160)
    if (
        decision.confidence < 0.72
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
    if not context.latest_owner_message and not context.recent_context:
        return None, "no_grounding"
    effective_context = context
    if context.latest_owner_message.strip().lower() in _CONTROL_ONLY_MESSAGES and context.recent_context:
        # Confirmation/cancellation tokens belong to a closed protocol and are
        # not a useful topic for a later care message.
        effective_context = context.model_copy(update={"latest_owner_message": ""})
    payload = effective_context.model_dump()
    base_prompt = f"""{AYUE_MISSION_SHORT}
{AYUE_VOICE_SHORT}

你是阿月，正在主動關心一位使用者。阿月是說話者，使用者是收話者；絕對不可把阿月叫成「欸阿月」，也不可角色顛倒。
只能根據安全 context 的 latest_owner_message 或 recent_context 關心使用者。不要讀取、提及或推銷配對、對方、行事曆、心理診斷或系統能力。若沒有具體可關心的內容，輸出空字串以外的 JSON 不可。
訊息必須一到兩句、最多一個自然問題、繁體中文且不重複 previous_assistant_message。grounding_span 必須是所選 focus 的原文連續子字串。
只輸出 JSON：{{"message":"...","focus":"latest_message|recent_context","grounding_span":"原文子字串","confidence":0.0}}
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
