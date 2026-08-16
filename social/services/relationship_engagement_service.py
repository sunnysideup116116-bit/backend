"""Canonical feedback, probe, and post-chat engagement operations.

This service owns the relationship-side state used by public chat, private
mediator chat, and proactive delivery.  It intentionally exposes small
functions while the HTTP adapters are split in separate phases.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid

from bson.objectid import ObjectId

from database import matches_coll, messages_coll, profiles_coll
from services.ai_service import generate_chat_completion
from services.mediator_event_service import queue_mediator_event
from services.match_state_service import verified_accepted_match_query


PROBE_PENDING_TTL = 72 * 3600
PROBE_IN_FLIGHT_STATUSES = {"queued", "awaiting_answer", "awaiting_sentiment", "awaiting_consent"}
LOW_SENSITIVITY_PROBES = {"fun_fact", "weekend", "conversation_hook", "availability"}
PROBE_QUESTIONS = {
    "sentiment": "你跟這位聊起來感覺如何？",
    "fun_fact": "有沒有一件關於你的有趣小事，可以讓我之後幫你們找話題？",
    "weekend": "你這週末大概想怎麼過？",
    "conversation_hook": "如果要讓對方更好開話題，你希望我透露哪個輕鬆的小線索？",
    "availability": "你近期哪個時間比較方便認識新朋友？",
}


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


def probe_policy(user_id: str) -> tuple[str, int, int, int]:
    doc = profiles_coll.find_one({"user_id": user_id}, {"probe_mode": 1}) or {}
    mode = doc.get("probe_mode", "balanced")
    if mode == "manual":
        return mode, 10**9, 10**9, 86400
    if mode == "active":
        return mode, 6, 600, 21600
    if os.getenv("MEDIATOR_DEMO_FAST_PROBE", "0") == "1":
        return mode, 6, 120, 300
    return mode, 8, 1800, 86400


def trigger_proactive_match(user_id: str, source: str = "automatic", force_new: bool = False) -> None:
    from services.match_action_service import start_match_search

    start_match_search(user_id, source=source, force_new=force_new)


def choose_probe_kind(match_doc: dict, requested_kind: str | None = None) -> str:
    if requested_kind in PROBE_QUESTIONS:
        return requested_kind
    recent = [item.get("kind") for item in (match_doc.get("probe_history", []) or [])[-5:]]
    for kind in ("fun_fact", "conversation_hook", "weekend", "availability", "sentiment"):
        if (not recent or kind != recent[-1]) and kind not in recent[-3:]:
            return kind
    return "fun_fact"


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
    history = list(messages_coll.find(
        {"room_id": room_id}, {"_id": 0, "sender_id": 1, "content": 1},
    ).sort("timestamp", -1).limit(20))[::-1]
    transcript = "\n".join(f"{message['sender_id']}: {message['content']}" for message in history)
    prompt = f'''

請根據以下兩人的聊天紀錄，整理給媒人使用的關係摘要。

只輸出 JSON：

{{"shared_summary":"一句話摘要","interaction_tone":"互動語氣","common_topics":["共同話題"],"conversation_hooks":["下次可延伸話題"]}}



聊天紀錄：

{transcript}

'''
    try:
        data = json.loads(generate_chat_completion(prompt, temperature=0.2, json_output=True).content)
        data["last_summarized_count"] = count
        data["updated_at"] = time.time()
        matches_coll.update_one({"_id": match_id}, {"$set": {"relationship_memory": data}})
    except Exception as exc:
        print(f"Relationship summary error: {exc}")


def queue_due_feedback(user_id: str) -> None:
    mode, min_messages, idle_seconds, cooldown_seconds = probe_policy(user_id)
    if mode == "manual":
        return
    now = time.time()
    # Keep one stable candidate snapshot for this poll.  The loop updates the
    # same collection and must not let those writes change which rows belong
    # to the current delivery pass.
    candidates = list(matches_coll.find(verified_accepted_match_query(user_id)))
    for match_doc in candidates:
        count = int(match_doc.get("shared_message_count", 0))
        if count < min_messages or float(match_doc.get("last_chat_at", now)) > now - idle_seconds:
            continue
        state = participant_probe_state(match_doc, user_id)
        status = state.get("status", "idle")
        if status in PROBE_IN_FLIGHT_STATUSES:
            if float(state.get("asked_at", now)) < now - PROBE_PENDING_TTL:
                matches_coll.update_one(
                    {"_id": match_doc["_id"]},
                    {"$set": {participant_probe_field(match_doc, user_id) + ".status": "expired"}},
                )
            continue
        last_count = int(state.get("message_count_snapshot", 0))
        if state.get("completed_at") and (count - last_count < 6 or now < float(state.get("cooldown_until", 0))):
            continue
        other_id = match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"]
        kind = choose_probe_kind(match_doc)
        probe_id = uuid.uuid4().hex
        state_field = participant_probe_field(match_doc, user_id)
        probe_state = {
            "status": "queued", "trigger": "auto", "requester_id": None, "probe_id": probe_id,
            "kind": kind, "question": PROBE_QUESTIONS[kind], "asked_at": now,
            "message_count_snapshot": count, "cooldown_until": now + cooldown_seconds,
        }
        claimed = matches_coll.update_one(
            {
                "_id": match_doc["_id"],
                "$or": [
                    {f"{state_field}.status": {"$nin": list(PROBE_IN_FLIGHT_STATUSES)}},
                    {f"{state_field}.asked_at": {"$lt": now - PROBE_PENDING_TTL}},
                ],
            },
            {"$set": {state_field: probe_state}, "$push": {"probe_history": {
                "probe_id": probe_id, "kind": kind, "asked_to": user_id, "asked_at": now,
                "status": "queued", "trigger": "auto",
            }}},
        )
        if not claimed.modified_count:
            continue
        queue_mediator_event(
            user_id, PROBE_QUESTIONS[kind], "probe_question", match_id=str(match_doc["_id"]),
            other_id=other_id, origin="auto", probe_kind=kind, probe_id=probe_id,
        )
        return


def queue_manual_fun_fact_probe(match_doc: dict, requester_id: str, target_id: str) -> bool:
    """Ask one low-sensitivity question, without re-asking it during cooldown."""
    now = time.time()
    previous = participant_probe_state(match_doc, target_id)
    if previous.get("kind") == "fun_fact":
        if previous.get("status") in PROBE_IN_FLIGHT_STATUSES:
            return False
        if previous.get("status") in {"completed", "declined"} and float(previous.get("cooldown_until", 0) or 0) > now:
            return False
    probe_id = uuid.uuid4().hex
    state_field = participant_probe_field(match_doc, target_id)
    state = {
        "status": "queued", "trigger": "manual", "requester_id": requester_id,
        "probe_id": probe_id, "kind": "fun_fact", "question": PROBE_QUESTIONS["fun_fact"],
        "asked_at": now, "message_count_snapshot": int(match_doc.get("shared_message_count", 0)),
        "cooldown_until": now + 7 * 86400,
    }
    claimed = matches_coll.update_one(
        {
            "_id": match_doc["_id"],
            "$or": [
                {f"{state_field}.status": {"$nin": list(PROBE_IN_FLIGHT_STATUSES | {"completed", "declined"})}},
                {f"{state_field}.cooldown_until": {"$lte": now}},
                {f"{state_field}.asked_at": {"$lt": now - PROBE_PENDING_TTL}},
            ],
        },
        {
            "$set": {state_field: state},
            "$push": {"probe_history": {
                "probe_id": probe_id, "kind": "fun_fact", "asked_to": target_id,
                "asked_at": now, "status": "queued", "trigger": "manual",
                "requester_id": requester_id,
            }},
        },
    )
    if not claimed.modified_count:
        return False
    queue_mediator_event(
        target_id, PROBE_QUESTIONS["fun_fact"], "probe_question", match_id=str(match_doc["_id"]),
        other_id=requester_id, origin="manual", requester_id=requester_id,
        probe_kind="fun_fact", probe_id=probe_id,
    )
    return True


def consume_pending_probe_answer(
    match_doc: dict, user_doc: dict, user_id: str, other_id: str, message: str,
) -> str | None:
    """Consume a delivered probe once so it cannot fall through to the chat model."""
    pending = user_doc.get("pending_private_feedback") or {}
    if pending.get("match_id") != str(match_doc["_id"]) or pending.get("other_id") != other_id:
        return None
    probe_id = pending.get("probe_id")
    state_field = participant_probe_field(match_doc, user_id)
    state = participant_probe_state(match_doc, user_id)
    if not probe_id or state.get("probe_id") != probe_id or state.get("status") not in {"awaiting_answer", "awaiting_sentiment"}:
        profiles_coll.update_one(
            {"user_id": user_id, "pending_private_feedback.probe_id": probe_id},
            {"$unset": {"pending_private_feedback": ""}},
        )
        return None
    answer = re.sub(r"\s+", " ", message or "").strip()
    if not answer:
        return "這題我還沒收到內容；你想回答時再跟我說就好。"
    declined = any(phrase in answer for phrase in ("不想回答", "不方便說", "先跳過", "略過", "不回答"))
    kind = state.get("kind") or pending.get("kind") or "sentiment"
    now = time.time()
    completed_state = {
        **state, "status": "declined" if declined else "completed", "answered_at": now,
        "answer": answer, "completed_at": now,
    }
    if declined:
        completed_state.pop("answer", None)
    result_record = {
        "probe_id": probe_id, "kind": kind, "answer": answer if not declined else "",
        "answered_by": user_id, "requester_id": state.get("requester_id"),
        "status": completed_state["status"], "answered_at": now,
        "shareable": bool(kind in LOW_SENSITIVITY_PROBES and not declined),
    }
    updated = matches_coll.update_one(
        {
            "_id": match_doc["_id"], f"{state_field}.probe_id": probe_id,
            f"{state_field}.status": {"$in": ["awaiting_answer", "awaiting_sentiment"]},
        },
        {"$set": {state_field: completed_state, f"mediator_state.probe_results.{probe_id}": result_record}},
    )
    if not updated.modified_count:
        return "這題已經處理完成，我不會再重問。"
    profiles_coll.update_one(
        {"user_id": user_id, "pending_private_feedback.probe_id": probe_id},
        {"$unset": {"pending_private_feedback": ""}},
    )
    requester_id = state.get("requester_id")
    if requester_id and requester_id != user_id and kind in LOW_SENSITIVITY_PROBES and not declined:
        queue_mediator_event(
            requester_id, f"我幫你問到一個可聊的點：對方說「{answer}」。你可以順著這個接話。",
            "probe_result", match_id=str(match_doc["_id"]), other_id=user_id, probe_id=probe_id,
        )
    if declined:
        return "收到，這題我先幫你跳過，也不會再拿同一題來問。"
    if kind == "sentiment":
        return "收到，這是你的私下想法；我先不替你轉述。"
    return "收到，我記下來了，之後會用這個幫你們找話題。"
