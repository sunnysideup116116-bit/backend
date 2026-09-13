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
import traceback
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import StreamingResponse

from database import matches_coll, messages_coll, profiles_coll
from models import DirectChatRequest
from services.ai_service import generate_chat_completion
from services.appwrite_identity_service import authenticated_owner_matches
from services.ayue_agent import (
    mark_public_confirmation_presented,
    run_public_agent_turn_v3,
)
from services.ayue_agent.contracts import PublicAgentRequestContext
from services.assessment_session_service import (
    assessment_public_state_for_room,
    assessment_session_for_room,
)
from services.ayue_agent.proactive_care import record_proactive_activity
from services.proactive_followup_service import record_owner_activity
from services.ayue_agent.onboarding import complete_public_ayue_onboarding
from services.ayue_agent.product_identity import PUBLIC_RETRY_REPLY, PUBLIC_RUNTIME_ERROR_REPLY
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
from services.ai_room_service import (
    ensure_room_title,
    get_room as get_ai_room,
    mark_first_message_for_title,
)
from services.conversation_compaction_service import (
    queue_conversation_compaction_shadow as _queue_conversation_compaction_shadow,
)
from services.profile_skills import profile_skills_mode_for_user
from services.profile_task_service import queue_profile_skills as _queue_profile_skills
from services.message_use_service import (
    MessageUse,
    mark_message_use,
    mark_message_use_from_turn,
)
from services.ayue_agent.v3.place_references import (
    PlaceReferencePersistenceError,
    publish_place_presentation,
)
from services.ayue_agent.v3.relationship_recommendations import (
    RecommendationPersistenceError,
    save_snapshot as save_relationship_recommendation_snapshot,
)
from services.relationship_engagement_service import (
    find_accepted_match,
    mark_post_chat_activity,
    summarize_relationship,
)
from services.ayue_agent.public_relationship_projection import (
    mentioned_contact_refs,
    validated_mentioned_contact_ids,
)
from services.semantic_plan_service import process_relationship_semantic_plan, track_message_metrics
from services.risk_policy_service import pair_message_risk_gate
from services.risk_block_service import (
    RiskBlockServiceUnavailable,
    risk_block_service,
)
from services.proposal_namespace import namespace_for_document


def _log_public_stream_exception(exc: Exception) -> None:
    """Log only diagnostic structure; never include prompts or user content."""
    frames = traceback.extract_tb(exc.__traceback__)[-6:]
    location = " > ".join(
        f"{Path(frame.filename).name}:{frame.lineno}:{frame.name}"
        for frame in frames
    )
    status_code = int(getattr(exc, "status_code", 0) or 0)
    print(
        f"[PUBLIC_CHAT_ERROR] type={type(exc).__name__} "
        f"status={status_code or '-'} location={location or '-'}",
        flush=True,
    )


router = APIRouter()
MATCH_READINESS_THRESHOLD = 75


def _validate_focused_match(req: DirectChatRequest) -> dict | None:
    """Resolve one Hub card against canonical participant/namespace state."""
    match_id = str(req.focused_match_id or "").strip()
    if not match_id:
        return None
    from bson.objectid import ObjectId

    try:
        document = matches_coll.find_one({"_id": ObjectId(match_id)})
    except Exception:
        document = None
    if not document:
        raise HTTPException(status_code=404, detail="這張牽線卡已不存在")
    if req.user_id not in {document.get("from_user"), document.get("to_user")}:
        raise HTTPException(status_code=403, detail="只能詢問自己收到的牽線卡")
    namespace = namespace_for_document(document)
    if req.focused_match_namespace and req.focused_match_namespace != namespace:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "這張牽線卡的類型已更新，請重新查看。",
                "current_namespace": namespace,
                "current_status": document.get("status"),
                "current_revision": int(document.get("proposal_revision", 0) or 0),
            },
        )
    current_revision = int(document.get("proposal_revision", 0) or 0)
    if req.focused_match_revision is not None and req.focused_match_revision != current_revision:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "這張牽線卡的狀態已更新，請重新查看。",
                "current_namespace": namespace,
                "current_status": document.get("status"),
                "current_revision": current_revision,
            },
        )
    return document


def _resolve_ai_room_id(req: DirectChatRequest) -> str:
    """Resolve the room id for a Public Ayue turn.

    When the client supplies ``ai_room_id`` it must belong to ``req.user_id``
    (enforced via :func:`get_ai_room`); otherwise we fall back to the legacy
    deterministic AI room. Invalid/foreign room ids are rejected with 403.
    """
    if req.focused_match_id:
        # Validate before persisting the owner message, so a stale/foreign
        # focus cannot become part of conversation history.
        _validate_focused_match(req)
    if req.ai_room_id:
        if not get_ai_room(req.ai_room_id, req.user_id):
            raise HTTPException(status_code=403, detail="無權存取此聊天室")
        return req.ai_room_id
    return generate_room_id(req.user_id, req.contact_id)

_PUBLIC_PROGRESS_STAGE_BY_AGENT = {
    "calendar": ("checking_calendar", "阿月正在確認你的行事曆…"),
    "places": ("finding_places", "阿月正在整理地點資訊…"),
    "web": ("checking_web", "阿月正在查證公開資訊…"),
    "match": ("checking_match", "阿月正在確認配對狀態…"),
    "relationship": ("checking_relationship", "阿月正在確認關係資訊…"),
    "profile": ("checking_profile", "阿月正在整理你的資料…"),
    "product_info": ("checking_product", "阿月正在確認產品資訊…"),
    "synthesizer": ("composing", "阿月正在整理回覆…"),
}


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


def queue_conversation_compaction_shadow(
    background_tasks, user_id: str, room_id: str,
) -> dict:
    """Queue continuity maintenance without changing the originating turn."""
    return _queue_conversation_compaction_shadow(background_tasks, user_id, room_id)

def check_and_trigger_date_activation(room_id: str, user_id: str, contact_id: str, message: str, match_doc: dict):
    """A proposal creates one invitation; it never opens a form without the partner."""
    from services.ai_service import detect_date_activation
    from services.date_coordination_service import create_invite
    if match_doc and detect_date_activation(message):
        coordination = create_invite(match_doc, user_id, contact_id)
        if coordination:
            try:
                from services.ayue_agent.v3.date_coordination_references import remember_date_coordination
                remember_date_coordination(
                    user_id,
                    room_id,
                    match_doc,
                    coordination,
                    other_id=contact_id,
                )
            except Exception:
                # The reference is convenience state. The pair-card write is
                # already committed and remains canonical if storage is down.
                pass


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
    if req.choice_action is not None:
        return ""
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
    external_calendar_authorized: bool = False,
) -> dict:
    """Run and persist one V3 turn after the owner message has been saved."""
    fetched_history = list(
        messages_coll.find({"room_id": room_id})
        .sort([("timestamp", -1), ("_id", -1)])
        .limit(33)
    )
    history_truncated = len(fetched_history) > 32
    history = list(reversed(fetched_history[:32]))
    user_doc = profiles_coll.find_one({"user_id": req.user_id}) or {}
    from services.memory_service import refresh_owner_memory_profile
    user_doc = refresh_owner_memory_profile(req.user_id, user_doc)
    room_session = assessment_session_for_room(
        user_doc, room_id, include_unscoped=not bool(req.ai_room_id),
    )
    agent_profile = dict(user_doc)
    if room_session is None:
        agent_profile.pop("agentic_assessment_session", None)
    agent_ctx = PublicAgentRequestContext(
        user_id=req.user_id,
        room_id=room_id,
        message=_public_request_message(req),
        assessment_action=req.assessment_action,
        choice_id=req.choice_id,
        choice_action=req.choice_action,
        message_id=user_message_id,
        history_truncated=history_truncated,
        mentioned_ids=requested_mentions,
        mention_overflow=mention_overflow,
        focused_match_id=req.focused_match_id,
        focused_match_namespace=req.focused_match_namespace,
        focused_match_revision=req.focused_match_revision,
        user_profile=agent_profile,
        recent_history=history,
        device_location=(
            req.device_location.model_dump(mode="json")
            if req.device_location is not None else None
        ),
        external_calendar_authorized=external_calendar_authorized,
    )
    agent_result = run_public_agent_turn_v3(
        agent_ctx, on_progress=on_progress, on_token=on_token,
        debug_enabled=debug_enabled,
    )
    owner_profile_message = _owner_profile_message(req, requested_mentions)
    message_use = mark_message_use_from_turn(
        user_message_id,
        user_id=req.user_id,
        room_id=room_id,
        profile_write_reason=agent_result.profile_write_reason,
        owner_message=owner_profile_message,
        calendar_operation=agent_result.calendar_state_changed,
    )
    if user_message_id is None:
        # A turn without a saved owner source (button control or a defensive
        # fallback path) must never make its assistant receipt reusable.
        message_use = MessageUse.UNKNOWN
    complete_public_ayue_onboarding(req.user_id)
    latest_profile = profiles_coll.find_one(
        {"user_id": req.user_id}, {"_id": 0, "agentic_assessment_session": 1},
    ) or {}
    assessment_state = assessment_public_state_for_room(
        latest_profile, room_id, include_unscoped=not bool(req.ai_room_id),
    )
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
    if agent_result.choice_prompt:
        metadata["choice_prompt"] = dict(agent_result.choice_prompt)
    if metadata:
        saved_reply = save_message(room_id, "ai_assistant", ai_reply, metadata=metadata)
    else:
        saved_reply = save_message(room_id, "ai_assistant", ai_reply)
    place_presentation_published = False
    place_presentation_failed = False
    if isinstance(saved_reply, dict):
        try:
            mark_message_use(
                saved_reply.get("message_id"),
                user_id=req.user_id,
                room_id=room_id,
                sender_id="ai_assistant",
                use=message_use,
                reason="owner_turn_excluded",
            )
        except Exception:
            # Unmarked messages fail closed in every reuse consumer. Keep the
            # already-saved reply available even when the auxiliary marker
            # write is temporarily unavailable.
            pass
        if (
            agent_result.relationship_recommendation_snapshot
            and saved_reply.get("message_id")
            and run_id
        ):
            try:
                save_relationship_recommendation_snapshot(
                    req.user_id,
                    room_id,
                    run_id,
                    str(saved_reply["message_id"]),
                    agent_result.relationship_recommendation_snapshot,
                )
            except RecommendationPersistenceError as exc:
                print(f"[relationship_recommendation] snapshot skipped: {type(exc).__name__}")
        if (
            run_id
            and saved_reply.get("message_id")
            and agent_result.place_presentation_required
        ):
            # Place snapshots are created before this assistant write but stay
            # unpublished until the message has a durable message_id. This
            # prevents a failed assistant save from arming an unreferenced
            # candidate list for a later turn.
            try:
                published = publish_place_presentation(
                    req.user_id,
                    room_id,
                    run_id,
                    str(saved_reply["message_id"]),
                )
            except PlaceReferencePersistenceError:
                published = False
            if not published:
                place_presentation_failed = True
                # The assistant row was saved before its source relation could
                # be committed. Quarantine it so a later turn cannot resolve
                # an unbound list, and return the same fail-closed copy used by
                # Scheduler persistence failures.
                try:
                    from bson.objectid import ObjectId
                    messages_coll.update_one(
                        {"_id": ObjectId(str(saved_reply["message_id"]))},
                        {"$set": {"is_blocked": True}},
                    )
                except Exception:
                    pass
                ai_reply = "這次找到地點，但暫時無法保存候選清單；請稍後再試，我還沒有替你建立行程。"
                reply_messages = [ai_reply]
            else:
                place_presentation_published = True
    if place_presentation_published and on_token is not None:
        # Scheduler withholds its normal final-reply replay for place turns.
        # Release exactly the text stored in the assistant row only after the
        # snapshot is linked to that durable row, so a client never observes a
        # candidate list that cannot be selected on a later turn.
        persisted_reply = str(saved_reply.get("content") or ai_reply)
        for start in range(0, len(persisted_reply), 120):
            on_token(persisted_reply[start:start + 120])
    if (
        run_id
        and isinstance(saved_reply, dict)
        and saved_reply.get("message_id")
        and not place_presentation_failed
    ):
        mark_public_confirmation_presented(
            user_id=req.user_id,
            origin_run_id=run_id,
            message_id=str(saved_reply["message_id"]),
            persisted_content=str(saved_reply.get("content") or ""),
        )
    # Assessment answers are a separate, owner-scoped workflow. They must not
    # become recent-context or durable-memory evidence. Other saved public
    # messages still reach the isolated extractor.
    profile_skill_mode = "off"
    profile_process_run_key = None
    if background_tasks is not None and user_message_id and message_use is MessageUse.ORDINARY:
        candidate_run_key = uuid.uuid4().hex
        profile_skill_mode = queue_profile_skills(
            background_tasks, req.user_id, owner_profile_message, user_message_id, "global",
            progress_token=candidate_run_key,
        )
        if profile_skill_mode in {"on", "shadow"} and profile_skills_mode_for_user(req.user_id) != "off":
            profile_process_run_key = candidate_run_key
    if background_tasks is not None:
        queue_conversation_compaction_shadow(background_tasks, req.user_id, room_id)
    return {
        "reply": ai_reply,
        "messages": reply_messages,
        "is_locked": False,
        "conversation_intent": agent_result.conversation_intent,
        "calendar_state_changed": agent_result.calendar_state_changed,
        "match_state_changed": agent_result.match_state_changed,
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
        "choice_prompt": None if place_presentation_failed else agent_result.choice_prompt,
        "choice_resolution": agent_result.choice_resolution,
    }


def _run_public_stream_turn(
    req: DirectChatRequest, background_tasks: BackgroundTasks, on_progress,
    *, on_token=None, debug_enabled: bool = False,
    external_calendar_authorized: bool = False,
) -> dict:
    """Public-only stream path; mirrors the V3 branch of direct_chat exactly once."""
    room_id = _resolve_ai_room_id(req)
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
    user_message = None
    if req.choice_action is None:
        user_message = save_message(
            room_id, req.user_id, display_message,
            metadata=user_metadata or None,
        )
    # Multi-room AI surface: generate a title from the first user message via
    # one extra LLM call. Runs in a daemon thread so it never blocks the agent
    # turn; the room stays needs_title=True until a title lands.
    if (
        req.choice_action is None
        and req.ai_room_id
        and mark_first_message_for_title(room_id, req.user_id)
    ):
        threading.Thread(
            target=ensure_room_title,
            args=(room_id, req.user_id, request_message),
            name="ayue-room-title",
            daemon=True,
        ).start()
    profiles_coll.update_one(
        {"user_id": req.user_id}, {"$set": {"last_user_activity_at": time.time()}}, upsert=True,
    )
    return _complete_public_turn(
        req, room_id, requested_mentions, mention_overflow, on_progress,
        on_token=on_token, background_tasks=background_tasks,
        user_message_id=(user_message or {}).get("message_id"),
        debug_enabled=debug_enabled,
        external_calendar_authorized=external_calendar_authorized,
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
    if event_type == "subagent_started" and run_id:
        stage = _PUBLIC_PROGRESS_STAGE_BY_AGENT.get(str(event.get("agent") or ""))
        if stage is None:
            return None
        stage_name, text = stage
        return {
            "type": "stage",
            "agent_run_id": run_id,
            "stage": stage_name,
            "text": text,
        }
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
    event_queue: queue.Queue[dict | None] = queue.Queue()
    fallback_run_id = uuid.uuid4().hex
    state = {"agent_run_id": fallback_run_id}
    worker_done = threading.Event()
    debug_enabled = _is_loopback_debug_request(request)
    external_calendar_authorized = authenticated_owner_matches(
        request.headers.get("Authorization") if request else None,
        req.user_id,
    )
    token_stream_enabled = bool(
        request
        and request.headers.get("x-ayue-stream-tokens", "").strip().lower() == "v1"
    )

    def enqueue(item: dict | None, *, terminal: bool = False) -> bool:
        event_queue.put_nowait(item)
        return True

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
                turn_options = {
                    "on_token": emit_token if token_stream_enabled else None,
                }
                if external_calendar_authorized:
                    turn_options["external_calendar_authorized"] = True
                if debug_enabled:
                    turn_options["debug_enabled"] = True
                    response = _run_public_stream_turn(
                        req, worker_background_tasks, emit, **turn_options,
                    )
                else:
                    response = _run_public_stream_turn(
                        req, worker_background_tasks, emit, **turn_options,
                    )
            else:
                # Stream is intentionally optional for legacy/private contacts;
                # preserve their established direct-chat behavior as one final event.
                response = direct_chat(req, worker_background_tasks)
            emit({"type": "final", "response": response})
        except Exception as exc:
            _log_public_stream_exception(exc)
            if debug_enabled:
                finish_debug_run(state["agent_run_id"], status="error")
            emit({
                "type": "error", "agent_run_id": state["agent_run_id"],
                "reply": PUBLIC_RUNTIME_ERROR_REPLY,
            })
        finally:
            worker_done.set()
            try:
                enqueue(None, terminal=True)
                asyncio.run(worker_background_tasks())
            except Exception:
                pass

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

    threading.Thread(target=worker, name="ayue-direct-chat-stream", daemon=True).start()
    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/direct_chat")
def direct_chat(
    req: DirectChatRequest,
    background_tasks: BackgroundTasks,
    request: Request = None,
):
    """Handle Public Ayue with V3, or preserve the existing pair-chat adapter."""
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
        room_id = _resolve_ai_room_id(req)
        user_message = None
        if req.choice_action is None:
            user_message = save_message(
                room_id, req.user_id, display_message, metadata=user_metadata or None,
            )
        if (
            req.choice_action is None
            and req.ai_room_id
            and mark_first_message_for_title(room_id, req.user_id)
        ):
            threading.Thread(
                target=ensure_room_title,
                args=(room_id, req.user_id, request_message),
                name="ayue-room-title",
                daemon=True,
            ).start()
        # Owner activity is tracked independently from the legacy fixed
        # frequency scheduler. Follow-up candidates are proposed by the
        # background Profile task from this persisted source message.
        if user_message:
            record_owner_activity(req.user_id)
        return _complete_public_turn(
            req, room_id, requested_mentions, mention_overflow,
            background_tasks=background_tasks,
            user_message_id=(user_message or {}).get("message_id"),
            external_calendar_authorized=authenticated_owner_matches(
                request.headers.get("Authorization") if request else None,
                req.user_id,
            ),
        )

    room_id = generate_room_id(req.user_id, req.contact_id)
    match_doc = find_accepted_match(req.user_id, req.contact_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail="只能傳送訊息給已接受的配對")

    # Relationship blocks are an availability gate, unlike advisory message
    # risk analysis. Check before both image/text persistence and all side effects.
    try:
        pair_is_blocked = risk_block_service.is_pair_blocked(
            req.user_id,
            req.contact_id,
        )
    except RiskBlockServiceUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail="安全關係狀態暫時無法確認，訊息未送出",
        ) from exc
    if pair_is_blocked:
        raise HTTPException(status_code=403, detail="目前無法傳送訊息給此聯絡人")

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

    # Pair messages always belong to the person who sent them, including the
    # very first turn. Ayue's shared opener is persisted at mutual acceptance,
    # under ai_assistant, never synthesized as the recipient's reply here.
    message_count = mark_post_chat_activity(match_doc, room_id)
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
