"""Private Pi lifecycle for an accepted relationship.

Pi owns the conversation/tool loop.  Python still owns context admission,
relationship authority, confirmation, memory candidates and all writes.
Public Ayue's orchestrator is intentionally not imported here.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Callable

from database import db, messages_coll
from services.ai_service import generate_chat_completion_with_tools
from services.ayue_agent.private_contracts import (
    PrivateAgentResult,
    PrivateSurfaceHandoff,
)
from services.ayue_agent.private_v2 import (
    PRIVATE_CLARIFICATION_REPLY,
    PRIVATE_CONFIRMATIONS,
    PRIVATE_RUNTIME_FALLBACK_REPLY,
    _execute_read,
    _execute_write,
    _save_confirmation,
)
from services.ayue_agent.product_identity import PRIVATE_REDIRECT_COPY
from services.ayue_agent.shared.confirmation import (
    INTERACTION_BUBBLE,
    SURFACE_PRIVATE,
    ConfirmationManager,
    sync_choice_message_projection,
)
from services.relationship_engagement_service import (
    consume_pending_post_date_feedback,
    consume_pending_probe_answer,
)
from services.relationship_memory_service import enqueue_relationship_memory_extraction
from .bridge_client import provider_messages, run_bridge
from .context import PrivatePiTurnContext, build_private_pi_context
from .policy import PRIVATE_PI_POLICY
from .registry import PRIVATE_TOOL_MAP, PRIVATE_TOOL_NAMES, tool_schemas
from .settings import pi_available


PRIVATE_PI_RUNS = db["private_agent_runs"]
_INTERNAL_KEYS = {
    "_id", "user_id", "other_id", "room_id", "match_id", "event_id",
    "relationship_id", "coordination_id", "memory_id", "probe_id",
    "message_id", "source_message_id", "revision", "pair_revision",
}
_MEMORY_CATEGORIES = {"impression", "preference", "boundary", "future_intent"}
_FEEDBACK_ACTIONS = {"answer", "decline"}
_MEMORY_SUBJECTIVE_RE = re.compile(
    r"(?:^|[，。！？\s])(?:我|本人)[^。！？]{0,40}?"
    r"(?:覺得|感覺|喜歡|不喜歡|在意|希望|期待|想(?:要)?|需要|擔心|"
    r"很在乎|沒感覺|無感|自在|舒服|不安|尷尬|緊張|開心|難過|失望|界線)",
)
_MEMORY_OPERATION_RE = re.compile(
    r"(?:幫我|請(?:你)?|能不能|可以(?:幫我)?|要不要|怎麼(?:辦|回|約)|"
    r"約(?:他|她|對方)|問(?:他|她|對方)|通知對方|查(?:一下|詢)?|"
    r"找(?:一下)?|確認(?:一下)?(?:是否|要不要|約會|安排|行程|卡)|取消|"
    r"想知道|請問|什麼|哪個|哪裡|誰|為什麼|"
    r"嗎[？?]?$|[？?])",
)
_EXPLICIT_CANCEL_RE = re.compile(
    r"^\s*(?:(?:取消|撤回|收起)[^\r\n]{0,40}|"
    r"先不要|不用(?:了)?|不要(?:了|安排|問了)?)(?:[。！!，,\s]|$)",
)
_FORBIDDEN_REPLY = re.compile(
    r"(?:user_id|other_id|room_id|event_id|match_id|revision|資料庫|prompt|"
    r"tool|工具|系統|權限|PRIVATE|seed_user|demo_user|\[\[confirmation\]\])",
    re.IGNORECASE,
)
_MEMORY_CLAIM = re.compile(r"(?:我已?記住|我已?記下|已經保存|已經存下|幫你記錄好了)")
_INTERNAL_VALUE_RE = re.compile(
    r"(?:@?seed_user_[\w-]+|@?demo_user|@?user[_-]?\d+)",
    re.IGNORECASE,
)


def _strip_internal(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_internal(item) for item in value]
    if isinstance(value, dict):
        return {
            key: _strip_internal(item)
            for key, item in value.items()
            if key not in _INTERNAL_KEYS
        }
    if isinstance(value, str):
        return _INTERNAL_VALUE_RE.sub("對方", value)
    return value


def _safe_reply(text: Any, observations: list[dict[str, Any]]) -> str | None:
    reply = re.sub(r"\s+", " ", str(text or "")).strip()
    if not reply or len(reply) > 600 or _FORBIDDEN_REPLY.search(reply):
        return None
    if not any(
        item.get("status") == "memory_saved"
        for item in observations
        if isinstance(item, dict)
    ) and _MEMORY_CLAIM.search(reply):
        return None
    return reply


def _memory_candidate_allowed(message: str, evidence: str) -> bool:
    """Apply a small server-side admission check before queuing extraction.

    Pi still decides when to call the candidate tool, but obvious operation
    requests/questions must never become relationship-memory jobs merely
    because the evidence string is present in the message.
    """
    text = re.sub(r"\s+", " ", str(message or "")).strip()
    span = re.sub(r"\s+", " ", str(evidence or "")).strip()
    if not text or not span or span not in text:
        return False
    if _MEMORY_OPERATION_RE.search(text):
        return False
    return bool(_MEMORY_SUBJECTIVE_RE.search(text))


def _explicit_cancel_requested(message: str) -> bool:
    return bool(_EXPLICIT_CANCEL_RE.search(str(message or "")))


def _coordination_state(match_doc: dict[str, Any]) -> dict[str, Any]:
    coordination = match_doc.get("date_coordination") or {}
    status = str(coordination.get("status") or "none")
    return {
        "status": status,
        "has_form": bool(coordination.get("form")),
        "has_scheduled_date": bool(coordination.get("calendar_event_id")),
        "next_step": (
            "fill_form" if status == "active" and not coordination.get("form")
            else "both_confirm" if status == "active"
            else "wait_partner" if status == "pending_partner"
            else "completed" if status == "completed"
            else "none"
        ),
    }


def _active_private_choices(user_id: str, room_id: str) -> list[dict[str, Any]]:
    try:
        return ConfirmationManager(PRIVATE_CONFIRMATIONS).list_active(
            user_id=user_id,
            room_id=room_id,
            surface=SURFACE_PRIVATE,
            interaction_mode=INTERACTION_BUBBLE,
        )
    except Exception:
        return []


def _mark_pi_confirmation(*, user_id: str, origin_run_id: str) -> None:
    """Annotate the compatibility confirmation row without changing its API."""
    try:
        PRIVATE_CONFIRMATIONS.update_one(
            {
                "user_id": user_id,
                "origin_run_id": origin_run_id,
                "surface": SURFACE_PRIVATE,
                "status": "prepared",
            },
            {"$set": {
                "source_engine": "pi",
                "tool_protocol_version": "private_pi.v1",
            }},
        )
    except Exception:
        # The confirmation itself remains valid if telemetry annotation is
        # unavailable; authority continues to be enforced by its payload.
        pass


def _choice_result(
    *,
    run_id: str,
    reply: str,
    resolution: dict[str, Any] | None = None,
) -> PrivateAgentResult:
    return PrivateAgentResult(
        handled=True,
        reply=reply,
        conversation_intent="private_confirmation",
        agent_run_id=run_id,
        agent_mode="pi",
        choice_resolution=resolution,
        profile_write_allowed=False,
        profile_write_reason="confirmation",
    )


def _resolve_choice(
    *,
    ctx: PrivatePiTurnContext,
    match_doc: dict[str, Any],
    run_id: str,
    choice_id: str,
    choice_action: str,
) -> PrivateAgentResult:
    manager = ConfirmationManager(PRIVATE_CONFIRMATIONS)
    record = manager.record_for_choice(
        user_id=ctx.base.user_id,
        room_id=ctx.base.room_id,
        surface=SURFACE_PRIVATE,
        choice_id=choice_id,
    )
    if record is None:
        resolution = manager.choice_projection(
            user_id=ctx.base.user_id,
            room_id=ctx.base.room_id,
            surface=SURFACE_PRIVATE,
            choice_id=choice_id,
        )
        if resolution:
            sync_choice_message_projection(
                messages_coll, room_id=ctx.base.room_id, projection=resolution,
            )
        return _choice_result(
            run_id=run_id,
            reply=(
                "這個選擇已經過期或處理完成；我沒有再次通知對方。"
                if resolution else "我找不到這個待確認邀請，因此沒有通知對方。"
            ),
            resolution=resolution,
        )
    if choice_action == "cancel":
        resolution = manager.cancel_choice(
            user_id=ctx.base.user_id,
            room_id=ctx.base.room_id,
            surface=SURFACE_PRIVATE,
            choice_id=choice_id,
        )
        if resolution:
            sync_choice_message_projection(
                messages_coll, room_id=ctx.base.room_id, projection=resolution,
            )
        return _choice_result(run_id=run_id, reply="好，我先不會通知對方。", resolution=resolution)

    def button_executor(
        tool_name: str,
        _arguments: dict[str, Any],
        _user_id: str,
        payload: dict[str, Any],
    ) -> tuple[bool, str, str | None]:
        if (
            str(payload.get("other_id") or "") != ctx.base.other_id
            or match_doc.get("status") != "accepted"
            or int(match_doc.get("proposal_revision", 0) or 0)
            != int(payload.get("pair_revision", -1) or -1)
        ):
            return False, "relationship_changed", "stale_relationship"
        return _execute_write(tool_name, ctx.base.user_id, ctx.base.other_id, match_doc)

    results = manager.execute_confirmed(
        user_id=ctx.base.user_id,
        choice_id=choice_id,
        room_id=ctx.base.room_id,
        surface=SURFACE_PRIVATE,
        interaction_mode=INTERACTION_BUBBLE,
        executor=button_executor,
    )
    resolution = manager.choice_projection(
        user_id=ctx.base.user_id,
        room_id=ctx.base.room_id,
        surface=SURFACE_PRIVATE,
        choice_id=choice_id,
    )
    if resolution:
        sync_choice_message_projection(
            messages_coll, room_id=ctx.base.room_id, projection=resolution,
        )
    item = results[0] if results else {}
    code = str((item.get("data") or {}).get("reply") or "")
    ok = bool(item.get("ok"))
    reply = (
        "好，我已經先問對方是否願意一起協調約會；對方同意後會到共同聊天室確認。"
        if ok and code == "date_invite_created"
        else "目前已經有一個約會協調在進行中，我先不重複通知對方。"
        if ok
        else "這個邀請目前無法送出。"
    )
    return _choice_result(run_id=run_id, reply=reply, resolution=resolution)


def _trace(run_id: str, payload: dict[str, Any]) -> None:
    try:
        PRIVATE_PI_RUNS.insert_one({
            "run_id": run_id,
            "surface": "private_mediator_pi",
            "agent_version": "pi_private.v1",
            "created_at": time.time(),
            **payload,
        })
    except Exception:
        pass


def run_private_pi_turn(
    *,
    user_id: str,
    other_id: str,
    message: str,
    match_doc: dict[str, Any],
    source_message_id: str = "",
    on_progress: Callable[[dict[str, str]], None] | None = None,
    on_token: Callable[[str], None] | None = None,
    agent_run_id: str | None = None,
    choice_id: str | None = None,
    choice_action: str | None = None,
    external_calendar_authorized: bool = False,
) -> PrivateAgentResult:
    started = time.perf_counter()
    run_id = str(agent_run_id or uuid.uuid4().hex)
    trace: dict[str, Any] = {
        "visible_tools": sorted(PRIVATE_TOOL_NAMES),
        "tool_results": [],
        "guard": [],
        "model_calls": 0,
        "tool_calls": 0,
    }
    if not pi_available():
        trace.update({
            "fallback": "pi_runtime_unavailable",
            "latency_ms": round((time.perf_counter() - started) * 1000),
        })
        _trace(run_id, trace)
        return PrivateAgentResult(
            handled=True,
            reply=PRIVATE_RUNTIME_FALLBACK_REPLY,
            conversation_intent="private_clarification",
            agent_run_id=run_id,
            agent_mode="pi",
            fallback_reason="pi_runtime_unavailable",
        )

    try:
        context_started = time.perf_counter()
        ctx = build_private_pi_context(
            user_id=user_id,
            other_id=other_id,
            message=message,
            match_doc=match_doc,
            source_message_id=source_message_id,
            external_calendar_authorized=external_calendar_authorized,
        )
        trace["context_ms"] = round((time.perf_counter() - context_started) * 1000)
    except Exception as exc:
        trace["exception"] = type(exc).__name__
        trace["latency_ms"] = round((time.perf_counter() - started) * 1000)
        _trace(run_id, trace)
        return PrivateAgentResult(
            handled=True,
            reply=PRIVATE_RUNTIME_FALLBACK_REPLY,
            conversation_intent="private_clarification",
            agent_run_id=run_id,
            agent_mode="pi",
            fallback_reason=type(exc).__name__,
        )

    if choice_action is not None:
        result = _resolve_choice(
            ctx=ctx,
            match_doc=match_doc,
            run_id=run_id,
            choice_id=str(choice_id or ""),
            choice_action=str(choice_action),
        )
        trace["result"] = {"intent": result.conversation_intent, "fallback": result.fallback_reason}
        trace["latency_ms"] = round((time.perf_counter() - started) * 1000)
        _trace(run_id, trace)
        return result

    observations: list[dict[str, Any]] = []
    terminal = False
    terminal_reply: str | None = None
    terminal_intent = "private_confirmation"
    handoff: PrivateSurfaceHandoff | None = None
    choice_created = False
    memory_candidate: dict[str, str] | None = None
    post_date_feedback_recorded = False
    model_call_count = 0
    tool_call_count = 0
    schema_failures: dict[str, int] = {}
    tool_schemas_by_name = {item["name"]: item for item in tool_schemas()}
    manager = ConfirmationManager(PRIVATE_CONFIRMATIONS)

    def emit(event_type: str, **payload: str) -> None:
        if on_progress is None:
            return
        try:
            on_progress({"type": event_type, "agent_run_id": run_id, **payload})
        except Exception:
            pass

    def model_call(
        messages: list[dict[str, Any]],
        deadline: float,
        active_tool_names: list[str] | None = None,
    ) -> dict[str, Any]:
        nonlocal model_call_count
        model_call_count += 1
        trace["model_calls"] = model_call_count
        if model_call_count > 5 or time.monotonic() >= deadline:
            return {"error": "pi_model_budget_exhausted"}
        allowed = set(
            PRIVATE_TOOL_NAMES if active_tool_names is None else active_tool_names
        )
        schemas = [
            {"type": "function", "function": schema}
            for name, schema in tool_schemas_by_name.items()
            if name in allowed
        ]
        fragments: list[str] = []
        try:
            completion = generate_chat_completion_with_tools(
                "",
                schemas,
                system_prompt=ctx.system_prompt(PRIVATE_PI_POLICY),
                temperature=0,
                max_tokens=1600,
                deadline_monotonic=deadline,
                model_owner="pi",
                on_token=fragments.append,
                conversation_messages=provider_messages(messages),
            )
        except Exception as exc:
            trace.setdefault("model_errors", []).append(type(exc).__name__)
            return {"error": "pi_provider_error"}
        text = str(getattr(completion, "content", "") or "") or "".join(fragments)
        normalized_tool_calls: list[dict[str, Any]] = []
        for call in list(getattr(completion, "tool_calls", []) or []):
            if not isinstance(call, dict):
                continue
            arguments = call.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except (TypeError, ValueError):
                    arguments = {}
            normalized_tool_calls.append({
                "name": str(call.get("name") or ""),
                "arguments": arguments if isinstance(arguments, dict) else {},
            })
        return {
            "content": text,
            "tool_calls": normalized_tool_calls,
            "input_tokens": int(getattr(completion, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(completion, "output_tokens", 0) or 0),
        }

    def project(
        name: str,
        status: str,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        item = {
            "status": status,
            "tool": name,
            "result": _strip_internal(result or {}),
            "error_code": error_code,
        }
        observations.append(item)
        trace["tool_results"].append({"tool": name, "status": status, "error_code": error_code})
        return item

    def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        nonlocal terminal, terminal_reply, handoff, choice_created
        nonlocal terminal_intent, memory_candidate, post_date_feedback_recorded, tool_call_count
        tool_started_at = time.perf_counter()
        tool_call_count += 1
        trace["tool_calls"] = tool_call_count
        binding = PRIVATE_TOOL_MAP.get(name)
        if binding is None or name not in PRIVATE_TOOL_NAMES:
            return {"observations": [project(name, "failed", error_code="tool_not_allowed")], "stop": True}
        if tool_call_count > 8:
            trace["guard"].append("tool_budget_exhausted")
            return {"observations": [project(name, "failed", error_code="pi_tool_budget_exhausted")], "stop": True}

        emit("tool_started", step_id=f"{tool_call_count}:private", text=binding.progress_text)
        result: dict[str, Any] = {}
        error_code: str | None = None
        status = "ok"
        try:
            if name in {
                "private.relationship.get_pair_summary",
                "private.relationship.get_shared_history",
                "private.relationship.search_shared_history",
                "private.calendar.get_counterparty_availability",
                "private.calendar.get_viewer_availability",
            }:
                safe_args = {
                    "scope": str(arguments.get("scope") or "")[:80],
                    "query": str(arguments.get("query") or "")[:80],
                }
                ok, result, error_code = _execute_read(name, ctx.base, safe_args)
                status = "ok" if ok else "failed"
            elif name == "private.date.get_coordination_state":
                result = _coordination_state(match_doc)
            elif name == "private.date.start_coordination":
                active = _active_private_choices(user_id, ctx.base.room_id)
                if active:
                    terminal = True
                    terminal_reply = "上方的約會確認還有效；按下確認後我才會通知對方。"
                    result = {"pending_confirmation": True, "reused": True}
                else:
                    from services.ayue_agent.private_contracts import PrivateAgentDecision

                    decision = PrivateAgentDecision(
                        kind="confirmation",
                        intent="date_coordination",
                        tool_name=name,
                        confidence=1,
                        evidence_span=str(message or "")[:80],
                    )
                    terminal_reply = _save_confirmation(ctx.base, decision, origin_run_id=run_id)
                    _mark_pi_confirmation(user_id=user_id, origin_run_id=run_id)
                    terminal = True
                    choice_created = True
                    result = {"pending_confirmation": True, "reused": False}
            elif name == "private.interaction.cancel_pending":
                if not _explicit_cancel_requested(message):
                    status, error_code = "failed", "explicit_action_required"
                    result = {"pending_confirmation": True, "cancelled": False}
                    item = project(name, status, result, error_code)
                    emit(
                        "tool_finished",
                        step_id=f"{tool_call_count}:private",
                        outcome="error",
                        duration_ms=str(round((time.perf_counter() - tool_started_at) * 1000)),
                    )
                    return {"observations": [item], "stop": False}
                active = _active_private_choices(user_id, ctx.base.room_id)
                if not active:
                    result = {"pending_confirmation": False, "cancelled": False}
                else:
                    active.sort(key=lambda item: float(item.get("created_at", 0) or 0), reverse=True)
                    choice = manager.cancel_choice(
                        user_id=user_id,
                        room_id=ctx.base.room_id,
                        surface=SURFACE_PRIVATE,
                        choice_id=str(active[0].get("_id") or ""),
                    )
                    if choice:
                        sync_choice_message_projection(
                            messages_coll, room_id=ctx.base.room_id, projection=choice,
                        )
                    terminal = True
                    terminal_reply = "好，我先把目前這張確認卡收起來。"
                    result = {"pending_confirmation": False, "cancelled": bool(choice)}
            elif name == "private.surface.redirect_to_public":
                handoff = PrivateSurfaceHandoff(
                    target="public_ayue",
                    mode="prefill",
                    original_message=str(message or "")[:1200],
                    auto_send=False,
                )
                terminal = True
                terminal_reply = PRIVATE_REDIRECT_COPY["warm"]
                result = {"redirect": True}
            elif name == "private.relationship.record_post_date_feedback":
                action = str(arguments.get("action") or "")
                evidence = str(arguments.get("evidence_span") or "")[:300]
                if action not in _FEEDBACK_ACTIONS or not evidence or evidence not in message:
                    status, error_code = "failed", "tool_schema_invalid"
                    result = {"needs_clarification": True}
                elif not ctx.pending_post_date_feedback:
                    result = {"status": "not_related", "recorded": False}
                else:
                    consumed = consume_pending_post_date_feedback(
                        match_doc,
                        ctx.profile_state,
                        user_id,
                        other_id,
                        message,
                        source_message_id=ctx.source_message_id,
                    )
                    if consumed is None:
                        result = {"status": "not_related", "recorded": False}
                    else:
                        post_date_feedback_recorded = True
                        terminal = True
                        terminal_reply = consumed
                        terminal_intent = "private_feedback"
                        result = {
                            "status": "recorded",
                            "recorded": True,
                            "visibility": "owner_private",
                            "shared_with_counterparty": False,
                        }
            elif name == "private.relationship.capture_memory_candidate":
                category = str(arguments.get("category") or "")
                evidence = str(arguments.get("evidence_span") or "")[:300]
                if (
                    category not in _MEMORY_CATEGORIES
                    or not _memory_candidate_allowed(message, evidence)
                ):
                    status, error_code = "failed", "tool_schema_invalid"
                    result = {"needs_clarification": True}
                else:
                    memory_candidate = {"category": category, "evidence_span": evidence}
                    result = {"status": "queued_for_review", "saved": False}
            elif name == "private.relationship.respond_to_probe":
                action = str(arguments.get("action") or "")
                evidence = str(arguments.get("evidence_span") or "")[:300]
                if action not in _FEEDBACK_ACTIONS or not evidence or evidence not in message:
                    status, error_code = "failed", "tool_schema_invalid"
                    result = {"needs_clarification": True}
                elif not ctx.pending_probe:
                    result = {"status": "not_related", "recorded": False}
                else:
                    consumed = consume_pending_probe_answer(
                        match_doc,
                        ctx.profile_state,
                        user_id,
                        other_id,
                        message,
                    )
                    if consumed:
                        terminal = True
                        terminal_reply = consumed
                        terminal_intent = "private_feedback"
                    result = {
                        "status": "recorded" if consumed else "not_related",
                        "recorded": bool(consumed),
                    }
            else:
                status, error_code = "failed", "tool_not_allowed"
        except Exception as exc:
            count = schema_failures.get(name, 0) + 1
            schema_failures[name] = count
            status, error_code = "failed", "tool_schema_invalid" if count <= 2 else type(exc).__name__
            result = {"needs_clarification": True}
            if count >= 2:
                terminal = True
                terminal_reply = PRIVATE_CLARIFICATION_REPLY

        item = project(name, status, result, error_code)
        emit(
            "tool_finished",
            step_id=f"{tool_call_count}:private",
            outcome="ok" if status == "ok" else "error",
            duration_ms=str(round((time.perf_counter() - tool_started_at) * 1000)),
        )
        if terminal:
            return {"observations": [item], "stop": True}
        return {"observations": [item], "stop": False}

    def final_validate(text: str, repair_attempted: bool) -> dict[str, Any]:
        del repair_attempted
        reply = _safe_reply(text, observations)
        if reply:
            return {"accept": True, "text": reply}
        return {
            "accept": False,
            "disableTools": True,
            "repairPrompt": (
                "上一個答案沒有通過 Private 安全檢查。只能使用目前已驗證的 context 與 tool result，"
                "重寫成繁體中文自然短答；不要提及內部欄位，不要聲稱尚未完成的邀請或記憶已成功。"
            ),
            "error": "pi_private_reply_invalid",
        }

    failure: str | None = None
    bridge_result: dict[str, Any] = {}
    try:
        emit("stage", text="我先抓一下重點，免得聊歪～")
        bridge_result = run_bridge(
            {
                "systemPrompt": ctx.system_prompt(PRIVATE_PI_POLICY),
                "prompt": str(message or "")[:1200],
                "history": ctx.history(),
                "tools": tool_schemas(),
                "initialToolNames": sorted(PRIVATE_TOOL_NAMES),
                "maxRounds": 5,
                "maxToolCalls": 8,
                "validateFinal": True,
            },
            model_call,
            tool_call,
            final_validate=final_validate,
            timeout=90,
            max_model_calls=5,
            max_tool_calls=8,
        )
        failure = str(bridge_result.get("error") or "") or None
        if bridge_result.get("budget_exhausted") and not terminal:
            failure = failure or "pi_step_limit"
    except Exception as exc:
        failure = "pi_deadline_exceeded" if str(exc) == "pi_deadline_exceeded" else "pi_runtime_failed"
        trace["exception"] = type(exc).__name__

    reply = ""
    conversation_intent = "private_advice"
    if handoff is not None:
        reply = terminal_reply or PRIVATE_REDIRECT_COPY["warm"]
        conversation_intent = "private_redirect"
    elif terminal:
        reply = terminal_reply or "請先完成畫面上的確認，這次還沒有執行變更。"
        conversation_intent = terminal_intent
    elif failure:
        reply = PRIVATE_RUNTIME_FALLBACK_REPLY
        conversation_intent = "private_clarification"
    else:
        reply = _safe_reply(str(bridge_result.get("finalText") or ""), observations) or PRIVATE_CLARIFICATION_REPLY

    memory_queued = False
    if memory_candidate and not failure and ctx.source_message_id:
        try:
            memory_queued = bool(
                enqueue_relationship_memory_extraction(
                    user_id,
                    other_id,
                    str(match_doc.get("_id") or ""),
                    ctx.source_message_id,
                )
            )
        except Exception:
            memory_queued = False
        if memory_queued:
            observations.append({
                "status": "memory_candidate_queued",
                "tool": "private.relationship.capture_memory_candidate",
                "result": {"saved": False},
                "error_code": None,
            })

    if on_token and reply:
        for start_at in range(0, len(reply), 18):
            on_token(reply[start_at:start_at + 18])

    choice_prompt = None
    try:
        choice_prompt = manager.choice_for_run(
            user_id=user_id,
            room_id=ctx.base.room_id,
            surface=SURFACE_PRIVATE,
            origin_run_id=run_id,
        )
    except Exception:
        choice_prompt = None

    result = PrivateAgentResult(
        handled=True,
        reply=reply,
        conversation_intent=conversation_intent,
        agent_run_id=run_id,
        agent_mode="pi",
        fallback_reason=failure,
        handoff=handoff,
        choice_prompt=choice_prompt,
        profile_write_allowed=False if memory_candidate or choice_created else True,
        profile_write_reason=(
            "relationship_memory_candidate" if memory_candidate
            else "confirmation" if choice_created
            else "casual"
        ),
        relationship_memory_candidate=(memory_candidate if memory_queued else None),
        post_date_feedback_recorded=post_date_feedback_recorded,
    )
    trace.update({
        "pi_model_calls": model_call_count,
        "pi_tool_calls": tool_call_count,
        "memory_candidate_queued": memory_queued,
        "post_date_feedback_recorded": post_date_feedback_recorded,
        "result": {"intent": result.conversation_intent, "fallback": result.fallback_reason},
        "latency_ms": round((time.perf_counter() - started) * 1000),
    })
    _trace(run_id, trace)
    return result
