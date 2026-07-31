import json

import asyncio

import time

import os

import re

import uuid

import queue

import threading

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, BackgroundTasks
from fastapi.responses import StreamingResponse

from pymongo import ReturnDocument

from models import (

    ChatRequest, DirectChatRequest, MediatorPrivateRequest, MediatorProbeRequest,

    RelationshipGameRequest, RelationshipQuizAnswerRequest, ResetRequest,

    DateUpdateRequest, DateConfirmRequest, DateInviteResponseRequest, CalendarActionRequest

)

from database import profiles_coll, messages_coll, matches_coll

from config import OLLAMA_FAST_CHAT_MODEL

from services.ai_service import analyze_big_five, analyze_deep_profile, get_embedding, generate_chat_completion, orchestrate_date_coordination

from services.chat_service import generate_room_id, save_message

from services.memory_service import observe_user_memory, observe_profile_memory, get_user_graph_memories
from services.ayue_agent import run_public_agent_turn
from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.runtime import agent_mode_for_user
from services.ayue_agent.proactive_care import (
    build_proactive_care_context,
    claim_proactive_care,
    finalize_proactive_care_claim,
    generate_proactive_care,
    proactive_care_claim_is_current,
    proactive_frequency_seconds,
    record_proactive_activity,
    consume_proactive_delivery,
)
from services.ayue_agent.public_relationship_projection import (
    mentioned_contact_refs,
    validated_mentioned_contact_ids,
)
from services.profile_projection import safe_recent_context
from services.profile_skills import process_profile_message, profile_skills_mode_for_user
from services.ayue_agent.private_runtime import (
    PrivateAgentTurnContext,
    private_agent_mode_for_user,
    run_private_agent_turn,
)
from services.ayue_agent.private_v2 import private_v2_mode_for_user, run_private_agent_turn_v2

from services.mediator_event_service import claim_next_mediator_event, queue_mediator_event
from services.match_state_service import verified_accepted_match_query

from services.semantic_plan_service import (

    get_relationship_semantic_context,

    process_relationship_semantic_plan,

    track_message_metrics,

)



router = APIRouter(prefix="/api", tags=["Chat"])

MATCH_READINESS_THRESHOLD = 75

FEEDBACK_COOLDOWN_SECONDS = 120

PROBE_PENDING_TTL = 72 * 3600

PROBE_IN_FLIGHT_STATUSES = {

    "queued", "awaiting_answer", "awaiting_sentiment", "awaiting_consent"

}

QUIZ_TTL_SECONDS = 7 * 86400

def queue_profile_skills(
    background_tasks, user_id: str, message: str, message_id: str | None,
    surface: str, match_id: str | None = None, *, allow_legacy_fallback: bool = True,
) -> str:
    """Run profile writes from the saved owner message exactly once."""
    mode = profile_skills_mode_for_user(user_id)
    if mode == "off":
        if allow_legacy_fallback:
            background_tasks.add_task(observe_user_memory, user_id, message, surface, match_id, message_id)
        return mode
    background_tasks.add_task(process_profile_message, user_id, message, message_id, surface, match_id)
    return mode

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

    "date_coordination_request", "date_coordination_result"

}



PROBE_QUESTIONS = {

    "sentiment": "你跟這位聊起來感覺如何？",

    "fun_fact": "有沒有一件關於你的有趣小事，可以讓我之後幫你們找話題？",

    "weekend": "你這週末大概想怎麼過？",

    "conversation_hook": "如果要讓對方更好開話題，你希望我透露哪個輕鬆的小線索？",

    "availability": "你近期哪個時間比較方便認識新朋友？",

}

LOW_SENSITIVITY_PROBES = {"fun_fact", "weekend", "conversation_hook", "availability"}



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
    from services.match_action_service import start_match_search

    start_match_search(user_id, source=source, force_new=force_new)






def check_and_trigger_date_activation(room_id: str, user_id: str, contact_id: str, message: str, match_doc: dict):
    """A proposal creates one invitation; it never opens a form without the partner."""
    from services.ai_service import detect_date_activation
    from services.date_coordination_service import create_invite
    if match_doc and detect_date_activation(message):
        create_invite(match_doc, user_id, contact_id)


def ai_process_date_coordination_step(room_id: str, user_id: str, contact_id: str, message: str, match_doc: dict):
    """AI may enrich the active canonical form, but cannot create another card."""
    from services.ai_service import extract_date_form_updates
    from services.date_coordination_service import update_form
    from services.calendar_service import normalize_form
    if not match_doc:
        return
    fresh_match = matches_coll.find_one({"_id": match_doc["_id"]}) or match_doc
    coordination = fresh_match.get("date_coordination") or {}
    if coordination.get("status") != "active" or not coordination.get("coordination_id"):
        return
    current_form = normalize_form(coordination.get("form", {}))
    updated_form = normalize_form(extract_date_form_updates(message, current_form))
    if updated_form != current_form:
        try:
            update_form(user_id, contact_id, coordination["coordination_id"], int(coordination.get("revision", 1)), updated_form)
        except HTTPException as exc:
            print(f"Date form AI update skipped: {exc.detail}")

def match_outcome_followup_reply(user_id: str) -> str:
    latest = matches_coll.find_one(
        {"status": "declined", "$or": [{"from_user": user_id}, {"to_user": user_id}]},
        sort=[("updated_at", -1), ("created_at", -1)],
    )
    if not latest:
        return "你是問剛剛哪一個媒合結果嗎？我目前找不到一筆可確認的婉拒紀錄。"
    other_id = latest.get("to_user") if latest.get("from_user") == user_id else latest.get("from_user")
    decision = latest.get("last_decision") or {}
    actor = decision.get("actor")
    if actor and actor != user_id:
        return f"@{other_id} 這次先婉拒了，但我沒有收到對方提供的具體原因，所以不能替對方亂猜。"
    if actor == user_id:
        return f"這張是你這邊先收起來的，對象是 @{other_id}。如果你是問另一張，我可以再幫你確認。"
    return f"@{other_id} 這張提案已經結束，但系統沒有留下可確認的婉拒原因。"

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
            "display_name": card.get("other_label", "對方"),

            "target_context": card.get("your_context"),

            "current_context": card.get("other_context"),

            "top_reasons": card.get("reasons") or [],

            "score_breakdown": {"total": card.get("score") or 0},

            "recommendation_reason": card.get("recommendation_reason", ""),

            "receiver_reason": card.get("receiver_reason", ""),

            "viewer_reason": card.get("viewer_reason", ""),

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


def is_calendar_query(message: str) -> bool:
    """Keep schedule questions on a deterministic, calendar-only path."""
    compact = re.sub(r"\s+", "", message or "")
    if not compact:
        return False
    calendar_words = ("行事曆", "日曆", "行程", "有空", "有約", "空檔", "時間表")
    query_words = ("有沒有", "看到", "看得到", "看的到", "幫我看", "查看", "哪天", "什麼時候", "幾點", "最近")
    return any(word in compact for word in calendar_words) and (
        "？" in compact or "?" in compact or any(word in compact for word in query_words)
    )


def calendar_reply_for_mediator(user_id: str) -> str:
    """Render the viewer's actual calendar without leaking match/probe state."""
    from services.calendar_service import calendar_access_enabled, get_calendar_context, get_timezone

    if not calendar_access_enabled(user_id):
        return "你目前沒有授權我讀取行事曆，所以我不會亂猜你的時間。"

    now_utc = datetime.now(timezone.utc)
    events = get_calendar_context(user_id, None, now_utc, now_utc + timedelta(days=90)).get("viewer_events", [])
    visible_events = [event for event in events if event.get("status") != "cancelled"]
    if not visible_events:
        return "我有看過行事曆，接下來 90 天目前沒有排定行程。"

    weekday_names = "一二三四五六日"

    def format_event(event: dict) -> str:
        zone = get_timezone(event.get("timezone") or "Asia/Taipei")
        start = datetime.fromisoformat(str(event["start_at"]).replace("Z", "+00:00")).astimezone(zone)
        end = datetime.fromisoformat(str(event["end_at"]).replace("Z", "+00:00")).astimezone(zone)
        title = event.get("activity") or event.get("title") or "行程"
        status = "（內容待重新確認）" if event.get("status") == "pending_reconfirmation" else ""
        return f"{start.month}/{start.day}（週{weekday_names[start.weekday()]}）{start:%H:%M}–{end:%H:%M} {title}{status}"

    lines = [format_event(event) for event in visible_events[:5]]
    remaining = len(visible_events) - len(lines)
    suffix = f"，另外還有 {remaining} 筆。" if remaining else "。"
    return f"有，我看到 {len(visible_events)} 筆近期行程：" + "；".join(lines) + suffix


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

    candidates = list(matches_coll.find(
        verified_accepted_match_query(user_id)
    ))

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

            room_id = generate_room_id(req.user_id, "ai_assistant")

            count = messages_coll.count_documents({"room_id": room_id})

            if count == 0:

                save_message(room_id, "ai_assistant", "我大概更了解你的個性了。接下來可以聊聊你最近想做什麼、想去哪裡。")



        profiles_coll.update_one(

            {"user_id": req.user_id}, 

            {"$set": update_fields}, 

            upsert=True

        )



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

        current_context = safe_recent_context(user_doc.get("current_context", "") if user_doc else "", "尚無近期情境")

        

        user_context = {"big_five": big_five, "current_context": current_context}

        

        result = analyze_deep_profile(req.message, prev_deep, interaction_count, user_context)

        

        update_fields = {

            "temp_deep_profile": result.get("deep_profile", {}),

            "interaction_count_deep": interaction_count + 1

        }

        

        if result.get("is_complete", False):

            update_fields["deep_profile"] = result.get("deep_profile", {})

            room_id = generate_room_id(req.user_id, "ai_assistant")

            count = messages_coll.count_documents({"room_id": room_id})

            if count == 0:

                save_message(room_id, "ai_assistant", "我也更了解你重視什麼了。之後你想找人一起做什麼，直接跟我說就好。")



        profiles_coll.update_one(

            {"user_id": req.user_id}, 

            {"$set": update_fields}, 

            upsert=True

        )



        return {

            "status": "success", 

            "deep_profile": result.get("deep_profile"), 

            "reply": result.get("reply"),

            "is_complete": result.get("is_complete", False)

        }

    else:

        raise HTTPException(status_code=400, detail="Invalid state")



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



@router.get("/messages/{contact_id}")

def get_messages(contact_id: str, user_id: str):

    room_id = generate_room_id(user_id, contact_id)

    

    if contact_id == "ai_assistant":

        count = messages_coll.count_documents({"room_id": room_id})

        if count == 0:

            save_message(room_id, "ai_assistant", "哈囉，我是阿月。最近想做什麼、想去哪裡，儘管跟我說；我會邊聊邊幫你留意合適的人。")

            

    msgs = list(messages_coll.find({"room_id": room_id}, {"_id": 0}).sort("timestamp", 1))
    user_doc = profiles_coll.find_one({"user_id": user_id})
    active_proposal_id = (user_doc or {}).get("active_match_proposal_id")
    
    date_coordination = None
    established_dates = []
    if contact_id != "ai_assistant":
        from routers.chat import find_accepted_match
        match_doc = find_accepted_match(user_id, contact_id)
        if match_doc:
            date_coordination = match_doc.get("date_coordination")
            established_dates = match_doc.get("established_dates", [])
            
    return {"messages": msgs, "active_match_proposal_id": active_proposal_id, "date_coordination": date_coordination, "established_dates": established_dates}



def _requested_mentions(req: DirectChatRequest) -> list[str]:
    mentions: list[str] = []
    for other_id in (req.mentioned_other_ids or []) + ([req.mentioned_other_id] if req.mentioned_other_id else []):
        if other_id and other_id not in mentions:
            mentions.append(other_id)
    return mentions


def _validated_requested_mentions(req: DirectChatRequest) -> tuple[list[str], bool]:
    """Never let an arbitrary client ID become an Ayue relationship target."""
    return validated_mentioned_contact_ids(req.user_id, _requested_mentions(req))


def _mention_display_prefix(user_id: str, mentioned_ids: list[str]) -> str:
    return " ".join("@" + item["display_name"] for item in mentioned_contact_refs(user_id, mentioned_ids))


def _complete_public_v2_turn(
    req: DirectChatRequest,
    room_id: str,
    requested_mentions: list[str],
    mention_overflow: bool = False,
    on_progress=None,
    background_tasks: BackgroundTasks | None = None,
    user_message_id: str | None = None,
) -> dict:
    """Run and persist one V2 turn after the owner message has been saved."""
    history = list(messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(12))[::-1]
    user_doc = profiles_coll.find_one({"user_id": req.user_id})
    agent_result = run_public_agent_turn(
        AgentTurnContext(
            user_id=req.user_id,
            room_id=room_id,
            message=req.message,
            mentioned_ids=requested_mentions,
            mention_overflow=mention_overflow,
            user_profile=user_doc or {},
            recent_history=history,
        ),
        mode="on",
        on_progress=on_progress,
    )
    ai_reply = agent_result.reply or "我先把這件事記下來。"
    sources = agent_result.sources[:5]
    if sources:
        save_message(room_id, "ai_assistant", ai_reply, metadata={"sources": sources})
    else:
        save_message(room_id, "ai_assistant", ai_reply)
    # Every saved public-owner message reaches the isolated extractor.  The
    # chat planner must not suppress mixed messages such as a real activity
    # plus a matchmaking request; only the extractor decides whether to write.
    if background_tasks is not None and user_message_id:
        queue_profile_skills(
            background_tasks, req.user_id, req.message, user_message_id, "global",
            allow_legacy_fallback=False,
        )
    return {
        "reply": ai_reply,
        "is_locked": False,
        "conversation_intent": agent_result.conversation_intent,
        "mentioned_other_ids": agent_result.mentioned_other_ids,
        "context_changed": agent_result.context_changed,
        "context_confirmation_needed": agent_result.context_confirmation_needed,
        "profile_update_pending": bool(background_tasks is not None and user_message_id),
        "agent_run_id": agent_result.agent_run_id,
        "agent_mode": agent_result.agent_mode,
        "agent_version": "v2",
        "match_readiness_state": agent_result.match_readiness_state,
        "match_guidance_shown": agent_result.match_guidance_shown,
        "sources": sources,
    }


def _run_public_v2_stream_turn(
    req: DirectChatRequest, background_tasks: BackgroundTasks, on_progress,
) -> dict:
    """Public-only stream path; mirrors the V2 branch of direct_chat exactly once."""
    room_id = generate_room_id(req.user_id, req.contact_id)
    requested_mentions, mention_overflow = _validated_requested_mentions(req)
    display_message = req.message
    if requested_mentions:
        display_message = f"{_mention_display_prefix(req.user_id, requested_mentions)} {req.message}".strip()
    user_message = save_message(
        room_id, req.user_id, display_message,
        metadata={"owner_raw_content": req.message} if display_message != req.message else None,
    )
    profiles_coll.update_one(
        {"user_id": req.user_id}, {"$set": {"last_user_activity_at": time.time()}}, upsert=True,
    )
    return _complete_public_v2_turn(
        req, room_id, requested_mentions, mention_overflow, on_progress,
        background_tasks=background_tasks, user_message_id=user_message.get("message_id"),
    )


def _sanitize_public_stream_event(event: dict) -> dict | None:
    """Allow only the documented public event shape across the HTTP boundary."""
    if not isinstance(event, dict):
        return None
    event_type = event.get("type")
    run_id = str(event.get("agent_run_id") or "")[:128]
    if event_type == "run_started" and run_id:
        return {"type": "run_started", "agent_run_id": run_id}
    if event_type == "tool_started" and run_id:
        return {
            "type": "tool_started",
            "agent_run_id": run_id,
            "step_id": str(event.get("step_id") or "")[:64],
            "text": str(event.get("text") or "我確認一下…")[:200],
        }
    if event_type == "tool_finished" and run_id:
        outcome = "ok" if event.get("outcome") == "ok" else "error"
        return {
            "type": "tool_finished",
            "agent_run_id": run_id,
            "step_id": str(event.get("step_id") or "")[:64],
            "outcome": outcome,
        }
    if event_type == "final" and isinstance(event.get("response"), dict):
        return {"type": "final", "response": event["response"]}
    if event_type == "error":
        return {
            "type": "error",
            "agent_run_id": run_id,
            "reply": "我現在沒辦法安全地處理這件事，請稍後再試。",
        }
    return None


@router.post("/direct_chat/stream")
def direct_chat_stream(req: DirectChatRequest, background_tasks: BackgroundTasks):
    """NDJSON public-agent events; legacy and private chat retain direct JSON."""
    event_queue: queue.Queue[dict | None] = queue.Queue(maxsize=16)
    fallback_run_id = uuid.uuid4().hex
    state = {"agent_run_id": fallback_run_id}

    def enqueue(item: dict | None, *, terminal: bool = False) -> bool:
        while True:
            try:
                event_queue.put_nowait(item)
                return True
            except queue.Full:
                if not terminal:
                    return False
                try:
                    event_queue.get_nowait()
                except queue.Empty:
                    return False

    def emit(event: dict) -> bool:
        public_event = _sanitize_public_stream_event(event)
        if public_event is None:
            return False
        if public_event.get("agent_run_id"):
            state["agent_run_id"] = str(public_event["agent_run_id"])
        return enqueue(public_event, terminal=public_event["type"] in {"final", "error"})

    def worker() -> None:
        # These tasks belong to the run, not to the lifetime of the client
        # connection. Execute them in this worker even if the stream disconnects.
        worker_background_tasks = BackgroundTasks()
        try:
            if req.contact_id == "ai_assistant" and agent_mode_for_user(req.user_id) == "on":
                response = _run_public_v2_stream_turn(req, worker_background_tasks, emit)
            else:
                # Stream is intentionally optional for legacy/private contacts;
                # preserve their established direct-chat behavior as one final event.
                response = direct_chat(req, worker_background_tasks)
            emit({"type": "final", "response": response})
        except Exception:
            emit({
                "type": "error", "agent_run_id": state["agent_run_id"],
                "reply": "我現在沒辦法安全地處理這件事，請稍後再試。",
            })
        finally:
            try:
                enqueue(None, terminal=True)
                asyncio.run(worker_background_tasks())
            except Exception:
                pass

    def event_stream():
        while True:
            event = event_queue.get()
            if event is None:
                break
            yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    threading.Thread(target=worker, name="ayue-direct-chat-stream", daemon=True).start()
    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


@router.post("/direct_chat")

def direct_chat(req: DirectChatRequest, background_tasks: BackgroundTasks):

    room_id = generate_room_id(req.user_id, req.contact_id)

    requested_mentions, mention_overflow = _validated_requested_mentions(req)

    display_message = req.message

    if req.contact_id == "ai_assistant" and requested_mentions:

        prefix = _mention_display_prefix(req.user_id, requested_mentions)

        display_message = f"{prefix} {req.message}".strip()

    user_message = save_message(
        room_id, req.user_id, display_message,
        metadata={"owner_raw_content": req.message} if display_message != req.message else None,
    )

    latest_assistant_message = None
    if req.contact_id == "ai_assistant":
        latest_assistant_message = messages_coll.find_one(
            {"room_id": room_id, "sender_id": "ai_assistant"},
            sort=[("timestamp", -1)],
        )
    # V2 owns every public-agent turn when enabled; legacy outcome routing is
    # retained only for the explicit rollback mode.
    public_agent_mode = agent_mode_for_user(req.user_id) if req.contact_id == "ai_assistant" else "off"
    legacy_match_routing = None
    outcome_followup = False
    if req.contact_id == "ai_assistant" and public_agent_mode == "off":
        # Keep rollback-only language routing out of every V2 request.  Importing
        # here also makes the emergency rollback dependency explicit and testable.
        from services.ayue_agent import legacy_match_routing as legacy_match_routing_module

        legacy_match_routing = legacy_match_routing_module
        outcome_followup = legacy_match_routing.should_answer_match_outcome_followup(
            req.message,
            (latest_assistant_message or {}).get("content", ""),
        )
    if public_agent_mode == "on":
        profile_skill_mode = "deferred_to_public_agent"
    elif outcome_followup:
        profile_skill_mode = profile_skills_mode_for_user(req.user_id)
    else:
        profile_skill_mode = queue_profile_skills(
            background_tasks, req.user_id, req.message, user_message.get("message_id"),
            "global" if req.contact_id == "ai_assistant" else "pair_chat",
        )

    if req.contact_id == "ai_assistant":
        record_proactive_activity(req.user_id)
    else:
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$set": {"last_user_activity_at": time.time()}}, upsert=True,
        )

    if req.contact_id != "ai_assistant":
        match_doc = find_accepted_match(req.user_id, req.contact_id)
        if match_doc:
            background_tasks.add_task(check_and_trigger_date_activation, room_id, req.user_id, req.contact_id, req.message, match_doc)
            background_tasks.add_task(process_relationship_semantic_plan, match_doc, room_id)
            background_tasks.add_task(ai_process_date_coordination_step, room_id, req.user_id, req.contact_id, req.message, match_doc)

    

    if req.contact_id == "ai_assistant":

        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(12)

        history = list(history_cursor)[::-1]

        

        user_doc = profiles_coll.find_one({"user_id": req.user_id})

        if outcome_followup:
            ai_reply = match_outcome_followup_reply(req.user_id)
            save_message(room_id, "ai_assistant", ai_reply)
            return {
                "reply": ai_reply, "is_locked": False,
                "conversation_intent": "match_outcome_followup",
                "mentioned_other_ids": [], "context_changed": False,
                "context_confirmation_needed": False,
            }

        if public_agent_mode == "on":
            return _complete_public_v2_turn(
                req, room_id, requested_mentions, mention_overflow,
                background_tasks=background_tasks, user_message_id=user_message.get("message_id"),
            )

        if is_calendar_query(req.message):
            ai_reply = calendar_reply_for_mediator(req.user_id)
            save_message(room_id, "ai_assistant", ai_reply)
            return {
                "reply": ai_reply,
                "is_locked": False,
                "conversation_intent": "calendar_query",
                "mentioned_other_ids": [],
                "context_changed": False,
                "context_confirmation_needed": False,
            }

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

                return {

                    "reply": ai_reply, "is_locked": False, "mode": "match_confirmation",

                    "match_request_status": status

                }

        # A chat message must never accept, decline, or cancel a match.  Decisions are
        # intentionally bound to the currently visible card and its expected status.
        from routers.match import reconcile_match_state, derive_match_stage
        from routers.match import reconcile_match_state, derive_match_stage
        active_match = reconcile_match_state(req.user_id)
        if active_match and classify_proposal_intent(req.message, "proposal_response") is not None:
            stage = derive_match_stage(active_match, req.user_id)
            other_id = active_match.get("to_user") if active_match.get("from_user") == req.user_id else active_match.get("from_user")
            if stage == "waiting_other":
                ai_reply = f"@{other_id} 還在考慮；如果你想換下一位，請在卡片上按取消邀請。"
            else:
                ai_reply = "這張牽線要由你在卡片上按接受或婉拒，我不會替你做決定。"
            save_message(room_id, "ai_assistant", ai_reply)
            return {"reply": ai_reply, "is_locked": False, "mode": "proposal_card_required"}
        bf = user_doc.get("big_five", {}) if user_doc else {}

        interaction_count = user_doc.get("ai_chat_interaction_count", 0) if user_doc else 0

        

        current_context = safe_recent_context(user_doc.get("current_context", "") if user_doc else "", "尚無近期情境")

        current_round = interaction_count + 1

        accepted_matches = list(matches_coll.find(
            verified_accepted_match_query(req.user_id)
        ))

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

        # Calendar context is explicitly scoped to the current user.  This lets
        # 阿月 answer schedule questions without exposing a partner's private events.
        calendar_summary = "使用者未授權阿月讀取行事曆"
        try:
            from services.calendar_service import calendar_access_enabled, get_calendar_context
            if calendar_access_enabled(req.user_id):
                now_utc = datetime.now(timezone.utc)
                calendar_summary = json.dumps(
                    get_calendar_context(req.user_id, None, now_utc, now_utc + timedelta(days=90)).get("viewer_events", []),
                    ensure_ascii=False,
                )
        except Exception as calendar_exc:
            print(f"Calendar context unavailable: {calendar_exc}")



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

            return {

                "reply": deterministic_relationship_reply,

                "is_locked": False,

                "match_readiness_score": 0,

                "match_readiness_state": "learning",

                "conversation_intent": "relationship_chat",

                "mentioned_other_ids": [item["other_id"] for item in relationship_lines],

                "context_changed": False,

                "context_confirmation_needed": False,

            }



        sys_prompt = f"""

{MEDIATOR_PERSONA}

媒人語氣：{mediator_style(req.user_id)}



你是阿月，一個會聊天、會觀察契機的媒人。你的目標不是硬推配對，而是在自然聊天中理解使用者最近想做什麼、何時想做、是否想找人一起。



使用者資料：

- Big Five 摘要：{bf.get('summary', '未知')}

- 目前近期情境：{current_context}

- 長期記憶摘要：{memory_summary}

- 我的近期行事曆（只在使用者已授權時提供）：{calendar_summary}

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

12. 使用者問自己的行程、是否有空或何時有約時，只能根據「我的近期行事曆」回答；沒有授權或沒有資料時要直接說明，不能猜測。



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

            llm_match_suggestion = bool(ai_res.get("explicit_match_request"))
            explicit_match_request = (
                llm_match_suggestion
                and not relationship_query
                and legacy_match_routing is not None
                and legacy_match_routing.is_explicit_match_request(req.message)
            )
            context_intent_can_update = intent == "recent_context" or explicit_match_request

            context_should_update = bool(ai_res.get("context_should_update")) and context_intent_can_update and context_confidence >= 0.85

            if profile_skill_mode != "off":

                context_should_update = False

            if explicit_context:

                context_should_update = True

                intent = "recent_context"

            if explicit_no_memory or ((explicit_mentions or referenced_user_ids) and not explicit_context):

                context_should_update = False

            if profile_skill_mode != "off":

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
                    stage = derive_match_stage(active_match, req.user_id)
                    other_id = active_match.get("to_user") if active_match.get("from_user") == req.user_id else active_match.get("from_user")
                    if stage == "waiting_user":
                        ai_reply = f"@{other_id} 的提案還在等你按鈕決定；我不會替你收掉。"
                    elif stage == "waiting_other":
                        ai_reply = f"這條已經送給 @{other_id}，正在等對方回覆。想換下一位請在卡片上按取消邀請。"
                    else:
                        ai_reply = f"現在有 @{other_id} 的邀請在等你決定，請在卡片上接受或婉拒。"
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

                return {"reply": ai_reply, "is_locked": False, "mode": "match_request", "match_request_status": status}

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

                pass
        except Exception as e:

            print(f"Chat error (AI): {e}")

            ai_reply = "銝末????冽?暺頝荔?隢?敺?閰佗?"

            is_locked = False

            

        save_message(room_id, "ai_assistant", ai_reply)

        return {

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

        }

        

    else:

        target_doc = profiles_coll.find_one({"user_id": req.contact_id})

        target_bf = target_doc.get("big_five", {}) if target_doc else {}

        

        history_cursor = messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(20)

        history = list(history_cursor)[::-1]

        

        sys_prompt = f"你現在要扮演使用者 {req.contact_id} 回覆聊天。請參考他的 Big Five：{target_bf}。回覆要自然、簡短，像真人，不要說自己是 AI。"

        prompt = sys_prompt + "\n\n最近聊天紀錄：\n"

        for m in history:

            speaker = "對方" if m["sender_id"] == req.user_id else "你"

            prompt += f"{speaker}: {m['content']}\n"

        prompt += "你："

            

        try:

            reply = generate_chat_completion(prompt, temperature=0.7, json_output=False)

        except Exception as e:

            print(f"Chat error (User {req.contact_id}): {e}")

            reply = "我剛剛有點分心，等我一下，我再好好回你。"

        save_message(room_id, req.contact_id, reply)

        match_doc = matches_coll.find_one(
            verified_accepted_match_query(req.user_id, req.contact_id)
        )

        message_count = mark_post_chat_activity(match_doc, room_id)

        if match_doc and message_count >= 2:

            track_message_metrics(room_id)

        if match_doc and message_count >= 6:

            background_tasks.add_task(summarize_relationship, match_doc["_id"], room_id)

        return {"reply": reply, "feedback_scheduled": bool(match_doc)}



@router.get("/contacts")

def get_contacts(user_id: str):

    user_doc = profiles_coll.find_one({"user_id": user_id})

    ai_locked = user_doc.get("ai_chat_locked", False) if user_doc else False

    

    query = verified_accepted_match_query(user_id)

    matches = list(matches_coll.find(query))

    

    contacts = [

        {"id": "ai_assistant", "name": "阿月", "role": "system", "context": "你的媒人助理，會邊聊天邊幫你留意合適的人。", "is_locked": ai_locked}

    ]

    

    for m in matches:

        other_id = m["to_user"] if m["from_user"] == user_id else m["from_user"]

        other_doc = profiles_coll.find_one({"user_id": other_id})

        ctx = other_doc.get("current_context", "尚無近期情境") if other_doc else "尚無近期情境"

        contacts.append({

            "id": other_id,

            "name": mentioned_contact_refs(user_id, [other_id])[0]["display_name"],

            "role": "user",

            "context": ctx

        })

        

    return {"contacts": contacts}



def generate_mediator_private_room_id(user_id: str, other_id: str):

    return f"mediator_private::{user_id}::{other_id}"



def find_accepted_match(user_id: str, other_id: str):

    return matches_coll.find_one(
        verified_accepted_match_query(user_id, other_id)
    )



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


def _run_private_v2_saved_turn(req: MediatorPrivateRequest, match_doc: dict, room_id: str, on_progress=None, agent_run_id: str | None = None) -> dict:
    """Persist exactly one private V2 final after the owner message is saved."""
    result = run_private_agent_turn_v2(
        user_id=req.user_id, other_id=req.other_id, message=req.message,
        match_doc=match_doc, on_progress=on_progress, agent_run_id=agent_run_id,
    )
    reply = result.reply or "我剛才沒能整理好這件事。你最想確認哪一點？"
    save_private_mediator_reply(room_id, reply, "agentic_private_v2")
    return {
        "reply": reply, "pending_step": None, "agent_run_id": result.agent_run_id,
        "agent_mode": "v2", "agent_version": "v2", "conversation_intent": result.conversation_intent,
    }



def is_fun_fact_probe_request(message: str) -> bool:
    compact = re.sub(r"\s+", "", message or "")
    return "有趣小事" in compact or "問趣事" in compact or ("幫我問" in compact and "趣事" in compact)


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
        "status": "queued",
        "trigger": "manual",
        "requester_id": requester_id,
        "probe_id": probe_id,
        "kind": "fun_fact",
        "question": PROBE_QUESTIONS["fun_fact"],
        "asked_at": now,
        "message_count_snapshot": int(match_doc.get("shared_message_count", 0)),
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
                "probe_id": probe_id,
                "kind": "fun_fact",
                "asked_to": target_id,
                "asked_at": now,
                "status": "queued",
                "trigger": "manual",
                "requester_id": requester_id,
            }},
        },
    )
    if not claimed.modified_count:
        return False
    queue_mediator_event(
        target_id,
        PROBE_QUESTIONS["fun_fact"],
        "probe_question",
        match_id=str(match_doc["_id"]),
        other_id=requester_id,
        origin="manual",
        requester_id=requester_id,
        probe_kind="fun_fact",
        probe_id=probe_id,
    )
    return True


def consume_pending_probe_answer(match_doc: dict, user_doc: dict, user_id: str, other_id: str, message: str) -> str | None:
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
        **state,
        "status": "declined" if declined else "completed",
        "answered_at": now,
        "answer": answer,
        "completed_at": now,
    }
    if declined:
        completed_state.pop("answer", None)

    result_record = {
        "probe_id": probe_id,
        "kind": kind,
        "answer": answer if not declined else "",
        "answered_by": user_id,
        "requester_id": state.get("requester_id"),
        "status": completed_state["status"],
        "answered_at": now,
        "shareable": bool(kind in LOW_SENSITIVITY_PROBES and not declined),
    }
    updated = matches_coll.update_one(
        {
            "_id": match_doc["_id"],
            f"{state_field}.probe_id": probe_id,
            f"{state_field}.status": {"$in": ["awaiting_answer", "awaiting_sentiment"]},
        },
        {"$set": {
            state_field: completed_state,
            f"mediator_state.probe_results.{probe_id}": result_record,
        }},
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
            requester_id,
            f"我幫你問到一個可聊的點：對方說「{answer}」。你可以順著這個接話。",
            "probe_result",
            match_id=str(match_doc["_id"]),
            other_id=user_id,
            probe_id=probe_id,
        )

    if declined:
        return "收到，這題我先幫你跳過，也不會再拿同一題來問。"
    if kind == "sentiment":
        return "收到，這是你的私下想法；我先不替你轉述。"
    return "收到，我記下來了，之後會用這個幫你們找話題。"


@router.post("/mediator/private")

def mediator_private_chat(req: MediatorPrivateRequest, background_tasks: BackgroundTasks):

    match_doc = find_accepted_match(req.user_id, req.other_id)

    if not match_doc:

        raise HTTPException(status_code=403, detail="只能在已接受配對中私聊媒人")



    room_id = generate_mediator_private_room_id(req.user_id, req.other_id)

    user_message = save_message(room_id, req.user_id, req.message)

    profiles_coll.update_one(

        {"user_id": req.user_id},

        {"$set": {"last_user_activity_at": time.time()}},

        upsert=True,

    )

    queue_profile_skills(
        background_tasks, req.user_id, req.message, user_message.get("message_id"),
        "relationship_private", str(match_doc["_id"]),
    )

    user_doc = profiles_coll.find_one({"user_id": req.user_id}) or {}
    probe_reply = consume_pending_probe_answer(match_doc, user_doc, req.user_id, req.other_id, req.message)
    if probe_reply is not None:
        save_private_mediator_reply(room_id, probe_reply)
        return {"reply": probe_reply, "pending_step": None}

    # V2 owns the entire private turn when explicitly enabled. It must not
    # fall through into legacy keyword branches after a planner/tool failure.
    if private_v2_mode_for_user(req.user_id) == "on":
        return _run_private_v2_saved_turn(req, match_doc, room_id)

    # Date coordination is now a shared, consent-based flow. Private prompts can
    # start the invitation, but never create an additional form/card by themselves.
    current_coordination = match_doc.get("date_coordination") or {}
    if "幫我們協調約會" in req.message:
        from services.date_coordination_service import create_invite
        created = create_invite(match_doc, req.user_id, req.other_id)
        if created:
            reply = "我先問問對方願不願意一起協調；對方同意後，共同聊天室會出現同一張約會表單。"
        elif current_coordination.get("status") == "pending_partner":
            reply = "這次約會邀請還在等對方回覆，我先不重複開新表單。"
        else:
            reply = "你們目前已有一張約會表單，直接到共同聊天室一起更新就好。"
        save_private_mediator_reply(room_id, reply, "date_coordination_invite")
        return {"reply": reply, "pending_step": None}

    if current_coordination.get("status") in {"pending_partner", "active"}:
        reply = "約會協調正在共同聊天室進行中；我會同步整理你們在那裡確認的時間與地點。"
        save_private_mediator_reply(room_id, reply, "date_coordination_redirect")
        return {"reply": reply, "pending_step": None}

    private_agent_mode = private_agent_mode_for_user(req.user_id)
    if private_agent_mode != "off":
        private_agent_result = run_private_agent_turn(
            PrivateAgentTurnContext(
                user_id=req.user_id,
                other_id=req.other_id,
                room_id=room_id,
                message=req.message,
                match_doc=match_doc,
                user_profile=user_doc,
            ),
            mode=private_agent_mode,
        )
        if private_agent_mode == "on" and private_agent_result.handled:
            reply = private_agent_result.reply or "我先幫你整理一下。"
            save_private_mediator_reply(room_id, reply, "agentic_private")
            return {
                "reply": reply,
                "pending_step": None,
                "agent_run_id": private_agent_result.agent_run_id,
                "agent_mode": private_agent_result.agent_mode,
                "conversation_intent": private_agent_result.conversation_intent,
            }

    if is_fun_fact_probe_request(req.message):
        queued = queue_manual_fun_fact_probe(match_doc, req.user_id, req.other_id)
        reply = "我去問問他，拿到適合公開的小趣事再帶回來給你。" if queued else "我已經在問這題了，等對方回覆就好。"
        save_private_mediator_reply(room_id, reply, "probe_question")
        return {"reply": reply, "pending_step": None}

    date_coordination = match_doc.get("date_coordination", {}) or {}

    is_active_date = date_coordination.get("status") in ["active", "gathering"]



    if "幫我們協調約會" in req.message and not is_active_date:

        date_coordination = {

            "status": "gathering",

            "form": {"date": "", "time": "", "activity": "", "budget": ""},

            "confirmations": {},

            "room_id": str(match_doc["_id"]),

            "established_at": time.time()

        }

        matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {"date_coordination": date_coordination}})

        is_active_date = True



    if is_active_date:

        pair_room_id = generate_room_id(match_doc["from_user"], match_doc["to_user"])

        semantic_context = get_relationship_semantic_context(match_doc, pair_room_id)

        relationship_context = {

            "viewer": {"user_id": req.user_id, "profile": user_doc.get("deep_profile")},

            "partner": {"user_id": req.other_id},

            "relationship": {

                "match_id": str(match_doc["_id"]),

                "shared_chat_summary": match_doc.get("relationship_memory", {}),

                "semantic_plan": semantic_context.get("semantic_plan", {}),

                "chat_knowledge_graph": semantic_context.get("knowledge_graph_triples", []),

            }

        }

        

        result = orchestrate_date_coordination(req.message, date_coordination, relationship_context)

        reply = result.get("reply", "我了解了。")

        save_private_mediator_reply(room_id, reply, "date_coordination_chat")

        

        if result.get("form"):

            date_coordination["form"] = result["form"]

            date_coordination["confirmations"] = {}

            matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {"date_coordination": date_coordination}})

            

        if result.get("show_form"):

            date_coordination["status"] = "active"

            matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {"date_coordination.status": "active"}})

            

            form_payload = date_coordination["form"]

            card_metadata = {

                "event_type": "date_coordination_form",

                "match_id": str(match_doc["_id"]),

                "form": form_payload,

                "confirmations": date_coordination["confirmations"]

            }

            

            queue_mediator_event(

                req.other_id,

                "對方正在提議約會時間跟地點，你來看看適不適合！",

                "date_coordination_form",

                match_id=str(match_doc["_id"]),

                other_id=req.user_id,

                form=form_payload,

                confirmations=date_coordination["confirmations"]

            )

            save_message(pair_room_id, "ai_assistant", "阿月幫你們整理了約會表單，雙方確認後就成立囉！", message_type="mediator_card", metadata=card_metadata)



        return {"reply": reply, "pending_step": None}



    feedback = match_doc.get("private_feedback", {}) or {}

    consented_signal = None

    other_feedback = feedback.get(req.other_id, {}) or {}

    if other_feedback.get("share_consent") is True:

        consented_signal = other_feedback.get("sentiment")

    pair_room_id = generate_room_id(match_doc["from_user"], match_doc["to_user"])

    semantic_context = get_relationship_semantic_context(match_doc, pair_room_id)

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

            "semantic_plan": semantic_context.get("semantic_plan", {}),

            "chat_knowledge_graph": semantic_context.get("knowledge_graph_triples", []),

        },

    }

    prompt = f"""

{MEDIATOR_PERSONA}

媒人語氣：{mediator_style(req.user_id)}



你現在是使用者的一位「共同朋友」，正在私下跟他聊他的配對對象。請展現出朋友的【主觀與同理心】 (Agency & Empathy)，而不是一個客觀冰冷的分析工具。

回答守則：

1. **要有主觀意見與同理心**：請適度偏袒使用者，給予情感上的安撫與支持。使用非常口語化的語氣（例如：「我覺得」、「老實說」、「我懂！」），遇到看不懂的情況可以直接說不知道。

2. **具體破冰與延續話題建議**：當察覺到尷尬或卡關時，主動提供具體的聊天方向或話題包裝方式。

3. **微量透漏小秘密**：偶爾可以從 `chat_knowledge_graph` 中提取對方的一些無傷大雅的正面喜好或習慣（例如「偷偷跟你說，她有跟我提到滿喜歡貓的喔」）來助攻。

4. **保留神秘感與邊界**：絕對不要直接告訴使用者對方極度私密的心意、地雷或核心感情決策。鼓勵使用者自己去探索。

5. **動態角色扮演**：嚴格遵守 semantic_plan 裡的 current_role：

   - FRIEND (朋友): 建立共鳴點，提供輕鬆好接的話題開場白。

   - ADVISER (顧問): 給出戰術建議，告訴使用者該怎麼轉移話題或如何包裝回覆。

   - MENTOR (導師): 提出模糊、引導思考的提問，問使用者他們真正想達成的目標是什麼。不要給出具體答案。

   - FACILITATOR (引導員): 提供一個輕微的推動，維持當前話題的熱度。



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


@router.post("/mediator/private/stream")
def mediator_private_chat_stream(req: MediatorPrivateRequest, background_tasks: BackgroundTasks):
    """NDJSON private-V2 stream; legacy private chat remains JSON-only."""
    if private_v2_mode_for_user(req.user_id) != "on":
        raise HTTPException(status_code=409, detail="悄悄話 V2 尚未啟用")
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能在已接受配對中私聊媒人")
    event_queue: queue.Queue[dict | None] = queue.Queue(maxsize=16)
    fallback_run_id = uuid.uuid4().hex

    def emit(event: dict) -> None:
        if event.get("type") not in {"run_started", "tool_started", "tool_finished"}:
            return
        safe = {key: event[key] for key in ("type", "agent_run_id", "step_id", "text", "outcome") if key in event}
        try:
            event_queue.put_nowait(safe)
        except queue.Full:
            pass

    def worker() -> None:
        worker_tasks = BackgroundTasks()
        try:
            room_id = generate_mediator_private_room_id(req.user_id, req.other_id)
            user_message = save_message(room_id, req.user_id, req.message)
            queue_profile_skills(worker_tasks, req.user_id, req.message, user_message.get("message_id"), "relationship_private", str(match_doc["_id"]))
            emit({"type": "run_started", "agent_run_id": fallback_run_id})
            response = _run_private_v2_saved_turn(req, match_doc, room_id, emit, fallback_run_id)
            event_queue.put({"type": "final", "response": response})
        except Exception:
            event_queue.put({"type": "error", "agent_run_id": fallback_run_id, "reply": "我剛才沒能整理好這件事。你最想確認哪一點？"})
        finally:
            try:
                asyncio.run(worker_tasks())
            except Exception:
                pass
            event_queue.put(None)

    def event_stream():
        while True:
            event = event_queue.get()
            if event is None:
                break
            yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    threading.Thread(target=worker, name="ayue-private-stream", daemon=True).start()
    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


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
            "relationship_games.compatibility_quiz.cancelled_at": __import__('time').time()
        }}
    )
    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or match_doc
    return _public_quiz_state(refreshed, req.user_id)

@router.post("/relationship/date/invite/respond")
def respond_to_date_invite(req: DateInviteResponseRequest):
    from services.date_coordination_service import respond_to_invite
    return {"coordination": respond_to_invite(req.user_id, req.other_id, req.coordination_id, req.accepted)}


@router.get("/relationship/date/state")
def get_date_state(user_id: str, other_id: str):
    from services.date_coordination_service import get_state
    return {"coordination": get_state(user_id, other_id)}


@router.post("/relationship/date/update")
def update_date_form(req: DateUpdateRequest):
    from services.date_coordination_service import update_form
    return {"coordination": update_form(req.user_id, req.other_id, req.coordination_id, req.revision, req.form)}


@router.post("/relationship/date/confirm")
def confirm_date_form(req: DateConfirmRequest):
    from services.date_coordination_service import confirm_form
    coordination, event = confirm_form(req.user_id, req.other_id, req.coordination_id, req.revision)
    return {"coordination": coordination, "event": event}


@router.post("/relationship/date/cancel")
def cancel_date_coordination(req: CalendarActionRequest, other_id: str, coordination_id: str):
    from services.date_coordination_service import cancel_coordination_or_event
    return {"coordination": cancel_coordination_or_event(req.user_id, other_id, coordination_id)}



@router.get("/proactive_check")
def proactive_check(user_id: str, conversation_active: bool = False):
    user_doc = profiles_coll.find_one({"user_id": user_id})
    if not user_doc:
        return {"has_new": False}

    queue_due_feedback(user_id)

    notice_doc = profiles_coll.find_one_and_update(
        {"user_id": user_id, "memory_notices.0": {"$exists": True}},
        {"$pop": {"memory_notices": -1}},
        projection={"memory_notices": 1},
        return_document=ReturnDocument.BEFORE
    )
    if notice_doc and notice_doc.get("memory_notices"):
        notice = notice_doc["memory_notices"][0]
        return {
            "has_new": True, "surface": "ephemeral_notice", "type": "memory_learned",
            "message": notice.get("message"), "memory": notice.get("memory")
        }

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
            and (event_type in RELATIONSHIP_EVENT_TYPES or event.get("match_id"))
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

        room_id = generate_room_id(user_id, "ai_assistant")
        message_type = "mediator_card" if event_type in {"match_proposal", "incoming_match_interest"} else "text"
        if event_type in {"match_proposal", "incoming_match_interest"}:
            match_id = event.get("match_id")
            live_match = None
            if match_id:
                try:
                    from bson.objectid import ObjectId
                    live_match = matches_coll.find_one({
                        "_id": ObjectId(match_id), "status": {"$in": ["draft", "pending"]},
                        "$or": [{"from_user": user_id}, {"to_user": user_id}],
                    })
                except Exception:
                    live_match = None
            if not live_match:
                return {"has_new": False, "stale": True}
        save_message(room_id, "ai_assistant", event.get("message", "阿月有一則新的媒合消息。"),
                     message_type=message_type, metadata=message_metadata)
        return {
            "has_new": True, "surface": "global_mediator", "message": event.get("message"),
            "type": event_type, "matches": event.get("matches", []),
            "metadata": message_metadata, "debug_info": event.get("debug_info", [])
        }
    if not conversation_active:
        marker = consume_proactive_delivery(user_id)
        if marker:
            return {
                "has_new": True, "message": marker["message"], "type": "proactive_care",
                "surface": "global_mediator", "metadata": {"event_type": "proactive_care"},
            }
    return {"has_new": False}

@router.post("/demo/reset_db_state")
def reset_db_state():
    from database import profiles_coll, matches_coll, messages_coll
    messages_coll.delete_many({"room_id": {"$regex": "mediator_private"}})
    matches_coll.update_many({}, {"$unset": {"date_coordination": ""}})
    profiles_coll.update_many({}, {"$set": {"mediator_inbox": []}})
    print("\n[DEBUG] Demo Tool: Database reset successfully!\n")
    return {"status": "success", "message": "DB state reset"}


