import json
import time
import os
import re
import uuid
from fastapi import APIRouter, HTTPException, BackgroundTasks
from pymongo import ReturnDocument
from pymongo.errors import PyMongoError
from models import (
    ChatRequest, DirectChatRequest, MediatorPrivateRequest, MediatorProbeRequest,
    RelationshipGameRequest, RelationshipQuizAnswerRequest, ResetRequest,
    RiskFeedbackRequest, GuidanceActivityRequest, GuidanceSuggestionRequest,
)
from database import profiles_coll, messages_coll, matches_coll
from config import OLLAMA_FAST_CHAT_MODEL
from services.ai_service import analyze_big_five, analyze_deep_profile, get_embedding, generate_chat_completion
from services.chat_service import generate_room_id, save_message
from services.memory_service import observe_user_memory, get_user_graph_memories
from services.mediator_event_service import claim_next_mediator_event, queue_mediator_event
from services import risk_client

router = APIRouter(prefix="/api", tags=["Chat"])


def analysis_room_id(user_id: str, state: str) -> str:
    return generate_room_id(user_id, f"{state}_assistant")


MATCH_READINESS_THRESHOLD = 75
FEEDBACK_COOLDOWN_SECONDS = 120
PROBE_PENDING_TTL = 72 * 3600
PROBE_IN_FLIGHT_STATUSES = {
    "queued", "awaiting_answer", "awaiting_sentiment", "awaiting_consent"
}
QUIZ_TTL_SECONDS = 7 * 86400
QUIZ_QUESTIONS = [
    {
        "id": "weekend",
        "text": "週末比較想怎麼安排？",
        "options": ["安靜休息", "出去走走", "找人一起活動"],
    },
    {
        "id": "first_meet",
        "text": "第一次見面你比較喜歡哪種節奏？",
        "options": ["輕鬆聊天", "一起吃飯", "做一件小活動"],
    },
    {
        "id": "chat_rhythm",
        "text": "你喜歡怎樣的聊天頻率？",
        "options": ["慢慢聊", "即時回覆", "看情況自然來"],
    },
]

MEDIATOR_PERSONA = """
雿????臭?雿鈭箇?詻澈????撖???鈭箝?雿??臬恥??隤芾店?芰???站嚗?曉???瑽踝?銝憯??蹂犖鋆?鈭?隢?敺蝙?刻?嗥?擃葉???身?芸?銝?伐?敹???憭?伐?銝璅?????"""

MEDIATOR_PERSONA = """
你是阿月，一個會聊天、會觀察契機的校園媒人。你不是冷冰冰的配對按鈕，而是會邊聊邊理解使用者近況、偏好、地雷與當下需求的人。
你的風格是自然、短句、有一點熟朋友的吐槽，但不能冒犯；目標是在對話裡找到適合牽線的時機，並在資訊足夠時主動幫使用者留意人選。
"""

MEDIATOR_TONES = {
    "friend": "像熟朋友一樣自然、有點嘴但不冒犯，回覆要短、有人味。",
    "gentle": "溫柔、穩定、少一點玩笑，多一點照顧感。",
    "enthusiastic": "活潑、主動、帶一點興奮感，但不要吵。"
}

RELATIONSHIP_EVENT_TYPES = {
    "feedback_request", "feedback_consent_request", "probe_result",
    "gentle_closure", "mutual_interest", "probe_question",
    "date_coordination_request", "date_coordination_result",
    "date_coordination_cancelled", "match_connected",
    "compatibility_quiz_invite"
}

PROBE_QUESTIONS = {
    "sentiment": "你跟這位聊起來感覺如何？",
    "fun_fact": "有沒有一件關於你的有趣小事，可以讓我之後幫你們找話題？",
    "weekend": "你這週末大概想怎麼過？",
    "conversation_hook": "如果要讓對方更好開話題，你希望我透露哪個輕鬆的小線索？",
    "availability": "你近期哪個時間比較方便認識新朋友？",
}
LOW_SENSITIVITY_PROBES = {"fun_fact", "weekend", "conversation_hook", "availability"}

def profile_display_name(doc: dict | None, fallback_id: str) -> str:
    if doc:
        for key in ("name", "display_name", "nickname", "username"):
            value = str(doc.get(key) or "").strip()
            if value:
                return value
    return fallback_id

def mediator_style(user_id: str) -> str:
    doc = profiles_coll.find_one({"user_id": user_id}, {"mediator_tone": 1}) or {}
    tone = doc.get("mediator_tone", "friend")
    return MEDIATOR_TONES.get(tone, MEDIATOR_TONES["friend"])

def relationship_unread_field(match_doc: dict, user_id: str) -> str:
    role = "from" if match_doc.get("from_user") == user_id else "to"
    return f"private_unread.{role}"

def participant_role(match_doc: dict, user_id: str) -> str:
    return "from" if match_doc.get("from_user") == user_id else "to"

def participant_probe_state(match_doc: dict, user_id: str) -> dict:
    return (((match_doc.get("mediator_state") or {}).get("participants") or {}).get(participant_role(match_doc, user_id)) or {})

def participant_probe_field(match_doc: dict, user_id: str) -> str:
    return f"mediator_state.participants.{participant_role(match_doc, user_id)}"

def probe_policy(user_id: str):
    doc = profiles_coll.find_one({"user_id": user_id}, {"probe_mode": 1}) or {}
    mode = doc.get("probe_mode", "balanced")
    if mode == "manual":
        return mode, 10**9, 10**9, 86400
    if mode == "active":
        return mode, 6, 600, 21600
    if os.getenv("MEDIATOR_DEMO_FAST_PROBE", "0") == "1":
        return mode, 6, 120, 300
    return mode, 8, 1800, 86400

def trigger_proactive_match(user_id: str, source: str = "automatic", force_new: bool = False):
    from routers.match import create_proactive_match_proposal
    create_proactive_match_proposal(user_id, source=source, force_new=force_new)


def is_explicit_match_request(message: str) -> bool:
    if not (message or "").strip():
        return False
    prompt = f"""
你是交友媒人 App 的意圖判斷器。
請判斷使用者這句話是不是「明確要求阿月開始找配對/找人陪/幫忙介紹人」。

使用者訊息：{message}

請只輸出 JSON：
{{"is_match_request":true|false,"confidence":0.0}}

判斷重點：
- true：使用者已經明確叫阿月找人、配人、介紹人、找伴、找隊友、找飯友、找讀書夥伴。
- false：只是說自己想做某件事、閒聊、補充條件、還沒要求阿月開始找。
- false：只是在說取消、撤回、不要目前配對；除非明確說「取消後重新找」。
- 不要靠固定關鍵字，請理解中文口語。
"""
    try:
        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True))
        return bool(result.get("is_match_request")) and float(result.get("confidence", 0)) >= 0.6
    except Exception as e:
        print(f"Explicit match request LLM classification failed: {e}")
        return False


def classify_active_match_followup(message: str, stage: str, other_id: str | None = None) -> dict:
    if not (message or "").strip():
        return {"action": "none", "confidence": 0.0}
    prompt = f"""
你是媒人 App 的狀態意圖分類器。使用者目前已有一個進行中的配對狀態。

目前狀態：{stage}
對方：{other_id or "未知"}
使用者訊息：{message}

請判斷使用者是否真的在談「目前這張配對」或「重新配對」。
不要靠固定關鍵字；請理解語意。
如果使用者只是在聊股市、人生、閒聊、其他話題，即使有「怎麼辦」也要輸出 none。

只輸出 JSON：
{{
  "action": "none|show_current|restart|cancel|status",
  "confidence": 0.0
}}

action 說明：
- none：不是在處理目前配對。
- show_current：想看上一張卡、目前卡片、幫我處理那張。
- restart：想換一個、重新配對、重新找人。
- cancel：想取消、撤回、不要這條。
- status：問目前配對進度、對方回了沒、現在怎樣。
"""
    try:
        result = json.loads(
            generate_chat_completion(
                prompt,
                temperature=0,
                json_output=True,
                model=OLLAMA_FAST_CHAT_MODEL,
            )
        )
        action = result.get("action", "none")
        confidence = float(result.get("confidence", 0) or 0)
        if action not in {"none", "show_current", "restart", "cancel", "status"}:
            action = "none"
        if confidence < 0.65:
            action = "none"
        return {"action": action, "confidence": confidence}
    except Exception as e:
        print(f"Active match follow-up classification failed: {e}")
        return {"action": "none", "confidence": 0.0}


def active_match_card_metadata(card: dict) -> dict:
    return {
        "event_type": card.get("event_type"),
        "match_id": card.get("match_id"),
        "other_id": card.get("other_id"),
        "proposal_role": card.get("proposal_role"),
        "matches": [{
            "match_id": card.get("match_id"),
            "matched_user_id": card.get("other_id"),
            "target_context": card.get("your_context"),
            "current_context": card.get("other_context"),
            "top_reasons": card.get("reasons") or [],
            "score_breakdown": {"total": card.get("score") or 0},
            "recommendation_reason": card.get("recommendation_reason", ""),
            "receiver_reason": card.get("receiver_reason", ""),
        }],
    }


def active_user_match_query(user_id: str) -> dict:
    return {
        "$or": [
            {"status": "draft", "from_user": user_id},
            {
                "status": "pending",
                "$or": [{"from_user": user_id}, {"to_user": user_id}],
            },
        ]
    }


def is_match_status_question(message: str) -> bool:
    compact = re.sub(r"\s+", "", message or "")
    return any(
        keyword in compact
        for keyword in ("結果", "進度", "狀態", "對方回", "回覆", "接受了嗎", "婉拒", "拒絕", "答應", "怎樣", "怎麼樣")
    )


def latest_match_for_user(user_id: str):
    return matches_coll.find_one(
        {"$or": [{"from_user": user_id}, {"to_user": user_id}]},
        sort=[("created_at", -1)],
    )


def match_status_reply(match_doc: dict | None, user_id: str) -> str:
    if not match_doc:
        return "目前沒有正在等待回覆的牽線提案。"
    status = match_doc.get("status")
    other_id = (
        match_doc.get("to_user")
        if match_doc.get("from_user") == user_id
        else match_doc.get("from_user")
    )
    if status == "draft":
        return f"這張 @{other_id} 的牽線提案還在等你決定，還沒有送去問對方。"
    if status == "pending":
        if match_doc.get("from_user") == user_id:
            return f"我已經幫你把邀請送給 @{other_id}，目前還在等對方回覆。"
        return f"@{other_id} 的邀請正在等你決定，還沒有變成配對成功。"
    if status == "accepted":
        return f"@{other_id} 已經接受，聊天室已經開好了。"
    if status == "declined":
        return f"這張 @{other_id} 的牽線已經婉拒/結束，沒有變成接受。"
    if status == "expired":
        return f"這張 @{other_id} 的牽線提案已經過期。"
    return f"這張 @{other_id} 的牽線狀態是 {status or '未知'}。"


def surface_active_match_help(room_id: str, user_id: str, match_doc: dict) -> dict:
    from routers.match import build_active_proposal_card, derive_match_stage

    stage = derive_match_stage(match_doc, user_id)
    other_id = (
        match_doc.get("to_user")
        if match_doc.get("from_user") == user_id
        else match_doc.get("from_user")
    )
    if stage in {"waiting_user", "incoming_decision"}:
        card = build_active_proposal_card(match_doc, user_id)
        if card:
            card_metadata = active_match_card_metadata(card)
            ai_reply = "有，這張還在等你決定。我幫你把卡片叫回來。"
            save_message(room_id, "ai_assistant", ai_reply)
            save_message(
                room_id,
                "ai_assistant",
                card.get("opening", "阿月有一則牽線提案。"),
                message_type="mediator_card",
                metadata=card_metadata,
            )
            return {
                "reply": ai_reply,
                "is_locked": False,
                "mode": "active_match_help",
                "match_request_status": stage,
                "match_id": str(match_doc["_id"]),
                "mediator_card": {
                    "text": card.get("opening", "阿月有一則牽線提案。"),
                    "metadata": card_metadata,
                },
            }
    if stage == "waiting_other":
        ai_reply = f"這條我已經幫你問出去了，現在等 @{other_id} 點頭。先別重複開新局，不然會撞單。"
    else:
        ai_reply = f"這條配對目前狀態是 {stage}，我先不重複翻名單。"
    save_message(room_id, "ai_assistant", ai_reply)
    return {
        "reply": ai_reply,
        "is_locked": False,
        "mode": "active_match_help",
        "match_request_status": stage,
        "match_id": str(match_doc["_id"]),
    }


def latest_shared_chat(match_doc: dict, limit: int = 16):
    if not match_doc:
        return []
    room_id = generate_room_id(match_doc["from_user"], match_doc["to_user"])
    history = list(messages_coll.find(
        {"room_id": room_id}, {"_id": 0, "sender_id": 1, "content": 1}
    ).sort("timestamp", -1).limit(limit))[::-1]
    return [{"sender_id": item.get("sender_id"), "content": item.get("content", "")} for item in history]


def relevant_graph_memories(user_id: str, message: str, limit: int = 9):
    memories = get_user_graph_memories(user_id, 20)
    compact = re.sub(r"\s+", "", message.lower())
    grams = {compact[i:i + 2] for i in range(max(0, len(compact) - 1))}

    def relevance(item):
        haystack = " ".join(str(item.get(key, "")).lower() for key in ("key", "label", "category"))
        overlap = sum(1 for gram in grams if gram and gram in haystack)
        return (overlap, float(item.get("confidence", 0)), float(item.get("last_seen_at", 0)))

    related = sorted(memories, key=relevance, reverse=True)
    directly_related = [item for item in related if relevance(item)[0] > 0][:6]
    selected = directly_related[:]
    for item in sorted(memories, key=lambda value: (
        float(value.get("confidence", 0)), float(value.get("last_seen_at", 0))
    ), reverse=True):
        if item not in selected and len(selected) < limit:
            selected.append(item)
    return selected


def mediator_profile_context(user_id: str, message: str):
    doc = profiles_coll.find_one({"user_id": user_id}, {
        "_id": 0, "user_id": 1, "initial_interest": 1, "current_context": 1,
        "big_five": 1, "deep_profile": 1,
    }) or {}
    return {
        "owner_user_id": user_id,
        "initial_interest": doc.get("initial_interest"),
        "current_context": doc.get("current_context"),
        "big_five": doc.get("big_five", {}),
        "deep_profile": doc.get("deep_profile", {}),
        "graph_memories": relevant_graph_memories(user_id, message),
    }


def has_context_signal(value) -> bool:
    if value is None or value is False:
        return False
    text = str(value).strip().lower()
    return text not in {
        "", "null", "none", "unknown", "n/a", "false",
        "沒有", "無", "不確定", "尚未提供", "未提及"
    }


def is_naturally_social_activity(signals: dict) -> bool:
    activity_text = " ".join(
        str(signals.get(key) or "") for key in ("activity", "preference")
    ).lower()
    companion_text = str(signals.get("companion_intent") or "").lower()
    if any(word in companion_text or word in activity_text for word in (
        "自己", "一個人", "獨處", "放空", "不用人陪", "不要人陪", "alone"
    )):
        return False
    social_keywords = (
        "吃", "飯", "宵夜", "豬腳", "火鍋", "咖啡", "酒", "居酒屋",
        "電影", "看海", "散步", "逛", "運動", "籃球", "健身", "跑步",
        "讀書", "lol", "遊戲", "唱歌", "夜景"
    )
    return any(keyword in activity_text for keyword in social_keywords)


def deterministic_readiness(intent: str, signals: dict) -> tuple[int, list[str]]:
    if intent != "recent_context" or not isinstance(signals, dict):
        return 0, ["activity", "timing", "preference", "companion_intent"]
    weights = {"activity": 40, "timing": 20, "preference": 20, "companion_intent": 20}
    available = {key: has_context_signal(signals.get(key)) for key in weights}
    companion_text = str(signals.get("companion_intent", "")).lower()
    if any(word in companion_text for word in (
        "自己", "一個人", "獨處", "放空", "不用", "不要", "不想", "alone", "no companion"
    )):
        available["companion_intent"] = False
    if not available["companion_intent"] and available["activity"] and available["timing"]:
        available["companion_intent"] = is_naturally_social_activity(signals)
    if not (available["activity"] and available["timing"] and available["companion_intent"]):
        missing = [key for key in weights if not available[key]]
        return 0, missing
    score = sum(weight for key, weight in weights.items() if available[key])
    missing = [key for key in weights if not available[key]]
    return score, missing


def social_opportunity_followup(signals: dict, missing: list[str]) -> str:
    if not isinstance(signals, dict) or not has_context_signal(signals.get("activity")):
        return ""
    activity = str(signals.get("activity") or "").strip()
    if "companion_intent" in missing:
        return f"這種局我先當成可以幫你留意人選；你補個時間或想要的氛圍，我就比較好抓方向。"
    if "timing" in missing:
        return "我先把它當成可以牽線的局。你大概想什麼時候去？時間一明確，我就比較好幫你看人。"
    if "preference" in missing:
        return "那我會先照你剛剛說的方向留意，不用一次把條件講完。"
    return ""


def contains_question(text: str) -> bool:
    return "?" in (text or "") or "？" in (text or "")


def remove_question_sentences(text: str) -> str:
    sentences = [part.strip() for part in re.split(r"(?<=[?？。！!])\s*", text or "") if part.strip()]
    kept = [sentence for sentence in sentences if not contains_question(sentence)]
    return "".join(kept).strip()


def is_relationship_query(message: str, mentioned_ids: list[str]) -> bool:
    if mentioned_ids:
        return True
    compact = re.sub(r"\s+", "", message.lower())
    keywords = (
        "他", "她", "對方", "那個人", "剛剛那位", "這個人",
        "喜歡什麼", "個性", "興趣", "適合", "聊什麼", "問問"
    )
    return any(keyword in compact for keyword in keywords)


def unsupported_relationship_fact(
    message: str,
    evidence_catalog: dict,
    evidence_owners: dict | None = None,
    subject_ids: list[str] | None = None,
) -> str | None:
    compact = re.sub(r"\s+", "", message or "")
    allowed_subjects = set(subject_ids or [])
    evidence_text = " ".join(
        str(value) for key, value in evidence_catalog.items()
        if not allowed_subjects or not evidence_owners
        or evidence_owners.get(key) in allowed_subjects
    ).lower()
    if not evidence_text:
        return None
    fact_keywords = ("生日", "星座", "系級", "住哪", "住哪裡", "前任", "感情史", "收入", "家庭")
    for keyword in fact_keywords:
        if keyword in compact and keyword not in evidence_text:
            return keyword
    return None


def validate_relationship_claims(
    claims: list,
    accepted_ids: set[str],
    evidence_catalog: dict,
    evidence_owners: dict,
) -> tuple[set[str], list[str]]:
    valid_subjects = set()
    valid_evidence = []
    for claim in claims or []:
        subject_id = claim.get("subject_user_id")
        if subject_id not in accepted_ids:
            continue
        claim_evidence = [
            evidence_id for evidence_id in (claim.get("evidence_ids") or [])
            if evidence_id in evidence_catalog
            and evidence_owners.get(evidence_id) == subject_id
        ]
        if claim_evidence:
            valid_subjects.add(subject_id)
            for evidence_id in claim_evidence:
                if evidence_id not in valid_evidence:
                    valid_evidence.append(evidence_id)
    return valid_subjects, valid_evidence


def grounded_relationship_fallback(relationships: list[dict], compare: bool = False) -> str:
    if not relationships:
        return "我現在沒有足夠的已驗證聊天資料可以判斷，先不要亂猜比較好。"
    if compare and len(relationships) >= 2:
        ranked = sorted(
            relationships,
            key=lambda item: (
                float((item.get("score_breakdown") or {}).get("total", 0) or 0),
                int(item.get("shared_message_count", 0) or 0)
            ),
            reverse=True
        )
        best = ranked[0]
        evidence_notes = []
        for item in ranked[:3]:
            evidence = str(item.get("shared_summary") or item.get("public_context") or "目前只有少量互動資料").strip()
            if len(evidence) > 72:
                evidence = evidence[:72].rstrip("。！？!?") + "..."
            evidence_notes.append(f"@{item['other_id']}：{evidence.rstrip('。！？!?')}")
        return "我只能根據目前看得到的互動來說：" + "；".join(evidence_notes) + f"。如果要我選，我會先看 @{best['other_id']}。"
    ids = "、".join("@" + item["other_id"] for item in relationships)
    return f"我目前能根據已接受配對與聊天資料回答：{ids}。更細的判斷要等你們多聊一點。"

def choose_probe_kind(match_doc: dict, requested_kind: str | None = None) -> str:
    if requested_kind in PROBE_QUESTIONS:
        return requested_kind
    recent = [item.get("kind") for item in (match_doc.get("probe_history", []) or [])[-5:]]
    for kind in ("fun_fact", "conversation_hook", "weekend", "availability", "sentiment"):
        if (not recent or kind != recent[-1]) and kind not in recent[-3:]:
            return kind
    return "fun_fact"


def normalize_date_answer(stage: str, message: str):
    if stage == "availability":
        choices = [value for value in ("今天", "明天", "週末", "晚上", "下午") if value in message]
        return choices or ["時間再確認"]
    if stage == "activity":
        choices = [value for value in ("吃飯", "咖啡", "運動", "讀書", "看電影", "散步") if value in message]
        return choices or ["輕鬆活動"]
    if any(word in message for word in ("一千", "1000", "不限")):
        return "1000以上"
    if any(word in message for word in ("五百", "500", "便宜")):
        return "500以內"
    return "500-1000"


def is_date_cancellation(message: str) -> bool:
    compact = re.sub(r"\s+", "", message)
    return any(phrase in compact for phrase in ("不要約", "取消", "先不要", "不用了", "改天", "不想約", "暫停"))


def date_overlap(first: dict, second: dict):
    times = [item for item in first.get("availability", []) if item in second.get("availability", [])]
    activities = [item for item in first.get("activity", []) if item in second.get("activity", [])]
    return {
        "time": times[0] if times else None,
        "activity": activities[0] if activities else None,
        "budget": first.get("budget") if first.get("budget") == second.get("budget") else "彈性預算",
    }

def classify_feedback(message: str) -> str:
    try:
        prompt = f"""
請判斷以下配對回饋的情緒，只輸出 JSON：{{"sentiment":"positive|negative|neutral"}}
使用者訊息：{message}
"""
        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True))
        sentiment = result.get("sentiment", "neutral")
        return sentiment if sentiment in {"positive", "negative", "neutral"} else "neutral"
    except Exception:
        return "neutral"

def classify_proposal_intent(message: str, situation: str = "match_search_confirmation"):
    compact = re.sub(r"\s+", "", (message or "").strip())
    if not compact:
        return None

    prompt = f"""
你是交友媒人 App 的意圖判斷器。請根據目前狀態判斷使用者這句話的意思。

目前可能狀態：
- match_search_confirmation：阿月剛問「要不要開始翻名單 / 開始找人」。
- proposal_response：阿月剛提出一個配對人選，正在等使用者要接受或婉拒。

這次狀態：{situation}
使用者訊息：{message}

請只輸出 JSON：
{{"intent":"accept|decline|unclear","confidence":0.0}}

判斷規則：
- accept：使用者自然語意上是在同意、答應、請阿月開始找、請阿月幫忙問、願意試試。
- decline：使用者自然語意上是在拒絕、暫時不要、取消、婉拒、不想配。
- unclear：只是閒聊、單純道謝、還在補充條件、沒有明確要開始/接受/拒絕。
- 不要要求固定關鍵字，請理解中文口語，例如「好謝謝」「麻煩你」「那就交給你」「可以啊」。
- 但「謝謝」單獨出現通常只是禮貌，不等於同意開始找。
"""
    try:
        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True))
        intent = str(result.get("intent", "unclear")).lower()
        confidence = float(result.get("confidence", 0))
        if confidence < 0.55:
            return None
        if intent == "accept":
            return True
        if intent == "decline":
            return False
        return None
    except Exception as e:
        print(f"Proposal intent LLM classification failed: {e}")

    # Fallback only when the LLM classifier is unavailable.
    lower = compact.lower()
    if any(word in lower for word in ("不要", "不用", "先不要", "取消", "婉拒", "拒絕", "no", "nah")):
        return False
    if any(word in lower for word in ("開始", "找吧", "可以", "好", "ok", "yes")):
        return True
    return None

def feedback_share_consent(message: str) -> bool:
    try:
        prompt = f"""
請判斷使用者是否同意把這段配對回饋轉述給對方或讓媒人拿去做後續協調。
只輸出 JSON：{{"consent":true|false,"confidence":0.0}}
使用者訊息：{message}
"""
        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True))
        return bool(result.get("consent")) and float(result.get("confidence", 0)) >= 0.6
    except Exception:
        return False


def handle_private_feedback(user_id: str, user_doc: dict, message: str):
    match_id = user_doc.get("pending_feedback_match_id")
    if not match_id:
        return None
    try:
        from bson.objectid import ObjectId
        match_doc = matches_coll.find_one({"_id": ObjectId(match_id)})
    except Exception:
        match_doc = None
    if not match_doc:
        profiles_coll.update_one(
            {"user_id": user_id},
            {"$unset": {"pending_feedback_match_id": "", "pending_feedback_other_id": ""}}
        )
        return None

    other_id = match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"]
    sentiment = classify_feedback(message)
    share_consent = feedback_share_consent(message)
    feedback_entry = {
        "sentiment": sentiment,
        "share_consent": share_consent,
        "updated_at": time.time()
    }
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {
            f"private_feedback.{user_id}": feedback_entry,
            f"private_feedback_text.{user_id}": message,
        }}
    )
    profiles_coll.update_one(
        {"user_id": user_id},
        {"$unset": {"pending_feedback_match_id": "", "pending_feedback_other_id": ""}}
    )

    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or match_doc
    feedback = refreshed.get("private_feedback", {})
    other_feedback = feedback.get(other_id, {})
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
                "probe_result",
                match_id=str(match_doc["_id"]),
                other_id=user_id
            )
        elif sentiment == "negative":
            queue_mediator_event(
                other_id,
                "我幫你問過了，對方目前沒有想再往前推。我會先幫你們保留體面，不硬撮合。",
                "gentle_closure",
                match_id=str(match_doc["_id"]),
                other_id=user_id
            )
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$pull": {"probe_requested_by": other_id}}
        )

    if sentiment == "positive" and share_consent and other_sentiment == "positive" and other_consent:
        for recipient, crush in ((user_id, other_id), (other_id, user_id)):
            queue_mediator_event(
                recipient,
                f"我兩邊都確認過了，{crush} 對你也有好感。你們可以自然多聊一點，我會在旁邊幫忙看節奏。",
                "mutual_interest",
                match_id=str(match_doc["_id"]),
                other_id=crush
            )
        return f"我記下來了，也會幫你把好感小心地傳給 {other_id}。"

    if sentiment == "negative":
        if share_consent and other_sentiment == "positive" and other_consent:
            queue_mediator_event(
                other_id,
                "我幫你探過了，對方目前沒有想繼續往前。我會幫你們自然收住，不讓場面尷尬。",
                "gentle_closure",
                match_id=str(match_doc["_id"]),
                other_id=user_id
            )
        if share_consent:
            return "收到，我會把你的意思整理成比較溫和的說法，不會把原話硬丟給對方。"
        return "收到，我只把這個當成你的私下回饋，不會轉述給對方。"

    if sentiment == "positive":
        if share_consent:
            return f"懂了，我會幫你把這份好感小心傳給 {other_id}，不會講得太用力。"
        return "收到，我先幫你記著這份好感，暫時不替你轉述。"
    return "收到，我先幫你記下來。等你想更明確一點，我再幫你往前推。"

def mark_post_chat_activity(match_doc: dict, room_id: str):
    if not match_doc:
        return 0
    count = messages_coll.count_documents({"room_id": room_id})
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"shared_message_count": count, "last_chat_at": time.time()}}
    )
    return count

def summarize_relationship(match_id, room_id: str):
    match_doc = matches_coll.find_one({"_id": match_id})
    if not match_doc:
        return
    count = messages_coll.count_documents({"room_id": room_id})
    memory = match_doc.get("relationship_memory", {}) or {}
    if count < 6 or count - int(memory.get("last_summarized_count", 0)) < 4:
        return
    history = list(messages_coll.find(
        {"room_id": room_id}, {"_id": 0, "sender_id": 1, "content": 1}
    ).sort("timestamp", -1).limit(20))[::-1]
    transcript = "\n".join(f"{m['sender_id']}: {m['content']}" for m in history)
    prompt = f"""
請根據以下兩人的聊天紀錄，整理給媒人使用的關係摘要。
只輸出 JSON：
{{"shared_summary":"一句話摘要","interaction_tone":"互動語氣","common_topics":["共同話題"],"conversation_hooks":["下次可延伸話題"]}}

聊天紀錄：
{transcript}
"""
    try:
        data = json.loads(generate_chat_completion(prompt, temperature=0.2, json_output=True))
        data["last_summarized_count"] = count
        data["updated_at"] = time.time()
        matches_coll.update_one({"_id": match_id}, {"$set": {"relationship_memory": data}})
    except Exception as e:
        print(f"Relationship summary error: {e}")

def queue_due_feedback(user_id: str):
    mode, min_messages, idle_seconds, cooldown_seconds = probe_policy(user_id)
    if mode == "manual":
        return
    now = time.time()
    candidates = list(matches_coll.find({"status": "accepted", "$or": [{"from_user": user_id}, {"to_user": user_id}]}))
    for match_doc in candidates:
        count = int(match_doc.get("shared_message_count", 0))
        if count < min_messages or float(match_doc.get("last_chat_at", now)) > now - idle_seconds:
            continue
        state = participant_probe_state(match_doc, user_id)
        status = state.get("status", "idle")
        if status in PROBE_IN_FLIGHT_STATUSES:
            if float(state.get("asked_at", now)) < now - PROBE_PENDING_TTL:
                matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {
                    participant_probe_field(match_doc, user_id) + ".status": "expired"}})
            continue
        last_count = int(state.get("message_count_snapshot", 0))
        if state.get("completed_at") and (count - last_count < 6 or now < float(state.get("cooldown_until", 0))):
            continue
        other_id = match_doc["to_user"] if match_doc["from_user"] == user_id else match_doc["from_user"]
        kind = choose_probe_kind(match_doc)
        question = PROBE_QUESTIONS[kind]
        probe_id = uuid.uuid4().hex
        probe_state = {"status": "queued", "trigger": "auto", "requester_id": None,
                       "probe_id": probe_id,
                       "kind": kind, "question": question,
                       "asked_at": now, "message_count_snapshot": count,
                       "cooldown_until": now + cooldown_seconds}
        state_field = participant_probe_field(match_doc, user_id)
        claimed = matches_coll.update_one(
            {
                "_id": match_doc["_id"],
                "$or": [
                    {f"{state_field}.status": {"$nin": list(PROBE_IN_FLIGHT_STATUSES)}},
                    {f"{state_field}.asked_at": {"$lt": now - PROBE_PENDING_TTL}},
                ],
            },
            {"$set": {participant_probe_field(match_doc, user_id): probe_state},
             "$push": {"probe_history": {
                 "probe_id": probe_id,
                 "kind": kind, "asked_to": user_id, "asked_at": now,
                 "status": "queued", "trigger": "auto"
             }}}
        )
        if not claimed.modified_count:
            continue
        queue_mediator_event(
            user_id, question, "probe_question", match_id=str(match_doc["_id"]),
            other_id=other_id, origin="auto", probe_kind=kind, probe_id=probe_id
        )
        return

@router.post("/chat")
def chat_endpoint(req: ChatRequest):
    if req.state not in {"big_five", "deep_profile"}:
        raise HTTPException(status_code=400, detail="Invalid state")

    room_id = analysis_room_id(req.user_id, req.state)
    save_message(room_id, req.user_id, req.message)

    if req.state == "big_five":
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        prev_big_five = user_doc.get("temp_big_five", {}) if user_doc else {}
        interaction_count = user_doc.get("interaction_count", 0) if user_doc else 0

        result = analyze_big_five(req.message, prev_big_five, interaction_count, req.initial_interest)

        update_fields = {
            "temp_big_five": result.get("big_five", {}),
            "interaction_count": interaction_count + 1
        }

        if result.get("is_complete", False):
            update_fields["big_five"] = result.get("big_five", {})
            ai_room_id = generate_room_id(req.user_id, "ai_assistant")
            count = messages_coll.count_documents({"room_id": ai_room_id})
            if count == 0:
                save_message(ai_room_id, "ai_assistant", "我大概更了解你的個性了。接下來可以聊聊你最近想做什麼、想去哪裡。")

        profiles_coll.update_one(
            {"user_id": req.user_id},
            {"$set": update_fields},
            upsert=True
        )
        save_message(room_id, f"{req.state}_assistant", result.get("reply", ""))

        return {
            "status": "success",
            "big_five": result.get("big_five"),
            "reply": result.get("reply"),
            "is_complete": result.get("is_complete", False)
        }
    elif req.state == "deep_profile":
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        prev_deep = user_doc.get("temp_deep_profile", {}) if user_doc else {}
        interaction_count = user_doc.get("interaction_count_deep", 0) if user_doc else 0
        big_five = user_doc.get("big_five", {}) if user_doc else {}
        current_context = user_doc.get("current_context", "") if user_doc else ""

        user_context = {"big_five": big_five, "current_context": current_context}

        result = analyze_deep_profile(req.message, prev_deep, interaction_count, user_context)

        update_fields = {
            "temp_deep_profile": result.get("deep_profile", {}),
            "interaction_count_deep": interaction_count + 1
        }

        if result.get("is_complete", False):
            update_fields["deep_profile"] = result.get("deep_profile", {})
            ai_room_id = generate_room_id(req.user_id, "ai_assistant")
            count = messages_coll.count_documents({"room_id": ai_room_id})
            if count == 0:
                save_message(ai_room_id, "ai_assistant", "我也更了解你重視什麼了。之後你想找人一起做什麼，直接跟我說就好。")

        profiles_coll.update_one(
            {"user_id": req.user_id},
            {"$set": update_fields},
            upsert=True
        )
        save_message(room_id, f"{req.state}_assistant", result.get("reply", ""))

        return {
            "status": "success",
            "deep_profile": result.get("deep_profile"),
            "reply": result.get("reply"),
            "is_complete": result.get("is_complete", False)
        }

@router.post("/chat/reset")
def reset_chat_state(req: ResetRequest):
    if req.state == "big_five":
        profiles_coll.update_one(
            {"user_id": req.user_id},
            {"$set": {"interaction_count": 0, "temp_big_five": {}}}
        )
    elif req.state == "deep_profile":
        profiles_coll.update_one(
            {"user_id": req.user_id},
            {"$set": {"interaction_count_deep": 0, "temp_deep_profile": {}}}
        )
    return {"status": "success"}

@router.get("/chat/messages/{state}")
def get_analysis_messages(state: str, user_id: str):
    if state not in {"big_five", "deep_profile"}:
        raise HTTPException(status_code=400, detail="Invalid state")

    room_id = analysis_room_id(user_id, state)
    count = messages_coll.count_documents({"room_id": room_id})
    if count == 0:
        user_doc = profiles_coll.find_one({"user_id": user_id})
        interest = user_doc.get("initial_interest") if user_doc else None
        if state == "big_five" and interest:
            greeting = (
                f"我看到你註冊時提到「{interest}」。我們就從這個開始聊，"
                "我會透過幾個情境問題整理你的性格傾向。"
            )
        elif state == "big_five":
            greeting = (
                "我們先從最近讓你印象深刻的一件事開始聊起，"
                "我會逐步整理你的性格傾向。"
            )
        else:
            greeting = "我們來聊聊你在關係和未來生活中真正重視的事情。"
        save_message(room_id, f"{state}_assistant", greeting)

    msgs = []
    for msg in messages_coll.find({"room_id": room_id}).sort("timestamp", 1):
        msg["id"] = str(msg.pop("_id"))
        msgs.append(msg)
    return {"messages": msgs}

@router.get("/messages/{contact_id}")
def get_messages(contact_id: str, user_id: str):
    room_id = generate_room_id(user_id, contact_id)
    
    if contact_id == "ai_assistant":
        count = messages_coll.count_documents({"room_id": room_id})
        if count == 0:
            save_message(room_id, "ai_assistant", "哈囉，我是阿月。最近想做什麼、想去哪裡，儘管跟我說；我會邊聊邊幫你留意合適的人。")
            
    msgs = []
    for msg in messages_coll.find({"room_id": room_id}).sort("timestamp", 1):
        msg["id"] = str(msg.pop("_id"))
        msgs.append(msg)
    user_doc = profiles_coll.find_one({"user_id": user_id})
    active_proposal_id = (user_doc or {}).get("active_match_proposal_id")
    return {"messages": msgs, "active_match_proposal_id": active_proposal_id}

@router.post("/direct_chat")
def direct_chat(req: DirectChatRequest, background_tasks: BackgroundTasks):
    room_id = generate_room_id(req.user_id, req.contact_id)
    risk_assessment = risk_client.check_risk(
        conversation_id=room_id,
        sender_id=req.user_id,
        receiver_id=req.contact_id,
        content=req.message,
    )
    if risk_client.is_blocked(risk_assessment):
        save_message(
            room_id, req.user_id, req.message,
            risk_assessment=risk_assessment,
            is_blocked=True,
            delivery_status="blocked"
        )
        return {
            "reply": None,
            "is_blocked": True,
            "ui_priority": "risk",
            "risk_assessment": risk_assessment,
        }

    def with_risk(response: dict) -> dict:
        return risk_client.attach_to_response(response, risk_assessment)

    requested_mentions = []
    for other_id in (req.mentioned_other_ids or []) + ([req.mentioned_other_id] if req.mentioned_other_id else []):
        if other_id and other_id not in requested_mentions:
            requested_mentions.append(other_id)
    display_message = req.message
    if req.contact_id == "ai_assistant" and requested_mentions:
        prefix = " ".join("@" + other_id for other_id in requested_mentions)
        display_message = f"{prefix} {req.message}".strip()
    save_message(
        room_id, req.user_id, display_message,
        risk_assessment=risk_assessment,
        is_blocked=False,
        delivery_status="delivered"
    )
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"last_user_activity_at": time.time()}},
        upsert=True,
    )
    if req.contact_id != "ai_assistant":
        background_tasks.add_task(observe_user_memory, req.user_id, req.message, "pair_chat")
    
    if req.contact_id == "ai_assistant":
        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(12)
        history = list(history_cursor)[::-1]
        
        user_doc = profiles_coll.find_one({"user_id": req.user_id})
        match_search = (user_doc or {}).get("match_search", {}) or {}
        if match_search.get("status") == "awaiting_confirmation":
            search_intent = classify_proposal_intent(req.message, "match_search_confirmation")
            if search_intent is not None:
                if search_intent:
                    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                        "status": "queued", "source": match_search.get("source", "explicit_next"),
                        "requested_at": time.time()
                    }}})
                    background_tasks.add_task(
                        trigger_proactive_match, req.user_id,
                        match_search.get("source", "explicit_next"), True
                    )
                    ai_reply = "好，我現在真的開始翻名單；有找到或這輪沒有，我都會回來說。"
                    status = "queued"
                else:
                    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                        "status": "cancelled", "source": match_search.get("source", "explicit_next"),
                        "updated_at": time.time()
                    }}})
                    ai_reply = "好，那這輪先不找。我會繼續聽你聊，等有更適合的時機再說。"
                    status = "cancelled"
                save_message(room_id, "ai_assistant", ai_reply)
                return with_risk({
                    "reply": ai_reply, "is_locked": False, "mode": "match_confirmation",
                    "match_request_status": status
                })
        active_proposal_id = (user_doc or {}).get("active_match_proposal_id")
        active_proposal_doc = None
        if active_proposal_id:
            try:
                from bson.objectid import ObjectId
                active_proposal_doc = matches_coll.find_one({"_id": ObjectId(active_proposal_id)})
            except Exception:
                active_proposal_doc = None
            if not active_proposal_doc or active_proposal_doc.get("status") not in {"draft", "pending"}:
                profiles_coll.update_one(
                    {"user_id": req.user_id},
                    {"$unset": {"active_match_proposal_id": ""}},
                )

        if is_match_status_question(req.message):
            status_doc = active_proposal_doc or latest_match_for_user(req.user_id)
            ai_reply = match_status_reply(status_doc, req.user_id)
            save_message(room_id, "ai_assistant", ai_reply)
            return with_risk({
                "reply": ai_reply,
                "is_locked": False,
                "mode": "match_status",
                "match_status": status_doc.get("status") if status_doc else "none",
            })

        proposal_intent = classify_proposal_intent(req.message, "proposal_response") if active_proposal_doc else None
        if active_proposal_doc and proposal_intent is not None:
            from models import AcceptRequest
            from routers.match import accept_match, decline_match
            action_req = AcceptRequest(user_id=req.user_id, match_id=active_proposal_id)
            try:
                result = (
                    accept_match(action_req, background_tasks)
                    if proposal_intent
                    else decline_match(action_req, background_tasks)
                )
                if proposal_intent and result.get("new_status") == "pending":
                    ai_reply = "好，我幫你去問問對方，但不替對方做決定。先等我消息。"
                elif proposal_intent and result.get("new_status") == "accepted":
                    ai_reply = "太好了，對方也點頭了。我已經幫你們開好聊天室。"
                else:
                    ai_reply = "收到，這位我先幫你婉拒，也會記下你的理由，之後少往這個方向推。"
                save_message(room_id, "ai_assistant", ai_reply)
                return with_risk({"reply": ai_reply, "is_locked": False, "mode": "proposal_response", **result})
            except Exception as e:
                print(f"Proposal response failed: {e}")

        bf = user_doc.get("big_five", {}) if user_doc else {}
        interaction_count = user_doc.get("ai_chat_interaction_count", 0) if user_doc else 0
        
        current_context = user_doc.get("current_context", "") if user_doc else ""
        current_round = interaction_count + 1
        accepted_matches = list(matches_coll.find({
            "status": "accepted", "$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]
        }))
        accepted_ids = {
            matched["to_user"] if matched["from_user"] == req.user_id else matched["from_user"]
            for matched in accepted_matches
        }
        explicit_mentions = []
        for other_id in requested_mentions + [
            accepted_id for accepted_id in accepted_ids if accepted_id in req.message
        ]:
            if other_id in accepted_ids and other_id not in explicit_mentions:
                explicit_mentions.append(other_id)
        comparison_query = any(
            phrase in req.message for phrase in ("比較", "誰比較", "哪個", "哪一個", "更適合")
        )
        relationship_query = is_relationship_query(req.message, explicit_mentions)
        relationship_lines = []
        evidence_catalog = {}
        evidence_owners = {}
        shared_message_budget = 48
        for matched in accepted_matches:
            other = matched["to_user"] if matched["from_user"] == req.user_id else matched["from_user"]
            state = (matched.get("mediator_state", {}) or {}).get("participants", {})
            other_doc = profiles_coll.find_one(
                {"user_id": other},
                {"_id": 0, "user_id": 1, "initial_interest": 1, "current_context": 1,
                 "big_five": 1, "deep_profile": 1}
            ) or {}
            other_graph = relevant_graph_memories(other, req.message) if relationship_query else []
            shared_summary = (matched.get("relationship_memory", {}) or {}).get("shared_summary", "")
            message_limit = 16 if other in explicit_mentions else (8 if comparison_query else 0)
            message_limit = min(message_limit, shared_message_budget)
            recent_messages = latest_shared_chat(matched, message_limit) if message_limit else []
            shared_message_budget -= len(recent_messages)
            reason = matched.get("reason", "") if matched.get("from_user") == req.user_id else matched.get("receiver_reason", matched.get("reason", ""))
            evidence = {}
            for key, text in (
                (f"profile:{other}:interest", other_doc.get("initial_interest")),
                (f"profile:{other}:context", other_doc.get("current_context")),
                (f"profile:{other}:personality", (other_doc.get("big_five") or {}).get("summary")),
            ):
                if text:
                    evidence[key] = str(text)
                    evidence_owners[key] = other
            for memory in other_graph:
                memory_key = f"graph:{other}:{memory.get('key')}"
                evidence[memory_key] = (
                    f"{memory.get('stance', 'like')}:{memory.get('label', '')}"
                )
                evidence_owners[memory_key] = other
            if reason:
                reason_key = f"match:{matched['_id']}:reason"
                evidence[reason_key] = reason
                evidence_owners[reason_key] = "relationship"
            if shared_summary:
                summary_key = f"relationship:{matched['_id']}:summary"
                evidence[summary_key] = shared_summary
                evidence_owners[summary_key] = "relationship"
            for index, chat_message in enumerate(recent_messages):
                chat_key = f"relationship:{matched['_id']}:chat:{index}"
                sender_id = chat_message.get("sender_id")
                evidence[chat_key] = f"{sender_id}: {chat_message.get('content', '')}"
                evidence_owners[chat_key] = sender_id
            evidence_catalog.update(evidence)
            relationship_lines.append({
                "other_id": other, "match_id": str(matched["_id"]),
                "owner_user_id": other,
                "status": "accepted", "match_reason": reason,
                "public_interest": other_doc.get("initial_interest"),
                "public_context": other_doc.get("current_context"),
                "public_personality": (other_doc.get("big_five") or {}).get("summary"),
                "deep_profile": other_doc.get("deep_profile", {}),
                "graph_memories": other_graph,
                "score_breakdown": matched.get("score_breakdown", {}),
                "shared_summary": shared_summary,
                "shared_message_count": matched.get("shared_message_count", 0),
                "mediator_progress": state,
                "latest_shared_messages": recent_messages,
                "evidence": evidence,
                "evidence_owners": {
                    key: evidence_owners.get(key) for key in evidence
                },
            })
        active_proposals = []
        for matched in matches_coll.find({
            "status": {"$in": ["draft", "pending"]},
            "$or": [{"from_user": req.user_id}, {"to_user": req.user_id}]
        }):
            other = matched["to_user"] if matched["from_user"] == req.user_id else matched["from_user"]
            active_proposals.append({
                "other_id": other, "status": matched.get("status"),
                "label": "目前提案", "match_reason": matched.get("reason", "")
            })
        relationship_context = json.dumps(relationship_lines, ensure_ascii=False)
        proposal_context = json.dumps(active_proposals, ensure_ascii=False)
        memory_summary = (user_doc or {}).get("profile_memory_summary", "目前還沒有整理好的長期記憶")

        unsupported_fact = unsupported_relationship_fact(
            req.message, evidence_catalog, evidence_owners, explicit_mentions
        )
        deterministic_relationship_reply = None
        if relationship_query and unsupported_fact:
            if relationship_lines:
                known_ids = "、".join("@" + item["other_id"] for item in relationship_lines)
                deterministic_relationship_reply = (
                    f"這件事我目前沒有足夠證據能確認：{unsupported_fact}。"
                    f"我看得到的資料主要來自 {known_ids}，所以先不亂猜。"
                )
            else:
                deterministic_relationship_reply = "我目前沒有足夠的已驗證資料可以判斷，先不亂猜比較好。"
        elif comparison_query:
            deterministic_relationship_reply = grounded_relationship_fallback(relationship_lines, True)
        if deterministic_relationship_reply is not None:
            save_message(room_id, "ai_assistant", deterministic_relationship_reply)
            return with_risk({
                "reply": deterministic_relationship_reply,
                "is_locked": False,
                "match_readiness_score": 0,
                "match_readiness_state": "learning",
                "conversation_intent": "relationship_chat",
                "mentioned_other_ids": [item["other_id"] for item in relationship_lines],
                "context_changed": False,
                "context_confirmation_needed": False,
            })

        sys_prompt = f"""
{MEDIATOR_PERSONA}
媒人語氣：{mediator_style(req.user_id)}

你是阿月，一個會聊天、會觀察契機的媒人。你的目標不是硬推配對，而是在自然聊天中理解使用者最近想做什麼、何時想做、是否想找人一起。

使用者資料：
- Big Five 摘要：{bf.get('summary', '未知')}
- 目前近期情境：{current_context}
- 長期記憶摘要：{memory_summary}
- 已接受配對與證據：{relationship_context}
- 進行中的提案：{proposal_context}
- 使用者明確提到的對象：{explicit_mentions or []}

聊天規則：
1. 回覆要像真人媒人，短句為主但不要固定句數；可以一句收、兩句接住、偶爾三句展開，依情緒與資訊量自然變化。
2. 如果使用者只是閒聊，要接住話題並自然延伸，不要把話聊死；不要每次都用「先共感一句 + 追問一句」的固定模板。
3. 如果使用者透露近期想做的事，要萃取 context_signals：activity、timing、preference、companion_intent。沒有明講就填 null。
4. 在這個 App 裡，使用者多半是來讓阿月留意人選；除非他明講想一個人，否則不要一直問「要不要找人」，而是自然把近期活動視為可能的牽線契機。
5. 當 activity + timing 已明確且活動適合社交時，不要再問「你想自己去還是找人」；改成媒人式主動提議：「這種局我可以幫你留意一個合拍的人，要不要我開始翻名單？」。
5-1. 如果使用者明講想一個人、放空、不要人陪，或情緒明顯脆弱，先陪聊，不要推媒合。
6. 如果使用者已明確請你找人，conversation_intent 可為 command，但不要自己承諾已經開始，系統會另外觸發。
7. 如果在聊已接受配對對象，只能根據 evidence_ids 支持的資料說，不要編造。
8. 如果使用者明確要求阿月開始找配對/找人陪/幫忙介紹人，explicit_match_request 設為 true；只是聊想做某件事但還沒要求找人時設為 false。
9. 如果使用者要求帥、正、好看、身高等外貌條件，不要假裝知道候選人的外貌；請把需求轉成資料可支持的氣質/風格偏好，例如乾淨感、自信、成熟、外向、會打扮，並可簡短提醒「外貌我不能亂保證」。這類明確找人請求仍可把 explicit_match_request 設為 true。
10. 明確找人請求如果包含新的活動、時間、偏好或外貌/氣質條件，也要把 context_should_update 設為 true，並在 context_summary/context_signals 放入這次要找人的條件。
11. 「取消、撤回、不要這條」本身不是開始找人；只有使用者明確說「取消後重新找、換一個、重新配」才是重新配對。

請只輸出 JSON：
{{
  "reply": "給使用者看的回覆",
  "conversation_intent": "recent_context|relationship_chat|profile_fact|casual_chat|command",
  "explicit_match_request": false,
  "mentioned_other_ids": [],
  "evidence_ids": [],
  "relationship_claims": [],
  "context_should_update": false,
  "context_confidence": 0.0,
  "context_summary": "若需要更新近期情境，放一句摘要，否則空字串",
  "context_signals": {{
    "activity": null,
    "timing": null,
    "preference": null,
    "companion_intent": null
  }}
}}
"""
        prompt = sys_prompt + "\n\n最近聊天紀錄：\n"
        for m in history:
            speaker = "使用者" if m["sender_id"] == req.user_id else "阿月"
            prompt += f"{speaker}: {m['content']}\n"

        prompt += "\n請依照 JSON 格式回覆。"
            
        # Add the current message which is already in history because of save_message above
        
        try:
            ai_res_str = generate_chat_completion(
                prompt,
                temperature=0.2 if relationship_query else 0.6,
                json_output=True,
                model=OLLAMA_FAST_CHAT_MODEL,
            )
            ai_res = json.loads(ai_res_str)
            ai_reply = ai_res.get("reply", "收到。")
            is_locked = False
            intent = ai_res.get("conversation_intent", "casual_chat")
            referenced_user_ids = []
            for other_id in explicit_mentions + (ai_res.get("mentioned_other_ids") or []):
                if other_id in accepted_ids and other_id not in referenced_user_ids:
                    referenced_user_ids.append(other_id)
            valid_claim_subjects, evidence_ids = validate_relationship_claims(
                ai_res.get("relationship_claims") or [],
                accepted_ids,
                evidence_catalog,
                evidence_owners,
            )
            if not relationship_query:
                evidence_ids = [
                    evidence_id for evidence_id in (ai_res.get("evidence_ids") or [])
                    if evidence_id in evidence_catalog
                ]
            try:
                context_confidence = float(ai_res.get("context_confidence", 0))
            except (TypeError, ValueError):
                context_confidence = 0.0
            if context_confidence > 1:
                context_confidence /= 100
            context_confidence = max(0.0, min(1.0, context_confidence))
            explicit_context = any(p in req.message for p in ("更新近況", "記一下", "幫我記", "最近我"))
            explicit_no_memory = any(p in req.message for p in ("不要記", "別記", "不用記"))
            explicit_match_request = bool(ai_res.get("explicit_match_request")) and not relationship_query
            if bool(ai_res.get("explicit_match_request")) and intent == "command":
                explicit_match_request = True
            context_intent_can_update = intent == "recent_context" or explicit_match_request
            context_should_update = bool(ai_res.get("context_should_update")) and context_intent_can_update and context_confidence >= 0.85
            if explicit_context:
                context_should_update = True
                intent = "recent_context"
            if explicit_no_memory or ((explicit_mentions or referenced_user_ids) and not explicit_context):
                context_should_update = False
            candidate_ctx = ai_res.get("context_summary") or current_context
            context_changed = bool(context_should_update and candidate_ctx != current_context)
            new_ctx = candidate_ctx if context_changed else current_context
            old_revision = int((user_doc or {}).get("current_context_revision", 0))
            new_revision = old_revision + 1 if context_changed else old_revision
            context_signals = ai_res.get("context_signals", {}) if context_intent_can_update else {}
            readiness_score, missing_signals = deterministic_readiness(intent, context_signals)
            signal_labels = {
                "activity": "想做的事", "timing": "時間", "preference": "偏好",
                "companion_intent": "是否想找人一起"
            }
            readiness_reason = (
                "已經足夠判斷可詢問是否開始找人"
                if readiness_score >= MATCH_READINESS_THRESHOLD
                else "還缺：" + "、".join(signal_labels[key] for key in missing_signals[:2])
            )
            update_fields = {"match_readiness_score": readiness_score,
                "match_readiness_reason": readiness_reason,
                "context_signals": context_signals,
                "last_conversation_intent": intent, "ai_chat_locked": False}
            increments = {"ai_chat_interaction_count": 1}
            if context_changed:
                update_fields.update({"current_context": new_ctx, "current_context_revision": new_revision,
                                      "previous_context": current_context})
            profiles_coll.update_one({"user_id": req.user_id}, {"$set": update_fields, "$inc": increments}, upsert=True)

            active_match = matches_coll.find_one(active_user_match_query(req.user_id))
            last_auto_revision = (user_doc or {}).get("last_auto_match_revision")
            matching_in_progress = bool((user_doc or {}).get("matchmaking_in_progress"))
            if explicit_match_request:
                if active_match:
                    from models import AcceptRequest
                    from routers.match import decline_match, derive_match_stage
                    stage = derive_match_stage(active_match, req.user_id)
                    other_id = (
                        active_match.get("to_user")
                        if active_match.get("from_user") == req.user_id
                        else active_match.get("from_user")
                    )
                    if stage == "waiting_user":
                        decline_match(
                            AcceptRequest(user_id=req.user_id, match_id=str(active_match["_id"])),
                            background_tasks,
                        )
                        profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                            "status": "queued", "source": "explicit_next",
                            "context_revision": new_revision,
                            "requested_at": time.time()}}})
                        background_tasks.add_task(trigger_proactive_match, req.user_id, "explicit_next", True)
                        ai_reply = f"好，我先把上一張 @{other_id} 收掉，改照你現在這個方向重新找。"
                        status = "queued"
                    elif stage == "waiting_other":
                        ai_reply = f"這條我已經幫你問出去了，現在等 @{other_id} 點頭。等這條有結果後，我再幫你開下一局。"
                        status = stage
                    elif stage == "incoming_decision":
                        ai_reply = f"現在有 @{other_id} 的邀請在等你決定，先處理這張比較順。"
                        status = stage
                    else:
                        ai_reply = "你現在已經有一個進行中的配對了，我先不重複翻名單。"
                        status = stage
                elif matching_in_progress:
                    ai_reply = "我已經在翻名單了，先等這輪結果回來。"
                    status = "already_searching"
                else:
                    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                        "status": "queued", "source": "explicit_next",
                        "context_revision": new_revision,
                        "requested_at": time.time()}}})
                    background_tasks.add_task(trigger_proactive_match, req.user_id, "explicit_next", True)
                    ai_reply = "好，我現在真的開始翻名單；有找到或這輪沒有，我都會回來說。"
                    status = "queued"
                save_message(room_id, "ai_assistant", ai_reply)
                background_tasks.add_task(observe_user_memory, req.user_id, req.message, "global")
                return with_risk({"reply": ai_reply, "is_locked": False, "mode": "match_request", "match_request_status": status})
            if (context_changed and readiness_score >= MATCH_READINESS_THRESHOLD and not active_match
                    and last_auto_revision != new_revision and not matching_in_progress):
                try:
                    context_embedding = get_embedding(new_ctx)
                    profiles_coll.update_one({"user_id": req.user_id}, {"$set": {
                        "context_embedding": context_embedding, "last_auto_match_revision": new_revision}}, upsert=True)
                except HTTPException as e:
                    print(f"Embedding skipped in direct_chat: {e.detail}")
                profiles_coll.update_one({"user_id": req.user_id}, {"$set": {"match_search": {
                    "status": "awaiting_confirmation", "source": "automatic",
                    "updated_at": time.time()
                }}})
                if contains_question(ai_reply):
                    ai_reply = remove_question_sentences(ai_reply) or "我大概知道你現在想找什麼樣的人了。"
                ai_reply = (
                    ai_reply.rstrip()
                    + "\n\n我現在大概知道要找什麼人了。要我開始翻名單嗎？"
                )
            elif (
                intent == "recent_context"
                and readiness_score < MATCH_READINESS_THRESHOLD
                and not active_match
                and not matching_in_progress
                and not relationship_query
            ):
                followup = social_opportunity_followup(context_signals, missing_signals)
                if followup and followup not in ai_reply and not contains_question(ai_reply):
                    ai_reply = ai_reply.rstrip()
                    if not ai_reply.endswith(("。", "？", "?", "！", "!")):
                        ai_reply += "。"
                    ai_reply += "\n" + followup
            compare_grounding_ok = (
                len(referenced_user_ids) >= min(2, len(relationship_lines))
                and all(f"@{other_id}" in ai_reply for other_id in referenced_user_ids)
                and all(other_id in valid_claim_subjects for other_id in referenced_user_ids)
            )
            relationship_grounding_ok = (
                bool(referenced_user_ids)
                and all(f"@{other_id}" in ai_reply for other_id in referenced_user_ids)
                and all(other_id in valid_claim_subjects for other_id in referenced_user_ids)
            )
            if relationship_query and not (
                compare_grounding_ok if comparison_query and len(relationship_lines) > 1
                else relationship_grounding_ok
            ):
                ai_reply = grounded_relationship_fallback(relationship_lines, comparison_query)
                referenced_user_ids = [item["other_id"] for item in relationship_lines]
            unsupported_fact = unsupported_relationship_fact(
                req.message, evidence_catalog, evidence_owners, explicit_mentions
            )
            if relationship_query and unsupported_fact:
                known_ids = "、".join("@" + item["other_id"] for item in relationship_lines)
                ai_reply = (
                    f"這件事我目前沒有足夠證據能確認：{unsupported_fact}。"
                    f"我看得到的資料主要來自 {known_ids}，所以先不亂猜。"
                )
            if comparison_query and not unsupported_fact:
                ai_reply = grounded_relationship_fallback(relationship_lines, True)
            if not explicit_no_memory:
                background_tasks.add_task(observe_user_memory, req.user_id, req.message, "global")
                    
        except Exception as e:
            print(f"Chat error (AI): {e}")
            ai_reply = "銝末????冽?暺頝荔?隢?敺?閰佗?"
            is_locked = False
            
        save_message(room_id, "ai_assistant", ai_reply)
        return with_risk({
            "reply": ai_reply,
            "is_locked": is_locked,
            "match_readiness_score": locals().get("readiness_score", 0),
            "match_readiness_state": (
                "ready" if locals().get("readiness_score", 0) >= MATCH_READINESS_THRESHOLD else "learning"
            ),
            "conversation_intent": locals().get("intent", "casual_chat"),
            "mentioned_other_ids": locals().get("referenced_user_ids", []),
            "context_changed": locals().get("context_changed", False),
            "context_confirmation_needed": bool(locals().get("intent") == "recent_context" and 0.65 <= locals().get("context_confidence", 0) < 0.85)
        })
        
    else:
        match_doc = matches_coll.find_one({
            "status": "accepted",
            "$or": [
                {"from_user": req.user_id, "to_user": req.contact_id},
                {"from_user": req.contact_id, "to_user": req.user_id}
            ]
        })
        message_count = mark_post_chat_activity(match_doc, room_id)
        if match_doc and message_count >= 6:
            background_tasks.add_task(summarize_relationship, match_doc["_id"], room_id)
        return with_risk({"reply": None, "message_saved": True, "feedback_scheduled": bool(match_doc)})

@router.post("/risk/feedback")
def submit_risk_feedback(req: RiskFeedbackRequest):
    success = risk_client.submit_feedback(
        triggered_by_msg_id=req.triggered_by_msg_id,
        role=req.role,
        feedback=req.feedback,
    )
    return {"status": "success" if success else "failed"}


@router.post("/guidance/activity")
def report_guidance_activity(req: GuidanceActivityRequest):
    now = time.time()
    room_id = generate_room_id(req.user_id, req.contact_id)
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {
            "last_user_activity_at": now,
            "guidance.last_activity_at": now,
            "guidance.last_contact_id": req.contact_id,
            "guidance.last_room_id": room_id,
        }},
        upsert=True,
    )
    return {"status": "success"}


@router.get("/guidance/status")
def get_guidance_status(user_id: str, contact_id: str):
    doc = profiles_coll.find_one(
        {"user_id": user_id},
        {"guidance": 1, "mediator_tone": 1, "probe_mode": 1},
    ) or {}
    return {
        "status": "available",
        "enabled": True,
        "contact_id": contact_id,
        "last_activity_at": (doc.get("guidance") or {}).get("last_activity_at"),
        "mediator_tone": doc.get("mediator_tone", "friend"),
        "probe_mode": doc.get("probe_mode", "balanced"),
    }


@router.post("/guidance/suggestion")
def get_guidance_suggestion(req: GuidanceSuggestionRequest):
    room_id = generate_room_id(req.user_id, req.contact_id)
    recent = list(messages_coll.find(
        {"room_id": room_id},
        {"_id": 0, "sender_id": 1, "content": 1},
    ).sort("timestamp", -1).limit(8))[::-1]
    history = "\n".join(
        f"{item.get('sender_id', 'unknown')}: {item.get('content', '')}"
        for item in recent
    )
    input_text = (req.input_text or "").strip()
    prompt = f"""
{MEDIATOR_PERSONA}
你要幫使用者產生一句可以直接貼進聊天框的自然回覆。

限制：
- 只輸出一句中文。
- 不要解釋策略。
- 不要太油、不要冒犯、不要暴露你看過後台資料。
- 如果資訊不足，就給一個低壓、自然延續對話的回覆。

最近聊天：
{history or "目前沒有足夠聊天紀錄。"}

使用者目前草稿或需求：
{input_text or "請根據目前聊天狀態，給我一個自然、不尷尬的回覆方向。"}
"""
    try:
        suggestion = generate_chat_completion(
            prompt,
            temperature=0.45,
            json_output=False,
            model=OLLAMA_FAST_CHAT_MODEL,
        ).strip()
    except Exception as e:
        print(f"Guidance suggestion failed: {e}")
        suggestion = "可以先順著對方剛剛說的點回一句，再輕鬆問一個小問題。"
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {
            "guidance.last_suggestion_at": time.time(),
            "guidance.last_contact_id": req.contact_id,
        }},
        upsert=True,
    )
    return {
        "suggestion": suggestion,
        "ui_nudge": suggestion,
        "audit_trail": "local_guidance_v1",
        "suggestion_id": str(uuid.uuid4()),
        "role": "message_reply",
    }

@router.get("/contacts")
def get_contacts(user_id: str):
    default_contacts = [
        {"id": "ai_assistant", "name": "阿月", "role": "system", "context": "你的媒人助理，會邊聊天邊幫你留意合適的人。", "is_locked": False}
    ]
    try:
        user_doc = profiles_coll.find_one({"user_id": user_id})
    except PyMongoError as e:
        print(f"Failed to load contacts user profile for {user_id}: {e}")
        return {"contacts": default_contacts}

    ai_locked = user_doc.get("ai_chat_locked", False) if user_doc else False

    query = {
        "status": "accepted",
        "$or": [{"from_user": user_id}, {"to_user": user_id}]
    }
    try:
        matches = list(matches_coll.find(query))
    except PyMongoError as e:
        print(f"Failed to load accepted contacts for {user_id}: {e}")
        default_contacts[0]["is_locked"] = ai_locked
        return {"contacts": default_contacts}

    contacts = [
        {"id": "ai_assistant", "name": "阿月", "role": "system", "context": "你的媒人助理，會邊聊天邊幫你留意合適的人。", "is_locked": ai_locked}
    ]
    
    for m in matches:
        other_id = m["to_user"] if m["from_user"] == user_id else m["from_user"]
        unread_role = "from" if m.get("from_user") == user_id else "to"
        mediator_unread_count = int((m.get("private_unread", {}) or {}).get(unread_role, 0) or 0)
        try:
            other_doc = profiles_coll.find_one({"user_id": other_id})
        except PyMongoError as e:
            print(f"Failed to load contact profile {other_id}: {e}")
            other_doc = None
        ctx = other_doc.get("current_context", "尚無近期情境") if other_doc else "尚無近期情境"
        contacts.append({
            "id": other_id,
            "name": profile_display_name(other_doc, other_id),
            "role": "user",
            "context": ctx,
            "mediator_unread_count": mediator_unread_count,
            "private_unread_count": mediator_unread_count
        })
        
    return {"contacts": contacts}

def generate_mediator_private_room_id(user_id: str, other_id: str):
    return f"mediator_private::{user_id}::{other_id}"

def find_accepted_match(user_id: str, other_id: str):
    return matches_coll.find_one({
        "status": "accepted",
        "$or": [
            {"from_user": user_id, "to_user": other_id},
            {"from_user": other_id, "to_user": user_id}
        ]
    })

@router.get("/mediator/private/{other_id}")
def get_mediator_private_messages(other_id: str, user_id: str):
    match_doc = find_accepted_match(user_id, other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能查看已接受配對的媒人私聊")
    room_id = generate_mediator_private_room_id(user_id, other_id)
    if messages_coll.count_documents({"room_id": room_id}) == 0:
        save_message(room_id, "ai_assistant", f"這裡可以私下跟我打聽 {other_id}，我會盡量只講有根據的事。")
    unread_field = relationship_unread_field(match_doc, user_id)
    unread_count = int((match_doc.get("private_unread", {}) or {}).get(
        "from" if match_doc.get("from_user") == user_id else "to", 0
    ))
    matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {unread_field: 0}})
    user_doc = profiles_coll.find_one({"user_id": user_id}) or {}
    pending = user_doc.get("pending_private_feedback") or {}
    if pending.get("other_id") != other_id:
        pending = {}
    pending_date = user_doc.get("pending_date_coordination") or {}
    if pending_date.get("other_id") != other_id:
        pending_date = {}
    msgs = list(messages_coll.find({"room_id": room_id}, {"_id": 0}).sort("timestamp", 1))
    return {
        "messages": msgs,
        "other_id": other_id,
        "unread_count": unread_count,
        "pending_step": pending.get("stage") or (
            "date_" + pending_date.get("stage") if pending_date.get("stage") else None
        ),
        "probe_state": participant_probe_state(match_doc, user_id),
        "other_probe_state": participant_probe_state(match_doc, other_id),
        "mediator_tone": user_doc.get("mediator_tone", "friend")
    }

def consent_intent(message: str):
    try:
        prompt = f"""
請判斷使用者是否同意媒人把訊息/回饋轉述或分享給另一位配對對象。
只輸出 JSON：{{"consent":true|false|null,"confidence":0.0}}
使用者訊息：{message}
"""
        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True))
        if float(result.get("confidence", 0)) < 0.55:
            return None
        consent = result.get("consent")
        if consent is True:
            return True
        if consent is False:
            return False
    except Exception as e:
        print(f"Consent intent classification failed: {e}")
    return None

def save_private_mediator_reply(room_id: str, reply: str, event_type="text", actions=None):
    message_type = "mediator_card" if actions else "text"
    save_message(
        room_id, "ai_assistant", reply, message_type=message_type,
        metadata={"event_type": event_type, "actions": actions or []}
    )

def deliver_consented_signal(match_doc: dict, feedback_user: str, requester_id: str, sentiment: str):
    if sentiment == "positive":
        queue_mediator_event(
            requester_id,
            f"我幫你問到一點好消息：{feedback_user} 對你的感覺是正向的，可以自然繼續聊。",
            "probe_result", match_id=str(match_doc["_id"]), other_id=feedback_user
        )
    elif sentiment == "negative":
        queue_mediator_event(
            requester_id,
            "我幫你探過了，對方目前沒有想往前推。我會幫你們自然收住，不硬撮合。",
            "gentle_closure", match_id=str(match_doc["_id"]), other_id=feedback_user
        )

def request_relationship_probe(
    user_id: str, other_id: str, force: bool = False, requested_kind: str | None = None
):
    match_doc = find_accepted_match(user_id, other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能對已接受配對打聽")
    target_state = participant_probe_state(match_doc, other_id)
    status = target_state.get("status", "idle")
    now, count = time.time(), int(match_doc.get("shared_message_count", 0))
    if status in PROBE_IN_FLIGHT_STATUSES and float(target_state.get("asked_at", now)) > now - PROBE_PENDING_TTL:
        return {"status": "already_pending", "reply": "我已經在幫你問了，先等對方回覆。"}
    new_messages = count - int(target_state.get("message_count_snapshot", 0))
    if status == "completed" and new_messages < 6:
        return {"status": "recently_completed", "reply": "我剛幫你問過一次，先讓你們多聊幾句再打聽會比較自然。"}
    if status == "completed" and now < float(target_state.get("cooldown_until", 0)) and not force:
        return {"status": "needs_confirmation", "reply": "我剛打聽過，現在再問可能有點密。你確定要我再問一次嗎？"}
    _, _, _, cooldown = probe_policy(user_id)
    kind = choose_probe_kind(match_doc, requested_kind)
    question = PROBE_QUESTIONS[kind]
    probe_id = uuid.uuid4().hex
    state = {"status": "queued", "trigger": "manual", "requester_id": user_id,
             "probe_id": probe_id,
             "kind": kind, "question": question,
             "asked_at": now, "message_count_snapshot": count, "cooldown_until": now + cooldown}
    state_field = participant_probe_field(match_doc, other_id)
    claimed = matches_coll.update_one(
        {
            "_id": match_doc["_id"],
            "$or": [
                {f"{state_field}.status": {"$nin": list(PROBE_IN_FLIGHT_STATUSES)}},
                {f"{state_field}.asked_at": {"$lt": now - PROBE_PENDING_TTL}},
            ],
        },
        {"$set": {participant_probe_field(match_doc, other_id): state},
         "$push": {"probe_history": {
             "probe_id": probe_id,
             "kind": kind, "asked_to": other_id, "asked_at": now, "status": "queued",
             "trigger": "manual", "requester_id": user_id
         }}}
    )
    if not claimed.modified_count:
        return {"status": "already_pending", "reply": "我已經在幫你問了，先等對方回覆。"}
    queue_mediator_event(
        other_id, question, "probe_question", match_id=str(match_doc["_id"]),
        other_id=user_id, origin="probe", requester_id=user_id, probe_kind=kind,
        probe_id=probe_id
    )
    return {"status": "started", "kind": kind,
            "reply": "好，我會用不尷尬的方式幫你探一下。"}

def is_relationship_probe_request(message: str) -> bool:
    try:
        prompt = f"""
請判斷使用者是否在要求媒人私下幫忙打聽另一位配對對象，例如問對方想法、好感、近況、可不可以透露一些八卦。
只輸出 JSON：{{"probe":true|false,"confidence":0.0}}
使用者訊息：{message}
"""
        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True))
        return bool(result.get("probe")) and float(result.get("confidence", 0)) >= 0.6
    except Exception as e:
        print(f"Relationship probe classification failed: {e}")
        return False

@router.post("/mediator/probe")
def mediator_probe(req: MediatorProbeRequest):
    return request_relationship_probe(req.user_id, req.other_id, req.force, req.kind)

@router.post("/mediator/private")
def mediator_private_chat(req: MediatorPrivateRequest, background_tasks: BackgroundTasks):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能在已接受配對中私聊媒人")

    room_id = generate_mediator_private_room_id(req.user_id, req.other_id)
    save_message(room_id, req.user_id, req.message)
    safe_summary = re.sub(r"\s+", " ", req.message).strip()[:60]
    profiles_coll.update_one(
        {"user_id": req.user_id},
        {"$set": {"last_user_activity_at": time.time()}},
        upsert=True,
    )
    background_tasks.add_task(observe_user_memory, req.user_id, req.message, "relationship_private", str(match_doc["_id"]))
    user_doc = profiles_coll.find_one({"user_id": req.user_id}) or {}
    pending = user_doc.get("pending_private_feedback") or {}
    pending_matches = pending.get("match_id") == str(match_doc["_id"]) and pending.get("other_id") == req.other_id
    pending_date = user_doc.get("pending_date_coordination") or {}
    date_matches = (
        pending_date.get("match_id") == str(match_doc["_id"])
        and pending_date.get("other_id") == req.other_id
    )

    if is_date_cancellation(req.message) and (date_matches or "約" in req.message):
        role = participant_role(match_doc, req.user_id)
        date_state = match_doc.get("date_coordination", {}) or {}
        other_role = participant_role(match_doc, req.other_id)
        other_had_started = bool((date_state.get("participants", {}) or {}).get(other_role))
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$unset": {"pending_date_coordination": ""}}
        )
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {
                "$set": {
                    "date_coordination.status": "cancelled",
                    "date_coordination.cancelled_by": req.user_id,
                    "date_coordination.cancelled_at": time.time(),
                },
                "$unset": {f"date_coordination.participants.{role}": ""},
            },
        )
        if other_had_started:
            queue_mediator_event(
                req.other_id, "對方先取消這次約會協調，我們先不往下排。",
                "date_coordination_cancelled", match_id=str(match_doc["_id"]),
                other_id=req.user_id,
            )
        reply = "好，這次約會協調我先幫你取消。"
        save_private_mediator_reply(room_id, reply, "date_coordination_cancelled")
        return {"reply": reply, "pending_step": None}

    if "約" in req.message and not date_matches:
        pending_date = {
            "match_id": str(match_doc["_id"]), "other_id": req.other_id,
            "stage": "availability", "data": {}
        }
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$set": {"pending_date_coordination": pending_date}}
        )
        reply = "可以，我先幫你們對時間。你比較方便今天、明天、週末，還是晚上？"
        save_private_mediator_reply(room_id, reply, "date_coordination_request")
        return {"reply": reply, "pending_step": "date_availability"}

    if date_matches:
        stage = pending_date.get("stage", "availability")
        data = pending_date.get("data", {})
        data[stage] = normalize_date_answer(stage, req.message)
        if stage == "availability":
            pending_date.update({"stage": "activity", "data": data})
            profiles_coll.update_one(
                {"user_id": req.user_id}, {"$set": {"pending_date_coordination": pending_date}}
            )
            reply = "好，那活動想排吃飯、咖啡、運動、讀書、看電影，還是散步？"
            save_private_mediator_reply(room_id, reply, "date_coordination_request")
            return {"reply": reply, "pending_step": "date_activity"}
        if stage == "activity":
            pending_date.update({"stage": "budget", "data": data})
            profiles_coll.update_one(
                {"user_id": req.user_id}, {"$set": {"pending_date_coordination": pending_date}}
            )
            reply = "預算大概抓多少？500 以內、500 到 1000，還是 1000 以上？"
            save_private_mediator_reply(room_id, reply, "date_coordination_request")
            return {"reply": reply, "pending_step": "date_budget"}

        role = participant_role(match_doc, req.user_id)
        data["budget"] = data.pop("budget", normalize_date_answer("budget", req.message))
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {f"date_coordination.participants.{role}": data}}
        )
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$unset": {"pending_date_coordination": ""}}
        )
        refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or {}
        participants = ((refreshed.get("date_coordination") or {}).get("participants") or {})
        other_role = participant_role(match_doc, req.other_id)
        if not participants.get(other_role):
            queue_mediator_event(
                req.other_id, "對方想跟你對一下約會時間和活動，我來幫你們兩邊協調。",
                "date_coordination_request", match_id=str(match_doc["_id"]), other_id=req.user_id
            )
            reply = "好，我先幫你記下來，也會去問對方方便的時間和活動。"
        else:
            overlap = date_overlap(data, participants[other_role])
            if overlap["time"] and overlap["activity"]:
                result_text = f"我看你們可以約 {overlap['time']}，活動可以先抓 {overlap['activity']}，預算大概是 {overlap['budget']}。"
            else:
                result_text = "你們目前時間或活動還沒有完全重疊，我建議先多丟兩個備案。"
            matches_coll.update_one(
                {"_id": match_doc["_id"]},
                {"$set": {"date_coordination.status": "completed",
                          "date_coordination.overlap": overlap,
                          "date_coordination.completed_at": time.time()}}
            )
            queue_mediator_event(
                req.other_id, result_text, "date_coordination_result",
                match_id=str(match_doc["_id"]), other_id=req.user_id
            )
            reply = result_text
        save_private_mediator_reply(room_id, reply, "date_coordination_result")
        return {"reply": reply, "pending_step": None}

    is_probe_command = is_relationship_probe_request(req.message)
    if pending_matches and pending.get("stage") == "probe_answer":
        kind = pending.get("kind", "fun_fact")
        requester_id = pending.get("requester_id")
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {
                participant_probe_field(match_doc, req.user_id) + ".status": "completed",
                participant_probe_field(match_doc, req.user_id) + ".completed_at": time.time(),
                participant_probe_field(match_doc, req.user_id) + ".shared_summary": safe_summary,
            }, "$push": {"probe_history": {
                "kind": kind, "asked_to": req.user_id, "answered_at": time.time(),
                "status": "completed", "shared_summary": safe_summary
            }}}
        )
        profiles_coll.update_one({"user_id": req.user_id}, {"$unset": {"pending_private_feedback": ""}})
        if requester_id and requester_id == req.other_id:
            queue_mediator_event(
                requester_id, f"對方回覆我了：{safe_summary}",
                "probe_result", match_id=str(match_doc["_id"]), other_id=req.user_id,
                probe_kind=kind
            )
        reply = "收到，我會幫你整理成不尷尬的說法。"
        save_private_mediator_reply(room_id, reply)
        return {"reply": reply, "pending_step": None, "probe_kind": kind}

    if pending_matches and pending.get("stage") == "sentiment" and is_probe_command:
        probe_result = request_relationship_probe(req.user_id, req.other_id, False, "sentiment")
        save_private_mediator_reply(room_id, probe_result["reply"])
        return {"reply": probe_result["reply"], "pending_step": "sentiment", "probe_status": probe_result["status"]}

    if pending_matches and pending.get("stage") == "sentiment":
        sentiment = classify_feedback(req.message)
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {f"private_feedback.{req.user_id}": {
                "sentiment": sentiment, "share_consent": None, "updated_at": time.time()
            }}}
        )
        pending["stage"] = "consent"
        pending["sentiment"] = sentiment
        matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {
            participant_probe_field(match_doc, req.user_id) + ".status": "awaiting_consent",
            participant_probe_field(match_doc, req.user_id) + ".sentiment": sentiment}})
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$set": {"pending_private_feedback": pending}}
        )
        reply = "這段回饋要不要讓我用比較溫和的方式轉述給對方？"
        actions = [
            {"label": "可以轉述", "value": "可以，你幫我整理後轉述"},
            {"label": "不要轉述", "value": "先不要，這只給你知道"}
        ]
        save_private_mediator_reply(room_id, reply, "feedback_consent_request", actions)
        return {"reply": reply, "pending_step": "consent", "actions": actions}

    if pending_matches and pending.get("stage") == "consent":
        consent = consent_intent(req.message)
        if consent is None:
            reply = "我怕誤會你，這段要不要讓我轉述給對方？可以直接說可以或先不要。"
            save_private_mediator_reply(room_id, reply)
            return {"reply": reply, "pending_step": "consent"}
        sentiment = pending.get("sentiment", "neutral")
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {
                f"private_feedback.{req.user_id}.share_consent": consent,
                f"private_feedback.{req.user_id}.updated_at": time.time()
            }}
        )
        profiles_coll.update_one({"user_id": req.user_id}, {"$unset": {"pending_private_feedback": ""}})
        matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {
            participant_probe_field(match_doc, req.user_id) + ".status": "completed",
            participant_probe_field(match_doc, req.user_id) + ".sentiment": sentiment,
            participant_probe_field(match_doc, req.user_id) + ".share_consent": consent,
            participant_probe_field(match_doc, req.user_id) + ".completed_at": time.time()}})
        requester_id = pending.get("requester_id")
        if consent and requester_id and requester_id == req.other_id:
            deliver_consented_signal(match_doc, req.user_id, requester_id, sentiment)
        refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or {}
        feedback = refreshed.get("private_feedback", {}) or {}
        mine = feedback.get(req.user_id, {}) or {}
        theirs = feedback.get(req.other_id, {}) or {}
        if (mine.get("sentiment") == "positive" and mine.get("share_consent") is True
                and theirs.get("sentiment") == "positive" and theirs.get("share_consent") is True):
            queue_mediator_event(
                req.user_id, f"我兩邊都確認過了，{req.other_id} 對你也有好感。可以自然多聊一點。",
                "mutual_interest", match_id=str(match_doc["_id"]), other_id=req.other_id
            )
        reply = "好，我會幫你溫和轉述。" if consent else "收到，我只把這個當成你私下跟我說的，不會轉述。"
        save_private_mediator_reply(room_id, reply)
        return {"reply": reply, "pending_step": None}

    asks_about_feelings = is_probe_command
    if asks_about_feelings:
        requested_kind = "fun_fact" if any(word in req.message for word in ("有趣", "八卦", "小事", "話題")) else "sentiment"
        probe_result = request_relationship_probe(req.user_id, req.other_id, False, requested_kind)
        reply = probe_result["reply"]
    else:
        feedback = match_doc.get("private_feedback", {}) or {}
        consented_signal = None
        other_feedback = feedback.get(req.other_id, {}) or {}
        if other_feedback.get("share_consent") is True:
            consented_signal = other_feedback.get("sentiment")
        relationship_context = {
            "viewer": mediator_profile_context(req.user_id, req.message),
            "partner": mediator_profile_context(req.other_id, req.message),
            "relationship": {
                "match_id": str(match_doc["_id"]),
                "match_reason_for_viewer": (
                    match_doc.get("reason") if match_doc.get("from_user") == req.user_id
                    else match_doc.get("receiver_reason", match_doc.get("reason"))
                ),
                "validated_reason_items": match_doc.get("reason_items", []),
                "shared_chat_summary": match_doc.get("relationship_memory", {}),
                "latest_shared_chat": latest_shared_chat(match_doc, 16),
                "viewer_mediator_state": participant_probe_state(match_doc, req.user_id),
                "partner_mediator_state": participant_probe_state(match_doc, req.other_id),
                "partner_consented_signal": consented_signal,
            },
        }
        prompt = f"""
{MEDIATOR_PERSONA}
媒人語氣：{mediator_style(req.user_id)}
你正在跟使用者私下聊另一位已配對對象。請像媒人一樣回答，但只能根據提供的資料，不要編造對方隱私或心意。

資料：
{json.dumps(relationship_context, ensure_ascii=False)}

使用者訊息：{req.message}
"""
        try:
            reply = generate_chat_completion(prompt, temperature=0.35, json_output=False)
        except Exception as e:
            print(f"Mediator private chat error: {e}")
            reply = "我剛剛有點卡住，等我一下再幫你整理得更清楚。"

    save_private_mediator_reply(room_id, reply)
    return {"reply": reply, "pending_step": None}


def _public_quiz_state(match_doc: dict, user_id: str):
    games = match_doc.get("relationship_games", {}) or {}
    quiz = games.get("compatibility_quiz", {}) or {}
    if quiz.get("status") == "active" and quiz.get("expires_at", 0) < time.time():
        quiz = {**quiz, "status": "expired"}
        matches_coll.update_one(
            {"_id": match_doc["_id"]},
            {"$set": {"relationship_games.compatibility_quiz.status": "expired"}},
        )
    answers = quiz.get("answers", {}) or {}
    return {
        "status": quiz.get("status", "idle"),
        "round_id": quiz.get("round_id"),
        "questions": quiz.get("questions", QUIZ_QUESTIONS),
        "my_answers": answers.get(user_id, {}),
        "my_completed": user_id in answers,
        "waiting_for_partner": quiz.get("status") == "active" and user_id in answers,
        "result": quiz.get("result") if quiz.get("status") == "completed" else None,
        "topic_box": games.get("topic_box", {}),
    }


@router.get("/relationship/fun/{other_id}")
def relationship_fun_state(other_id: str, user_id: str):
    match_doc = find_accepted_match(user_id, other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能在已接受配對中使用互動小遊戲")
    return _public_quiz_state(match_doc, user_id)


@router.post("/relationship/quiz/start")
def start_relationship_quiz(req: RelationshipGameRequest):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能在已接受配對中使用互動小遊戲")
    current = ((match_doc.get("relationship_games", {}) or {}).get("compatibility_quiz", {}) or {})
    if current.get("status") == "active" and current.get("expires_at", 0) >= time.time():
        return _public_quiz_state(match_doc, req.user_id)
    quiz = {
        "round_id": f"{int(time.time())}-{str(match_doc['_id'])[-6:]}",
        "status": "active",
        "started_by": req.user_id,
        "started_at": time.time(),
        "expires_at": time.time() + QUIZ_TTL_SECONDS,
        "questions": QUIZ_QUESTIONS,
        "answers": {},
    }
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"relationship_games.compatibility_quiz": quiz}},
    )
    queue_mediator_event(
        req.other_id, f"{req.user_id} 邀請你一起玩默契小測驗，看看你們有哪些地方合拍。",
        "compatibility_quiz_invite", match_id=str(match_doc["_id"]), other_id=req.user_id,
    )
    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or match_doc
    return _public_quiz_state(refreshed, req.user_id)


@router.post("/relationship/quiz/answer")
def answer_relationship_quiz(req: RelationshipQuizAnswerRequest):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能在已接受配對中使用互動小遊戲")
    quiz = ((match_doc.get("relationship_games", {}) or {}).get("compatibility_quiz", {}) or {})
    if quiz.get("status") != "active" or quiz.get("expires_at", 0) < time.time():
        raise HTTPException(status_code=409, detail="這輪測驗已經結束，請重新開始")
    valid_answers = {}
    for question in quiz.get("questions", QUIZ_QUESTIONS):
        answer = req.answers.get(question["id"])
        if answer not in question["options"]:
            raise HTTPException(status_code=422, detail=f"答案不在選項內：{question['text']}")
        valid_answers[question["id"]] = answer
    answers = dict(quiz.get("answers", {}) or {})
    answers[req.user_id] = valid_answers
    quiz["answers"] = answers

    participants = {match_doc["from_user"], match_doc["to_user"]}
    if participants.issubset(answers.keys()):
        first, second = match_doc["from_user"], match_doc["to_user"]
        matches = []
        for question in quiz.get("questions", QUIZ_QUESTIONS):
            question_id = question["id"]
            if answers[first][question_id] == answers[second][question_id]:
                matches.append({
                    "question_id": question_id,
                    "question": question["text"],
                    "answer": answers[first][question_id],
                })
        quiz["status"] = "completed"
        quiz["completed_at"] = time.time()
        quiz["result"] = {"match_count": len(matches), "matches": matches, "total": len(QUIZ_QUESTIONS)}
        summary = (
            f"你們這輪默契測驗有 {len(matches)} 題答得一樣："
            + ("、".join(item["answer"] for item in matches) if matches else "這次沒有一樣的答案，但也可以當成新的聊天話題。")
        )
        save_message(
            generate_room_id(first, second), "ai_assistant", summary,
            message_type="mediator_card",
            metadata={"event_type": "compatibility_quiz_result", "result": quiz["result"]},
        )
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"relationship_games.compatibility_quiz": quiz}},
    )
    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or match_doc
    return _public_quiz_state(refreshed, req.user_id)


@router.post("/relationship/quiz/cancel")
def cancel_relationship_quiz(req: RelationshipGameRequest):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能在已接受配對中取消測驗")
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {
            "relationship_games.compatibility_quiz.status": "cancelled",
            "relationship_games.compatibility_quiz.cancelled_by": req.user_id,
            "relationship_games.compatibility_quiz.cancelled_at": time.time(),
        }},
    )
    return {"status": "cancelled"}


@router.post("/relationship/topic")
def draw_relationship_topic(req: RelationshipGameRequest):
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能在已接受配對中抽話題")
    games = match_doc.get("relationship_games", {}) or {}
    quiz = games.get("compatibility_quiz", {}) or {}
    if quiz.get("status") != "completed":
        raise HTTPException(status_code=409, detail="請先完成默契測驗再抽話題")
    today = time.strftime("%Y-%m-%d", time.localtime())
    topic_box = games.get("topic_box", {}) or {}
    if topic_box.get("drawn_date") == today:
        return {"status": "already_drawn", "topic": topic_box.get("topic")}
    overlaps = (quiz.get("result", {}) or {}).get("matches", [])
    if overlaps:
        common = overlaps[0]["answer"]
        topic = f"你們剛剛都選了「{common}」。可以聊聊：為什麼這個答案最像你？"
        source = "quiz_overlap"
    else:
        reason_items = match_doc.get("reason_items", []) or []
        if reason_items:
            topic = f"我當初覺得你們合拍，是因為「{reason_items[0].get('text')}」。你們可以聊聊這點準不準。"
            source = "validated_match_reason"
        else:
            topic = "如果今天只問對方一個輕鬆但能更了解彼此的問題，你會想問什麼？"
            source = "safe_question_bank"
    topic_box = {
        "drawn_date": today, "drawn_at": time.time(), "drawn_by": req.user_id,
        "topic": topic, "source": source,
    }
    matches_coll.update_one(
        {"_id": match_doc["_id"]},
        {"$set": {"relationship_games.topic_box": topic_box}},
    )
    save_message(
        generate_room_id(match_doc["from_user"], match_doc["to_user"]),
        "ai_assistant", topic, message_type="mediator_card",
        metadata={"event_type": "topic_box", "source": source},
    )
    return {"status": "drawn", "topic": topic}


@router.get("/proactive_check")
def proactive_check(user_id: str, conversation_active: bool = False):
    user_doc = profiles_coll.find_one({"user_id": user_id})
    if not user_doc:
        return {"has_new": False}

    queue_due_feedback(user_id)

    profiles_coll.update_one(
        {"user_id": user_id, "memory_notices.0": {"$exists": True}},
        {"$set": {"memory_notices": []}},
    )

    # Deliver the highest-priority queued event. Claiming by event_id prevents
    # duplicate delivery when the same account is open in multiple tabs.
    event = claim_next_mediator_event(user_id)
    if event:
        event_type = event.get("type", "mediator_message")
        other_id = event.get("other_id")
        event_match = None
        if event.get("match_id"):
            try:
                from bson.objectid import ObjectId
                event_match = matches_coll.find_one({"_id": ObjectId(event.get("match_id"))})
            except Exception:
                event_match = None
        if not event_match and other_id:
            event_match = find_accepted_match(user_id, other_id)
        relationship_private = bool(
            other_id and event_match and event_match.get("status") == "accepted"
            and event_type in RELATIONSHIP_EVENT_TYPES
        )
        message_metadata = {
            "event_id": event.get("event_id"),
            "event_type": event_type,
            "match_id": event.get("match_id"),
            "other_id": other_id,
            "probe_id": event.get("probe_id"),
            "proposal_role": event.get("proposal_role"),
            "matches": event.get("matches", []),
            "actions": event.get("actions", [])
        }
        if relationship_private:
            room_id = generate_mediator_private_room_id(user_id, other_id)
            if event_type in {"feedback_request", "probe_question"}:
                # Older builds could enqueue the same probe on every poll. Once
                # one is claimed, discard the remaining copies for this relation.
                profiles_coll.update_one(
                    {"user_id": user_id},
                    {"$pull": {"mediator_inbox": {
                        "type": {"$in": ["feedback_request", "probe_question"]},
                        "match_id": event.get("match_id"),
                    }}},
                )
                state = participant_probe_state(event_match, user_id)
                if event.get("probe_id") and state.get("probe_id") != event.get("probe_id"):
                    return {"has_new": False, "deduplicated": True}
                asked_at = float(state.get("asked_at", 0))
                duplicate_query = {
                    "room_id": room_id,
                    "metadata.event_type": event_type,
                }
                if event.get("probe_id"):
                    duplicate_query["metadata.probe_id"] = event.get("probe_id")
                else:
                    duplicate_query["timestamp"] = {"$gte": asked_at - 1}
                duplicate = asked_at and messages_coll.find_one(duplicate_query)
                if duplicate and state.get("status") in {"awaiting_answer", "awaiting_sentiment", "awaiting_consent"}:
                    return {"has_new": False, "deduplicated": True}
            delivered_message = event.get("message", "阿月有一則新消息。")
            if event_type == "feedback_request":
                delivered_message = "你跟這位聊起來感覺如何？"
                message_metadata["actions"] = []
            save_message(
                room_id, "ai_assistant", delivered_message,
                message_type="mediator_card" if message_metadata["actions"] else "text",
                metadata=message_metadata
            )
            unread_field = relationship_unread_field(event_match, user_id)
            updated_match = matches_coll.find_one_and_update(
                {"_id": event_match["_id"]}, {"$inc": {unread_field: 1}},
                return_document=ReturnDocument.AFTER
            ) or event_match
            role = "from" if event_match.get("from_user") == user_id else "to"
            unread_count = int((updated_match.get("private_unread", {}) or {}).get(role, 1))
            if event_type in {"feedback_request", "probe_question"}:
                requester_id = event.get("requester_id") or participant_probe_state(event_match, user_id).get("requester_id")
                probe_kind = event.get("probe_kind") or participant_probe_state(event_match, user_id).get("kind", "sentiment")
                stage = "sentiment" if probe_kind == "sentiment" else "probe_answer"
                profiles_coll.update_one({"user_id": user_id}, {"$set": {"pending_private_feedback": {
                    "match_id": str(event_match["_id"]), "other_id": other_id,
                    "stage": stage, "kind": probe_kind, "origin": event.get("origin", "auto"),
                    "requester_id": requester_id, "probe_id": event.get("probe_id")}}})
                matches_coll.update_one({"_id": event_match["_id"]}, {"$set": {
                    participant_probe_field(event_match, user_id) + ".status":
                        "awaiting_sentiment" if stage == "sentiment" else "awaiting_answer",
                    participant_probe_field(event_match, user_id) + ".asked_at": time.time()}})
            elif event_type == "date_coordination_request":
                profiles_coll.update_one({"user_id": user_id}, {"$set": {
                    "pending_date_coordination": {
                        "match_id": str(event_match["_id"]), "other_id": other_id,
                        "stage": "availability", "data": {}
                    }
                }})
            return {
                "has_new": True, "surface": "relationship_private", "other_id": other_id,
                "unread_count": unread_count, "message": delivered_message,
                "type": event_type, "metadata": message_metadata
            }

        if other_id and event_type in RELATIONSHIP_EVENT_TYPES:
            return {
                "has_new": False,
                "surface": "relationship_private",
                "type": event_type,
                "other_id": other_id,
                "stale_relationship_event": True,
            }

        room_id = generate_room_id(user_id, "ai_assistant")
        message_type = "mediator_card" if event_type in {"match_proposal", "incoming_match_interest"} else "text"
        if event_type == "match_proposal":
            match_id = event.get("match_id")
            profile_state = profiles_coll.find_one(
                {"user_id": user_id},
                {"active_match_proposal_id": 1},
            ) or {}
            live_match = None
            if match_id:
                try:
                    from bson.objectid import ObjectId
                    live_match = matches_coll.find_one({
                        "_id": ObjectId(match_id),
                        "status": {"$in": ["draft", "pending"]},
                        "$or": [{"from_user": user_id}, {"to_user": user_id}],
                    })
                except Exception:
                    live_match = None
            active_proposal_id = profile_state.get("active_match_proposal_id")
            if not live_match or str(active_proposal_id or "") != str(match_id or ""):
                return {"has_new": False, "stale": True}
        save_message(room_id, "ai_assistant", event.get("message", "阿月有一則新的媒合消息。"), message_type=message_type, metadata=message_metadata)
        if event_type in {"match_proposal", "incoming_match_interest"}:
            profiles_coll.update_one(
                {"user_id": user_id},
                {"$set": {"active_match_proposal_id": event.get("match_id"), "ai_chat_locked": False}}
            )
        return {
            "has_new": True, "surface": "global_mediator", "message": event.get("message"),
            "type": event_type, "matches": event.get("matches", []),
            "metadata": message_metadata, "debug_info": event.get("debug_info", [])
        }

    freq_str = str(user_doc.get("proactive_frequency", "none"))
    if freq_str == "none":
        return {"has_new": False}
        
    try:
        freq_seconds = int(freq_str)
    except ValueError:
        return {"has_new": False}

    last_activity = float(user_doc.get("last_user_activity_at", 0) or 0)
    handled_activity = float(user_doc.get("last_followup_activity_at", 0) or 0)
    current_time = time.time()

    # Send a gentle follow-up only after new user activity and the configured quiet interval.
    if (
        last_activity > handled_activity
        and not conversation_active
        and current_time - last_activity >= freq_seconds
    ):
        
        bf = user_doc.get("big_five", {})
        ctx = user_doc.get("current_context", "尚無近期情境")
        
        prompt = f"""
{MEDIATOR_PERSONA}
媒人語氣：{mediator_style(user_id)}
你要主動關心使用者，但不要像通知機器人。請根據他的個性與近期情境，給一句自然、短的 follow-up。
Big Five 摘要：{bf.get('summary', '未知')}
近期情境：{ctx}
"""
        try:
            ai_reply = generate_chat_completion(prompt, temperature=0.7, json_output=False)
            room_id = generate_room_id(user_id, "ai_assistant")
            save_message(room_id, "ai_assistant", ai_reply)
            profiles_coll.update_one({"user_id": user_id}, {"$set": {
                "ai_chat_locked": False,
                "ai_chat_interaction_count": 0,
                "last_proactive_time": current_time,
                "last_followup_activity_at": last_activity,
            }})
            return {"has_new": True, "message": ai_reply}
        except Exception as e:
            print(f"Proactive chat error: {e}")
            return {"has_new": False}
            
    return {"has_new": False}
