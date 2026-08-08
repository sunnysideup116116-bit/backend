"""Public direct-chat HTTP adapters and V3 orchestration.

This module owns only /api/direct_chat and /api/direct_chat/stream. Public
Ayue delegates to services.ayue_agent.v3.scheduler; there is no legacy
public runtime anymore.
"""

import asyncio
import ipaddress
import json
import queue
import re
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse

from config import OLLAMA_FAST_CHAT_MODEL
from database import matches_coll, messages_coll, profiles_coll
from models import DirectChatRequest
from services.ai_service import generate_chat_completion, get_embedding
from services.ayue_agent import run_public_agent_turn_v3
from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.proactive_care import record_proactive_activity
from services.ayue_agent.product_identity import (
    PUBLIC_AYUE_PERSONA,
    PUBLIC_RETRY_REPLY,
    PUBLIC_RUNTIME_ERROR_REPLY,
)
from services.ayue_agent.public_relationship_projection import (
    mentioned_contact_refs,
    validated_mentioned_contact_ids,
)
from services.ayue_agent.v3.scheduler import agent_mode_for_user_v3
from services.ayue_agent.v3.debug_trace import (
    finish_run as finish_debug_run,
    local_debug_enabled,
)
from services.chat_service import generate_room_id, save_message
from services.match_state_service import verified_accepted_match_query
from services.mediator_context_service import (
    latest_shared_chat,
    mediator_style,
    relevant_graph_memories,
)
from services.profile_projection import safe_recent_context
from services.profile_skills import profile_skills_mode_for_user
from services.profile_task_service import queue_profile_skills as _queue_profile_skills
from services.relationship_engagement_service import (
    find_accepted_match,
    mark_post_chat_activity,
    summarize_relationship,
    trigger_proactive_match,
)
from services.semantic_plan_service import process_relationship_semantic_plan, track_message_metrics


router = APIRouter()
MATCH_READINESS_THRESHOLD = 75


def queue_profile_skills(
    background_tasks, user_id: str, message: str, message_id: str | None,
    surface: str, match_id: str | None = None, *, allow_legacy_fallback: bool = True,
    progress_token: str | None = None,
) -> str:
    """Compatibility facade for public-chat callers and their test seam."""
    return _queue_profile_skills(
        background_tasks, user_id, message, message_id, surface, match_id,
        allow_legacy_fallback=allow_legacy_fallback,
        mode_resolver=profile_skills_mode_for_user,
        progress_token=progress_token,
    )

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

        result = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True).content)

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


def _owner_profile_message(req: DirectChatRequest, mentioned_ids: list[str]) -> str:
    """Keep inline mention labels out of the owner-only profile extraction input."""
    text = str(req.message or "")
    if not req.mentions_inline:
        return text
    for item in mentioned_contact_refs(req.user_id, mentioned_ids):
        label = str(item.get("display_name") or "").strip()
        if label:
            text = text.replace("@" + label, "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _complete_public_turn(
    req: DirectChatRequest,
    room_id: str,
    requested_mentions: list[str],
    mention_overflow: bool = False,
    on_progress=None,
    background_tasks: BackgroundTasks | None = None,
    user_message_id: str | None = None,
    debug_enabled: bool = False,
) -> dict:
    """Run and persist one V3 turn after the owner message has been saved."""
    history = list(messages_coll.find({"room_id": room_id}).sort("timestamp", -1).limit(12))[::-1]
    user_doc = profiles_coll.find_one({"user_id": req.user_id})
    agent_ctx = AgentTurnContext(
        user_id=req.user_id,
        room_id=room_id,
        message=req.message,
        message_id=user_message_id,
        mentioned_ids=requested_mentions,
        mention_overflow=mention_overflow,
        user_profile=user_doc or {},
        recent_history=history,
    )
    agent_result = run_public_agent_turn_v3(
        agent_ctx, mode="on", on_progress=on_progress, debug_enabled=debug_enabled,
    )
    ai_reply = agent_result.reply or PUBLIC_RETRY_REPLY
    sources = agent_result.sources[:5]
    place_cards = agent_result.place_cards[:8]
    metadata = {}
    if sources:
        metadata["sources"] = sources
    if place_cards:
        metadata["place_cards"] = place_cards
    if metadata:
        save_message(room_id, "ai_assistant", ai_reply, metadata=metadata)
    else:
        save_message(room_id, "ai_assistant", ai_reply)
    # Assessment answers are a separate, owner-scoped workflow. They must not
    # become recent-context or durable-memory evidence. Other saved public
    # messages still reach the isolated extractor.
    profile_skill_mode = "off"
    profile_process_run_key = None
    if (
        background_tasks is not None and user_message_id
        and agent_result.profile_write_reason != "assessment"
    ):
        candidate_run_key = uuid.uuid4().hex
        owner_profile_message = _owner_profile_message(req, requested_mentions)
        profile_skill_mode = queue_profile_skills(
            background_tasks, req.user_id, owner_profile_message, user_message_id, "global",
            allow_legacy_fallback=False,
            progress_token=candidate_run_key,
        )
        if profile_skill_mode in {"on", "shadow"}:
            profile_process_run_key = candidate_run_key
    return {
        "reply": ai_reply,
        "is_locked": False,
        "conversation_intent": agent_result.conversation_intent,
        "calendar_state_changed": agent_result.calendar_state_changed,
        "mentioned_other_ids": agent_result.mentioned_other_ids,
        "context_changed": agent_result.context_changed,
        "context_confirmation_needed": agent_result.context_confirmation_needed,
        "profile_update_pending": bool(profile_process_run_key),
        "profile_process_run_key": profile_process_run_key,
        "agent_run_id": agent_result.agent_run_id,
        "agent_mode": agent_result.agent_mode,
        "agent_version": "v3",
        "match_readiness_state": agent_result.match_readiness_state,
        "match_guidance_shown": agent_result.match_guidance_shown,
        "assessment_state": agent_result.assessment_state,
        "assessment_kind": agent_result.assessment_kind,
        "assessment_revision": agent_result.assessment_revision,
        "sources": sources,
        "place_cards": place_cards,
        "llm_call_metrics": agent_result.llm_call_metrics or [],
    }


def _run_public_stream_turn(
    req: DirectChatRequest, background_tasks: BackgroundTasks, on_progress,
    *, debug_enabled: bool = False,
) -> dict:
    """Public-only stream path; mirrors the V3 branch of direct_chat exactly once."""
    room_id = generate_room_id(req.user_id, req.contact_id)
    requested_mentions, mention_overflow = _validated_requested_mentions(req)
    display_message = req.message
    if requested_mentions and not req.mentions_inline:
        display_message = f"{_mention_display_prefix(req.user_id, requested_mentions)} {req.message}".strip()
    owner_raw_content = _owner_profile_message(req, requested_mentions)
    user_metadata = {}
    if display_message != owner_raw_content:
        user_metadata["owner_raw_content"] = owner_raw_content
    mention_labels = [item["display_name"] for item in mentioned_contact_refs(req.user_id, requested_mentions)]
    if mention_labels:
        user_metadata["mention_labels"] = mention_labels
    user_message = save_message(
        room_id, req.user_id, display_message,
        metadata=user_metadata or None,
    )
    profiles_coll.update_one(
        {"user_id": req.user_id}, {"$set": {"last_user_activity_at": time.time()}}, upsert=True,
    )
    return _complete_public_turn(
        req, room_id, requested_mentions, mention_overflow, on_progress,
        background_tasks=background_tasks, user_message_id=user_message.get("message_id"),
        debug_enabled=debug_enabled,
    )


def _is_loopback_debug_request(request: Request | None) -> bool:
    if request is None or not local_debug_enabled() or request.client is None:
        return False
    try:
        client_is_loopback = ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        client_is_loopback = request.client.host == "localhost"
    hostname = (request.url.hostname or "").lower()
    try:
        host_is_loopback = hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        host_is_loopback = hostname == "localhost"
    return client_is_loopback and host_is_loopback


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
            "text": str(event.get("text") or "我確認一下…")[:200],
        }
    if event_type == "tool_finished" and run_id:
        outcome = "ok" if event.get("outcome") == "ok" else "error"
        return {
            "type": "tool_finished",
            "agent_run_id": run_id,
            "outcome": outcome,
            "duration_ms": max(0, int(event.get("duration_ms") or 0)),
        }
    if event_type == "final" and isinstance(event.get("response"), dict):
        return {"type": "final", "response": event["response"]}
    if event_type == "error":
        return {
            "type": "error",
            "agent_run_id": run_id,
            "reply": PUBLIC_RUNTIME_ERROR_REPLY,
        }
    return None


@router.post("/direct_chat/stream")
def direct_chat_stream(
    req: DirectChatRequest, background_tasks: BackgroundTasks, request: Request = None,
):
    """NDJSON public-agent events; legacy and private chat retain direct JSON."""
    event_queue: queue.Queue[dict | None] = queue.Queue(maxsize=16)
    fallback_run_id = uuid.uuid4().hex
    state = {"agent_run_id": fallback_run_id}
    debug_enabled = _is_loopback_debug_request(request)

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
            if req.contact_id == "ai_assistant" and agent_mode_for_user_v3(req.user_id) == "on":
                if debug_enabled:
                    response = _run_public_stream_turn(
                        req, worker_background_tasks, emit, debug_enabled=True,
                    )
                else:
                    response = _run_public_stream_turn(req, worker_background_tasks, emit)
            else:
                # Stream is intentionally optional for legacy/private contacts;
                # preserve their established direct-chat behavior as one final event.
                response = direct_chat(req, worker_background_tasks)
            emit({"type": "final", "response": response})
        except Exception:
            if debug_enabled:
                finish_debug_run(state["agent_run_id"], status="error")
            emit({
                "type": "error", "agent_run_id": state["agent_run_id"],
                "reply": PUBLIC_RUNTIME_ERROR_REPLY,
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

    if req.contact_id == "ai_assistant" and requested_mentions and not req.mentions_inline:

        prefix = _mention_display_prefix(req.user_id, requested_mentions)

        display_message = f"{prefix} {req.message}".strip()

    owner_raw_content = _owner_profile_message(req, requested_mentions)
    user_metadata = {}
    if display_message != owner_raw_content:
        user_metadata["owner_raw_content"] = owner_raw_content
    mention_labels = [item["display_name"] for item in mentioned_contact_refs(req.user_id, requested_mentions)]
    if mention_labels:
        user_metadata["mention_labels"] = mention_labels
    user_message = save_message(
        room_id, req.user_id, display_message,
        metadata=user_metadata or None,
    )

    latest_assistant_message = None
    if req.contact_id == "ai_assistant":
        latest_assistant_message = messages_coll.find_one(
            {"room_id": room_id, "sender_id": "ai_assistant"},
            sort=[("timestamp", -1)],
        )
    # V3 owns every public-agent turn when enabled. There is no legacy public
    # runtime anymore; the V3 allowlist is the only rollout gate.
    v3_mode = agent_mode_for_user_v3(req.user_id) if req.contact_id == "ai_assistant" else "off"
    public_agent_mode = "v3" if v3_mode == "on" else "off"
    if public_agent_mode == "v3":
        profile_skill_mode = "deferred_to_public_agent"
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

        if public_agent_mode == "v3":
            return _complete_public_turn(
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



        sys_prompt = f"""{PUBLIC_AYUE_PERSONA}

媒人語氣：{mediator_style(req.user_id)}



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

            ai_res = json.loads(ai_res_str.content)

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

            ai_reply = "我剛剛沒有整理好。你最想先確認哪一點？"

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

            reply = generate_chat_completion(prompt, temperature=0.7, json_output=False).content

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
