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

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse

from database import matches_coll, messages_coll, profiles_coll
from models import DirectChatRequest
from services.ai_service import generate_chat_completion
from services.ayue_agent import run_public_agent_turn_v3
from services.ayue_agent.contracts import AgentTurnContext
from services.assessment_session_service import assessment_public_state
from services.ayue_agent.proactive_care import record_proactive_activity
from services.ayue_agent.onboarding import complete_public_ayue_onboarding
from services.ayue_agent.product_identity import PUBLIC_RETRY_REPLY, PUBLIC_RUNTIME_ERROR_REPLY
from services.ayue_agent.public_relationship_projection import (
    mentioned_contact_refs,
    validated_mentioned_contact_ids,
)
from services.ayue_agent.v3.debug_trace import (
    finish_run as finish_debug_run,
    local_debug_enabled,
)
from services.chat_service import (
    generate_room_id,
    save_message,
    save_pair_owner_message_once,
    save_system_message_once,
)
from services.profile_skills import profile_skills_mode_for_user
from services.profile_task_service import queue_profile_skills as _queue_profile_skills
from services.relationship_engagement_service import (
    find_accepted_match,
    mark_post_chat_activity,
    summarize_relationship,
)
from services.semantic_plan_service import process_relationship_semantic_plan, track_message_metrics
from services.risk_policy_service import pair_message_risk_gate


router = APIRouter()
MATCH_READINESS_THRESHOLD = 75


def queue_profile_skills(
    background_tasks, user_id: str, message: str, message_id: str | None,
    surface: str, match_id: str | None = None, *, progress_token: str | None = None,
) -> str:
    """Compatibility facade for public-chat callers and their test seam."""
    return _queue_profile_skills(
        background_tasks, user_id, message, message_id, surface, match_id,
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


def _public_request_message(req: DirectChatRequest) -> str:
    """Canonicalize typed UI actions before saving or entering the agent."""
    if req.assessment_action == "cancel":
        return "退出測驗"
    return str(req.message or "")


def _complete_public_turn(
    req: DirectChatRequest,
    room_id: str,
    requested_mentions: list[str],
    mention_overflow: bool = False,
    on_progress=None,
    on_token=None,
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
        message=_public_request_message(req),
        assessment_action=req.assessment_action,
        message_id=user_message_id,
        mentioned_ids=requested_mentions,
        mention_overflow=mention_overflow,
        user_profile=user_doc or {},
        recent_history=history,
    )
    agent_result = run_public_agent_turn_v3(
        agent_ctx, on_progress=on_progress, on_token=on_token,
        debug_enabled=debug_enabled,
    )
    complete_public_ayue_onboarding(req.user_id)
    latest_profile = profiles_coll.find_one(
        {"user_id": req.user_id}, {"_id": 0, "agentic_assessment_session": 1},
    ) or {}
    assessment_state = assessment_public_state(latest_profile)
    ai_reply = agent_result.reply or PUBLIC_RETRY_REPLY
    reply_messages = [str(item).strip() for item in (agent_result.messages or []) if str(item).strip()][:3]
    if not reply_messages:
        reply_messages = [ai_reply]
    ai_reply = "\n\n".join(reply_messages)
    sources = agent_result.sources[:5]
    place_cards = agent_result.place_cards[:8]
    presentation_blocks = [
        block.model_dump(mode="json", exclude_none=True)
        for block in (agent_result.presentation_blocks or [])[:12]
    ]
    metadata = {}
    run_id = str(agent_result.agent_run_id or "")
    if re.fullmatch(r"[a-f0-9]{32}", run_id):
        metadata["agent_run_id"] = run_id
    if sources:
        metadata["sources"] = sources
    if place_cards:
        metadata["place_cards"] = place_cards
    if presentation_blocks:
        metadata["presentation_blocks"] = presentation_blocks
    if len(reply_messages) > 1:
        metadata["presentation_messages"] = reply_messages
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
            progress_token=candidate_run_key,
        )
        if profile_skill_mode in {"on", "shadow"}:
            profile_process_run_key = candidate_run_key
    return {
        "reply": ai_reply,
        "messages": reply_messages,
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
        "assessment_state": agent_result.assessment_state
        if agent_result.assessment_state is not None else assessment_state["assessment_state"],
        "assessment_kind": agent_result.assessment_kind
        if agent_result.assessment_kind is not None else assessment_state["assessment_kind"],
        "assessment_revision": agent_result.assessment_revision
        if agent_result.assessment_revision is not None else assessment_state["assessment_revision"],
        "sources": sources,
        "place_cards": place_cards,
        "presentation_blocks": presentation_blocks,
        "llm_call_metrics": agent_result.llm_call_metrics or [],
    }


def _run_public_stream_turn(
    req: DirectChatRequest, background_tasks: BackgroundTasks, on_progress,
    *, on_token=None, debug_enabled: bool = False,
) -> dict:
    """Public-only stream path; mirrors the V3 branch of direct_chat exactly once."""
    room_id = generate_room_id(req.user_id, req.contact_id)
    requested_mentions, mention_overflow = _validated_requested_mentions(req)
    request_message = _public_request_message(req)
    display_message = request_message
    if requested_mentions and not req.mentions_inline:
        display_message = f"{_mention_display_prefix(req.user_id, requested_mentions)} {request_message}".strip()
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
        on_token=on_token, background_tasks=background_tasks,
        user_message_id=user_message.get("message_id"),
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
    if event_type == "token":
        text = str(event.get("text") or "")
        if not text:
            return None
        return {
            "type": "token",
            "agent_run_id": run_id or None,
            "text": text[:600],
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
            if req.contact_id == "ai_assistant":
                def emit_token(fragment: str) -> None:
                    emit({"type": "token", "agent_run_id": state["agent_run_id"], "text": fragment})
                if debug_enabled:
                    response = _run_public_stream_turn(
                        req, worker_background_tasks, emit,
                        on_token=emit_token, debug_enabled=True,
                    )
                else:
                    response = _run_public_stream_turn(
                        req, worker_background_tasks, emit, on_token=emit_token,
                    )
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
    """Handle Public Ayue with V3, or preserve the existing pair-chat adapter."""
    room_id = generate_room_id(req.user_id, req.contact_id)
    requested_mentions, mention_overflow = _validated_requested_mentions(req)
    request_message = _public_request_message(req)
    display_message = request_message
    if req.contact_id == "ai_assistant" and requested_mentions and not req.mentions_inline:
        display_message = f"{_mention_display_prefix(req.user_id, requested_mentions)} {request_message}".strip()
    owner_raw_content = _owner_profile_message(req, requested_mentions)
    user_metadata = {}
    if display_message != owner_raw_content:
        user_metadata["owner_raw_content"] = owner_raw_content
    mention_labels = [item["display_name"] for item in mentioned_contact_refs(req.user_id, requested_mentions)]
    if mention_labels:
        user_metadata["mention_labels"] = mention_labels
    if req.contact_id == "ai_assistant":
        user_message = save_message(
            room_id, req.user_id, display_message, metadata=user_metadata or None,
        )
        record_proactive_activity(req.user_id)
        return _complete_public_turn(
            req, room_id, requested_mentions, mention_overflow,
            background_tasks=background_tasks,
            user_message_id=user_message.get("message_id"),
        )

    match_doc = find_accepted_match(req.user_id, req.contact_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能傳送訊息給已接受的配對")

    client_message_id = req.client_message_id or uuid.uuid4().hex

    # Image messages carry no analyzable text; skip the risk gate and any
    # text-driven assist so an empty message never reaches the LLM.
    if req.file_id:
        risk_projection = {
            "level": "safe",
            "ui_priority": "coach",
            "delivery": "delivered",
        }
        user_message = save_pair_owner_message_once(
            room_id,
            req.user_id,
            "",
            client_message_id=client_message_id,
            risk_projection=risk_projection,
            message_type="image",
            file_id=req.file_id,
        )
        if not user_message.get("created"):
            return {
                "reply": "",
                "duplicate": True,
                "risk_assessment": risk_projection,
                "ui_priority": risk_projection["ui_priority"],
            }
        profiles_coll.update_one(
            {"user_id": req.user_id}, {"$set": {"last_user_activity_at": time.time()}}, upsert=True,
        )
        return {
            "reply": "",
            "image_sent": True,
            "risk_assessment": risk_projection,
            "ui_priority": risk_projection["ui_priority"],
        }

    risk_decision = pair_message_risk_gate.evaluate(
        conversation_id=room_id,
        sender_id=req.user_id,
        receiver_id=req.contact_id,
        content=req.message,
        idempotency_key=f"{room_id}:{req.user_id}:{client_message_id}",
    )
    risk_projection = risk_decision.public_projection()
    if not risk_decision.may_persist:
        # 被攔截的訊息不會以 receiver 可見的訊息儲存；改寫一則系統通知卡，
        # 讓 receiver 知道系統攔截了一則可能造成不適的訊息，並可回報感受。
        # metadata 內帶上 receiver_directive，使收件端從歷史訊息也能依指令渲染。
        notice_metadata = {"event_type": "blocked_notice", "risk": dict(risk_projection)}
        if risk_projection.get("receiver_directive"):
            notice_metadata["receiver_directive"] = dict(risk_projection["receiver_directive"])
        save_system_message_once(
            room_id,
            "系統攔截了一則可能讓你感到不適的訊息，你可以繼續或結束這段對話。",
            message_type="system",
            metadata=notice_metadata,
            event_key=f"blocked-notice:{client_message_id}",
        )
        return {
            "reply": "",
            "is_blocked": True,
            "risk_assessment": risk_projection,
            "ui_priority": risk_projection["ui_priority"],
        }

    user_message = save_pair_owner_message_once(
        room_id,
        req.user_id,
        req.message,
        client_message_id=client_message_id,
        risk_projection=risk_projection,
    )
    if not user_message.get("created"):
        return {
            "reply": "",
            "duplicate": True,
            "risk_assessment": risk_projection,
            "ui_priority": risk_projection["ui_priority"],
        }

    profiles_coll.update_one(
        {"user_id": req.user_id}, {"$set": {"last_user_activity_at": time.time()}}, upsert=True,
    )
    background_tasks.add_task(
        check_and_trigger_date_activation, room_id, req.user_id, req.contact_id, req.message, match_doc,
    )
    background_tasks.add_task(process_relationship_semantic_plan, match_doc, room_id)
    background_tasks.add_task(
        ai_process_date_coordination_step, room_id, req.user_id, req.contact_id, req.message, match_doc,
    )

    # Ayue may provide one opening assist when a newly matched pair sends its
    # first message. Once either participant has already spoken in this room,
    # pair chat must remain person-to-person and must never impersonate the
    # receiver with another generated reply.
    pair_message_count = messages_coll.count_documents({
        "room_id": room_id,
        "sender_id": {"$in": [req.user_id, req.contact_id]},
        "message_type": "text",
        "is_blocked": {"$ne": True},
    })
    message_count = mark_post_chat_activity(match_doc, room_id)
    if pair_message_count > 1:
        if match_doc and message_count >= 2:
            track_message_metrics(room_id)
        if match_doc and message_count >= 6:
            background_tasks.add_task(summarize_relationship, match_doc["_id"], room_id)
        return {
            "reply": "",
            "opening_assist": False,
            "feedback_scheduled": True,
            "risk_assessment": risk_projection,
            "ui_priority": risk_projection["ui_priority"],
        }

    target_doc = profiles_coll.find_one({"user_id": req.contact_id}) or {}
    history = list(messages_coll.find(
        {"room_id": room_id, "is_blocked": {"$ne": True}},
    ).sort("timestamp", -1).limit(20))[::-1]
    prompt = (
        "你是聊天中的阿月，請自然協助使用者和對方聊天。保持簡潔、真誠，不要假裝知道未提供的事。\n"
        f"對方公開資料：{json.dumps(target_doc.get('big_five', {}), ensure_ascii=False)}\n"
        + "\n".join(
            f"{'使用者' if item.get('sender_id') == req.user_id else '對方'}：{item.get('content', '')}"
            for item in history
        )
        + f"\n使用者最新訊息：{req.message}"
    )
    try:
        reply = generate_chat_completion(prompt, temperature=0.7, json_output=False).content
    except Exception as exc:
        print(f"Chat error (User {req.contact_id}): {type(exc).__name__}")
        reply = "我先陪你把這段聊完，剛剛回覆沒有成功，再試一次就好。"
    save_message(
        room_id,
        req.contact_id,
        reply,
        metadata={"event_type": "conversation_opening_assist"},
    )
    if match_doc and message_count >= 2:
        track_message_metrics(room_id)
    if match_doc and message_count >= 6:
        background_tasks.add_task(summarize_relationship, match_doc["_id"], room_id)
    return {
        "reply": reply,
        "opening_assist": True,
        "feedback_scheduled": True,
        "risk_assessment": risk_projection,
        "ui_priority": risk_projection["ui_priority"],
    }
