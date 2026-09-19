"""Private mediator HTTP adapters for the isolated Private Pi runtime."""

from __future__ import annotations

import asyncio
from contextvars import copy_context
from agent_quota.api import budgeted
from agent_quota.service import unmetered
import json
import queue
import threading
import time
import uuid
from contextvars import ContextVar

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse

from database import matches_coll, messages_coll, profiles_coll
from models import MediatorPrivateRequest
from services.appwrite_identity_service import authenticated_owner_matches
from services.ayue_agent.private_contracts import PrivateClientAction
from services.ayue_agent.private_pi.runtime import run_private_pi_turn
from services.ayue_agent.private_v2 import (
    mark_private_confirmation_presented,
)
from services.chat_service import save_message
from services.profile_task_service import queue_profile_skills  # compatibility import; Private never invokes it
from services.relationship_engagement_service import (
    cleanup_legacy_private_probe_state,
    find_accepted_match,
    generate_mediator_private_room_id,
    relationship_unread_field,
)

router = APIRouter()
_PRIVATE_ADAPTER_ERROR_REPLY = "這次私聊服務沒有完成有效回覆，請稍後再試。"

# Keep the old symbol name as a compatibility seam for tests and downstream
# integrations while routing production turns through the isolated Private Pi
# runtime.  A request-local source id avoids a second "latest message" query
# without changing the public endpoint/request contract.
run_private_agent_turn_v2 = run_private_pi_turn
_SOURCE_MESSAGE_ID: ContextVar[str] = ContextVar(
    "private_source_message_id", default="",
)

@router.get("/mediator/private/{other_id}")

def get_mediator_private_messages(other_id: str, user_id: str):

    match_doc = find_accepted_match(user_id, other_id)

    if not match_doc:

        raise HTTPException(status_code=403, detail="只能查看已接受配對的媒人私聊")

    room_id = generate_mediator_private_room_id(user_id, other_id)

    if messages_coll.count_documents({"room_id": room_id}) == 0:

        save_message(
            room_id,
            "ai_assistant",
            f"這裡可以私下跟我打聽 {other_id}，我會盡量只講有根據的事。",
            metadata={"notification_eligible": False, "event_type": "private_welcome"},
        )

    unread_field = relationship_unread_field(match_doc, user_id)

    unread_count = int((match_doc.get("private_unread", {}) or {}).get(

        "from" if match_doc.get("from_user") == user_id else "to", 0

    ))

    matches_coll.update_one({"_id": match_doc["_id"]}, {"$set": {unread_field: 0}})

    user_doc = profiles_coll.find_one({"user_id": user_id}) or {}
    cleanup_legacy_private_probe_state(user_id, user_doc=user_doc)
    user_doc = {
        key: value for key, value in user_doc.items()
        if key not in {
            "pending_private_feedback",
            "pending_feedback_match_id",
            "pending_feedback_other_id",
        }
    }

    pending = user_doc.get("pending_private_feedback") or {}

    if pending.get("other_id") != other_id:

        pending = {}

    pending_date = user_doc.get("pending_date_coordination") or {}

    if pending_date.get("other_id") != other_id:

        pending_date = {}

    pending_post_date = user_doc.get("pending_post_date_feedback") or {}
    if pending_post_date.get("other_id") != other_id:
        pending_post_date = {}

    msgs = list(messages_coll.find({"room_id": room_id}, {"_id": 0}).sort("timestamp", 1))

    return {

        "messages": msgs,

        "other_id": other_id,

        "unread_count": unread_count,

        "pending_step": pending.get("stage") or (

            "date_" + pending_date.get("stage") if pending_date.get("stage") else (
                "post_date_feedback" if pending_post_date else None
            )

        ),

        "probe_state": {},

        "other_probe_state": {},

        "mediator_tone": user_doc.get("mediator_tone", "friend")

    }



def save_private_mediator_reply(
    room_id: str,
    reply: str,
    event_type="text",
    actions=None,
    handoff=None,
    choice_prompt=None,
    relationship_memory_entry=None,
):

    message_type = "mediator_card" if actions else "text"
    metadata = {"event_type": event_type, "actions": actions or []}
    if handoff:
        metadata["handoff"] = handoff
    if choice_prompt:
        metadata["choice_prompt"] = dict(choice_prompt)
    if relationship_memory_entry:
        metadata["relationship_memory_entry"] = dict(relationship_memory_entry)

    return save_message(

        room_id, "ai_assistant", reply, message_type=message_type,

        metadata=metadata

    )


def _run_private_v2_saved_turn(
    req: MediatorPrivateRequest,
    match_doc: dict,
    room_id: str,
    on_progress=None,
    agent_run_id: str | None = None,
    on_token=None,
    external_calendar_authorized: bool = False,
    source_message_id: str | None = None,
) -> dict:
    """Persist exactly one Private Pi final after the owner message is saved.

    The Pi runtime decides whether a message is a post-date answer, probe
    answer, or memory candidate.  The adapter must not pre-consume those
    workflows based only on the fact that a user message arrived.
    """
    if source_message_id is None:
        source_message_id = _SOURCE_MESSAGE_ID.get("")
    if req.choice_action is None and not source_message_id:
        try:
            source = messages_coll.find_one(
                {"room_id": room_id, "sender_id": req.user_id},
                {"_id": 1},
                sort=[("timestamp", -1), ("_id", -1)],
            ) or {}
        except Exception:
            source = {}
        source_message_id = str(source.get("_id") or "")
    result = run_private_agent_turn_v2(
        user_id=req.user_id, other_id=req.other_id, message=req.message,
        match_doc=match_doc, on_progress=on_progress, agent_run_id=agent_run_id,
        on_token=on_token,
        choice_id=req.choice_id, choice_action=req.choice_action,
        external_calendar_authorized=external_calendar_authorized,
        source_message_id=str(source_message_id or ""),
    )
    reply = str(getattr(result, "reply", "") or "").strip()
    if not reply:
        # The runtime owns timeout/error classification.  An empty adapter
        # result is an adapter failure and must not masquerade as a provider
        # timeout through the conversational fallback copy.
        reply = _PRIVATE_ADAPTER_ERROR_REPLY
    handoff = result.handoff.model_dump() if getattr(result, "handoff", None) else None
    memory_entry = (
        result.relationship_memory_entry.model_dump()
        if getattr(result, "relationship_memory_entry", None)
        else None
    )
    actions = []
    if handoff:
        actions = [PrivateClientAction(
            kind="navigate_public_prefill",
            label="回阿月主聊天室 →",
            value=handoff["original_message"],
        ).model_dump()]
    event_type = "agentic_private_redirect" if handoff else "agentic_private_pi"
    saved_reply = save_private_mediator_reply(
        room_id,
        reply,
        event_type,
        actions=actions,
        handoff=handoff,
        choice_prompt=result.choice_prompt,
        relationship_memory_entry=memory_entry,
    )
    if result.choice_prompt and saved_reply.get("message_id"):
        mark_private_confirmation_presented(
            user_id=req.user_id,
            origin_run_id=str(result.agent_run_id or ""),
            message_id=str(saved_reply["message_id"]),
            persisted_content=str(saved_reply.get("content") or ""),
        )
    agent_mode = str(getattr(result, "agent_mode", "") or "pi")
    agent_version = (
        "pi_private.v1" if agent_mode == "pi"
        else str(getattr(result, "agent_version", "") or "v2")
    )
    return {
        "reply": reply, "pending_step": None, "agent_run_id": result.agent_run_id,
        "agent_mode": agent_mode, "agent_version": agent_version,
        "conversation_intent": result.conversation_intent,
        "handoff": handoff,
        "actions": actions,
        "choice_prompt": result.choice_prompt,
        "choice_resolution": result.choice_resolution,
        "relationship_memory_entry": memory_entry,
    }



@router.post("/mediator/private")

@budgeted("private")
def mediator_private_chat(
    req: MediatorPrivateRequest,
    background_tasks: BackgroundTasks,
    request: Request = None,
):

    match_doc = find_accepted_match(req.user_id, req.other_id)

    if not match_doc:

        raise HTTPException(status_code=403, detail="只能在已接受配對中私聊媒人")


    cleanup_legacy_private_probe_state(req.user_id)
    room_id = generate_mediator_private_room_id(req.user_id, req.other_id)

    source_message_id = ""
    if req.choice_action is None:
        saved = save_message(room_id, req.user_id, req.message)
        source_message_id = str((saved or {}).get("message_id") or (saved or {}).get("_id") or "")

    profiles_coll.update_one(

        {"user_id": req.user_id},

        {"$set": {"last_user_activity_at": time.time()}},

        upsert=True,

    )

    # Current Private V2 owns every accepted-pair turn. It must not fall
    # through into the removed legacy keyword/free-form runtime.
    turn_options = {}
    if authenticated_owner_matches(
        request.headers.get("Authorization") if request else None,
        req.user_id,
    ):
        turn_options["external_calendar_authorized"] = True
    token = _SOURCE_MESSAGE_ID.set(source_message_id)
    try:
        return _run_private_v2_saved_turn(req, match_doc, room_id, **turn_options)
    finally:
        _SOURCE_MESSAGE_ID.reset(token)

@router.post("/mediator/private/stream")
@budgeted("private")
def mediator_private_chat_stream(
    req: MediatorPrivateRequest,
    background_tasks: BackgroundTasks,
    request: Request = None,
):
    """NDJSON stream for the current Private V2 runtime."""
    match_doc = find_accepted_match(req.user_id, req.other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能在已接受配對中私聊媒人")
    cleanup_legacy_private_probe_state(req.user_id)
    event_queue: queue.Queue[dict | None] = queue.Queue()
    fallback_run_id = uuid.uuid4().hex
    worker_done = threading.Event()
    external_calendar_authorized = authenticated_owner_matches(
        request.headers.get("Authorization") if request else None,
        req.user_id,
    )

    def emit(event: dict) -> None:
        if event.get("type") not in {
            "run_started", "stage", "tool_started", "tool_finished", "token",
        }:
            return
        safe = {
            key: event[key]
            for key in (
                "type", "agent_run_id", "step_id", "text", "outcome", "duration_ms",
            )
            if key in event
        }
        event_queue.put_nowait(safe)

    def worker() -> None:
        worker_tasks = BackgroundTasks()

        def emit_token(fragment: str) -> None:
            emit({"type": "token", "agent_run_id": fallback_run_id, "text": fragment})

        try:
            room_id = generate_mediator_private_room_id(req.user_id, req.other_id)
            source_message_id = ""
            if req.choice_action is None:
                saved = save_message(room_id, req.user_id, req.message)
                source_message_id = str((saved or {}).get("message_id") or (saved or {}).get("_id") or "")
            emit({"type": "run_started", "agent_run_id": fallback_run_id})
            turn_options = {"on_token": emit_token}
            if external_calendar_authorized:
                turn_options["external_calendar_authorized"] = True
            token = _SOURCE_MESSAGE_ID.set(source_message_id)
            try:
                response = _run_private_v2_saved_turn(
                    req,
                    match_doc,
                    room_id,
                    emit,
                    fallback_run_id,
                    **turn_options,
                )
            finally:
                _SOURCE_MESSAGE_ID.reset(token)
            event_queue.put({"type": "final", "response": response})
        except Exception:
            event_queue.put({
                "type": "error",
                "agent_run_id": fallback_run_id,
                "reply": _PRIVATE_ADAPTER_ERROR_REPLY,
            })
        finally:
            try:
                with unmetered():
                    asyncio.run(worker_tasks())
            except Exception:
                pass
            worker_done.set()
            event_queue.put(None)

    async def event_stream():
        while True:
            try:
                event = event_queue.get_nowait()
            except queue.Empty:
                if worker_done.is_set():
                    break
                await asyncio.sleep(0.01)
                continue
            if event is None:
                break
            yield json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"

    threading.Thread(target=copy_context().run, args=(worker,), name="ayue-private-stream", daemon=True).start()
    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )
