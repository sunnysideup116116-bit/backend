"""Canonical feedback, probe, and post-chat engagement operations.

This service owns the relationship-side state used by public chat, private
mediator chat, and proactive delivery.  It intentionally exposes small
functions while the HTTP adapters are split in separate phases.
"""

from __future__ import annotations

import json
import re
import time

from bson.objectid import ObjectId

from database import matches_coll, messages_coll, profiles_coll
from services.ai_service import generate_chat_completion
from services.mediator_event_service import queue_mediator_event
from services.match_state_service import verified_accepted_match_query


PROBE_IN_FLIGHT_STATUSES = {"queued", "awaiting_answer", "awaiting_sentiment", "awaiting_consent"}
LEGACY_PRIVATE_PROBE_EVENT_TYPES = frozenset({
    "probe_question",
    "feedback_request",
    "feedback_consent_request",
    "probe_result",
})
_DECLINE_FEEDBACK_RE = re.compile(r"(?:不想聊|不方便說|先跳過|略過|不回答|不要問)")
_UNRELATED_REQUEST_RE = re.compile(
    r"(?:天氣|氣溫|下雨|新聞|股票|匯率|導航|查(?:一下)?|搜尋|"
    r"我想問|請問|可以幫我|幫我(?:安排|設定|查|找)|行事曆|鬧鐘)",
    re.IGNORECASE,
)
_RELATIONSHIP_FEEDBACK_RE = re.compile(
    r"(?:約會|見面|碰面|相處|聊天|對方|他|她|喜歡|好感|開心|自在|尷尬|緊張|失望|難過|不錯|還好|普通|想再|不想再|感覺)",
    re.IGNORECASE,
)
_SHORT_FEEDBACKS = frozenset({"很好", "不錯", "還好", "普通", "開心", "很開心", "不好", "有點尷尬", "蠻好的"})


def generate_mediator_private_room_id(user_id: str, other_id: str) -> str:
    return f"mediator_private::{user_id}::{other_id}"


def find_accepted_match(user_id: str, other_id: str) -> dict | None:
    return matches_coll.find_one(verified_accepted_match_query(user_id, other_id))


def relationship_unread_field(match_doc: dict, user_id: str) -> str:
    return f"private_unread.{participant_role(match_doc, user_id)}"


def participant_role(match_doc: dict, user_id: str) -> str:
    return "from" if match_doc.get("from_user") == user_id else "to"


def participant_probe_state(match_doc: dict, user_id: str) -> dict:
    participants = (match_doc.get("mediator_state") or {}).get("participants") or {}
    return participants.get(participant_role(match_doc, user_id)) or {}


def participant_probe_field(match_doc: dict, user_id: str) -> str:
    return f"mediator_state.participants.{participant_role(match_doc, user_id)}"


def trigger_proactive_match(user_id: str, source: str = "automatic", force_new: bool = False) -> None:
    """Retained for old imports, but post-chat activity must never start matching."""
    return None


def cleanup_legacy_private_probe_state(
    user_id: str,
    *,
    user_doc: dict | None = None,
    now: float | None = None,
) -> dict[str, int]:
    """Cancel old Private probe state and remove queued probe events.

    This cleanup is idempotent and deliberately narrow: it only touches the
    legacy probe inbox entries/pending fields and an in-flight participant
    probe state.  Date follow-up state, confirmations, memories and chat
    history remain intact.
    """
    current = time.time() if now is None else float(now)
    if not user_id:
        return {"profiles": 0, "matches": 0}
    profiles_changed = 0
    try:
        result = profiles_coll.update_one(
            {"user_id": user_id},
            {
                "$pull": {
                    "mediator_inbox": {
                        "type": {"$in": sorted(LEGACY_PRIVATE_PROBE_EVENT_TYPES)},
                    },
                },
                "$unset": {
                    "pending_private_feedback": "",
                    "pending_feedback_match_id": "",
                    "pending_feedback_other_id": "",
                },
            },
        )
        profiles_changed = int(getattr(result, "modified_count", 0) or 0)
    except Exception:
        profiles_changed = 0

    matches_changed = 0
    try:
        candidates = list(matches_coll.find(verified_accepted_match_query(user_id)))
    except Exception:
        candidates = []
    for match_doc in candidates:
        if not isinstance(match_doc, dict) or not match_doc.get("_id"):
            continue
        field = participant_probe_field(match_doc, user_id)
        state = participant_probe_state(match_doc, user_id)
        if (
            state.get("status") not in PROBE_IN_FLIGHT_STATUSES
            or not state.get("probe_id")
        ):
            continue
        try:
            result = matches_coll.update_one(
                {
                    "_id": match_doc["_id"],
                    f"{field}.probe_id": state.get("probe_id"),
                    f"{field}.status": {"$in": sorted(PROBE_IN_FLIGHT_STATUSES)},
                },
                {"$set": {
                    f"{field}.status": "cancelled",
                    f"{field}.cancel_reason": "legacy_probe_disabled",
                    f"{field}.cancelled_at": current,
                }},
            )
            matches_changed += int(getattr(result, "modified_count", 0) or 0)
        except Exception:
            continue
    return {"profiles": profiles_changed, "matches": matches_changed}


def classify_feedback(message: str) -> str:
    try:
        prompt = f'''

請判斷以下配對回饋的情緒，只輸出 JSON：{{"sentiment":"positive|negative|neutral"}}

使用者訊息：{message}

'''
        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True).content)
        sentiment = result.get("sentiment", "neutral")
        return sentiment if sentiment in {"positive", "negative", "neutral"} else "neutral"
    except Exception:
        return "neutral"


def feedback_share_consent(message: str) -> bool:
    try:
        prompt = f'''

請判斷使用者是否同意把這段配對回饋轉述給對方或讓媒人拿去做後續協調。

只輸出 JSON：{{"consent":true|false,"confidence":0.0}}

使用者訊息：{message}

'''
        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True).content)
        return bool(result.get("consent")) and float(result.get("confidence", 0)) >= 0.6
    except Exception:
        return False


def handle_private_feedback(user_id: str, user_doc: dict, message: str) -> str | None:
    match_id = user_doc.get("pending_feedback_match_id")
    if not match_id:
        return None
    try:
        match_doc = matches_coll.find_one({"_id": ObjectId(match_id)})
    except Exception:
        match_doc = None
    if not match_doc:
        profiles_coll.update_one(
            {"user_id": user_id},
            {"$unset": {"pending_feedback_match_id": "", "pending_feedback_other_id": ""}},
        )
        return None

    other_id = match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"]
    sentiment = classify_feedback(message)
    share_consent = feedback_share_consent(message)
    feedback_entry = {"sentiment": sentiment, "share_consent": share_consent, "updated_at": time.time()}
    matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {
        f"private_feedback.{user_id}": feedback_entry,
        f"private_feedback_text.{user_id}": message,
    }})
    profiles_coll.update_one(
        {"user_id": user_id},
        {"$unset": {"pending_feedback_match_id": "", "pending_feedback_other_id": ""}},
    )

    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or match_doc
    other_feedback = (refreshed.get("private_feedback", {}) or {}).get(other_id, {})
    if isinstance(other_feedback, str):
        other_feedback = {"sentiment": other_feedback, "share_consent": False}
    other_sentiment = other_feedback.get("sentiment")
    other_consent = bool(other_feedback.get("share_consent"))
    probe_requesters = set(refreshed.get("probe_requested_by", []))

    if other_id in probe_requesters and share_consent:
        if sentiment == "positive":
            queue_mediator_event(
                other_id,
                f"我幫你打聽到一點好消息：{user_id} 對你的感覺是正向的。你可以放鬆一點繼續聊。",
                "probe_result", match_id=str(match_doc["_id"]), other_id=user_id,
            )
        elif sentiment == "negative":
            queue_mediator_event(
                other_id, "我幫你問過了，對方目前沒有想再往前推。我會先幫你們保留體面，不硬撮合。",
                "gentle_closure", match_id=str(match_doc["_id"]), other_id=user_id,
            )
        matches_coll.update_one({"_id": match_doc["_id"]}, {"$pull": {"probe_requested_by": other_id}})

    if sentiment == "positive" and share_consent and other_sentiment == "positive" and other_consent:
        for recipient, crush in ((user_id, other_id), (other_id, user_id)):
            queue_mediator_event(
                recipient,
                f"我兩邊都確認過了，{crush} 對你也有好感。你們可以自然多聊一點，我會在旁邊幫忙看節奏。",
                "mutual_interest", match_id=str(match_doc["_id"]), other_id=crush,
            )
        return f"我記下來了，也會幫你把好感小心地傳給 {other_id}。"
    if sentiment == "negative":
        if share_consent and other_sentiment == "positive" and other_consent:
            queue_mediator_event(
                other_id, "我幫你探過了，對方目前沒有想繼續往前。我會幫你們自然收住，不讓場面尷尬。",
                "gentle_closure", match_id=str(match_doc["_id"]), other_id=user_id,
            )
        return (
            "收到，我會把你的意思整理成比較溫和的說法，不會把原話硬丟給對方。"
            if share_consent else "收到，我只把這個當成你的私下回饋，不會轉述給對方。"
        )
    if sentiment == "positive":
        return (
            f"懂了，我會幫你把這份好感小心傳給 {other_id}，不會講得太用力。"
            if share_consent else "收到，我先幫你記著這份好感，暫時不替你轉述。"
        )
    return "收到，我先幫你記下來。等你想更明確一點，我再幫你往前推。"


def mark_post_chat_activity(match_doc: dict | None, room_id: str) -> int:
    if not match_doc:
        return 0
    count = messages_coll.count_documents({"room_id": room_id})
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"shared_message_count": count, "last_chat_at": time.time()}},
    )
    return count


def summarize_relationship(match_id, room_id: str) -> None:
    match_doc = matches_coll.find_one({"_id": match_id})
    if not match_doc:
        return
    count = messages_coll.count_documents({"room_id": room_id})
    memory = match_doc.get("relationship_memory", {}) or {}
    if count < 6 or count - int(memory.get("last_summarized_count", 0)) < 4:
        return
    last_count = max(0, int(memory.get("last_summarized_count", 0) or 0))
    history = list(messages_coll.find(
        {"room_id": room_id}, {"_id": 0, "sender_id": 1, "content": 1, "timestamp": 1},
    ).sort("timestamp", 1).skip(last_count).limit(20))
    if not history:
        return
    labels = {
        str(match_doc.get("from_user") or ""): "甲方",
        str(match_doc.get("to_user") or ""): "乙方",
        "ai_assistant": "阿月",
    }
    transcript_lines, transcript_chars = [], 0
    for message in history:
        content = re.sub(r"\s+", " ", str(message.get("content") or "")).strip()[:2000]
        line = f"{labels.get(str(message.get('sender_id') or ''), '參與者')}: {content}"
        if not content or transcript_chars + len(line) > 12000:
            continue
        transcript_lines.append(line)
        transcript_chars += len(line)
    if not transcript_lines:
        return
    transcript = "\n".join(transcript_lines)
    previous_summary = str(memory.get("shared_summary") or "")[:2000]
    prompt = f'''

請只根據共同聊天室的既有摘要與新增訊息，遞迴更新給媒人使用的關係摘要。不得加入任何悄悄話或私人記憶。

只輸出 JSON：

{{"shared_summary":"一句話摘要","interaction_tone":"互動語氣","common_topics":["共同話題"],"conversation_hooks":["下次可延伸話題"]}}



既有共同摘要：{previous_summary or "尚無"}

新增共同聊天紀錄：

{transcript}

'''
    try:
        data = json.loads(generate_chat_completion(prompt, temperature=0.2, json_output=True).content)
        data["last_summarized_count"] = min(count, last_count + len(history))
        data["updated_at"] = time.time()
        matches_coll.update_one({"_id": match_id}, {"$set": {"relationship_memory": data}})
    except Exception as exc:
        print(f"Relationship summary error: {exc}")


def queue_due_feedback(user_id: str) -> None:
    """Compatibility no-op; automatic Private probes are disabled."""
    del user_id
    return None


def queue_manual_fun_fact_probe(match_doc: dict, requester_id: str, target_id: str) -> bool:
    """Compatibility no-op; manual Private fun-fact probes are disabled."""
    del match_doc, requester_id, target_id
    return False


def consume_pending_probe_answer(
    match_doc: dict, user_doc: dict, user_id: str, other_id: str, message: str,
) -> str | None:
    """Compatibility no-op; old probe answers are no longer consumed."""
    del match_doc, user_doc, user_id, other_id, message
    return None


def consume_pending_post_date_feedback(
    match_doc: dict,
    user_doc: dict,
    user_id: str,
    other_id: str,
    message: str,
    *,
    source_message_id: str,
) -> str | None:
    """Consume only a clearly related post-date reply; unrelated turns fall through."""
    pending = user_doc.get("pending_post_date_feedback") or {}
    if (
        pending.get("relationship_id") != str(match_doc.get("_id") or "")
        or pending.get("other_id") != other_id
    ):
        return None
    answer = re.sub(r"\s+", " ", str(message or "")).strip()
    now = time.time()
    if not answer:
        return None
    if float(pending.get("expires_at", 0) or 0) <= now:
        profiles_coll.update_one(
            {"user_id": user_id, "pending_post_date_feedback.event_id": pending.get("event_id")},
            {"$unset": {"pending_post_date_feedback": ""}},
        )
        return None
    declined = bool(_DECLINE_FEEDBACK_RE.search(answer))
    if not declined and _UNRELATED_REQUEST_RE.search(answer):
        return None
    if (
        not declined
        and answer not in _SHORT_FEEDBACKS
        and not _RELATIONSHIP_FEEDBACK_RE.search(answer)
    ):
        return None
    profiles_coll.update_one(
        {"user_id": user_id, "pending_post_date_feedback.event_id": pending.get("event_id")},
        {"$unset": {"pending_post_date_feedback": ""}},
    )
    from services.post_date_followup_service import POST_DATE_FOLLOWUPS

    POST_DATE_FOLLOWUPS.update_one(
        {"event_id": pending.get("event_id"), "recipient_id": user_id, "status": "delivered"},
        {"$set": {
            "response_status": "declined" if declined else "answered",
            "response_message_id": source_message_id,
            "responded_at": now,
        }},
    )
    if declined:
        return "好，這次先不聊，我也不會把它記成你對對方的負面看法。"
    from services.relationship_memory_service import enqueue_relationship_memory_extraction

    enqueue_relationship_memory_extraction(
        user_id,
        other_id,
        str(match_doc.get("_id") or ""),
        source_message_id,
        date_event_id=str(pending.get("event_id") or ""),
    )
    return "收到，這是你私下告訴我的感受；我不會自動轉述給對方。"
