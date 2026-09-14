"""Production lifecycle for public Pi turns."""
from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from database import messages_coll
from services.assessment_session_service import (
    active_assessment_session,
    advance_assessment_session,
    assessment_cancel_choice,
    awaiting_assessment_commit,
    cancel_assessment_session,
    commit_assessment_session,
    expire_assessment_session,
)
from services.ayue_agent.context import build_public_agent_turn_context
from services.ayue_agent.capabilities import normalize_public_language
from services.ayue_agent.contracts import AgentResult, TurnClockV1
from services.ayue_agent.public_relationship_projection import validated_mentioned_contact_ids
from services.ayue_agent.shared.runtime_collections import (
    CONFIRMATIONS, CONTACT_SELECTIONS, OPERATION_BATCHES, RUNS,
)
from services.ayue_agent.shared.write_actions import (
    execute_write, prepare_date_coordination_for_contact_id,
)
from services.ayue_agent.time_context import build_turn_clock
from services.ayue_agent.shared.confirmation import (
    ASSESSMENT_COMMIT_ACTION,
    INTERACTION_BUBBLE,
    SURFACE_PUBLIC,
    ConfirmationManager,
    sync_choice_message_projection,
)
from services.ayue_agent.shared.contact_selections import (
    ContactSelectionManager,
    contact_selection_block,
)
from services.ayue_agent.shared.operation_batches import OperationBatchManager
from services.language_service import normalize_public_reply

from .reply import confirmed_reply
from .runtime import run_pi_turn


def _persist_trace(run_id: str, ctx: Any, trace: dict[str, Any], collection: Any = None) -> None:
    safe = {
        key: trace[key]
        for key in (
            "agent_runtime", "execution_mode", "reply_owner", "event_sequence",
            "pi_budget_class", "pi_model_budget", "pi_tool_budget",
            "pi_decision_llm_calls", "pi_presentation_llm_calls", "llm_call_count",
            "pi_reply_validation", "pi_tool_diagnostics", "tool_results", "result",
            "pi_model_diagnostics",
        )
        if key in trace
    }
    try:
        (RUNS if collection is None else collection).insert_one({
            "run_id": run_id, "user_id": ctx.user_id, "room_id": ctx.room_id,
            "agent_version": "pi_public.v1", "created_at": time.time(), **safe,
        })
    except Exception:
        pass


def _replay_tokens(on_token: Callable[[str], None] | None, text: str) -> None:
    if on_token is None:
        return
    for start in range(0, len(text), 120):
        on_token(text[start:start + 120])


def _assessment_result(outcome: dict[str, Any], session: dict[str, Any], run_id: str) -> AgentResult:
    state = str(outcome.get("session_state") or outcome.get("status") or "active")
    reply = str(outcome.get("reply") or "你可以換個方式說說看？")
    return AgentResult(
        handled=True, reply=reply, messages=[reply], conversation_intent="assessment",
        agent_run_id=run_id, agent_mode="pi",
        profile_write_allowed=False, profile_write_reason="assessment",
        assessment_state=state,
        assessment_kind=str(outcome.get("kind") or session.get("kind") or "") or None,
        assessment_revision=outcome.get("revision", int(session.get("revision", 0) or 0)),
    )


def _bind_interactions(
    result: AgentResult, *, ctx: Any, run_id: str,
    confirmations: ConfirmationManager, selections: ContactSelectionManager,
    batches: OperationBatchManager,
) -> AgentResult:
    # OpenCC s2twp maps 對象 to 物件. Apply person terminology after its final pass.
    messages = [normalize_public_language(normalize_public_reply(str(item))) for item in (result.messages or []) if str(item).strip()]
    if not messages and result.reply:
        messages = [normalize_public_language(normalize_public_reply(result.reply))]
    reply = "\n\n".join(messages)
    selection = selections.selection_for_run(
        user_id=ctx.user_id, room_id=ctx.room_id, origin_run_id=run_id,
    )
    if selection:
        block = contact_selection_block(selection)
        projection = (block or {}).get("selection") or {}
        labels = [
            str(item.get("display_name") or "").strip()
            for item in projection.get("candidates") or []
            if str(item.get("display_name") or "").strip()
        ]
        hint = str(projection.get("name_hint") or "").strip()
        label_text = "、".join(labels)
        reply = (
            f"我在已建立聯絡的人裡找到幾位接近「{hint}」的名字：{label_text}。你說的是哪一位？"
            if len(labels) > 1 else f"我找到「{label_text}」。你說的是這位嗎？"
        )
        messages = [reply]
        blocks = [{"type": "text", "message_index": 0}, block]
        selections.bind_final_preview(
            user_id=ctx.user_id, room_id=ctx.room_id, origin_run_id=run_id,
            final_content=reply, interaction_blocks_v1=blocks,
        )
        result = result.model_copy(update={
            "reply": reply, "messages": messages, "interaction_blocks_v1": blocks,
            "presentation_class": "transaction", "fallback_reason": None,
        })
    else:
        result = result.model_copy(update={"reply": reply, "messages": messages})

    confirmations.bind_final_preview(
        user_id=ctx.user_id, origin_run_id=run_id,
        final_content=result.reply or "", interaction_blocks_v1=result.interaction_blocks_v1,
    )
    choice = confirmations.choice_for_run(
        user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC,
        origin_run_id=run_id,
    )
    if choice:
        confirmation_count = sum(
            1 for item in result.interaction_blocks_v1
            if isinstance(item, dict) and item.get("type") == "confirmation"
        )
        blocks = result.interaction_blocks_v1
        if confirmation_count != 1:
            blocks = [
                {"type": "text", "message_index": 0},
                {"type": "confirmation", "slot": "primary_write_confirmation"},
            ]
            confirmations.bind_final_preview(
                user_id=ctx.user_id, origin_run_id=run_id,
                final_content=result.reply or "", interaction_blocks_v1=blocks,
            )
        result = result.model_copy(update={"choice_prompt": choice, "interaction_blocks_v1": blocks})
    batch = batches.latest_pending(user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC)
    if batch and str(batch.get("source_engine") or "dag") == "pi":
        current = batches.current_item(batch) or {}
        if choice:
            batches.bind_confirmation(str(batch.get("_id") or ""), str(choice.get("id") or ""))
        elif current.get("status") == "active":
            batches.mark_awaiting_input(str(batch.get("_id") or ""))
    return result


def _handle_choice(
    ctx: Any, turn: Any, run_id: str,
    confirmations: ConfirmationManager, selections: ContactSelectionManager,
    batches: OperationBatchManager,
) -> AgentResult | None:
    if ctx.choice_action is None:
        return None
    choice_id = str(ctx.choice_id or "")
    selection, selected_contact_id = selections.resolve(
        user_id=ctx.user_id, room_id=ctx.room_id,
        action_id=choice_id, action=str(ctx.choice_action or ""),
    )
    if selection is not None:
        state = str(selection.get("status") or "")
        if ctx.choice_action == "more" and state == "pending":
            reply = "我把其他相似的聯絡人也列出來了，你說的是哪一位？"
            block = contact_selection_block(selection)
            return AgentResult(
                handled=True, reply=reply, messages=[reply], presentation_class="transaction",
                conversation_intent="contact_selection", agent_run_id=run_id,
                agent_mode="pi",
                interaction_blocks_v1=[{"type": "text", "message_index": 0}, block] if block else [],
            )
        if ctx.choice_action == "cancel" and state == "none_selected":
            reply = "好，這些都不是。我先不建立邀請卡；你可以直接告訴我名字或再描述一下對方。"
            return AgentResult(handled=True, reply=reply, messages=[reply], presentation_class="transaction",
                               conversation_intent="contact_selection_cancelled", agent_run_id=run_id,
                               agent_mode="pi")
        if selected_contact_id:
            selected = next(
                (item for item in selection.get("candidates") or []
                 if str(item.get("other_id") or "") == selected_contact_id), {},
            )
            label = str(selected.get("display_name") or "對方")[:30]
            pending, preview = prepare_date_coordination_for_contact_id(
                ctx, selected_contact_id, safe_label=label, resolution_kind="selected",
            )
            if pending is None:
                reply = str(preview or "這位聯絡人的狀態已更新，我沒有建立邀請卡。")
                return AgentResult(handled=True, reply=reply, messages=[reply], presentation_class="transaction",
                                   conversation_intent="date_invitation_state", agent_run_id=run_id,
                                   agent_mode="pi")
            confirmations.create_confirmation(
                user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC,
                interaction_mode=INTERACTION_BUBBLE, agent_name="relationship",
                tool_name="relationship.start_date_coordination",
                arguments=dict(pending.get("arguments") or {}),
                payload={**dict(pending.get("data") or {}), "source_engine": "pi",
                         "tool_protocol_version": "pi_public.v1"},
                origin_run_id=run_id, preview=str(preview or "請確認是否建立邀請卡。"),
                idempotency_key=f"contact-selection:{selection.get('_id')}:{choice_id}",
            )
            reply = f"已確認是「{label}」。{preview or ''}".strip()
            return AgentResult(
                handled=True, reply=reply, messages=[reply], presentation_class="transaction",
                conversation_intent="date_invitation_confirmation", agent_run_id=run_id,
                agent_mode="pi", interaction_blocks_v1=[
                    {"type": "text", "message_index": 0},
                    {"type": "confirmation", "slot": "primary_write_confirmation"},
                ],
            )
        reply = "這張人選卡已過期或處理完成；我沒有建立邀請卡。"
        return AgentResult(handled=True, reply=reply, messages=[reply], presentation_class="transaction",
                           conversation_intent="contact_selection_missing", agent_run_id=run_id,
                           agent_mode="pi")

    record = confirmations.record_for_choice(
        user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC, choice_id=choice_id,
    )
    if record is None:
        resolution = confirmations.choice_projection(
            user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC, choice_id=choice_id,
        )
        reply = (
            "這個選擇已過期或處理完成；我沒有再次執行。"
            if resolution else "我找不到這個待確認操作，因此沒有執行任何變更。"
        )
        return AgentResult(handled=True, reply=reply, messages=[reply], presentation_class="transaction",
                           conversation_intent="confirmation_missing", agent_run_id=run_id,
                           agent_mode="pi", choice_resolution=resolution)
    if record.get("surface") == SURFACE_PUBLIC and record.get("source_engine") != "pi":
        confirmations.supersede_active(
            user_id=ctx.user_id,
            tool_name=str(record.get("tool_name") or ""),
            reason="dag_retired",
        )
        reply = "阿月已更新，這筆尚未確認的操作已失效。請重新告訴我你的需求；這次沒有執行變更。"
        return AgentResult(
            handled=True, reply=reply, messages=[reply], presentation_class="transaction",
            conversation_intent="confirmation_expired", agent_run_id=run_id, agent_mode="pi",
        )
    if ctx.choice_action == "cancel":
        resolution = confirmations.cancel_choice(
            user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC, choice_id=choice_id,
        )
        if resolution:
            sync_choice_message_projection(messages_coll, room_id=ctx.room_id, projection=resolution)
        reply = "好，這次先不執行變更。"
        batches.resolve_choice(
            user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC,
            choice_id=choice_id, outcome="cancelled", result_summary="使用者取消目前操作，未執行變更。",
        )
        return AgentResult(handled=True, reply=reply, messages=[reply], presentation_class="transaction",
                           conversation_intent="confirmation_cancelled", agent_run_id=run_id,
                           agent_mode="pi", choice_resolution=resolution)

    payload = dict(record.get("payload") or {})
    assessment_outcome: dict[str, Any] = {}

    def executor(tool_name: str, arguments: dict[str, Any], _user_id: str,
                 bound_payload: dict[str, Any]):
        if tool_name == ASSESSMENT_COMMIT_ACTION:
            outcome = commit_assessment_session(
                ctx.user_id, str(bound_payload.get("session_id") or ""),
                expected_revision=int(bound_payload.get("revision", 0) or 0),
                idempotency_key=(f"assessment-commit:{bound_payload.get('session_id')}:"
                                 f"{int(bound_payload.get('revision', 0) or 0)}"),
            )
            assessment_outcome.update(outcome)
            ok = str(outcome.get("status") or "") in {"committed", "already_committed"}
            return ok, str(outcome.get("reply") or "這份結果沒有完成套用。"), None if ok else str(outcome.get("status") or "assessment_commit_failed")
        return execute_write(tool_name, arguments, ctx, turn, run_id, 0,
                             confirmation_id=None, payload=bound_payload)

    results = confirmations.execute_confirmed(
        user_id=ctx.user_id, choice_id=choice_id, room_id=ctx.room_id,
        surface=SURFACE_PUBLIC, interaction_mode=INTERACTION_BUBBLE, executor=executor,
    )
    resolution = confirmations.choice_projection(
        user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC, choice_id=choice_id,
    )
    if resolution:
        sync_choice_message_projection(messages_coll, room_id=ctx.room_id, projection=resolution)
    if assessment_outcome:
        return _assessment_result(assessment_outcome, payload, run_id).model_copy(
            update={"choice_resolution": resolution},
        )
    reply = confirmed_reply(results)
    changed = any(bool(item.get("ok")) for item in results if isinstance(item, dict))
    batches.resolve_choice(
        user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC,
        choice_id=choice_id, outcome="completed" if changed else "failed",
        result_summary=reply,
    )
    return AgentResult(
        handled=True, reply=reply, messages=[reply], presentation_class="transaction",
        conversation_intent="confirmation", agent_run_id=run_id, agent_mode="pi",
        choice_resolution=resolution,
        calendar_state_changed=bool(changed and str(record.get("tool_name") or "").startswith("calendar.")),
        match_state_changed=bool(changed and str(record.get("tool_name") or "").startswith("match.")),
        profile_write_reason=("calendar_operation" if str(record.get("tool_name") or "").startswith("calendar.") else "confirmation"),
    )


def _handle_assessment(ctx: Any, run_id: str) -> AgentResult | None:
    session = awaiting_assessment_commit(ctx.user_profile) or active_assessment_session(ctx.user_profile)
    if not session:
        return None
    session_id = str(session.get("session_id") or "")
    kind = str(session.get("kind") or "")
    expires_at = float(session.get("expires_at", 0) or 0)
    if expires_at and expires_at <= time.time():
        return _assessment_result(expire_assessment_session(ctx.user_id, session_id, kind), session, run_id)
    if ctx.assessment_action == "cancel" or assessment_cancel_choice(ctx.message):
        return _assessment_result(cancel_assessment_session(ctx.user_id, session_id, kind), session, run_id)
    if awaiting_assessment_commit(ctx.user_profile):
        reply = "這份探索結果已整理好，請使用畫面上的確認或取消按鈕。"
        return AgentResult(handled=True, reply=reply, messages=[reply], conversation_intent="assessment",
                           agent_run_id=run_id, agent_mode="pi", profile_write_allowed=False,
                           profile_write_reason="assessment", assessment_state="awaiting_commit",
                           assessment_kind=kind, assessment_revision=int(session.get("revision", 0) or 0))
    outcome = advance_assessment_session(ctx.user_id, session_id, ctx.message, message_id=ctx.message_id)
    return _assessment_result(outcome, session, run_id)


def _continue_batch_after_choice(
    result: AgentResult, *, ctx: Any, turn: Any, run_id: str,
    trace: dict[str, Any], batches: OperationBatchManager,
    confirmation_store: Any, selection_store: Any, batch_store: Any,
    on_progress: Callable | None, debug_enabled: bool,
) -> AgentResult:
    """Continue the newly active item in the same HTTP run after a receipt."""
    if not result.choice_resolution:
        return result
    batch = batches.latest_pending(user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC)
    current = batches.current_item(batch)
    if (
        not batch or str(batch.get("source_engine") or "dag") != "pi"
        or not current or current.get("status") != "active"
    ):
        return result
    request = str(current.get("request") or "").strip()
    if not request:
        batches.pause(str(batch.get("_id") or ""), reason="missing_current_request")
        return result
    next_clock = turn.clock
    saved_clock = batch.get("source_clock")
    if isinstance(saved_clock, dict) and saved_clock:
        try:
            next_clock = TurnClockV1.model_validate(saved_clock)
        except Exception:
            pass
    next_ctx = ctx.model_copy(update={
        "message": request, "choice_id": None, "choice_action": None,
        "assessment_action": None, "message_id": None,
    })
    receipt = str(result.reply or "").strip()
    history = list(turn.recent_messages or [])
    if receipt:
        history.append({
            "role": "assistant", "content": receipt,
            "sent_at": turn.clock.local_iso, "timezone": turn.clock.timezone,
        })
    next_turn = turn.model_copy(update={
        "message": request, "clock": next_clock, "recent_messages": history[-12:],
    })
    object.__setattr__(next_turn, "_raw_ctx", next_ctx)
    object.__setattr__(next_turn, "_mentioned_ids", list(getattr(turn, "_mentioned_ids", []) or []))
    object.__setattr__(next_turn, "_operation_batch_context", batches.prompt_context(batch, auto_resume=True))
    object.__setattr__(next_turn, "_operation_batch_id", str(batch.get("_id") or ""))
    object.__setattr__(next_turn, "_operation_batch_revision", int(batch.get("revision", 0) or 0))
    object.__setattr__(next_turn, "_operation_item_id", str(current.get("item_id") or ""))
    next_result = run_pi_turn(
        next_turn, run_id=run_id, trace=trace, on_progress=on_progress,
        debug_enabled=debug_enabled, confirmation_collection=confirmation_store,
        contact_selection_collection=selection_store, operation_batch_collection=batch_store,
    )
    first_messages = list(result.messages or ([receipt] if receipt else []))
    combined_messages = [*first_messages, *list(next_result.messages or [])][:3]
    index_offset = len(first_messages)
    combined_blocks = [
        {**block, "message_index": int(block.get("message_index", 0) or 0) + index_offset}
        if isinstance(block, dict) and block.get("type") == "text"
        else dict(block)
        for block in (next_result.interaction_blocks_v1 or [])
        if isinstance(block, dict)
    ]
    if first_messages:
        combined_blocks = [
            *({"type": "text", "message_index": index} for index in range(len(first_messages))),
            *combined_blocks,
        ]
    return next_result.model_copy(update={
        "reply": "\n\n".join(combined_messages),
        "messages": combined_messages,
        "interaction_blocks_v1": combined_blocks[:4],
        "choice_resolution": result.choice_resolution,
        "calendar_state_changed": bool(result.calendar_state_changed or next_result.calendar_state_changed),
        "match_state_changed": bool(result.match_state_changed or next_result.match_state_changed),
        "llm_call_metrics": [*list(result.llm_call_metrics or []), *list(next_result.llm_call_metrics or [])],
    })


def run_pi_public_turn(
    ctx: Any, *, on_progress: Callable | None = None,
    on_token: Callable[[str], None] | None = None, debug_enabled: bool = False,
    confirmation_collection: Any = None, contact_selection_collection: Any = None,
    operation_batch_collection: Any = None, runs_collection: Any = None,
) -> AgentResult:
    """Run one authenticated Pi public turn without importing Scheduler."""
    if not bool(getattr(ctx, "external_calendar_authorized", False)):
        raise PermissionError("pi_authenticated_owner_required")
    confirmation_store = CONFIRMATIONS if confirmation_collection is None else confirmation_collection
    selection_store = CONTACT_SELECTIONS if contact_selection_collection is None else contact_selection_collection
    batch_store = OPERATION_BATCHES if operation_batch_collection is None else operation_batch_collection
    confirmations = ConfirmationManager(confirmation_store)
    selections = ContactSelectionManager(selection_store)
    batches = OperationBatchManager(batch_store)
    run_id = uuid.uuid4().hex
    mentioned_ids, overflow = validated_mentioned_contact_ids(ctx.user_id, ctx.mentioned_ids)
    ctx = ctx.model_copy(update={
        "mentioned_ids": mentioned_ids,
        "mention_overflow": bool(ctx.mention_overflow or overflow),
    })
    turn = build_public_agent_turn_context(ctx, clock=build_turn_clock(ctx.message))
    object.__setattr__(turn, "_raw_ctx", ctx)
    object.__setattr__(turn, "_mentioned_ids", mentioned_ids)

    pending_batches = batches.list_pending(user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC)
    active_batch = pending_batches[0] if pending_batches else None
    if active_batch and str(active_batch.get("source_engine") or "dag") != "pi":
        batches.pause(str(active_batch.get("_id") or ""), reason="engine_switched")
        active_batch = None
    if active_batch:
        current = batches.current_item(active_batch) or {}
        object.__setattr__(turn, "_operation_batch_context", batches.prompt_context(active_batch))
        object.__setattr__(turn, "_operation_batch_id", str(active_batch.get("_id") or ""))
        object.__setattr__(turn, "_operation_batch_revision", int(active_batch.get("revision", 0) or 0))
        object.__setattr__(turn, "_operation_item_id", str(current.get("item_id") or ""))

    trace: dict[str, Any] = {"event_sequence": []}
    choice_result = _handle_choice(ctx, turn, run_id, confirmations, selections, batches)
    if choice_result is not None:
        choice_result = _continue_batch_after_choice(
            choice_result, ctx=ctx, turn=turn, run_id=run_id, trace=trace,
            batches=batches, confirmation_store=confirmation_store,
            selection_store=selection_store, batch_store=batch_store,
            on_progress=on_progress, debug_enabled=debug_enabled,
        )
        result = _bind_interactions(choice_result, ctx=ctx, run_id=run_id,
                                    confirmations=confirmations, selections=selections, batches=batches)
    else:
        assessment_result = _handle_assessment(ctx, run_id)
        if assessment_result is not None:
            result = _bind_interactions(assessment_result, ctx=ctx, run_id=run_id,
                                        confirmations=confirmations, selections=selections, batches=batches)
        else:
            if ctx.choice_action is None and ctx.assessment_action is None:
                selections.supersede_active(user_id=ctx.user_id, room_id=ctx.room_id)
                resolution = confirmations.resolve_for_continuation(
                    user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC,
                )
                if resolution:
                    sync_choice_message_projection(messages_coll, room_id=ctx.room_id, projection=resolution)
            result = run_pi_turn(
                turn, run_id=run_id, trace=trace, on_progress=on_progress,
                debug_enabled=debug_enabled,
                confirmation_collection=confirmation_store,
                contact_selection_collection=selection_store,
                operation_batch_collection=batch_store,
            )
            result = _bind_interactions(result, ctx=ctx, run_id=run_id,
                                        confirmations=confirmations, selections=selections, batches=batches)
    trace["result"] = {
        "handled": True, "conversation_intent": result.conversation_intent,
        "fallback_reason": result.fallback_reason,
    }
    _persist_trace(run_id, ctx, trace, runs_collection)
    _replay_tokens(on_token, result.reply or "")
    return result
