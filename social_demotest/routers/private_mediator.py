"""Private mediator HTTP adapters for the current isolated V2 runtime."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import queue
import threading
import time
import uuid

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse

from database import matches_coll, messages_coll, profiles_coll
from models import MediatorPrivateRequest
from services.ayue_agent.private_contracts import PrivateClientAction
from services.ayue_agent.private_v2 import run_private_agent_turn_v2
from services.ayue_agent.product_identity import PRIVATE_RUNTIME_FALLBACK_REPLY
from services.ayue_agent.v3.debug_trace import local_debug_enabled
from services.chat_service import generate_room_id, save_message
from services.profile_task_service import queue_profile_skills  # compatibility import; Private never invokes it
from services.relationship_engagement_service import (
    consume_pending_probe_answer,
    find_accepted_match,
    generate_mediator_private_room_id,
    participant_probe_state,
    relationship_unread_field,
)

router = APIRouter()


def _is_loopback_debug_request(request: Request | None) -> bool:
    if request is None or not local_debug_enabled() or request.client is None:
        return False
    try:
        client_ok = ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        client_ok = request.client.host == "localhost"
    host = (request.url.hostname or "").lower()
    try:
        host_ok = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        host_ok = host == "localhost"
    return client_ok and host_ok

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



def save_private_mediator_reply(room_id: str, reply: str, event_type="text", actions=None, handoff=None):

    message_type = "mediator_card" if actions else "text"
    metadata = {"event_type": event_type, "actions": actions or []}
    if handoff:
        metadata["handoff"] = handoff

    save_message(

        room_id, "ai_assistant", reply, message_type=message_type,

        metadata=metadata

    )


def _run_private_v2_saved_turn(req: MediatorPrivateRequest, match_doc: dict, room_id: str, on_progress=None, agent_run_id: str | None = None, *, debug_enabled: bool = False) -> dict:
    """Persist exactly one private V2 final after the owner message is saved."""
    result = run_private_agent_turn_v2(
        user_id=req.user_id, other_id=req.other_id, message=req.message,
        match_doc=match_doc, on_progress=on_progress, agent_run_id=agent_run_id,
        debug_enabled=debug_enabled,
    )
    reply = result.reply or PRIVATE_RUNTIME_FALLBACK_REPLY
    handoff = result.handoff.model_dump() if getattr(result, "handoff", None) else None
    actions = []
    if handoff:
        actions = [PrivateClientAction(
            kind="navigate_public_prefill",
            label="回阿月主聊天室 →",
            value=handoff["original_message"],
        ).model_dump()]
    event_type = "agentic_private_redirect" if handoff else "agentic_private_v2"
    save_private_mediator_reply(room_id, reply, event_type, actions=actions, handoff=handoff)
    return {
        "reply": reply, "pending_step": None, "agent_run_id": result.agent_run_id,
        "agent_mode": "v2", "agent_version": "v2", "conversation_intent": result.conversation_intent,
        "handoff": handoff,
        "actions": actions,
    }



@router.post("/mediator/private")

def mediator_private_chat(req: MediatorPrivateRequest, background_tasks: BackgroundTasks, request: Request = None):

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

    user_doc = profiles_coll.find_one({"user_id": req.user_id}) or {}
    probe_reply = consume_pending_probe_answer(match_doc, user_doc, req.user_id, req.other_id, req.message)
    if probe_reply is not None:
        save_private_mediator_reply(room_id, probe_reply)
        return {"reply": probe_reply, "pending_step": None}

    # Current Private V2 owns every accepted-pair turn. It must not fall
    # through into the removed legacy keyword/free-form runtime.
    debug_enabled = _is_loopback_debug_request(request)
    if debug_enabled:
        return _run_private_v2_saved_turn(req, match_doc, room_id, debug_enabled=True)
    return _run_private_v2_saved_turn(req, match_doc, room_id)

@router.post("/mediator/private/stream")
def mediator_private_chat_stream(req: MediatorPrivateRequest, background_tasks: BackgroundTasks, request: Request = None):
    """NDJSON stream for the current Private V2 runtime."""
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
            emit({"type": "run_started", "agent_run_id": fallback_run_id})
            debug_enabled = _is_loopback_debug_request(request)
            if debug_enabled:
                response = _run_private_v2_saved_turn(req, match_doc, room_id, emit, fallback_run_id, debug_enabled=True)
            else:
                response = _run_private_v2_saved_turn(req, match_doc, room_id, emit, fallback_run_id)
            event_queue.put({"type": "final", "response": response})
        except Exception:
            event_queue.put({"type": "error", "agent_run_id": fallback_run_id, "reply": PRIVATE_RUNTIME_FALLBACK_REPLY})
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
