from __future__ import annotations

import asyncio
import base64
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from fastapi import WebSocket
from .language import display_transcript
from .capabilities import CATALOG, ACTIONS, available_actions
from .contextual import bind_target, safe_result
from .screen_context import screen_context_payload
from .recommendations import resolve_calendar_recommendation
from .next_step import (
    build_place_next_step_offer,
    place_next_step_action,
    select_place_next_step,
)
from .weather import resolve_weather_location
from .capability_proxy import (
    CapabilityRefError,
    CapabilityRefSigner,
    MAX_OPERATIONS,
    action_allowed,
    action_permissions_allowed,
    action_metadata,
    find_capabilities,
    proposal_from_capability,
)
from .experience import expand_catalog_operations
from .task_service import VoiceTaskService
from .drafts import (DRAFT_FIELDS, draft_editable, draft_question, draft_summary,
                     spoken_draft_patch)
from .telemetry import record_voice_metric
from .voice_experience import app_help
from .spoken_commands import cancellation_focus
from .template_dispatcher import (
    authoritative_template_proposal,
    template_proposal_from_function,
    template_tool_call_for_proposal,
)

from .contracts import (
    VoiceProposal,
    calendar_write_preflight,
    confirmation_matches,
    confirmation_phrase,
    context_allows_proposal,
    normalized_phrase,
    requires_confirmation,
    safe_context,
    safe_reply,
    validate_proposal,
    visible_choice_action,
    deterministic_proposal,
    is_chat_cooldown_reason_request,
    is_companion_matching_request,
)


SendEvent = Callable[[dict[str, Any]], Awaitable[None]]
SCREEN_CONTEXT_TIMEOUT_SECONDS = 0.75
SCREEN_CONTEXT_COALESCE_SECONDS = 0.1
BACKGROUND_RESULT_IDLE_SECONDS = 8.0


def session_started_prompt(context: dict[str, Any]) -> str:
    """Build the first Live control message in the configured reply language."""
    config = context.get("voice_config") or {}
    language = str(config.get("response_language") or "zh-TW")
    messages = {
        "zh-TW": "語音模式已就緒，請用一個很短的句子主動打招呼；有安全名稱時可以自然稱呼。",
        "zh-CN": "语音模式已就绪，请用一句很短的话主动打招呼；有安全名称时可以自然称呼。",
        "en-US": "Voice mode is ready. Greet the user in one short sentence; use the safe display name when available.",
    }
    return (
        "[VOICE_SESSION_STARTED]\n"
        f"response_language={language}\n"
        f"user_display_name={str(config.get('self_name') or '')[:40]}\n"
        f"{messages.get(language, messages['zh-TW'])}"
    )


@dataclass
class DuplexConfirmation:
    confirmation_id: str
    proposal: VoiceProposal
    phrase: str
    scope: str
    revision: int
    expires_at: float
    task_id: str = ""
    batch_id: str = ""


@dataclass(frozen=True)
class PendingToolResult:
    call_id: str
    name: str
    task_id: str = ""
    batch_id: str = ""


def _append_transcript(current: str, incoming: str) -> str:
    # Live transcription delivers deltas, including meaningful word spaces.
    # Trimming each delta turns "Hello " + "world" into "Helloworld".
    text = incoming
    if not text:
        return current
    if not current or text.startswith(current):
        return text[:2000]
    return f"{current}{text}"[:2000]


def _identity_question(text: str) -> bool:
    return normalized_phrase(text) in {
        "你是誰", "你是誰啊", "請問你是誰", "你叫什麼", "你的名字是什麼",
        "你是谁", "你是谁啊", "请问你是谁", "你叫什么", "你的名字是什么",
    }


def _proposal_from_function(call: Any, *, revision: int) -> VoiceProposal | None:
    raw = dict(call.args or {})
    name = str(call.name or "")
    intent = ""
    arguments: dict[str, Any] = {}
    if name == "select_screen_target":
        intent = "ui.target.select"
    elif name == 'read_agent_quota':
        intent = 'quota.query'
    elif name == 'read_chat_status':
        intent = 'chat.status.query'
        arguments = {'contact_name': raw.get('contact_name', '')}
    elif name in {"read_shared_dates", "respond_date_invitation", "update_shared_date", "confirm_shared_date"}:
        intent = {"read_shared_dates": "date.query", "respond_date_invitation": "date.respond", "update_shared_date": "date.update", "confirm_shared_date": "date.confirm"}[name]
        arguments = {"contact_name": raw.get("contact_name", "")}
        if name == "respond_date_invitation":
            arguments["accepted"] = raw.get("accepted")
        if name == "update_shared_date":
            arguments["changes"] = raw.get("changes")
    elif name == "navigate_app":
        intent = "app.navigate"
        arguments["destination"] = raw.get("destination")
    elif name == "open_app_page":
        intent = {
            "profile": "profile.open",
            "settings": "settings.open",
        }.get(str(raw.get("page") or ""), "")
    elif name == "patch_profile":
        intent = "profile.patch"
        arguments["changes"] = raw.get("changes")
    elif name == "request_profile_save":
        intent = "profile.request_commit"
    elif name == "set_app_setting":
        intent = "settings.set"
        arguments = {"key": raw.get("key"), "enabled": raw.get("enabled")}
    elif name == "write_post_caption":
        intent = {
            "open": "post.open_draft",
            "replace": "post.replace_caption",
            "append": "post.append_caption",
        }.get(str(raw.get("mode") or ""), "")
        arguments["caption"] = raw.get("caption")
    elif name == "request_post_publish":
        intent = "post.request_publish"
    elif name == "select_recent_post_photos":
        intent = "post.select_recent_photos"
        positions = raw.get("positions")
        if isinstance(positions, list) and positions:
            arguments["positions"] = positions
        else:
            arguments["count"] = raw.get("count")
    elif name == "open_post":
        intent = "post.open"
        arguments = {}
    elif name == "read_calendar":
        intent = "calendar.query"
        arguments = {
            key: raw.get(key)
            for key in ("range", "start_date", "end_date")
            if key in raw
        }
    elif name == "create_calendar_event":
        intent = "calendar.create"
        arguments = {
            "title": raw.get("title"),
            "date": raw.get("date"),
            "start_time": raw.get("start_time"),
            "end_time": raw.get("end_time"),
            "location": raw.get("location"),
            "notes": raw.get("notes"),
        }
    elif name == "update_calendar_event":
        intent = "calendar.update"
        arguments = {
            "target": raw.get("target"),
            "date": raw.get("date"),
            "start_time": raw.get("start_time"),
            "end_time": raw.get("end_time"),
            **({"location": raw.get("location")} if "location" in raw else {}),
            **({"notes": raw.get("notes")} if "notes" in raw else {}),
        }
    elif name == "cancel_calendar_event":
        intent = "calendar.cancel"
        arguments = {
            key: raw.get(key)
            for key in ("target", "date")
            if key in raw
        }
    elif name == "personality_exploration_turn":
        intent = "personality.explore"
        arguments["message"] = raw.get("message")
    elif name == "read_match_status":
        intent = "match.query"
        arguments["view"] = "status"
    elif name == "read_match_hub":
        intent = "match.query"
        arguments["view"] = "hub"
    elif name == "ask_matching_ayue":
        intent = "match.ayue_query"
        arguments["question"] = raw.get("question")
    elif name == "ask_public_ayue":
        intent = "ayue.public_query"
        arguments = {"domain": raw.get("domain"), "question": raw.get("question")}
    elif name == "ask_private_ayue":
        intent = "ayue.private_query"
        arguments = {
            "contact_name": raw.get("contact_name"),
            "question": raw.get("question"),
        }
    elif name == "list_contacts":
        intent = (
            "safety.blocked_users_query"
            if str(raw.get("view") or "accepted") == "blocked"
            else "contacts.query"
        )
    elif name == "read_self_profile":
        intent = "self.query"
        arguments["detail"] = raw.get("detail")
    elif name == "read_memories":
        intent = "memory.query"
        arguments["query"] = raw.get("query")
    elif name == "add_memory":
        intent = "memory.add"
        arguments = {
            "label": raw.get("label"),
            "stance": raw.get("stance"),
        }
    elif name == "activate_visible_choice":
        intent = "ui.choice.activate"
        arguments["action"] = raw.get("action")
    elif name == "open_chat":
        intent = (
            "ayue.private_open"
            if str(raw.get("mode") or "chat") == "private_ayue"
            else "chat.open"
        )
        arguments["contact_name"] = raw.get("contact_name")
    elif name == "send_chat_message":
        operation = str(raw.get("operation") or "send")
        intent = {
            "block": "safety.block_user",
            "unblock": "safety.unblock_user",
        }.get(operation, "chat.request_send")
        arguments = {"contact_name": raw.get("contact_name")}
        if intent == "chat.request_send":
            arguments["message"] = raw.get("message")
    if "target_ref" in raw:
        arguments["target_ref"] = raw["target_ref"]
    return validate_proposal(
        {
            "intent": intent,
            "arguments": arguments,
            "reply": "好的，我來處理。",
        },
        base_revision=revision,
    )


async def run_duplex_session(
    websocket: WebSocket,
    *,
    provider: Any,
    limiter: Any,
    identity: str,
    initial_context: dict[str, Any],
    max_session_seconds: int,
    send_event: SendEvent,
    conversation_memory: str = "",
    memory_turns: list[dict[str, str]] | None = None,
    weather_service: Any | None = None,
    weather_location_provider: Any | None = None,
    weather_user_id: str = "",
    user_id: str = "",
    voice_session_id: str = "",
    capability_secret: bytes = b"",
    routing_mode: str = "legacy",
    task_service: VoiceTaskService | None = None,
    quota_reader: Any | None = None,
    quota_poll_seconds: float = 30,
) -> None:
    """Bridge one app WebSocket to one persistent, full-duplex Gemini session."""

    context = safe_context(initial_context)
    initial_quota = None
    if quota_reader is not None:
        from .status_contract import public_quota
        try:
            initial_quota = public_quota(await quota_reader())
        except Exception:
            initial_quota = public_quota(None)
        if initial_quota.get('exhausted') or initial_quota.get('state') == 'unknown':
            await send_event({'type': 'quota_status', 'quota': initial_quota})
            await send_event({'type': 'error', 'code': 'agent_quota_exhausted'
                              if initial_quota.get('exhausted') else 'agent_quota_unavailable'})
            await websocket.close(code=1000)
            return
    recent_public_places: list[dict[str, str]] = []
    provider_routing_mode = (
        routing_mode if routing_mode in {"proxy", "template"} else "legacy"
    )
    live = provider.create_duplex_session(
        voice_config=context.get("voice_config"),
        conversation_memory=conversation_memory,
        routing_mode=provider_routing_mode,
    )
    await live.connect()
    last_screen_payload: str | None = None
    screen_sync_lock = asyncio.Lock()
    screen_sync_task: asyncio.Task[None] | None = None
    screen_update_generation = 0

    async def sync_screen_context(*, force: bool = False) -> bool:
        nonlocal last_screen_payload
        started_at = time.monotonic()
        try:
            # Include lock acquisition in the budget: screen updates share the
            # Live transport with audio and must never hold it indefinitely.
            async with asyncio.timeout(SCREEN_CONTEXT_TIMEOUT_SECONDS):
                async with screen_sync_lock:
                    payload = screen_context_payload(context)
                    if force or payload != last_screen_payload:
                        await live.send_screen_context(payload)
                        last_screen_payload = payload
            return True
        except Exception as error:
            record_voice_metric(
                "screen_context_skipped", routing_mode=provider_routing_mode,
                result_code="timeout" if isinstance(error, TimeoutError) else "send_failed",
                latency_ms=(time.monotonic() - started_at) * 1000,
            )
            return False

    def queue_screen_context() -> None:
        nonlocal screen_sync_task, screen_update_generation
        screen_update_generation += 1
        if screen_sync_task is not None and not screen_sync_task.done():
            return

        async def flush() -> None:
            while not stopped:
                await asyncio.sleep(SCREEN_CONTEXT_COALESCE_SECONDS)
                generation = screen_update_generation
                # Read the latest context after coalescing, including revoked
                # permissions. Do not accumulate stale payloads or retry loops.
                if not await sync_screen_context():
                    return
                if generation == screen_update_generation:
                    return

        screen_sync_task = asyncio.create_task(flush())
    pending: DuplexConfirmation | None = None
    active_draft_ref = ""
    draft_handled_text = ""
    draft_confirmation_needs_input = False
    client_metric_count = 0
    awaiting_results: dict[str, PendingToolResult] = {}
    state_lock = asyncio.Lock()
    task_schedule_lock = asyncio.Lock()
    stopped = False
    current_response_id: str | None = None
    current_sequence = 0
    current_transcript = ""
    current_audio_allowed: bool | None = None
    live_turn_active = False
    last_live_activity = time.monotonic()
    user_speaking = False
    last_user_transcript = ""
    input_transcript_finished = False
    next_reply_code = "conversation"
    close_after_turn = False
    first_chunk: bytes | None = None
    last_completed_first_chunk: bytes | None = None
    checking_replayed_turn = False
    suppress_current_turn = False
    seen_tool_calls: set[str] = set()
    template_fast_path_transcript = ""
    next_step_dispatched_intent = ""
    next_step_selected_proposal: VoiceProposal | None = None
    template_fast_path_call_ids: set[str] = set()
    tool_response_cache: dict[str, tuple[str, dict[str, Any]]] = {}
    non_blocking_tool_tasks: dict[str, asyncio.Task[None]] = {}
    non_blocking_tool_started_at: dict[str, float] = {}
    capability_argument_defaults: dict[str, dict[str, Any]] = {}
    background_actions: dict[str, VoiceProposal] = {}
    background_action_transcripts: dict[str, str] = {}
    task_actions: dict[str, tuple[str, str]] = {}
    queued_background_results: list[tuple[str | dict[str, Any], str]] = []
    progress_turn_pending = False
    background_flush_task: asyncio.Task[None] | None = None
    confirmation_timeout_task: asyncio.Task[None] | None = None
    task_watcher_task: asyncio.Task[None] | None = None
    quota_watcher_task: asyncio.Task[None] | None = None
    quota_stopped = False
    task_revision_cache: dict[str, int] = {}
    private_read_authorized = False
    last_delegated_proposal: VoiceProposal | None = None
    last_delegated_expires_at = 0.0
    recently_confirmed_id = ""
    recently_confirmed_until = 0.0
    started = time.monotonic()
    remembered_turns = memory_turns if memory_turns is not None else []
    last_remembered_user = ""
    proxy_enabled = routing_mode == "proxy"
    template_enabled = routing_mode == "template"
    task_tools_enabled = proxy_enabled or template_enabled
    active_routing_mode = provider_routing_mode
    capability_signer = CapabilityRefSigner(capability_secret or b"app-voice-test-secret")
    owner_id = str(user_id or weather_user_id or identity)[:128]
    session_id = str(voice_session_id or identity)[:128]
    task_service = task_service if task_service is not None and task_service.enabled else None
    record_voice_metric(
        "session_started", routing_mode=active_routing_mode,
        result_code="started",
    )

    async def read_quota():
        from .status_contract import public_quota
        try:
            return public_quota(await quota_reader()) if quota_reader else public_quota(None)
        except Exception:
            return public_quota(None)

    async def stop_for_quota(quota):
        nonlocal quota_stopped
        if quota_stopped:
            return
        quota_stopped = True
        if task_service is not None:
            for row in task_service.pause_for_quota(owner_id):
                await emit_task_update(row)
        await send_event({'type': 'quota_status', 'quota': quota})
        await send_event({'type': 'error', 'code': 'agent_quota_exhausted'
                          if quota.get('exhausted') else 'agent_quota_unavailable'})
        await live.close()
        # New clients keep only the result/control channel while already-issued
        # operations finish. No microphone or model input is accepted below.
        if not context.get('feature_status', {}).get('operational_status'):
            await websocket.close(code=1000)

    async def watch_quota():
        first = True
        while not stopped and not quota_stopped:
            quota = initial_quota if first and initial_quota is not None else await read_quota()
            first = False
            await send_event({'type': 'quota_status', 'quota': quota})
            if quota.get('exhausted') or quota.get('state') == 'unknown':
                await stop_for_quota(quota)
                return
            await asyncio.sleep(quota_poll_seconds)

    def remember(role: str, value: Any) -> None:
        nonlocal last_remembered_user
        text = display_transcript(str(value or "")).strip()[:2000]
        if role not in {"user", "assistant"} or not text:
            return
        if role == "user" and text == last_remembered_user:
            return
        item = {"role": role, "content": text}
        if remembered_turns and remembered_turns[-1] == item:
            return
        remembered_turns.append(item)
        if role == "user":
            last_remembered_user = text
        if len(remembered_turns) > 100:
            del remembered_turns[0]

    def cache_capability_defaults(result: dict[str, Any]) -> None:
        for match in result.get("matches") or []:
            for action in match.get("actions") or []:
                if not isinstance(action, dict):
                    continue
                ref = str(action.get("capability_ref") or "")
                defaults = action.get("suggested_arguments")
                if ref and isinstance(defaults, dict):
                    capability_argument_defaults[ref] = dict(defaults)
        # A session should never need an unbounded number of two-minute refs.
        while len(capability_argument_defaults) > 32:
            capability_argument_defaults.pop(next(iter(capability_argument_defaults)))

    await send_event({
        "type": "state",
        "state": "duplex_ready",
        "microphone": "streaming",
        "resumed": False,
    })

    async def tool_response(
        call: Any, response: dict[str, Any],
    ) -> None:
        call_id = str(call.id or "")
        name = str(call.name or "")
        if not call_id:
            return
        tool_started_at = non_blocking_tool_started_at.pop(call_id, None)
        if tool_started_at is not None:
            outcome = str(
                response.get("status") or response.get("error_code") or "ok"
            )
            record_voice_metric(
                "non_blocking_tool_completed",
                routing_mode=active_routing_mode,
                capability_id=name,
                latency_ms=(time.monotonic() - tool_started_at) * 1000,
                result_code=outcome,
            )
        if call_id in template_fast_path_call_ids:
            # A deterministic template fallback is an internal dispatch, not
            # a Gemini-issued function call. Sending a tool response with its
            # synthetic ID would be rejected by the Live API.
            await queue_background_result(response, "direct")
            return
        if call_id:
            tool_response_cache[call_id] = (name, dict(response))
        await live.send_tool_response(
            call_id=call_id,
            name=name,
            response=response,
        )

    async def reject_google_calendar_write(
        call: Any, proposal: VoiceProposal,
    ) -> bool:
        if proposal.intent not in {"calendar.update", "calendar.cancel"}:
            return False
        target = str(proposal.arguments.get("target") or "")
        calendar_block = calendar_write_preflight(
            f"修改 {target}" if proposal.intent == "calendar.update"
            else f"取消 {target}",
            context,
        )
        if calendar_block is None:
            return False
        await tool_response(call, {
            "status": calendar_block[0],
            "message": calendar_block[1],
            "spoken_prompt": calendar_block[1],
        })
        return True

    def localized_prompt(kind: str) -> str:
        language = str((context.get("voice_config") or {}).get("response_language") or "zh-TW")
        prompts = {
            "working": {
                "zh-TW": "我找一下，稍等一下。",
                "zh-CN": "我查一下，请稍等。",
                "en-US": "Let me check. One moment.",
            },
            "confirm": {
                "zh-TW": "如果要繼續，請說「確認」。",
                "zh-CN": "如果要继续，请说“确认”。",
                "en-US": 'To continue, say "confirm".',
            },
            "match_start_confirm": {
                "zh-TW": "要開始配對的話，說「開始」或「可以開始」。",
                "zh-CN": "要开始配对的话，说“开始”或“可以开始”。",
                "en-US": 'To start matching, say "start" or "go ahead".',
            },
            "private_confirm": {
                "zh-TW": "這次會讀取你看得到的聊天內容，要繼續請說「確認」。",
                "zh-CN": "这次会读取你可以看到的聊天内容，要继续请说“确认”。",
                "en-US": 'This will read chat content you can access. To continue, say "confirm".',
            },
        }
        return prompts[kind].get(language, prompts[kind]["zh-TW"])

    def action_confirmation_prompt(proposal: VoiceProposal) -> str:
        if proposal.intent == 'memory.disable':
            return f"停止使用長期偏好「{proposal.arguments.get('label', '')}」。要繼續請說「確認」。"
        if proposal.intent in DRAFT_FIELDS and context.get("feature_status", {}).get("conversation_drafts"):
            return f"{draft_summary(proposal.intent, proposal.arguments)}。要繼續請說「確認」，也可以直接修改。"
        kind = (
            "match_start_confirm"
            if confirmation_phrase(proposal) == "確認開始配對"
            else "confirm"
        )
        return localized_prompt(kind)

    async def emit_task_update(row: dict[str, Any] | None) -> None:
        if row is None or task_service is None or stopped:
            return
        record_voice_metric(
            "task_update", routing_mode=active_routing_mode,
            capability_id=row.get("capability_id"), task_stage=row.get("stage"),
            result_code=row.get("status"),
        )
        projected = task_service.project(row)
        task_ref = str(projected.get("task_ref") or "")
        if task_ref:
            task_revision_cache[task_ref] = int(projected.get("revision", 0) or 0)
        try:
            await send_event({
                "type": "task_update",
                "task_protocol_version": 1,
                "task": projected,
            })
        except Exception:
            # Durable state remains authoritative when the voice socket closes.
            return

    def cancel_confirmation_timeout() -> None:
        nonlocal confirmation_timeout_task
        if confirmation_timeout_task is not None:
            confirmation_timeout_task.cancel()
        confirmation_timeout_task = None

    def arm_confirmation_timeout(value: DuplexConfirmation) -> None:
        nonlocal confirmation_timeout_task
        cancel_confirmation_timeout()
        if not value.task_id or task_service is None:
            return

        async def expire() -> None:
            nonlocal pending, confirmation_timeout_task
            try:
                await asyncio.sleep(max(0.0, value.expires_at - time.time()))
                async with state_lock:
                    if pending is None or pending.confirmation_id != value.confirmation_id:
                        return
                    expired = pending
                    pending = None
                updated = task_service.update(
                    expired.task_id, status="waiting_input",
                    stage="confirmation_expired", error_code="confirmation_expired",
                    result_summary="確認已逾時，請重新說明是否繼續。",
                    expected_statuses={"waiting_confirmation"},
                    expected_action_id=expired.confirmation_id,
                )
                await emit_task_update(updated)
                await schedule_ready_tasks(expired.batch_id)
                if not stopped:
                    try:
                        await send_event({
                            "type": "confirmation_expired",
                            "confirmation_id": expired.confirmation_id,
                            "task_ref": (
                                task_service.project(updated).get("task_ref")
                                if updated else ""
                            ),
                        })
                    except Exception:
                        pass
            except asyncio.CancelledError:
                return
            finally:
                if confirmation_timeout_task is asyncio.current_task():
                    confirmation_timeout_task = None

        confirmation_timeout_task = asyncio.create_task(expire())

    async def dispatch_task_row(row: dict[str, Any]) -> None:
        nonlocal pending, progress_turn_pending, next_reply_code
        nonlocal draft_confirmation_needs_input
        if task_service is None or row.get("status") != "queued":
            return
        if quota_stopped:
            return
        if row.get('capability_id') == 'app.help':
            result = app_help(context, str((row.get('arguments') or {}).get('query') or ''))
            await emit_task_update(task_service.update(str(row['task_id']), status='completed', stage='completed',
                                                       result_summary='已整理 App 功能說明。', expected_statuses={'queued'}))
            await queue_background_result(result, 'task')
            return
        if row.get('capability_id') in {'date.plan','memory.disable'} and (
            not context.get('feature_status', {}).get('voice_experience')
            or not action_permissions_allowed(row['capability_id'], context)
        ):
            await emit_task_update(task_service.update(str(row['task_id']), status='failed', stage='permission_denied',
                result_summary='請確認 App 版本與所需的語音權限。', error_code='permission_denied', expected_statuses={'queued'}))
            return
        if row.get("draft") and draft_question(str(row.get("capability_id")), row.get("arguments") or {}):
            await emit_task_update(task_service.update(
                str(row["task_id"]), status="waiting_input", stage="draft_collecting",
                result_summary=draft_question(row["capability_id"], row.get("arguments") or {}),
                expected_statuses={"queued"},
            ))
            return
        proposal = proposal_from_capability(
            str(row.get("capability_id") or ""),
            row.get("arguments") or {},
            revision=int(context.get("revision") or 0),
        )
        if proposal is None:
            await emit_task_update(task_service.update(
                str(row.get("task_id") or ""), status="failed", stage="validation_failed",
                error_code="validation_failed", result_summary="任務參數無法通過驗證。",
                expected_statuses={"queued"},
            ))
            return
        if proposal.intent in {"weather.query", "calendar.query", "quota.query", "chat.status.query", "contacts.query", "match.query", "date.query"}:
            draft = current_draft()
            if draft:
                await manage_draft({"action": "pause", "task_ref": draft["task_ref"]})
        bound, target_error = bind_target(proposal.intent, proposal.arguments, context)
        if target_error:
            await emit_task_update(task_service.update(
                str(row.get("task_id") or ""), status="waiting_input", stage="needs_input",
                error_code=target_error, result_summary="請重新選擇目前畫面的項目。",
                expected_statuses={"queued"},
            ))
            return
        proposal = VoiceProposal(proposal.intent, bound, proposal.reply, proposal.base_revision)
        if not context_allows_proposal(context, proposal):
            await emit_task_update(task_service.update(
                str(row.get("task_id") or ""), status="failed", stage="permission_denied",
                error_code="permission_denied", result_summary="這項功能沒有被使用者授權。",
                expected_statuses={"queued"},
            ))
            return
        if proposal.intent == "post.request_publish" and not context.get("can_publish"):
            await emit_task_update(task_service.update(
                str(row.get("task_id") or ""), status="waiting_input", stage="not_ready",
                error_code="not_ready", result_summary="還沒有選擇可發布的圖片。",
                expected_statuses={"queued"},
            ))
            return
        if proposal.intent == 'quota.query':
            from .status_contract import status_result
            result = status_result(quota=await read_quota())
            await emit_task_update(task_service.update(
                str(row['task_id']), status='completed' if result['success'] else 'failed',
                stage='completed', result_summary=result['message'], expected_statuses={'queued'},
            ))
            return
        if proposal.intent == "weather.query":
            running = task_service.claim(
                str(row.get("task_id") or ""), stage="waiting_worker",
                progress_percent=0,
            )
            if running is None:
                return
            await emit_task_update(running)

            async def weather_job() -> None:
                nonlocal progress_turn_pending
                worker_acquired = False
                try:
                    lease_id = str(running.get("lease_id") or "")
                    while not worker_acquired:
                        current_task = task_service.get_task(
                            owner_id, str(row.get("task_id") or ""),
                        )
                        if not current_task or current_task.get("status") == "cancel_requested":
                            updated = task_service.update(
                                str(row.get("task_id") or ""), status="cancelled",
                                stage="cancelled", result_summary="任務已取消。",
                                expected_statuses={"cancel_requested"},
                            )
                            await emit_task_update(updated)
                            await schedule_ready_tasks(str(row.get("batch_id") or ""))
                            return
                        worker_acquired = await asyncio.to_thread(
                            task_service.acquire_worker_slot, 1.0,
                        )
                        if not worker_acquired:
                            task_service.renew_lease(
                                str(row.get("task_id") or ""), lease_id,
                            )
                    current_task = task_service.get_task(
                        owner_id, str(row.get("task_id") or ""),
                    )
                    if current_task and current_task.get("status") == "cancel_requested":
                        updated = task_service.update(
                            str(row.get("task_id") or ""), status="cancelled",
                            stage="cancelled", result_summary="任務已取消。",
                            expected_statuses={"cancel_requested"},
                        )
                        await emit_task_update(updated)
                        await schedule_ready_tasks(str(row.get("batch_id") or ""))
                        return
                    if not current_task or current_task.get("status") != "running":
                        return
                    updated = task_service.update(
                        str(row.get("task_id") or ""), status="running",
                        stage="weather_lookup", progress_percent=20,
                        expected_statuses={"running"},
                    )
                    await emit_task_update(updated)
                    if weather_service is None:
                        raise RuntimeError("weather_service_disabled")
                    location = await resolve_weather_location(
                        proposal.arguments.get("location"), weather_user_id,
                        weather_location_provider,
                    )
                    result = await asyncio.to_thread(weather_service.query, location)
                    current_task = task_service.get_task(owner_id, str(row.get("task_id") or ""))
                    if current_task and current_task.get("status") == "cancel_requested":
                        updated = task_service.update(
                            str(row.get("task_id") or ""), status="cancelled",
                            stage="cancelled", result_summary="任務已取消。",
                            expected_statuses={"cancel_requested"},
                        )
                        await emit_task_update(updated)
                        await schedule_ready_tasks(str(row.get("batch_id") or ""))
                        return
                    success = result.get("status") in {"ok", "partial"}
                    summary = str(result.get("message") or "")[:600]
                    updated = task_service.update(
                        str(row.get("task_id") or ""),
                        status="completed" if success else "failed",
                        stage="completed" if success else "failed",
                        progress_percent=100 if success else None,
                        result_summary=summary,
                        error_code="" if success else str(result.get("error_code") or "weather_unavailable"),
                        expected_statuses={"running"},
                    )
                except Exception as error:
                    updated = task_service.update(
                        str(row.get("task_id") or ""), status="failed", stage="failed",
                        error_code=str(error)[:80] or "weather_unavailable",
                        result_summary="目前暫時查不到天氣與空氣品質。",
                        expected_statuses={"running"},
                    )
                finally:
                    if worker_acquired:
                        task_service.release_worker_slot()
                await emit_task_update(updated)
                if not stopped and updated and updated.get("result_summary"):
                    await queue_background_result({
                        "task_ref": updated.get("task_ref"),
                        "status": updated.get("status"),
                        "message": updated.get("result_summary"),
                    }, "task")
                await schedule_ready_tasks(str(row.get("batch_id") or ""))

            asyncio.create_task(weather_job())
            return
        needs_private_confirmation = (
            proposal.intent == "ayue.private_query" and not private_read_authorized
        )
        if requires_confirmation(proposal) or needs_private_confirmation:
            if pending is not None:
                return
            phrase = confirmation_phrase(proposal)
            pending = DuplexConfirmation(
                confirmation_id=uuid.uuid4().hex,
                proposal=proposal,
                phrase=phrase,
                scope=str(context.get("scope") or "global"),
                revision=int(context.get("revision") or 0),
                expires_at=time.time() + 30,
                task_id=str(row.get("task_id") or ""),
                batch_id=str(row.get("batch_id") or ""),
            )
            updated = task_service.set_action(
                pending.task_id, action_id=pending.confirmation_id,
                status="waiting_confirmation", stage="waiting_confirmation",
                expected_statuses={"queued"},
            )
            if updated is None:
                pending = None
                return
            if drafts_enabled() and proposal.intent in DRAFT_FIELDS and not updated.get("draft"):
                updated = task_service.save_draft(
                    owner_id, session_id=session_id, intent=proposal.intent,
                    values={}, task_ref=updated["task_ref"], expected_revision=updated["revision"],
                )
                updated = task_service.set_action(
                    pending.task_id, action_id=pending.confirmation_id,
                    status="waiting_confirmation", stage="waiting_confirmation",
                    expected_statuses={"waiting_input"},
                )
                if updated is None:
                    pending = None
                    return
            await emit_task_update(updated)
            arm_confirmation_timeout(pending)
            if updated.get("draft"):
                draft_confirmation_needs_input = True
            await send_event({
                "type": "confirmation_required",
                "confirmation_id": pending.confirmation_id,
                "intent": proposal.intent,
                "arguments": proposal.arguments,
                "phrase": phrase,
                "spoken_prompt": action_confirmation_prompt(proposal),
                "scope": pending.scope,
                "base_revision": pending.revision,
                "expires_at": pending.expires_at,
                "task_id": pending.task_id,
                "batch_id": pending.batch_id,
            })
            return
        action_id = uuid.uuid4().hex
        updated = task_service.set_action(
            str(row.get("task_id") or ""), action_id=action_id,
            status="waiting_device", stage="waiting_device",
            cancellable=str(row.get("risk") or "") == "read",
            expected_statuses={"queued"},
        )
        if updated is None:
            return
        await emit_task_update(updated)
        task_actions[action_id] = (
            str(row.get("task_id") or ""), str(row.get("batch_id") or ""),
        )
        if proposal.intent in {
            "match.ayue_query", "ayue.public_query", "ayue.private_query",
            "personality.explore",
        }:
            background_actions[action_id] = proposal
            background_action_transcripts[action_id] = last_user_transcript
            progress_turn_pending = True
            next_reply_code = "ayue_delegated"
        else:
            awaiting_results[action_id] = PendingToolResult(
                call_id="", name=proposal.intent,
                task_id=str(row.get("task_id") or ""),
                batch_id=str(row.get("batch_id") or ""),
            )
        await send_event({
            "type": "action_proposal",
            "action_id": action_id,
            "intent": proposal.intent,
            "arguments": proposal.arguments,
            "confirmed": True,
            "scope": str(context.get("scope") or "global"),
            "base_revision": proposal.base_revision,
            "task_id": str(row.get("task_id") or ""),
            "batch_id": str(row.get("batch_id") or ""),
        })

    async def schedule_ready_tasks(batch_id: str) -> None:
        if task_service is None or stopped:
            return
        async with task_schedule_lock:
            # A completion in one batch may free a user-wide read or write slot
            # needed by another batch, so always consider the owner's queue.
            for _round in range(MAX_OPERATIONS):
                try:
                    queued_batches = task_service.queued_batch_ids(owner_id)
                    batch_ids = [
                        item for item in [batch_id, *queued_batches]
                        if item
                    ]
                    batch_ids = list(dict.fromkeys(batch_ids))
                    active_reads = task_service.active_count(owner_id, risk="read")
                    active_serialized = task_service.active_count(
                        owner_id, risk="serialized",
                    )
                except RuntimeError:
                    return
                changed = False
                for candidate_batch_id in batch_ids:
                    try:
                        ready = task_service.ready_queued_tasks(
                            owner_id, candidate_batch_id,
                        )
                    except RuntimeError:
                        continue
                    ready.sort(key=lambda row: (
                        str(row.get("risk") or "") != "read",
                        int(row.get("position", 0) or 0),
                    ))
                    for row in ready:
                        is_serialized = str(row.get("risk") or "") != "read"
                        if is_serialized:
                            if pending is None and active_serialized == 0:
                                await dispatch_task_row(row)
                                current = task_service.get_task(
                                    owner_id, str(row.get("task_id") or ""),
                                )
                                if current and current.get("status") != "queued":
                                    changed = True
                                if current and current.get("status") in {
                                    "running", "waiting_device",
                                    "waiting_confirmation", "cancel_requested",
                                }:
                                    active_serialized += 1
                            break
                        if active_reads >= task_service.per_user_concurrency:
                            continue
                        await dispatch_task_row(row)
                        current = task_service.get_task(
                            owner_id, str(row.get("task_id") or ""),
                        )
                        if current and current.get("status") != "queued":
                            changed = True
                        if current and current.get("status") in {
                            "running", "waiting_device",
                            "waiting_confirmation", "cancel_requested",
                        }:
                            active_reads += 1
                if not changed:
                    break

    def drafts_enabled() -> bool:
        return bool(task_service is not None and task_tools_enabled
                    and context.get("feature_status", {}).get("conversation_drafts"))

    async def clear_draft_confirmation(task_ref: str) -> None:
        nonlocal pending
        if pending is not None and task_ref == f"task_{pending.task_id}":
            old = pending
            pending = None
            cancel_confirmation_timeout()
            await send_event({"type": "confirmation_expired", "confirmation_id": old.confirmation_id})

    def current_draft() -> dict[str, Any] | None:
        if not drafts_enabled():
            return None
        ref = f"task_{pending.task_id}" if pending and pending.task_id else active_draft_ref
        row = task_service.get_by_ref(owner_id, ref) if ref else None
        return row if row and row.get("draft") and draft_editable(row) else None

    async def manage_draft(args: dict[str, Any]) -> dict[str, Any]:
        nonlocal active_draft_ref
        if not drafts_enabled():
            return {"status": "failed", "error_code": "draft_unsupported", "message": "目前版本請使用原有操作與確認流程。"}
        action = args.get("action")
        if action not in {"start", "update", "resume", "pause", "cancel", "list"}:
            return {"status": "failed", "error_code": "draft_action_invalid"}
        try:
            rows = [item for item in task_service.list_drafts(owner_id)
                    if item.get("draft", {}).get("editable")
                    and action_permissions_allowed(str(item.get("capability_id")), context)]
            ref = str(args.get("task_ref") or "")
            if action == "list":
                return {"status": "ok", "drafts": rows, "message": "請選擇要繼續的草稿。"}
            row = task_service.get_by_ref(owner_id, ref) if ref else current_draft()
            if pending is not None and current_draft() is None:
                return {"status": "needs_input", "message": "請先完成或取消目前待確認的操作，再處理草稿。"}
            if action != "start" and row is None and not ref:
                if len(rows) == 1:
                    row = task_service.get_by_ref(owner_id, rows[0]["task_ref"])
                else:
                    return {"status": "needs_input", "drafts": rows,
                            "message": "想繼續哪一份草稿？" if rows else "目前沒有可接續的草稿。"}
            if ref and (row is None or not row.get("draft") or not draft_editable(row)):
                raise ValueError("voice_draft_not_editable")
            intent = str((row or {}).get("capability_id") or args.get("intent") or "")
            if intent not in DRAFT_FIELDS:
                raise ValueError("draft_intent_invalid")
            if not action_permissions_allowed(intent, context):
                return {"status": "permission_denied", "message": "這項功能尚未授權，可到語音設定開啟權限；既有草稿仍會保留。"}
            if action == "start" and row is not None:
                return {"status": "needs_input", "message": "已有草稿，修改請用 update；要另建一份請先 pause。"}
            if action == "cancel":
                changed = task_service.cancel(owner_id, row["task_ref"], expected_revision=row["revision"])
                await clear_draft_confirmation(row["task_ref"])
                for item in changed:
                    await send_event({"type": "task_update", "task_protocol_version": 1, "task": item})
                active_draft_ref = ""
                return {"status": "cancelled", "message": "這份草稿已取消，沒有送出。"}
            fields = args.get("fields") or {}
            updated = task_service.save_draft(
                owner_id, session_id=session_id, intent=intent, values=fields,
                task_ref=row["task_ref"] if row else "",
                expected_revision=row["revision"] if row else None,
                paused=action == "pause",
            )
            await clear_draft_confirmation(updated["task_ref"])
            active_draft_ref = "" if action == "pause" else updated["task_ref"]
            await emit_task_update(updated)
            question = draft_question(intent, updated["arguments"])
            if not question and action != "pause":
                updated = task_service.retry(owner_id, updated["task_ref"], expected_revision=updated["revision"])
                await emit_task_update(updated)
                await schedule_ready_tasks(updated["batch_id"])
                updated = task_service.get_task(owner_id, updated["task_id"]) or updated
            if action == "pause":
                prompt = "草稿已保留，查完可以說『繼續剛才』。"
            elif question:
                prompt = question
            elif updated["status"] == "waiting_confirmation":
                prompt = action_confirmation_prompt(VoiceProposal(intent, updated["arguments"], "", int(context.get("revision") or 0)))
            else:
                prompt = str(updated.get("result_summary") or "草稿已保留，等目前的操作完成後會請你確認。")
            await send_event({"type": "draft_prompt", "message": prompt})
            return {"status": updated["status"], "task": task_service.project(updated),
                    "spoken_prompt": prompt, "message": prompt}
        except (ValueError, RuntimeError) as error:
            return {"status": "needs_input", "error_code": str(error)[:80],
                    "message": "草稿沒有變更，請檢查日期、時間或重新選擇尚未執行的任務。"}

    async def handle_draft_utterance(text: str) -> bool:
        nonlocal draft_handled_text
        if not drafts_enabled() or not text:
            return False
        if text == draft_handled_text:
            return True
        normalized = normalized_phrase(text)
        args = None
        if normalized in {"繼續剛才", "繼續剛才的安排", "繼續剛才的任務", "繼續草稿"}:
            args = {"action": "resume"}
        row = current_draft()
        if row and normalized in {"先等一下", "等一下", "先暫停", "暫停這個任務", "先給我看內容還不要傳", "先給我看內容不要傳", "先給我看不要傳", "先看內容不要送", "先給我看內容還不要送出", "不要傳先給我看", "不要送先給我看", "不要傳先給我看內容"}:
            args = {"action": "pause", "task_ref": row["task_ref"]}
        if row and normalized in {"取消", "取消這個任務", "取消草稿", "不要送了"}:
            args = {"action": "cancel", "task_ref": row["task_ref"]}
        if row and args is None:
            patch = spoken_draft_patch(text, row["capability_id"], row["arguments"])
            if patch:
                args = {"action": "update", "task_ref": row["task_ref"], "fields": patch}
        if args is None:
            return False
        result = await manage_draft(args)
        draft_handled_text = text
        await queue_background_result(result, "draft")
        return True

    async def handle_function(call: Any) -> None:
        nonlocal pending, next_reply_code, close_after_turn, progress_turn_pending
        nonlocal private_read_authorized
        nonlocal last_delegated_proposal, last_delegated_expires_at
        nonlocal recently_confirmed_id, recently_confirmed_until
        nonlocal next_step_dispatched_intent
        name = str(call.name or "")
        call_id = str(call.id or "")
        if quota_stopped:
            return
        if call_id in tool_response_cache:
            cached_name, cached_response = tool_response_cache[call_id]
            await tool_response(SimpleNamespace(
                id=call_id,
                name=cached_name,
            ), cached_response)
            return
        calendar_block = calendar_write_preflight(last_user_transcript, context)
        if calendar_block is not None:
            already_sent = template_fast_path_transcript == last_user_transcript
            await tool_response(call, {
                "status": "already_dispatched" if already_sent else calendar_block[0],
                "message": (
                    "這項 Google 日曆限制已回覆，請勿再次要求確認。"
                    if already_sent else calendar_block[1]
                ),
                **({"spoken_prompt": calendar_block[1]} if not already_sent else {}),
            })
            return
        if (
            next_step_dispatched_intent
            and template_fast_path_transcript == last_user_transcript
            and call_id not in template_fast_path_call_ids
        ):
            await tool_response(call, {
                "status": "already_dispatched",
                "message": "使用者選擇的下一步已送出，請等待已驗證的結果。",
            })
            return
        offer = context.get("_next_step_offer")
        if isinstance(offer, dict) and name in {
            "read_calendar", "read_app_data", "ask_matching_ayue", "ask_app_ayue",
            "find_app_capabilities", "run_app_capabilities",
        }:
            choice = select_place_next_step(last_user_transcript, offer)
            if choice in {"calendar", "companion", "clarify", "dismiss"} or (
                last_user_transcript == offer.get("source_transcript")
            ):
                # The offer is a question, never authorization for a model
                # call. The final transcript dispatches the selected read.
                await tool_response(call, {
                    "status": "needs_input",
                    "message": "請先等使用者明確選擇下一步；Server 會依完整語音內容執行。",
                })
                return
        if name == "manage_voice_draft":
            if last_user_transcript and draft_handled_text == last_user_transcript:
                await tool_response(call, {"status": "already_dispatched", "message": "這次改口已處理，請使用最新草稿。"})
            else:
                await tool_response(call, await manage_draft(dict(call.args or {})))
            return
        if drafts_enabled():
            raw = dict(call.args or {})
            draft_intent = ({"chat_send": "chat.request_send", "calendar_create": "calendar.create", "calendar_update": "calendar.update"}.get(raw.get("action"))
                            if name == "write_app_action" else None)
            if draft_intent:
                if last_user_transcript and draft_handled_text == last_user_transcript:
                    await tool_response(call, {"status": "already_dispatched", "message": "這次修改已處理。"})
                    return
                row = current_draft()
                fields = {key: value for key, value in raw.items() if key in {*DRAFT_FIELDS[draft_intent], "target_ref"} and value is not None}
                await tool_response(call, await manage_draft({"action": "update" if row else "start", "intent": draft_intent, "fields": fields}))
                return
            if pending and pending.task_id:
                row = current_draft()
                read_detour = (name == "read_weather" or (name == "read_app_data" and raw.get("domain") in {"calendar", "quota", "delivery", "contacts", "matching", "dates"}))
                if row and read_detour:
                    await manage_draft({"action": "pause", "task_ref": row["task_ref"]})
        if (
            template_fast_path_transcript
            and template_fast_path_transcript == last_user_transcript
            and call_id not in template_fast_path_call_ids
        ):
            direct = authoritative_template_proposal(
                template_fast_path_transcript, context=context,
            )
            expected = (
                template_tool_call_for_proposal(direct)
                if direct is not None else None
            )
            matching_duplicate = (
                direct is not None
                and (
                    direct.intent == "match.query"
                    or (
                        direct.intent == "match.ayue_query"
                        and is_companion_matching_request(template_fast_path_transcript)
                    )
                )
                and name in {
                    "navigate_app", "read_match_status", "read_match_hub", "read_app_data",
                    "ask_matching_ayue", "ask_public_ayue", "ask_app_ayue",
                    "find_app_capabilities", "run_app_capabilities",
                }
            )
            cooldown_reason_duplicate = (
                direct is not None
                and direct.intent == "chat.status.query"
                and is_chat_cooldown_reason_request(
                    template_fast_path_transcript,
                    scope=str(context.get("scope") or ""),
                )
                and name in {
                    "read_chat_status", "read_app_data",
                    "find_app_capabilities", "run_app_capabilities",
                }
            )
            if matching_duplicate or cooldown_reason_duplicate or (
                template_enabled and expected is not None and name == expected[0]
            ):
                await tool_response(call, {
                    "status": "already_dispatched",
                    "message": "這個明確操作已由 Server 送出，不能重複執行。",
                })
                return
        if call_id and call_id in seen_tool_calls:
            return
        if call_id:
            seen_tool_calls.add(call_id)
        proxy_proposal: VoiceProposal | None = None
        experience_intent = {'plan_date': 'date.plan', 'forget_preference': 'memory.disable', 'explain_app': 'app.help'}.get(name)
        if experience_intent:
            proxy_proposal = validate_proposal({'intent': experience_intent, 'arguments': dict(call.args or {})},
                                               base_revision=int(context.get('revision') or 0))
            if proxy_proposal is None:
                await tool_response(call, {'status': 'needs_input', 'message': '請補充有效的日期、時間或偏好內容。'})
                return
            if experience_intent == 'app.help':
                await tool_response(call, app_help(context, proxy_proposal.arguments.get('query', '')))
                return
            if not action_permissions_allowed(experience_intent, context):
                await tool_response(call, {'status':'permission_denied','message':'這項功能所需的資料權限尚未開啟。'})
                return
            name = '__proxy_run_single__'
        if task_tools_enabled and name == "find_app_capabilities":
            args = dict(call.args or {})
            requested_query = str(args.get("query") or "")[:1200]
            search_query = requested_query
            if str(args.get("mode") or "") == "perform" and last_user_transcript:
                model_route = deterministic_proposal(
                    requested_query, context=context,
                )
                transcript_route = deterministic_proposal(
                    last_user_transcript, context=context,
                )
                # Gemini may shorten a request while filling tool arguments
                # (for example, "幫我找新的配對" -> "新的配對") or
                # translate one noun with a typo. If that shortened query has
                # lost its deterministic route, keep the actual user utterance
                # as the authoritative search text for this same turn.
                if (
                    transcript_route is not None
                    and transcript_route.intent in ACTIONS
                    and (
                        model_route is None
                        or (
                            transcript_route.intent in {
                                "app.navigate", "chat.open", "match.ayue_query",
                            }
                            and model_route.intent != transcript_route.intent
                        )
                    )
                ):
                    search_query = last_user_transcript[:1200]
            search_started = time.perf_counter()
            result = find_capabilities(
                search_query,
                str(args.get("mode") or "explain"),
                context=context,
                signer=capability_signer,
                user_id=owner_id,
                session_id=session_id,
            )
            cache_capability_defaults(result)
            ranked_ids = [
                str(action.get("capability_id") or "")
                for match in result.get("matches") or []
                for action in (match.get("actions") or [])[:1]
            ][:8]
            record_voice_metric(
                "capability_search", routing_mode=active_routing_mode,
                capability_id=ranked_ids[0] if ranked_ids else "",
                search_ranking=",".join(ranked_ids),
                latency_ms=(time.perf_counter() - search_started) * 1000,
                result_code=result.get("status"),
            )
            if isinstance(result.get("coach"), dict):
                await send_event({
                    "type": "guide_update",
                    "guide": result["coach"],
                })
            if isinstance(result.get("permission_repair"), dict):
                await send_event({
                    "type": "permission_repair",
                    "repair": result["permission_repair"],
                })
            # Realtime models occasionally stop after successful discovery
            # instead of making the mechanical second tool call. For a single
            # exact, signed, permission-checked read/navigation capability we
            # can safely remove that unreliable hop. Writes and ambiguous or
            # multi-operation requests still require run_app_capabilities.
            auto_capabilities = {
                "quota.query", "chat.status.query",
                "app.digest.query", "app.navigate", "calendar.query",
                "chat.open", "contacts.query", "date.query", "match.ayue_query",
                "match.query", "memory.query", "self.query", "weather.query",
            }
            recommended = result.get("recommended_operations")
            if (
                str(args.get("mode") or "") == "perform"
                and isinstance(recommended, list)
                and len(recommended) == 1
                and isinstance(recommended[0], dict)
            ):
                operation = recommended[0]
                capability_ref = str(operation.get("capability_ref") or "")
                try:
                    action_id = capability_signer.verify(
                        capability_ref,
                        user_id=owner_id, session_id=session_id, context=context,
                    )
                    arguments = operation.get("arguments")
                    proposal = proposal_from_capability(
                        action_id,
                        arguments if isinstance(arguments, dict) else {},
                        revision=int(context.get("revision") or 0),
                    )
                    metadata = action_metadata(action_id)
                    safe_delegated_read = (
                        action_id == "ayue.private_query"
                        or (
                            action_id == "ayue.public_query"
                            and proposal is not None
                            and proposal.arguments.get("domain") == "places"
                        )
                    )
                    can_auto_execute = (
                        (action_id in auto_capabilities or safe_delegated_read)
                        and proposal is not None
                        and context_allows_proposal(context, proposal)
                        and metadata["risk"] in {"read", "control"}
                        and (
                            metadata["confirmation_required"] is False
                            or (
                                task_service is not None
                                and str(metadata.get("execution_kind") or "")
                                .startswith("background")
                            )
                        )
                    )
                except (CapabilityRefError, ValueError):
                    can_auto_execute = False
                    action_id = ""
                    proposal = None
                    metadata = {}
                if can_auto_execute and proposal is not None:
                    record_voice_metric(
                        "capability_auto_run", routing_mode="proxy",
                        capability_id=action_id, result_code="started",
                    )
                    if action_id == "weather.query":
                        if weather_service is None:
                            await tool_response(call, {
                                "status": "failed",
                                "error_code": "weather_service_disabled",
                                "message": "目前天氣與空氣品質服務尚未啟用。",
                            })
                            return
                        try:
                            location = await resolve_weather_location(
                                proposal.arguments.get("location"),
                                weather_user_id,
                                weather_location_provider,
                            )
                            weather_result = await asyncio.to_thread(
                                weather_service.query, location,
                            )
                        except Exception:
                            weather_result = {
                                "status": "failed",
                                "error_code": "weather_service_unavailable",
                                "message": "目前暫時查不到天氣與空氣品質。",
                            }
                        await tool_response(call, weather_result)
                        return
                    if str(metadata.get("execution_kind") or "").startswith(
                        "background"
                    ):
                        if task_service is not None:
                            try:
                                created = task_service.create_batch(
                                    user_id=owner_id,
                                    session_id=session_id,
                                    operations=[{
                                        "operation_key": str(
                                            operation.get("operation_key") or "op1"
                                        )[:40],
                                        "depends_on": [],
                                        "arguments": dict(proposal.arguments),
                                        "action": metadata,
                                    }],
                                )
                            except (ValueError, RuntimeError) as error:
                                await tool_response(call, {
                                    "status": "failed",
                                    "error_code": str(error)[:80],
                                    "message": "任務無法建立，這次沒有執行任何操作。",
                                })
                                return
                            for row in created.tasks:
                                await emit_task_update(row)
                            batch_id = str(created.batch.get("batch_id") or "")
                            await schedule_ready_tasks(batch_id)
                            current_rows = task_service.tasks_for_batch(
                                owner_id, batch_id,
                            )
                            waiting_confirmation = (
                                pending is not None and pending.batch_id == batch_id
                            )
                            await tool_response(call, {
                                "status": (
                                    "awaiting_confirmation"
                                    if waiting_confirmation else "queued"
                                ),
                                "batch_ref": created.batch.get("batch_ref"),
                                "tasks": [
                                    task_service.project(row) for row in current_rows
                                ],
                                "spoken_prompt": (
                                    localized_prompt("private_confirm")
                                    if waiting_confirmation
                                    and pending is not None
                                    and pending.proposal.intent == "ayue.private_query"
                                    else action_confirmation_prompt(pending.proposal)
                                    if waiting_confirmation
                                    else localized_prompt("working")
                                ),
                                "message": (
                                    "只逐字說出 spoken_prompt 一次；"
                                    "只表示已等待確認或建立任務，尚未完成。"
                                ),
                            })
                            return
                    else:
                        proxy_proposal = proposal
                        name = "__proxy_run_single__"
            if proxy_proposal is None:
                await tool_response(call, result)
                return
        if task_tools_enabled and name == "read_tasks":
            routed = deterministic_proposal(last_user_transcript, context=context)
            if routed is not None and routed.intent in {
                "app.digest.query", "calendar.query", "contacts.query",
                "date.query", "match.query", "memory.query", "self.query",
                "weather.query",
            }:
                if not context_allows_proposal(context, routed):
                    await tool_response(call, {
                        "status": "permission_denied",
                        "message": "這項資料沒有被使用者授權。",
                    })
                    return
                record_voice_metric(
                    "task_tool_rerouted", routing_mode="proxy",
                    capability_id=routed.intent, result_code="auto_run",
                )
                if routed.intent == "weather.query":
                    if weather_service is None:
                        await tool_response(call, {
                            "status": "failed",
                            "error_code": "weather_service_disabled",
                            "message": "目前天氣與空氣品質服務尚未啟用。",
                        })
                        return
                    try:
                        location = await resolve_weather_location(
                            routed.arguments.get("location"), weather_user_id,
                            weather_location_provider,
                        )
                        weather_result = await asyncio.to_thread(
                            weather_service.query, location,
                        )
                    except Exception:
                        weather_result = {
                            "status": "failed",
                            "error_code": "weather_service_unavailable",
                            "message": "目前暫時查不到天氣與空氣品質。",
                        }
                    await tool_response(call, weather_result)
                    return
                proxy_proposal = routed
                name = "__proxy_run_single__"
            if name == "read_tasks":
                if task_service is None:
                    await tool_response(call, {
                        "status": "ok", "tasks": [],
                        "message": "目前沒有開啟可恢復任務。",
                    })
                    return
                try:
                    rows = task_service.list_tasks(
                        owner_id,
                        str((call.args or {}).get("filter") or "active"),
                        limit=20,
                        include_edit_values=False,
                    )
                except RuntimeError:
                    await tool_response(call, {
                        "status": "failed", "error_code": "voice_task_storage_unavailable",
                        "message": "目前暫時無法讀取任務狀態。",
                    })
                    return
                await tool_response(call, {
                    "status": "ok",
                    "tasks": rows,
                    "message": (
                        "這裡只是 App Voice 背景工作進度，"
                        "不是配對、行事曆、天氣或其他 App 資料。"
                    ),
                })
                return
        if task_tools_enabled and name == "cancel_task":
            if task_service is None:
                await tool_response(call, {
                    "status": "failed", "error_code": "voice_tasks_disabled",
                    "message": "目前沒有開啟可取消任務。",
                })
                return
            try:
                changed = task_service.cancel(
                    owner_id, str((call.args or {}).get("task_ref") or ""),
                )
            except (ValueError, RuntimeError) as error:
                await tool_response(call, {
                    "status": "failed", "error_code": str(error)[:80],
                    "message": "找不到可取消的任務，或任務狀態已改變。",
                })
                return
            for item in changed:
                await send_event({
                    "type": "task_update", "task_protocol_version": 1, "task": item,
                })
            if pending is not None and any(
                item.get("task_ref") == f"task_{pending.task_id}"
                and item.get("status") in {"cancelled", "cancel_requested"}
                for item in changed
            ):
                pending = None
                cancel_confirmation_timeout()
            batch_ids = {
                str(row.get("batch_id") or "")
                for item in changed
                if (row := task_service.get_by_ref(owner_id, str(item.get("task_ref") or "")))
            }
            for batch_id in batch_ids:
                await schedule_ready_tasks(batch_id)
            cancellation_applied = any(
                item.get("status") in {"cancelled", "cancel_requested"}
                for item in changed
            )
            await tool_response(call, {
                "status": "cancelled" if cancellation_applied else "not_cancellable",
                "tasks": changed,
                "message": (
                    "已處理取消要求；已完成的寫入不會自動復原。"
                    if cancellation_applied
                    else "這項寫入已送出或已完成，不能取消或自動復原。"
                ),
            })
            return
        if task_tools_enabled and name == "resolve_pending_interaction":
            resolution = str((call.args or {}).get("action") or "")
            if resolution not in {"confirm", "cancel", "retry", "dismiss", "undo"}:
                await tool_response(call, {
                    "status": "failed", "error_code": "interaction_action_invalid",
                    "message": "這個待處理互動不支援指定操作。",
                })
                return
            if resolution in {"retry", "dismiss", "undo"}:
                if task_service is None:
                    await tool_response(call, {
                        "status": "failed", "error_code": "voice_tasks_disabled",
                        "message": "目前沒有開啟可互動任務。",
                    })
                    return
                task_ref = str((call.args or {}).get("task_ref") or "")
                if not task_ref:
                    await tool_response(call, {
                        "status": "needs_input", "error_code": "task_ref_required",
                        "message": "請先讀取任務，再使用回傳的 task_ref。",
                    })
                    return
                try:
                    if resolution == "retry":
                        updated = task_service.retry(owner_id, task_ref)
                        await emit_task_update(updated)
                        await schedule_ready_tasks(str(updated.get("batch_id") or ""))
                        payload = task_service.project(updated)
                    elif resolution == "dismiss":
                        updated = task_service.dismiss(owner_id, task_ref)
                        await emit_task_update(updated)
                        await schedule_ready_tasks(str(updated.get("batch_id") or ""))
                        payload = task_service.project(updated)
                    else:
                        created = task_service.create_undo_batch(
                            owner_id, task_ref, session_id=session_id,
                        )
                        original = task_service.get_by_ref(owner_id, task_ref)
                        await emit_task_update(original)
                        for row in created.tasks:
                            await emit_task_update(row)
                        await schedule_ready_tasks(
                            str(created.batch.get("batch_id") or ""),
                        )
                        payload = {
                            "batch_ref": created.batch.get("batch_ref"),
                            "tasks": [task_service.project(row) for row in created.tasks],
                        }
                except (ValueError, RuntimeError) as error:
                    await tool_response(call, {
                        "status": "failed", "error_code": str(error)[:80],
                        "message": "任務狀態已改變，這次沒有重複執行。",
                    })
                    return
                await tool_response(call, {
                    "status": "queued" if resolution != "dismiss" else "cancelled",
                    "task": payload,
                    "message": (
                        "任務已重新排入，這不代表已完成。"
                        if resolution == "retry"
                        else "已建立復原任務，這不代表已完成。"
                        if resolution == "undo"
                        else "這項任務已結束。"
                    ),
                })
                return
            if resolution == "cancel":
                cancelled = pending
                pending = None
                cancel_confirmation_timeout()
                next_reply_code = "cancelled"
                if cancelled and cancelled.task_id and task_service is not None:
                    updated = task_service.update(
                        cancelled.task_id, status="cancelled", stage="cancelled",
                        result_summary="使用者已取消待確認操作。",
                        expected_statuses={"waiting_confirmation"},
                        expected_action_id=cancelled.confirmation_id,
                    )
                    await emit_task_update(updated)
                    await schedule_ready_tasks(cancelled.batch_id)
                visible_status = dict(context.get("feature_status") or {})
                if cancelled is None and visible_status.get("visible_choice_pending") is True:
                    proposal = VoiceProposal(
                        "ui.choice.activate", {"action": "cancel"}, "",
                        int(context.get("revision") or 0),
                    )
                    action_id = call_id or uuid.uuid4().hex
                    awaiting_results[action_id] = PendingToolResult(
                        call_id=call_id or action_id, name=str(call.name or name),
                    )
                    await send_event({
                        "type": "action_proposal", "action_id": action_id,
                        "intent": proposal.intent, "arguments": proposal.arguments,
                        "confirmed": True, "scope": str(context.get("scope") or "global"),
                        "base_revision": proposal.base_revision,
                    })
                    return
                await tool_response(call, {
                    "status": "cancelled" if cancelled else "no_pending_interaction",
                    "message": "待確認操作已取消。" if cancelled else "目前沒有待取消操作。",
                })
                return
            visible_status = dict(context.get("feature_status") or {})
            if pending is None and visible_status.get("visible_choice_pending") is True:
                proposal = VoiceProposal(
                    "ui.choice.activate", {"action": "confirm"}, "",
                    int(context.get("revision") or 0),
                )
                action_id = call_id or uuid.uuid4().hex
                awaiting_results[action_id] = PendingToolResult(
                    call_id=call_id or action_id, name=str(call.name or name),
                )
                await send_event({
                    "type": "action_proposal", "action_id": action_id,
                    "intent": proposal.intent, "arguments": proposal.arguments,
                    "confirmed": True, "scope": str(context.get("scope") or "global"),
                    "base_revision": proposal.base_revision,
                })
                return
            # Reuse the existing, server-owned confirmation boundary below.
            name = "confirm_pending_action"
        if task_tools_enabled and name == "run_app_capabilities":
            raw_operations = (call.args or {}).get("operations")
            if not isinstance(raw_operations, list) or not 1 <= len(raw_operations) <= MAX_OPERATIONS:
                await tool_response(call, {
                    "status": "failed", "error_code": "operation_count_invalid",
                    "message": "一次只能處理 1 到 8 項操作。",
                })
                return
            verified_operations: list[dict[str, Any]] = []
            operations: list[dict[str, Any]] = []
            proposals: list[VoiceProposal] = []
            keys: set[str] = set()
            try:
                for index, raw_operation in enumerate(raw_operations):
                    if not isinstance(raw_operation, dict):
                        raise ValueError("operation_invalid")
                    key = str(raw_operation.get("operation_key") or f"op{index + 1}")[:40]
                    if not key or key in keys:
                        raise ValueError("operation_key_invalid")
                    keys.add(key)
                    capability_ref = str(raw_operation.get("capability_ref") or "")
                    action_id = capability_signer.verify(
                        capability_ref,
                        user_id=owner_id, session_id=session_id, context=context,
                    )
                    raw_arguments = raw_operation.get("arguments")
                    if not isinstance(raw_arguments, dict):
                        raw_arguments = {}
                    trusted_defaults = capability_argument_defaults.get(
                        capability_ref, {},
                    )
                    canonical_arguments = {
                        **trusted_defaults,
                        **raw_arguments,
                    }
                    if action_id == 'calendar.cancel':
                        focus = cancellation_focus(last_user_transcript)
                        if focus.get('question'):
                            raise ValueError('ambiguous_target')
                        if focus.get('date'):
                            selected = next((item for item in (context.get('screen') or {}).get('items', [])
                                if item.get('ref') == canonical_arguments.get('target_ref')), {})
                            if selected and (selected.get('attributes') or {}).get('date') != focus['date']:
                                raise ValueError('ambiguous_target')
                            canonical_arguments['date'] = focus['date']
                    proposal = proposal_from_capability(
                        action_id, canonical_arguments,
                        revision=int(context.get("revision") or 0),
                    )
                    if proposal is None and trusted_defaults:
                        proposal = proposal_from_capability(
                            action_id,
                            trusted_defaults,
                            revision=int(context.get("revision") or 0),
                        )
                    if proposal is None:
                        raise ValueError("operation_arguments_invalid")
                    if await reject_google_calendar_write(call, proposal):
                        return
                    verified_operations.append({
                        "operation_key": key,
                        "depends_on": [str(item)[:40] for item in raw_operation.get("depends_on") or []],
                        "arguments": dict(proposal.arguments),
                        "action_id": action_id,
                    })
                expanded = expand_catalog_operations(verified_operations)
                for expanded_operation in expanded:
                    action_id = str(expanded_operation.get("action_id") or "")
                    if not action_permissions_allowed(action_id, context):
                        raise ValueError("operation_permission_denied")
                    proposal = proposal_from_capability(
                        action_id, expanded_operation.get("arguments") or {},
                        revision=int(context.get("revision") or 0),
                    )
                    if proposal is None or not context_allows_proposal(context, proposal):
                        raise ValueError("operation_arguments_invalid")
                    if await reject_google_calendar_write(call, proposal):
                        return
                    proposals.append(proposal)
                    operations.append({
                        "operation_key": str(expanded_operation["operation_key"])[:40],
                        "depends_on": [
                            str(item)[:40]
                            for item in expanded_operation.get("depends_on") or []
                        ],
                        "arguments": dict(proposal.arguments),
                        "action": action_metadata(action_id),
                    })
            except CapabilityRefError as error:
                await tool_response(call, {
                    "status": "failed", "error_code": error.code,
                    "message": "功能授權已過期或畫面已改變，請重新搜尋功能。",
                })
                return
            except ValueError as error:
                await tool_response(call, {
                    "status": "failed", "error_code": str(error)[:80],
                    "message": "操作內容無法通過功能驗證。",
                })
                return
            needs_batch = len(operations) > 1 or any(
                item["action"]["execution_kind"].startswith("background")
                for item in operations
            )
            record_voice_metric(
                "capability_run", routing_mode=active_routing_mode,
                capability_id=",".join(
                    str(item.get("action_id") or "")
                    for item in verified_operations
                ),
                result_code="batch" if needs_batch else "inline",
            )
            if needs_batch:
                if task_service is None:
                    await tool_response(call, {
                        "status": "failed", "error_code": "voice_tasks_disabled",
                        "message": "多項或背景任務尚未對這個帳號開啟。",
                    })
                    return
                try:
                    created = task_service.create_batch(
                        user_id=owner_id, session_id=session_id, operations=operations,
                    )
                except (ValueError, RuntimeError) as error:
                    await tool_response(call, {
                        "status": "failed", "error_code": str(error)[:80],
                        "message": "任務無法建立，這次沒有執行任何操作。",
                    })
                    return
                for row in created.tasks:
                    await emit_task_update(row)
                created_batch_id = str(created.batch.get("batch_id") or "")
                await schedule_ready_tasks(created_batch_id)
                current_rows = task_service.tasks_for_batch(owner_id, created_batch_id)
                waiting_confirmation = (
                    pending is not None and pending.batch_id == created_batch_id
                )
                await tool_response(call, {
                    "status": "awaiting_confirmation" if waiting_confirmation else "queued",
                    "batch_ref": created.batch.get("batch_ref"),
                    "tasks": [task_service.project(row) for row in current_rows],
                    "spoken_prompt": (
                        localized_prompt("private_confirm")
                        if waiting_confirmation and pending and pending.proposal.intent == "ayue.private_query"
                        else action_confirmation_prompt(pending.proposal)
                        if waiting_confirmation
                        else localized_prompt("working")
                    ),
                    "message": (
                        "只逐字說出 spoken_prompt 一次；"
                        "只表示任務已建立，尚未完成。"
                    ),
                })
                return
            proxy_proposal = proposals[0]
            if proxy_proposal.intent == "weather.query":
                if weather_service is None:
                    await tool_response(call, {
                        "status": "failed", "error_code": "weather_service_disabled",
                        "message": "目前天氣與空氣品質服務尚未啟用。",
                    })
                    return
                try:
                    location = await resolve_weather_location(
                        proxy_proposal.arguments.get("location"), weather_user_id,
                        weather_location_provider,
                    )
                    result = await asyncio.to_thread(weather_service.query, location)
                except Exception:
                    result = {
                        "status": "failed", "error_code": "weather_service_unavailable",
                        "message": "目前暫時查不到天氣與空氣品質。",
                    }
                await tool_response(call, result)
                return
            name = "__proxy_run_single__"
        if proxy_enabled and call_id not in template_fast_path_call_ids and name not in {
            "__proxy_run_single__", "describe_current_screen",
            "confirm_pending_action", "close_voice_mode",
        }:
            await tool_response(call, {
                "status": "rejected", "error_code": "proxy_tool_not_allowed",
                "message": "這個工具不在 protocol v4 核心清單。",
            })
            return
        if name == "read_weather":
            if pending is not None:
                await tool_response(call, {
                    "status": "awaiting_confirmation",
                    "phrase": pending.phrase,
                    "spoken_prompt": action_confirmation_prompt(pending.proposal),
                    "message": "請先完成或取消目前待確認的操作。",
                })
                return
            if weather_service is None:
                await tool_response(call, {
                    "status": "failed",
                    "error_code": "weather_service_disabled",
                    "message": "目前天氣與空氣品質服務尚未啟用。",
                })
                return
            try:
                location = await resolve_weather_location(
                    (call.args or {}).get("location"),
                    weather_user_id,
                    weather_location_provider,
                )
                result = await asyncio.to_thread(
                    weather_service.query,
                    location,
                )
            except Exception:
                result = {
                    "status": "failed",
                    "error_code": "weather_service_unavailable",
                    "message": "目前暫時查不到天氣與空氣品質，請稍後再試。",
                }
            next_reply_code = (
                "weather"
                if result.get("status") == "ok"
                else "weather_partial"
                if result.get("status") == "partial"
                else "weather_unavailable"
            )
            if call_id in template_fast_path_call_ids:
                await queue_background_result(result, "direct")
            else:
                await tool_response(call, result)
            return
        visible_action = visible_choice_action(last_user_transcript)
        visible_status = dict(context.get("feature_status") or {})
        if (
            not proxy_enabled
            and
            pending is None
            and visible_status.get("visible_choice_pending") is True
            and visible_action is not None
        ):
            proposal = VoiceProposal(
                "ui.choice.activate",
                {"action": visible_action},
                "",
                int(context.get("revision") or 0),
            )
            action_id = call_id or uuid.uuid4().hex
            awaiting_results[action_id] = PendingToolResult(
                call_id=call_id or action_id,
                name=name or "activate_visible_choice",
            )
            await send_event({
                "type": "action_proposal",
                "action_id": action_id,
                "intent": proposal.intent,
                "arguments": proposal.arguments,
                "confirmed": True,
                "scope": str(context.get("scope") or "global"),
                "base_revision": proposal.base_revision,
            })
            return
        if name == "get_voice_capabilities":
            permissions = dict(context.get("permissions") or {})
            can_read_status = permissions.get("status_read") is True
            await tool_response(call, {
                "status": "ok",
                "permissions": permissions,
                "catalog_version": CATALOG["version"],
                "available_actions": available_actions(ACTIONS, permissions),
                "app_guide": app_help(context),
                "feature_status": (
                    dict(context.get("feature_status") or {})
                    if can_read_status
                    else {}
                ),
                "voice_config": dict(context.get("voice_config") or {}),
                "message": (
                    "可以回答自己的權限與已授權狀態。"
                    if can_read_status
                    else "使用者沒有開啟讀取功能狀態權限。"
                ),
            })
            return
        if name == "describe_current_screen":
            permissions = dict(context.get("permissions") or {})
            if permissions.get("screen_read") is not True:
                await tool_response(call, {
                    "status": "permission_denied",
                    "message": "使用者沒有開啟讀取目前畫面的權限。",
                })
                return
            direct_screen_action = deterministic_proposal(
                last_user_transcript, context=context,
            )
            if (
                proxy_enabled
                and direct_screen_action is not None
                and direct_screen_action.intent == "ayue.private_query"
                and context_allows_proposal(context, direct_screen_action)
            ):
                # A common Live-model path is to inspect the screen before a
                # request such as "讀取裡面的內容". The safe projection has
                # already resolved the contact, so continue into the normal
                # private-read confirmation boundary instead of leaving the
                # model to invent an untracked verbal confirmation.
                proxy_proposal = direct_screen_action
                name = "__proxy_run_single__"
            else:
                await tool_response(call, {
                    "status": "ok",
                    "scope": str(context.get("scope") or "global"),
                    "revision": context.get("revision", 0),
                    "screen": context.get("screen", {}),
                    "media_count": int(context.get("media_count") or 0),
                    "can_publish": context.get("can_publish") is True,
                    "next_step": (
                        "若原需求是讀取或摘要聊天內容，立即呼叫 "
                        "find_app_capabilities(mode=perform)，保留原需求與對象；"
                        "不可自行詢問確認。"
                    ),
                    "message": "只描述安全畫面狀態，不推測畫面文字或私人內容。",
                })
                return
        if name in {
            "__proxy_run_single__",
            "select_screen_target",
            "read_shared_dates", "respond_date_invitation", "update_shared_date", "confirm_shared_date",
            "navigate_app",
            "open_app_page",
            "patch_profile",
            "request_profile_save",
            "set_app_setting",
            "write_post_caption",
            "select_recent_post_photos",
            "open_post",
            "request_post_publish",
            "read_calendar",
            "create_calendar_event",
            "update_calendar_event",
            "cancel_calendar_event",
            "personality_exploration_turn",
            "read_match_status",
            "read_match_hub",
            "ask_matching_ayue",
            "ask_public_ayue",
            "ask_private_ayue",
            "read_app_data",
            "ask_app_ayue",
            "write_app_action",
            "activate_visible_choice",
            "list_contacts",
            "read_self_profile",
            "read_agent_quota", "read_chat_status",
            "read_memories",
            "add_memory",
            "open_chat",
            "send_chat_message",
        }:
            if pending is not None and drafts_enabled() and proxy_proposal is not None and proxy_proposal.intent in {
                "quota.query", "chat.status.query", "calendar.query", "contacts.query", "weather.query", "match.query", "date.query",
            }:
                row = current_draft()
                if row:
                    await manage_draft({"action": "pause", "task_ref": row["task_ref"]})
            if pending is not None and not (
                (name == 'read_app_data' and (call.args or {}).get('domain') in {'quota', 'delivery'})
                or name == 'read_chat_status'
                or (proxy_proposal is not None and proxy_proposal.intent in {'quota.query', 'chat.status.query'})
            ):
                await tool_response(call, {
                    "status": "awaiting_confirmation",
                    "phrase": pending.phrase,
                    "spoken_prompt": action_confirmation_prompt(pending.proposal),
                    "message": (
                        "不得重新建立原操作。若使用者剛說確認，"
                        "只呼叫 confirm_pending_action。"
                    ),
                })
                return
            proposal = (
                proxy_proposal
                if name == "__proxy_run_single__"
                else (
                    template_proposal_from_function(
                        call,
                        revision=int(context.get("revision") or 0),
                    )
                    if template_enabled
                    else _proposal_from_function(
                        call,
                        revision=int(context.get("revision") or 0),
                    )
                )
            )
            if last_user_transcript and name in {
                "navigate_app", "read_match_status", "read_match_hub", "read_app_data",
                "ask_matching_ayue", "ask_public_ayue", "ask_app_ayue",
            }:
                direct_match = deterministic_proposal(
                    last_user_transcript, context=context,
                )
                if (
                    direct_match is not None
                    and direct_match.intent == "match.query"
                ):
                    # A status question reads canonical App state; the device
                    # opens Match Hub while presenting that verified result.
                    proposal = direct_match
                elif (
                    direct_match is not None
                    and direct_match.intent == "match.ayue_query"
                    and is_companion_matching_request(last_user_transcript)
                    and name in {"ask_matching_ayue", "ask_public_ayue", "ask_app_ayue"}
                ):
                    # Keep the destination and companion request in one turn
                    # when Live incorrectly picked the places domain.
                    proposal = direct_match
            if template_enabled and last_user_transcript and name in {
                "navigate_app", "open_chat", "read_app_data",
                "ask_app_ayue", "write_app_action", "cancel_calendar_event",
            }:
                # The direct template should preserve the user's completed
                # utterance for dates, matching, navigation, and chat names.
                # Realtime models sometimes compress those arguments while
                # filling a domain tool. A deterministic proposal is the
                # authoritative correction when the request is unambiguous.
                direct = authoritative_template_proposal(
                    last_user_transcript, context=context,
                )
                if direct is not None and direct.intent in ACTIONS:
                    if proposal is None or proposal.intent != direct.intent or direct.intent in {
                        "app.navigate", "chat.open", "calendar.query",
                        "calendar.cancel", "match.ayue_query", "ayue.public_query",
                    }:
                        proposal = direct
            # Correct routing for a current, explicit memory question even if
            # Live mistakenly delegates it to the public persona.
            direct = deterministic_proposal(last_user_transcript, context=context)
            if direct is not None and direct.intent == "memory.query" and name in {
                "ask_public_ayue", "ask_matching_ayue", "read_self_profile", "read_memories",
            }:
                proposal = direct
            if (
                call_id in template_fast_path_call_ids
                and next_step_selected_proposal is not None
            ):
                proposal = next_step_selected_proposal
            if _identity_question(last_user_transcript):
                await tool_response(call, {
                    "status": "conversation_only",
                    "message": "這是一般問題，請直接回答你是阿月，不要操作 App。",
                })
                return
            if proposal is None:
                await tool_response(call, {
                    "status": "rejected",
                    "message": "這個操作不在 App 安全白名單內。",
                })
                return
            if await reject_google_calendar_write(call, proposal):
                return
            if drafts_enabled() and proposal.intent in DRAFT_FIELDS:
                row = current_draft()
                await tool_response(call, await manage_draft({
                    "action": "update" if row else "start", "intent": proposal.intent,
                    "fields": proposal.arguments,
                }))
                return
            if context.get('permissions', {}).get('public_ayue') is True:
                screen_places = ((context.get('screen') or {}).get('content') or {}).get('recommendations') or []
                proposal, place_question = resolve_calendar_recommendation(
                    proposal, screen_places or recent_public_places,
                )
                if place_question:
                    await tool_response(call, {'status': 'needs_input', 'message': place_question})
                    return
            bound, target_error = bind_target(proposal.intent, proposal.arguments, context)
            if proposal.intent == 'calendar.cancel':
                focus = cancellation_focus(last_user_transcript)
                if focus.get('question'):
                    await tool_response(call, {'status':'needs_input','message':focus['question']})
                    return
                if focus.get('date'):
                    selected = next((item for item in (context.get('screen') or {}).get('items', []) if item.get('ref') == bound.get('target_ref')), {})
                    if selected and (selected.get('attributes') or {}).get('date') != focus['date']:
                        await tool_response(call, {'status':'needs_input','message':'目前選到的行程日期不同，請選擇真正要取消的那一筆；其他行程會保留。'})
                        return
                    bound['date'] = focus['date']
            if target_error:
                await tool_response(call, {"status": "needs_input", "error_code": target_error,
                                           "message": "請重新讀取目前畫面，選擇已授權且仍有效的項目。"})
                return
            proposal = VoiceProposal(proposal.intent, bound, proposal.reply, proposal.base_revision)
            if not context_allows_proposal(context, proposal):
                await tool_response(call, {
                    "status": "permission_denied",
                    "message": "這項功能沒有被使用者授權，請不要執行或宣稱完成。",
                })
                return
            if proposal.intent == 'quota.query':
                from .status_contract import status_result
                quota = await read_quota()
                if quota_reader is not None and (quota.get('exhausted') or quota.get('state') == 'unknown'):
                    await stop_for_quota(quota)
                    return
                await tool_response(call, status_result(quota=quota))
                return
            if proposal.intent in {'date.plan','memory.disable'} and not context.get('feature_status', {}).get('voice_experience'):
                await tool_response(call, {'status':'failed','message':'目前 App 版本尚未支援這項語音功能，請更新 App。'})
                return
            if proposal.intent == 'app.help':
                await tool_response(call, app_help(context, proposal.arguments.get('query', '')))
                return
            if proposal.intent == 'chat.status.query' and not context.get('feature_status', {}).get('operational_status'):
                await tool_response(call, {'status': 'failed', 'message': '目前 App 版本尚未支援語音狀態查詢，請在聊天室查看冷卻提示。'})
                return
            if proposal.intent == "calendar.query":
                start_date = proposal.arguments.get("start_date")
                end_date = proposal.arguments.get("end_date")
                context["_calendar_reference"] = (
                    f"{start_date} 至 {end_date or start_date}"
                    if start_date else last_user_transcript[:300]
                )
            if proposal.intent == "post.request_publish" and not context.get("can_publish"):
                await tool_response(call, {
                    "status": "not_ready",
                    "message": "還沒有完成圖片選擇，請先選擇一到五張圖片。",
                })
                return
            if proposal.intent == "ayue.private_query" and not private_read_authorized:
                phrase = confirmation_phrase(proposal)
                pending = DuplexConfirmation(
                    confirmation_id=uuid.uuid4().hex,
                    proposal=proposal,
                    phrase=phrase,
                    scope=str(context.get("scope") or "global"),
                    revision=int(context.get("revision") or 0),
                    expires_at=time.time() + 30,
                )
                next_reply_code = "private_read_confirmation"
                await send_event({
                    "type": "confirmation_required",
                    "confirmation_id": pending.confirmation_id,
                    "intent": proposal.intent,
                    "arguments": proposal.arguments,
                    "phrase": phrase,
                    "spoken_prompt": localized_prompt("private_confirm"),
                    "scope": pending.scope,
                    "base_revision": pending.revision,
                    "expires_at": pending.expires_at,
                })
                await tool_response(call, {
                    "status": "confirmation_required",
                    "phrase": phrase,
                    "spoken_prompt": localized_prompt("private_confirm"),
                    "message": "同一個 session 確認一次即可。",
                })
                return
            if proposal.intent in {
                "match.ayue_query", "ayue.public_query", "ayue.private_query",
                "personality.explore",
            } and not requires_confirmation(proposal):
                action_id = str(call.id or uuid.uuid4().hex)
                background_actions[action_id] = proposal
                background_action_transcripts[action_id] = last_user_transcript
                progress_turn_pending = True
                next_reply_code = "ayue_delegated"
                await send_event({
                    "type": "action_proposal",
                    "action_id": action_id,
                    "intent": proposal.intent,
                    "arguments": proposal.arguments,
                    "confirmed": True,
                    "scope": str(context.get("scope") or "global"),
                    "base_revision": proposal.base_revision,
                })
                await tool_response(call, {
                    "status": "working",
                    "spoken_prompt": localized_prompt("working"),
                    "message": "只逐字說出 spoken_prompt，不可自行編造結果。",
                })
                return
            if requires_confirmation(proposal):
                phrase = confirmation_phrase(proposal)
                pending = DuplexConfirmation(
                    confirmation_id=uuid.uuid4().hex,
                    proposal=proposal,
                    phrase=phrase,
                    scope=str(context.get("scope") or "global"),
                    revision=int(context.get("revision") or 0),
                    expires_at=time.time() + 30,
                )
                next_reply_code = "confirmation_required"
                await send_event({
                    "type": "confirmation_required",
                    "confirmation_id": pending.confirmation_id,
                    "intent": proposal.intent,
                    "arguments": proposal.arguments,
                    "phrase": phrase,
                    "spoken_prompt": action_confirmation_prompt(proposal),
                    "scope": pending.scope,
                    "base_revision": pending.revision,
                    "expires_at": pending.expires_at,
                })
                await tool_response(call, {
                    "status": "confirmation_required",
                    "phrase": phrase,
                    "spoken_prompt": action_confirmation_prompt(proposal),
                    "message": "只能逐字朗讀 spoken_prompt 一次。",
                })
                return
            action_id = str(call.id or uuid.uuid4().hex)
            awaiting_results[action_id] = PendingToolResult(
                call_id=(
                    ""
                    if call_id in template_fast_path_call_ids
                    else str(call.id or action_id)
                ),
                name=str(call.name or name),
            )
            await send_event({
                "type": "action_proposal",
                "action_id": action_id,
                "intent": proposal.intent,
                "arguments": proposal.arguments,
                "confirmed": True,
                "scope": str(context.get("scope") or "global"),
                "base_revision": proposal.base_revision,
            })
            return

        if name == "confirm_pending_action":
            spoken = str(
                last_user_transcript
                if drafts_enabled() or (proxy_enabled and last_user_transcript)
                else (call.args or {}).get("spoken_phrase") or ""
            )[:200]
            if current_draft() and draft_confirmation_needs_input:
                await tool_response(call, {"status": "confirmation_mismatch", "message": "草稿已更新，請等待使用者確認最新內容。"})
                return
            if pending is None:
                if recently_confirmed_until >= time.time():
                    await tool_response(call, {
                        "status": "already_confirmed",
                        "confirmation_id": recently_confirmed_id,
                        "message": "剛才的確認已收到，操作只會執行一次。",
                    })
                    return
                if (
                    last_delegated_proposal is not None
                    and last_delegated_expires_at >= time.time()
                ):
                    previous = last_delegated_proposal
                    arguments = dict(previous.arguments)
                    if previous.intent == "personality.explore":
                        arguments["message"] = spoken or "確認"
                    else:
                        arguments["question"] = spoken or "確認"
                    follow_up = VoiceProposal(
                        previous.intent,
                        arguments,
                        "",
                        int(context.get("revision") or 0),
                    )
                    if not context_allows_proposal(context, follow_up):
                        await tool_response(call, {
                            "status": "permission_denied",
                            "message": "這項寫入權限已關閉，沒有執行確認。",
                        })
                        return
                    action_id = str(call.id or uuid.uuid4().hex)
                    background_actions[action_id] = follow_up
                    background_action_transcripts[action_id] = last_user_transcript
                    progress_turn_pending = True
                    next_reply_code = "ayue_delegated"
                    await send_event({
                        "type": "action_proposal",
                        "action_id": action_id,
                        "intent": follow_up.intent,
                        "arguments": follow_up.arguments,
                        "confirmed": True,
                        "scope": str(context.get("scope") or "global"),
                        "base_revision": follow_up.base_revision,
                    })
                    await tool_response(call, {
                        "status": "working",
                        "spoken_prompt": localized_prompt("working"),
                        "message": "已將口頭確認送回原本對話處理。",
                    })
                    return
                await tool_response(call, {
                    "status": "stale_confirmation",
                    "message": "沒有待確認操作，請勿重複要求確認。",
                })
                return
            if pending.expires_at < time.time():
                expired = pending
                pending = None
                cancel_confirmation_timeout()
                if expired.task_id and task_service is not None:
                    await emit_task_update(task_service.update(
                        expired.task_id, status="waiting_input", stage="confirmation_expired",
                        error_code="confirmation_expired",
                        result_summary="確認已逾時，請重新說明是否繼續。",
                        expected_statuses={"waiting_confirmation"},
                        expected_action_id=expired.confirmation_id,
                    ))
                    await schedule_ready_tasks(expired.batch_id)
                await tool_response(call, {
                    "status": "confirmation_expired",
                    "message": "確認已逾時，沒有執行變更。",
                })
                return
            if (
                context.get("scope") != pending.scope
                or context.get("revision") != pending.revision
            ):
                stale = pending
                pending = None
                cancel_confirmation_timeout()
                if stale.task_id and task_service is not None:
                    await emit_task_update(task_service.update(
                        stale.task_id, status="waiting_input", stage="context_stale",
                        error_code="stale_target",
                        result_summary="畫面或資料已改變，請重新選擇。",
                        expected_statuses={"waiting_confirmation"},
                        expected_action_id=stale.confirmation_id,
                    ))
                    await schedule_ready_tasks(stale.batch_id)
                await tool_response(call, {
                    "status": "stale_confirmation",
                    "message": "畫面內容已改變，沒有執行變更。",
                })
                return
            if not confirmation_matches(spoken, pending.proposal):
                phrase = pending.phrase
                await tool_response(call, {
                    "status": "confirmation_mismatch",
                    "phrase": phrase,
                    "message": action_confirmation_prompt(pending.proposal),
                })
                return
            if not context_allows_proposal(context, pending.proposal):
                denied = pending
                pending = None
                cancel_confirmation_timeout()
                if denied.task_id and task_service is not None:
                    await emit_task_update(task_service.update(
                        denied.task_id, status="waiting_input", stage="permission_denied",
                        error_code="permission_denied", result_summary="這項權限已關閉，草稿已保留；授權後需重新確認。",
                        expected_statuses={"waiting_confirmation"},
                        expected_action_id=denied.confirmation_id,
                    ))
                await send_event({"type": "confirmation_expired", "confirmation_id": denied.confirmation_id})
                await tool_response(call, {
                    "status": "permission_denied", "error_code": "permission_denied",
                    "message": "這項權限已關閉，沒有執行變更；重新授權後需再次確認。",
                })
                return
            confirmed = pending
            pending = None
            cancel_confirmation_timeout()
            recently_confirmed_id = confirmed.confirmation_id
            recently_confirmed_until = time.time() + 15
            if confirmed.proposal.intent == "ayue.private_query":
                private_read_authorized = True
            action_id = confirmed.confirmation_id
            if confirmed.task_id and task_service is not None:
                updated = task_service.set_action(
                    confirmed.task_id, action_id=action_id,
                    status="waiting_device", stage="waiting_device",
                    expected_action_id=confirmed.confirmation_id,
                    cancellable=action_metadata(confirmed.proposal.intent)["risk"] == "read",
                    expected_statuses={"waiting_confirmation"},
                )
                if updated is None:
                    await tool_response(call, {
                        "status": "stale_confirmation",
                        "message": "任務狀態已改變，沒有重複執行。",
                    })
                    return
                await emit_task_update(updated)
                task_actions[action_id] = (confirmed.task_id, confirmed.batch_id)
            if confirmed.proposal.intent == "ayue.private_query":
                background_actions[action_id] = confirmed.proposal
                background_action_transcripts[action_id] = last_user_transcript
                progress_turn_pending = True
                next_reply_code = "ayue_delegated"
            else:
                awaiting_results[action_id] = PendingToolResult(
                    call_id=("" if confirmed.task_id else str(call.id or action_id)),
                    name=str(call.name or name),
                    task_id=confirmed.task_id,
                    batch_id=confirmed.batch_id,
                )
            await send_event({
                "type": "action_proposal",
                "action_id": action_id,
                "confirmation_id": action_id,
                "intent": confirmed.proposal.intent,
                "arguments": confirmed.proposal.arguments,
                "confirmed": True,
                "scope": confirmed.scope,
                "base_revision": confirmed.revision,
                **({"task_id": confirmed.task_id, "batch_id": confirmed.batch_id}
                   if confirmed.task_id else {}),
            })
            if confirmed.proposal.intent == "ayue.private_query":
                await tool_response(call, {
                    "status": "working",
                    "spoken_prompt": localized_prompt("working"),
                    "message": "私人聊天讀取已在本 session 授權。",
                })
            elif confirmed.task_id:
                current_task = task_service.get_task(owner_id, confirmed.task_id)
                await tool_response(call, {
                    "status": "queued",
                    "task": task_service.project(current_task) if current_task else {},
                    "message": "已確認並交給任務執行；這不代表已完成。",
                })
            return

        if name == "cancel_current_action":
            pending = None
            cancel_confirmation_timeout()
            next_reply_code = "cancelled"
            await tool_response(call, {
                "status": "cancelled",
                "message": "待確認操作已取消。",
            })
            return

        if name == "close_voice_mode":
            pending = None
            cancel_confirmation_timeout()
            close_after_turn = True
            next_reply_code = "voice_mode_closed"
            await tool_response(call, {
                "status": "ok",
                "message": "請簡短說「好，語音模式已關閉」。",
            })
            return

        await tool_response(call, {
            "status": "rejected",
            "message": "未知工具，不得執行任何 App 操作。",
        })

    async def reset_output(*, interrupted: bool) -> None:
        nonlocal current_response_id, current_sequence
        nonlocal current_transcript, current_audio_allowed
        nonlocal first_chunk, suppress_current_turn
        if interrupted and current_response_id:
            await send_event({
                "type": "audio_interrupted",
                "response_id": current_response_id,
            })
        current_response_id = None
        current_sequence = 0
        current_transcript = ""
        current_audio_allowed = None
        first_chunk = None
        suppress_current_turn = False

    def is_non_blocking_tool(name: str) -> bool:
        checker = getattr(live, "is_non_blocking_tool", None)
        return bool(callable(checker) and checker(name))

    def mark_non_blocking_tool_started(call: Any) -> None:
        call_id = str(call.id or "")
        name = str(call.name or "")
        if not call_id or call_id in non_blocking_tool_started_at:
            return
        non_blocking_tool_started_at[call_id] = time.monotonic()
        record_voice_metric(
            "non_blocking_tool_started",
            routing_mode=active_routing_mode,
            capability_id=name,
            result_code="started",
        )

    async def run_non_blocking_tool(call: Any) -> None:
        call_id = str(call.id or "")
        name = str(call.name or "")
        try:
            await handle_function(call)
        except asyncio.CancelledError:
            tool_started_at = non_blocking_tool_started_at.pop(call_id, None)
            record_voice_metric(
                "non_blocking_tool_cancelled",
                routing_mode=active_routing_mode,
                capability_id=name,
                latency_ms=(
                    (time.monotonic() - tool_started_at) * 1000
                    if tool_started_at is not None else None
                ),
                result_code="cancelled",
            )
            raise
        except Exception:
            try:
                await tool_response(call, {
                    "status": "failed",
                    "error_code": "tool_execution_failed",
                    "message": "目前暫時無法完成這項查詢，請稍後再試。",
                })
            except Exception:
                tool_started_at = non_blocking_tool_started_at.pop(call_id, None)
                record_voice_metric(
                    "non_blocking_tool_completed",
                    routing_mode=active_routing_mode,
                    capability_id=name,
                    latency_ms=(
                        (time.monotonic() - tool_started_at) * 1000
                        if tool_started_at is not None else None
                    ),
                    result_code="response_failed",
                )
        finally:
            current = asyncio.current_task()
            if non_blocking_tool_tasks.get(call_id) is current:
                non_blocking_tool_tasks.pop(call_id, None)

    async def invoke_tool_call(call: Any) -> None:
        call_id = str(call.id or "")
        name = str(call.name or "")
        non_blocking = is_non_blocking_tool(name)
        if non_blocking:
            mark_non_blocking_tool_started(call)
        if non_blocking and name == "read_weather" and call_id:
            current = non_blocking_tool_tasks.get(call_id)
            if current is None or current.done():
                if len(non_blocking_tool_tasks) >= 4:
                    await tool_response(call, {
                        "status": "busy",
                        "error_code": "too_many_background_tools",
                        "message": "目前同時處理的查詢較多，請稍後再試。",
                    })
                    return
                non_blocking_tool_tasks[call_id] = asyncio.create_task(
                    run_non_blocking_tool(call),
                )
            return
        async with state_lock:
            await handle_function(call)

    async def cancel_non_blocking_tools(reason: str) -> None:
        tasks = list(non_blocking_tool_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        non_blocking_tool_tasks.clear()
        for call_id, tool_started_at in list(non_blocking_tool_started_at.items()):
            non_blocking_tool_started_at.pop(call_id, None)
            record_voice_metric(
                "non_blocking_tool_cancelled",
                routing_mode=active_routing_mode,
                latency_ms=(time.monotonic() - tool_started_at) * 1000,
                result_code=reason,
            )

    async def deliver_background_result(message: str | dict[str, Any], source: str) -> None:
        if quota_stopped:
            return
        rendered = json.dumps(message, ensure_ascii=False) if isinstance(message, dict) else str(message)[:12000]
        marker = (
            "[APP_VOICE_TASK_RESULT]" if source == "task"
            else "[APP_VOICE_DIRECT_RESULT]" if source == "direct"
            else "[DELEGATED_AYUE_RESULT]"
        )
        next_step_prompt = ""
        results = [message]
        if isinstance(message, dict) and isinstance(message.get("completed_results"), list):
            results.extend(message["completed_results"])
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("next_step_offer"), dict):
                continue
            offered_prompt = str(item["next_step_offer"].get("prompt") or "")
            current_offer = context.get("_next_step_offer") or {}
            if offered_prompt and offered_prompt == current_offer.get("prompt"):
                next_step_prompt = (
                    f" 先摘要景點，再於回答最後只問一次：{offered_prompt} "
                    "這是可選的提議；使用者明確選擇前，不得查行事曆或詢問配對阿月。"
                )
                break
        persona_instruction = (
            "可在最後的提議中說配對阿月會協助，其他內容仍由阿月第一人稱回答，"
            if next_step_prompt else "不要提到有多個阿月，"
        )
        await live.send_text(
            f"{marker}\n"
            f"source={source}\n"
            f"{rendered}\n"
            "這是已完成的工具結果。請理解重點後，直接以阿月第一人稱自然回答使用者，"
            "不要逐字照念、不要說你在轉述、"
            f"{persona_instruction}"
            "也不要增加結果沒有的事實或承諾。完整結果及 recommendations 是本次對話的參考資料；"
            "口頭可以摘要，但之後仍須記得未唸出的店名、地址與推薦，不能把摘要當成完整清單。"
            f"{next_step_prompt}",
        )

    async def flush_background_results(*, delay: float = 0.0) -> None:
        nonlocal progress_turn_pending, background_flush_task
        nonlocal live_turn_active, current_response_id, current_sequence
        nonlocal current_transcript, current_audio_allowed, first_chunk, suppress_current_turn
        nonlocal last_live_activity
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            # A missing turn_complete used to leave a finished App result
            # queued forever. Keep watching; recover only an idle model turn,
            # never active speech or a device operation still in flight.
            while (
                not stopped and not quota_stopped and queued_background_results
                and (user_speaking or live_turn_active or current_response_id is not None)
            ):
                if (
                    not user_speaking and not awaiting_results
                    and not background_actions and not non_blocking_tool_tasks
                    and time.monotonic() - last_live_activity >= BACKGROUND_RESULT_IDLE_SECONDS
                ):
                    stale_id = current_response_id
                    current_response_id = None
                    current_sequence = 0
                    current_transcript = ""
                    current_audio_allowed = None
                    first_chunk = None
                    suppress_current_turn = False
                    live_turn_active = False
                    if stale_id:
                        await send_event({"type": "audio_interrupted", "response_id": stale_id})
                    await send_event({
                        "type": "operation_status", "stage": "reply_recovering",
                        "message": "資料已取得，正在恢復語音回覆。",
                    })
                    record_voice_metric(
                        "background_reply_recovered", routing_mode=active_routing_mode,
                        result_code="idle_turn",
                    )
                    break
                await asyncio.sleep(min(0.25, BACKGROUND_RESULT_IDLE_SECONDS))
            if stopped or quota_stopped or not queued_background_results:
                return
            queued = list(queued_background_results)
            queued_background_results.clear()
            if len(queued) == 1:
                queued_message, queued_source = queued[0]
            else:
                queued_message = {
                    "completed_results": [item for item, _source in queued],
                }
                queued_source = "task" if any(
                    source == "task" for _item, source in queued
                ) else queued[0][1]
            # Keep later completions queued until this generated voice turn ends.
            progress_turn_pending = True
            live_turn_active = True
            last_live_activity = time.monotonic()
            await deliver_background_result(queued_message, queued_source)
        except asyncio.CancelledError:
            return
        finally:
            if background_flush_task is asyncio.current_task():
                background_flush_task = None

    async def queue_background_result(
        message: str | dict[str, Any], source: str,
    ) -> None:
        nonlocal progress_turn_pending, background_flush_task
        if stopped or quota_stopped:
            return
        queued_background_results.append((message, source))
        if current_response_id is not None or live_turn_active or source == "direct":
            progress_turn_pending = True
        if background_flush_task is None or background_flush_task.done():
            # A short window combines tasks finishing at nearly the same time.
            background_flush_task = asyncio.create_task(
                flush_background_results(delay=0.12),
            )

    async def send_task_snapshot(*, reset_baseline: bool) -> None:
        if task_service is None or stopped:
            return
        rows = (
            await asyncio.to_thread(
                task_service.list_tasks, owner_id, "all", limit=20,
            )
            if task_service.offload_io
            else task_service.list_tasks(owner_id, "all", limit=20)
        )
        if reset_baseline:
            task_revision_cache.clear()
        for item in rows:
            task_ref = str(item.get("task_ref") or "")
            if task_ref:
                task_revision_cache[task_ref] = max(
                    task_revision_cache.get(task_ref, -1),
                    int(item.get("revision", 0) or 0),
                )
        await send_event({
            "type": "task_snapshot", "task_protocol_version": 1,
            "tasks": rows,
        })

    async def watch_tasks() -> None:
        nonlocal pending
        while not stopped and task_service is not None:
            try:
                await asyncio.sleep(1.0)
                rows = (
                    await asyncio.to_thread(
                        task_service.list_tasks, owner_id, "all", limit=20,
                    )
                    if task_service.offload_io
                    else task_service.list_tasks(owner_id, "all", limit=20)
                )
                for item in rows:
                    task_ref = str(item.get("task_ref") or "")
                    revision = int(item.get("revision", 0) or 0)
                    if not task_ref or revision <= task_revision_cache.get(task_ref, -1):
                        continue
                    task_revision_cache[task_ref] = revision
                    record_voice_metric(
                        "task_update",
                        routing_mode=active_routing_mode,
                        capability_id=item.get("capability_id"),
                        task_stage=item.get("stage"), result_code=item.get("status"),
                    )
                    await send_event({
                        "type": "task_update", "task_protocol_version": 1,
                        "task": item,
                    })
                    status = str(item.get("status") or "")
                    raw = task_service.get_by_ref(owner_id, task_ref)
                    if (
                        pending is not None
                        and task_ref == f"task_{pending.task_id}"
                        and status != "waiting_confirmation"
                    ):
                        async with state_lock:
                            if pending is not None and task_ref == f"task_{pending.task_id}":
                                pending = None
                                cancel_confirmation_timeout()
                    if status in {
                        "queued", "completed", "failed", "cancelled",
                        "expired", "waiting_input",
                    }:
                        await schedule_ready_tasks(str((raw or {}).get("batch_id") or ""))
                    if status in {"completed", "failed", "waiting_input"}:
                        await queue_background_result({
                            "task_ref": task_ref,
                            "status": status,
                            "message": str(item.get("result_summary") or "")[:600],
                        }, "task")
            except asyncio.CancelledError:
                raise
            except Exception:
                # The owner-scoped REST endpoint remains available; retry on
                # the next bounded poll without ending the voice session.
                continue

    async def dispatch_template_fast_path() -> None:
        """Dispatch explicit actions even when Live omits a tool call."""

        nonlocal template_fast_path_transcript, suppress_current_turn, next_reply_code
        nonlocal next_step_dispatched_intent, next_step_selected_proposal
        if not last_user_transcript:
            return
        if template_fast_path_transcript == last_user_transcript:
            return
        offer = context.get("_next_step_offer")
        if (
            isinstance(offer, dict)
            and last_user_transcript != offer.get("source_transcript")
            and select_place_next_step(last_user_transcript, offer) is None
        ):
            context.pop("_next_step_offer", None)
        calendar_block = calendar_write_preflight(last_user_transcript, context)
        if calendar_block is not None:
            template_fast_path_transcript = last_user_transcript
            suppress_current_turn = True
            next_reply_code = "app_action"
            await queue_background_result({
                "status": calendar_block[0],
                "message": calendar_block[1],
            }, "direct")
            return
        if await handle_draft_utterance(last_user_transcript):
            return
        offer = context.get("_next_step_offer")
        if isinstance(offer, dict):
            source_utterance = last_user_transcript == offer.get("source_transcript")
            choice = (
                None if source_utterance else
                select_place_next_step(last_user_transcript, offer)
            )
            if choice in {"calendar", "companion", "clarify", "dismiss"}:
                if pending is not None:
                    return
                template_fast_path_transcript = last_user_transcript
                suppress_current_turn = True
                next_reply_code = "app_action"
                if choice in {"clarify", "dismiss"}:
                    if choice == "dismiss":
                        context.pop("_next_step_offer", None)
                    language = str(offer.get("response_language") or "zh-TW")
                    prompt = {
                        "zh-TW": (
                            "要先查行事曆，還是請配對阿月推薦同行人？",
                            "好，有需要再跟我說。",
                        ),
                        "zh-CN": (
                            "要先查日历，还是请配对阿月推荐同行的人？",
                            "好，有需要再告诉我。",
                        ),
                        "en-US": (
                            "Should I check your calendar or ask Matching Ayue about a companion?",
                            "Okay. Let me know if you need anything else.",
                        ),
                    }.get(language, ("要先查行事曆，還是請配對阿月推薦同行人？", "好，有需要再跟我說。"))[
                        0 if choice == "clarify" else 1
                    ]
                    next_step_dispatched_intent = "assistant.reply"
                    await queue_background_result({
                        "status": "needs_input" if choice == "clarify" else "success",
                        "message": prompt,
                    }, "direct")
                    return
                context.pop("_next_step_offer", None)
                selected = place_next_step_action(
                    choice, offer, selected_text=last_user_transcript,
                )
                if selected is None:
                    return
                proposal = VoiceProposal(
                    selected[0], selected[1], "", int(context.get("revision") or 0),
                )
                if not context_allows_proposal(context, proposal):
                    await queue_background_result({
                        "status": "permission_denied",
                        "message": "這項資料權限目前未開啟，無法執行。",
                    }, "direct")
                    return
                tool_shape = (
                    template_tool_call_for_proposal(proposal)
                    if template_enabled else
                    ("read_calendar", dict(proposal.arguments))
                    if choice == "calendar" else
                    ("ask_matching_ayue", dict(proposal.arguments))
                )
                if tool_shape is None:
                    return
                next_step_dispatched_intent = proposal.intent
                next_step_selected_proposal = proposal
                call_id = uuid.uuid4().hex
                template_fast_path_call_ids.add(call_id)
                await invoke_tool_call(SimpleNamespace(
                    id=call_id, name=tool_shape[0], args=tool_shape[1],
                ))
                return
            if choice is None and not source_utterance:
                context.pop("_next_step_offer", None)
        cooldown_reason = is_chat_cooldown_reason_request(
            last_user_transcript, scope=str(context.get("scope") or ""),
        )
        companion_request = is_companion_matching_request(last_user_transcript)
        if proxy_enabled and not (cooldown_reason or companion_request):
            return
        proposal = (
            authoritative_template_proposal(last_user_transcript, context=context)
            if template_enabled else
            deterministic_proposal(last_user_transcript, context=context)
        )
        if not template_enabled and not (
            proposal is not None
            and (
                proposal.intent == "match.query"
                or (
                    proposal.intent == "match.ayue_query"
                    and companion_request
                )
                or (proposal.intent == "chat.status.query" and cooldown_reason)
            )
        ):
            return
        if pending is not None and not (
            proposal is not None and proposal.intent == "chat.status.query"
        ) and not (drafts_enabled() and current_draft() and proposal and proposal.intent in {
            "quota.query", "chat.status.query", "calendar.query", "contacts.query", "weather.query", "match.query", "date.query",
        }):
            return
        if proposal is None or proposal.intent not in {
            "quota.query", "chat.status.query",
            "app.navigate", "chat.open", "ayue.private_open",
            "calendar.query", "match.query",
            "date.query", "contacts.query", "memory.query", "self.query",
            "ayue.private_query", "ayue.public_query", "match.ayue_query",
            "weather.query", "post.select_recent_photos",
            "safety.blocked_users_query", "safety.block_user",
            "safety.unblock_user",
            "personality.explore",
            "ui.choice.activate",
        }:
            return
        if template_enabled:
            tool_shape = template_tool_call_for_proposal(proposal)
        elif proposal.intent == "chat.status.query":
            tool_shape = ("read_chat_status", dict(proposal.arguments))
        elif proposal.intent == "match.query":
            tool_name = (
                "read_match_status"
                if proposal.arguments.get("view") == "status"
                else "read_match_hub"
            )
            tool_shape = (tool_name, {})
        else:
            tool_shape = ("ask_matching_ayue", dict(proposal.arguments))
        if tool_shape is None:
            return
        tool_name, tool_args = tool_shape
        template_fast_path_transcript = last_user_transcript
        call_id = uuid.uuid4().hex
        template_fast_path_call_ids.add(call_id)
        # If Gemini later emits a normal tool call for the same utterance, its
        # result is ignored as a duplicate. The verified App result below is
        # sent back as a fresh short model turn.
        suppress_current_turn = True
        next_reply_code = "app_action"
        await invoke_tool_call(SimpleNamespace(
            id=call_id,
            name=tool_name,
            args=tool_args,
        ))

    async def handle_live_message(response: Any) -> bool:
        nonlocal last_live_activity, user_speaking
        nonlocal current_response_id, current_sequence
        nonlocal current_transcript, current_audio_allowed
        nonlocal last_user_transcript, input_transcript_finished
        nonlocal next_reply_code, close_after_turn
        nonlocal first_chunk, last_completed_first_chunk
        nonlocal checking_replayed_turn, suppress_current_turn
        nonlocal progress_turn_pending
        nonlocal template_fast_path_transcript, draft_handled_text, draft_confirmation_needs_input
        nonlocal next_step_dispatched_intent, next_step_selected_proposal
        nonlocal live_turn_active

        if response.go_away:
            await send_event({"type": "state", "state": "reconnecting"})
            return True

        activity = ""
        voice_activity = getattr(response, "voice_activity", None)
        if voice_activity:
            activity = str(voice_activity.voice_activity_type or "")
        else:
            vad_signal = getattr(response, "voice_activity_detection_signal", None)
            if vad_signal:
                activity = str(vad_signal.vad_signal_type or "")
        if activity:
            last_live_activity = time.monotonic()
            if activity.endswith(("ACTIVITY_START", "SOS")):
                user_speaking = True
                last_user_transcript = ""
                draft_handled_text = ""
                draft_confirmation_needs_input = False
                input_transcript_finished = False
                template_fast_path_transcript = ""
                next_step_dispatched_intent = ""
                next_step_selected_proposal = None
                template_fast_path_call_ids.clear()
                await send_event({
                    "type": "microphone_state",
                    "state": "speech_started",
                })
            elif activity.endswith(("ACTIVITY_END", "EOS")):
                user_speaking = False
                await send_event({
                    "type": "microphone_state",
                    "state": "speech_ended",
                })

        content = response.server_content
        if content:
            if content.model_turn or content.output_transcription or content.input_transcription:
                last_live_activity = time.monotonic()
                live_turn_active = True
            if checking_replayed_turn and content.turn_complete and not content.model_turn:
                checking_replayed_turn = False
            if content.interrupted:
                # A barge-in starts a new input segment. Clear the previous
                # segment before processing any transcript delivered with the
                # interruption event itself.
                last_user_transcript = ""
                draft_handled_text = ""
                draft_confirmation_needs_input = False
                input_transcript_finished = False
                await reset_output(interrupted=True)
            interim = content.interim_input_transcription
            if interim and interim.text and not suppress_current_turn:
                await send_event({
                    "type": "user_transcript",
                    "text": display_transcript(str(interim.text)),
                    "final": False,
                })
            incoming = content.input_transcription
            if incoming and incoming.text and not suppress_current_turn:
                # Input transcription is delivered independently from VAD, so
                # the next segment can arrive before a new ACTIVITY_START.
                # A finished transcription is the reliable fallback boundary.
                if input_transcript_finished:
                    last_user_transcript = ""
                    input_transcript_finished = False
                    draft_handled_text = ""
                    draft_confirmation_needs_input = False
                last_user_transcript = _append_transcript(
                    last_user_transcript,
                    str(incoming.text),
                )
                await send_event({
                    "type": "user_transcript",
                    "text": display_transcript(last_user_transcript),
                    "final": incoming.finished is True,
                })
                if incoming.finished is True:
                    user_speaking = False
                    remember("user", last_user_transcript)
                    await dispatch_template_fast_path()
                    input_transcript_finished = True
            outgoing = content.output_transcription
            if outgoing and outgoing.text and not suppress_current_turn:
                current_transcript = _append_transcript(
                    current_transcript,
                    str(outgoing.text),
                )
                if current_response_id:
                    await send_event({
                        "type": "assistant_transcript",
                        "response_id": current_response_id,
                        "text": display_transcript(current_transcript, traditional=(context.get("voice_config") or {}).get("response_language", "zh-TW") == "zh-TW"),
                    })
            if content.model_turn:
                for part in content.model_turn.parts or []:
                    inline = getattr(part, "inline_data", None)
                    data = getattr(inline, "data", None) if inline else None
                    if not data:
                        continue
                    chunk = bytes(data)
                    if first_chunk is None:
                        first_chunk = chunk
                        if checking_replayed_turn:
                            suppress_current_turn = (
                                last_completed_first_chunk is not None
                                and hmac.compare_digest(
                                    first_chunk,
                                    last_completed_first_chunk,
                                )
                            )
                            checking_replayed_turn = False
                    if suppress_current_turn:
                        continue
                    if current_response_id is None:
                        current_response_id = uuid.uuid4().hex
                        current_audio_allowed = limiter.allow_tts(identity)
                        await send_event({
                            "type": "assistant_reply",
                            "text": display_transcript(current_transcript, traditional=(context.get("voice_config") or {}).get("response_language", "zh-TW") == "zh-TW") or "阿月正在回答…",
                            "code": next_reply_code,
                            "response_id": current_response_id,
                            "speech_mode": "gemini_live_duplex",
                            "can_interrupt": True,
                        })
                    if current_audio_allowed:
                        await send_event({
                            "type": "audio_chunk",
                            "response_id": current_response_id,
                            "sequence": current_sequence,
                            "mime_type": "audio/pcm;rate=24000",
                            "base64": base64.b64encode(chunk).decode("ascii"),
                        })
                        current_sequence += 1
            if content.turn_complete:
                user_speaking = False
                last_live_activity = time.monotonic()
                # Some Live responses do not set input_transcription.finished.
                # Model turn completion is the fallback boundary for the next
                # user segment when VAD and transcription arrive out of order.
                if last_user_transcript:
                    input_transcript_finished = True
                    offer = context.get("_next_step_offer")
                    if (
                        isinstance(offer, dict)
                        and last_user_transcript != offer.get("source_transcript")
                    ):
                        await dispatch_template_fast_path()
                live_turn_active = False
                if not suppress_current_turn and last_user_transcript:
                    remember("user", last_user_transcript)
                if not suppress_current_turn and current_transcript:
                    remember("assistant", current_transcript)
                response_id = current_response_id
                if response_id:
                    await send_event({
                        "type": "audio_complete" if current_audio_allowed else "audio_unavailable",
                        "response_id": response_id,
                        "chunks": current_sequence,
                    })
                if not suppress_current_turn and first_chunk is not None:
                    last_completed_first_chunk = first_chunk
                await reset_output(interrupted=False)
                next_reply_code = "conversation"
                if progress_turn_pending:
                    progress_turn_pending = False
                    await flush_background_results()
                if close_after_turn:
                    close_after_turn = False
                    await send_event({"type": "state", "state": "close_requested"})

        if response.tool_call:
            for call in response.tool_call.function_calls or []:
                await invoke_tool_call(call)
        if response.tool_call_cancellation:
            cancelled = set(response.tool_call_cancellation.ids or [])
            for call_id in cancelled:
                task = non_blocking_tool_tasks.get(str(call_id))
                if task is not None:
                    task.cancel()
            for action_id, tool in list(awaiting_results.items()):
                if tool.call_id in cancelled:
                    if tool.task_id:
                        # Barge-in may cancel Gemini's synchronous tool turn,
                        # but the durable App task must keep running.
                        awaiting_results[action_id] = PendingToolResult(
                            call_id="", name=tool.name,
                            task_id=tool.task_id, batch_id=tool.batch_id,
                        )
                    else:
                        awaiting_results.pop(action_id, None)
        return False

    async def reconnect_live() -> None:
        nonlocal pending, checking_replayed_turn
        resumed = await live.reconnect()
        await sync_screen_context(force=True)
        checking_replayed_turn = resumed
        if not resumed:
            lost_confirmation: DuplexConfirmation | None = None
            async with state_lock:
                lost_confirmation = pending
                pending = None
                cancel_confirmation_timeout()
                for action_id, tool in list(awaiting_results.items()):
                    if tool.task_id:
                        awaiting_results[action_id] = PendingToolResult(
                            call_id="", name=tool.name,
                            task_id=tool.task_id, batch_id=tool.batch_id,
                        )
                    else:
                        awaiting_results.pop(action_id, None)
            if task_service is not None:
                try:
                    if lost_confirmation and lost_confirmation.task_id:
                        await emit_task_update(task_service.update(
                            lost_confirmation.task_id,
                            status="waiting_input", stage="live_session_restarted",
                            error_code="confirmation_context_lost",
                            result_summary="語音模型連線已重建，請重新確認。",
                            expected_statuses={"waiting_confirmation"},
                            expected_action_id=lost_confirmation.confirmation_id,
                        ))
                    await send_task_snapshot(reset_baseline=False)
                    await schedule_ready_tasks(
                        lost_confirmation.batch_id if lost_confirmation else "",
                    )
                except RuntimeError:
                    await send_event({"type": "error", "code": "voice_task_storage_unavailable"})
        await send_event({
            "type": "state",
            "state": "duplex_ready",
            "microphone": "streaming",
            "resumed": resumed,
            "confirmations_preserved": resumed,
        })

    async def receive_live() -> None:
        reconnect_attempts = 0
        while not stopped and not quota_stopped:
            try:
                should_reconnect = False
                async for response in live.receive_turn():
                    should_reconnect = await handle_live_message(response)
                    if should_reconnect:
                        break
                if should_reconnect and not quota_stopped:
                    await reconnect_live()
                reconnect_attempts = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                if quota_stopped:
                    return
                from .status_contract import provider_error_code
                if provider_error_code(error) == 'provider_rate_limited':
                    await send_event({'type': 'error', 'code': 'provider_rate_limited'})
                    await websocket.close(code=1013)
                    return
                reconnect_attempts += 1
                if reconnect_attempts > 2 or stopped:
                    await send_event({
                        "type": "error",
                        "code": "gemini_live_disconnected",
                    })
                    try:
                        await websocket.close(code=1011)
                    except Exception:
                        pass
                    return
                await send_event({"type": "state", "state": "reconnecting"})
                await asyncio.sleep(0.25 * reconnect_attempts)
                if quota_stopped:
                    return
                try:
                    await reconnect_live()
                except Exception:
                    continue

    if task_service is not None:
        try:
            recovered_batches = task_service.recover_for_session(owner_id)
            await send_task_snapshot(reset_baseline=True)
            for recovered_batch in recovered_batches:
                await schedule_ready_tasks(recovered_batch)
            task_watcher_task = asyncio.create_task(watch_tasks())
        except RuntimeError:
            await send_event({"type": "error", "code": "voice_task_storage_unavailable"})

    receiver = asyncio.create_task(receive_live())
    if quota_reader is not None:
        quota_watcher_task = asyncio.create_task(watch_quota())
    try:
        await sync_screen_context()
        await live.send_text(session_started_prompt(context))
        while True:
            remaining = max_session_seconds - (time.monotonic() - started)
            if remaining <= 0:
                await send_event({
                    "type": "state",
                    "state": "session_expired",
                    "reconnect": True,
                })
                break
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                continue
            if message.get("type") == "websocket.disconnect":
                break
            binary = message.get("bytes")
            if quota_stopped and binary is not None:
                continue
            if binary is not None:
                if binary and len(binary) <= 4096:
                    try:
                        await live.send_audio(bytes(binary))
                    except Exception:
                        await send_event({
                            "type": "state",
                            "state": "reconnecting",
                        })
                continue
            raw = message.get("text")
            if not raw:
                continue
            try:
                control = json.loads(raw)
            except (TypeError, ValueError):
                continue
            kind = control.get("type")
            if kind == 'voice_metric':
                if client_metric_count >= 240:
                    continue
                metric = str(control.get('metric') or '')
                if metric in {'speech_to_audio', 'speech_to_result', 'action_duration', 'interrupt_stop', 'feedback_misheard', 'feedback_slow', 'feedback_wrong_action'}:
                    value = control.get('latency_ms')
                    if value is not None and (type(value) is not int or not 0 <= value <= 600000):
                        continue
                    client_metric_count += 1
                    record_voice_metric('client_' + metric, routing_mode=active_routing_mode,
                                        latency_ms=control.get('latency_ms'), result_code='observed')
                continue
            if quota_stopped and kind not in {'action_result', 'task_cancel', 'task_dismiss', 'stop'}:
                continue
            if kind == 'plan_selection':
                async with state_lock:
                    await handle_function(SimpleNamespace(id='', name='plan_date',
                        args={'operation': 'select', 'plan_ref': str(control.get('plan_ref') or '')}))
                continue
            if kind == "context_changed":
                next_context = safe_context(control.get("context"))
                if next_context.get('permissions', {}).get('public_ayue') is not True:
                    recent_public_places = []
                else:
                    screen_places = ((next_context.get('screen') or {}).get('content') or {}).get('recommendations') or []
                    if screen_places:
                        recent_public_places = screen_places
                if context.get("_calendar_reference"):
                    next_context["_calendar_reference"] = context["_calendar_reference"]
                if (
                    next_context.get("permissions", {}).get("calendar_read") is True
                    and context.get("_calendar_recent_at")
                ):
                    for key in (
                        "_calendar_recent_events", "_calendar_recent_count",
                        "_calendar_recent_at",
                    ):
                        next_context[key] = context.get(key)
                offer = context.get("_next_step_offer")
                next_permissions = next_context.get("permissions") or {}
                if (
                    isinstance(offer, dict)
                    and select_place_next_step("好", offer) is not None
                    and next_permissions.get("public_ayue") is True
                    and next_permissions.get("places") is True
                    and (
                        not offer.get("calendar_enabled")
                        or next_permissions.get("calendar_read") is True
                    )
                    and (
                        not offer.get("companion_enabled")
                        or next_permissions.get("match_ayue") is True
                    )
                ):
                    next_context["_next_step_offer"] = offer
                async with state_lock:
                    if pending and (
                        next_context["scope"] != pending.scope
                        or next_context["revision"] != pending.revision
                        or (
                            pending.proposal.arguments.get("target_ref")
                            and bind_target(pending.proposal.intent, pending.proposal.arguments, next_context)[1] is not None
                        )
                    ):
                        stale = pending
                        pending = None
                        cancel_confirmation_timeout()
                        if stale.task_id and task_service is not None:
                            await emit_task_update(task_service.update(
                                stale.task_id, status="waiting_input",
                                stage="context_stale", error_code="stale_target",
                                result_summary="畫面或資料已改變，請重新選擇。",
                                expected_statuses={"waiting_confirmation"},
                                expected_action_id=stale.confirmation_id,
                            ))
                            await schedule_ready_tasks(stale.batch_id)
                    context = next_context
                # UI animation/streaming updates must not block this receive
                # loop: it also carries microphone frames and action results.
                queue_screen_context()
            elif kind == "utterance" and template_enabled:
                text = str(control.get("text") or "").strip()[:2000]
                if not text:
                    continue
                last_user_transcript = text
                draft_handled_text = ""
                draft_confirmation_needs_input = False
                remember("user", text)
                await send_event({
                    "type": "user_transcript",
                    "text": display_transcript(text),
                    "final": True,
                })
                if await handle_draft_utterance(text):
                    continue
                if pending is not None and confirmation_matches(text, pending.proposal):
                    await handle_function(SimpleNamespace(
                        id="",
                        name="confirm_pending_action",
                        args={"spoken_phrase": text},
                    ))
                    continue
                before = template_fast_path_transcript
                await dispatch_template_fast_path()
                if template_fast_path_transcript == before:
                    await live.send_text(text)
            elif kind == "confirmation_response":
                confirmation_id = str(control.get("confirmation_id") or "")
                spoken_phrase = str(control.get("spoken_phrase") or "")[:200]
                async with state_lock:
                    current_confirmation = pending
                    if (
                        current_confirmation is None
                        or not confirmation_id
                        or not hmac.compare_digest(
                            confirmation_id,
                            current_confirmation.confirmation_id,
                        )
                    ):
                        continue
                    last_user_transcript = spoken_phrase
                    draft_confirmation_needs_input = False
                    await handle_function(SimpleNamespace(
                        id="",
                        name="resolve_pending_interaction",
                        args={
                            "action": (
                                "confirm"
                                if control.get("accepted") is True else "cancel"
                            ),
                            "spoken_phrase": spoken_phrase,
                        },
                    ))
            elif kind == "action_result":
                action_id = str(control.get("action_id") or "")
                if action_id in background_actions:
                    proposal = background_actions.pop(action_id)
                    source_transcript = background_action_transcripts.pop(
                        action_id, last_user_transcript,
                    )
                    task_info = task_actions.pop(action_id, None)
                    source = (
                        "task" if task_info
                        else "private" if proposal.intent == "ayue.private_query"
                        else "public"
                    )
                    result_text = str(control.get("message") or "")[:1200]
                    if control.get("success") is not True:
                        result_text = result_text or "目前暫時無法回覆。"
                    else:
                        last_delegated_proposal = proposal
                        last_delegated_expires_at = time.time() + 120
                    if control.get("result_version") == 1:
                        result_text = safe_result(control)
                        places = (result_text.get('data') or {}).get('recommendations') or []
                        if (control.get('success') is True and proposal.intent in {'ayue.public_query', 'match.ayue_query'}
                                and places):
                            recent_public_places = places
                        offer = build_place_next_step_offer(
                            proposal, result_text, context.get("permissions") or {},
                            response_language=str(
                                (context.get("voice_config") or {}).get("response_language") or "zh-TW"
                            ),
                        )
                        if offer is not None:
                            offer["source_transcript"] = source_transcript
                            context["_next_step_offer"] = offer
                            result_text["next_step_offer"] = {"prompt": offer["prompt"]}
                    if task_info and task_service is not None:
                        task_id, batch_id = task_info
                        updated = task_service.complete_action(
                            owner_id, action_id,
                            success=control.get("success") is True,
                            message=(
                                safe_reply(control.get("message"))
                                or ("操作已完成。" if control.get("success") is True else "操作沒有完成。")
                            ),
                            error_code=str(control.get("error_code") or ""),
                        )
                        await emit_task_update(updated)
                        await schedule_ready_tasks(batch_id)
                        if updated is None or updated.get("status") in {
                            "cancelled", "cancel_requested",
                        }:
                            continue
                    await queue_background_result(result_text, source)
                    continue
                async with state_lock:
                    target = awaiting_results.pop(action_id, None)
                if target:
                    success = control.get("success") is True
                    next_reply_code = "action_completed" if success else "action_failed"
                    result_response = safe_result(control)
                    calendar_data = result_response.get("data") or {}
                    if (
                        success
                        and context.get("permissions", {}).get("calendar_read") is True
                        and isinstance(calendar_data.get("events"), list)
                    ):
                        events = [
                            event for event in calendar_data["events"]
                            if event.get("source_type") in {"google", "personal", "date"}
                        ][:20]
                        context["_calendar_recent_events"] = events
                        context["_calendar_recent_count"] = calendar_data.get("count", len(events))
                        context["_calendar_recent_at"] = time.time()
                    if target.task_id and task_service is not None:
                        task_actions.pop(action_id, None)
                        updated = task_service.complete_action(
                            owner_id, action_id, success=success,
                            message=(
                                safe_reply(control.get("message"))
                                or ("操作已完成。" if success else "操作沒有完成。")
                            ),
                            error_code=str(control.get("error_code") or ""),
                        )
                        await emit_task_update(updated)
                        await schedule_ready_tasks(target.batch_id)
                        if updated is None or updated.get("status") in {
                            "cancelled", "cancel_requested",
                        }:
                            continue
                        if updated and updated.get("result_summary"):
                            result_item = {
                                **result_response,
                                "task_ref": updated.get("task_ref"),
                                "status": updated.get("status"),
                                "message": updated.get("result_summary"),
                            }
                            await queue_background_result(result_item, "task")
                    if target.call_id and not quota_stopped:
                        tool_response_cache[target.call_id] = (
                            target.name,
                            dict(result_response),
                        )
                        await live.send_tool_response(
                            call_id=target.call_id,
                            name=target.name,
                            response=result_response,
                        )
                    elif not target.task_id:
                        # Deterministic template fallbacks do not correspond
                        # to a Gemini-issued function call. Feed the verified
                        # App result back as a fresh short model turn instead
                        # of sending an invalid synthetic tool response.
                        template_fast_path_call_ids.discard(action_id)
                        await queue_background_result(result_response, "direct")
            elif kind == "task_cancel" and task_service is not None:
                try:
                    changed = task_service.cancel(
                        owner_id,
                        str(control.get("task_ref") or ""),
                        expected_revision=(
                            int(control["expected_revision"])
                            if control.get("expected_revision") is not None else None
                        ),
                    )
                    for item in changed:
                        await send_event({
                            "type": "task_update", "task_protocol_version": 1,
                            "task": item,
                        })
                    if pending is not None and any(
                        item.get("task_ref") == f"task_{pending.task_id}"
                        and item.get("status") in {"cancelled", "cancel_requested"}
                        for item in changed
                    ):
                        pending = None
                        cancel_confirmation_timeout()
                    batch_ids = {
                        str(row.get("batch_id") or "")
                        for item in changed
                        if (row := task_service.get_by_ref(
                            owner_id, str(item.get("task_ref") or ""),
                        ))
                    }
                    for batch_id in batch_ids:
                        await schedule_ready_tasks(batch_id)
                except (ValueError, RuntimeError) as error:
                    try:
                        rows = task_service.list_tasks(owner_id, "all", limit=20)
                    except RuntimeError:
                        rows = []
                    await send_event({
                        "type": "task_snapshot", "task_protocol_version": 1,
                        "tasks": rows, "error_code": str(error)[:80],
                    })
            elif kind in {
                "task_retry", "task_input", "task_dismiss", "task_undo", "task_pause",
            } and task_service is not None:
                task_ref = str(control.get("task_ref") or "")
                try:
                    expected_revision = (
                        int(control["expected_revision"])
                        if control.get("expected_revision") is not None else None
                    )
                    if kind == "task_pause" and drafts_enabled():
                        row = task_service.get_by_ref(owner_id, task_ref)
                        if row is None or row.get("revision") != expected_revision:
                            raise ValueError("voice_task_revision_stale")
                        result = await manage_draft({"action": "pause", "task_ref": task_ref})
                        await queue_background_result(result, "draft")
                    elif kind == "task_pause":
                        raise ValueError("draft_unsupported")
                    elif kind == "task_retry":
                        updated = task_service.retry(
                            owner_id, task_ref,
                            expected_revision=expected_revision,
                        )
                        await emit_task_update(updated)
                        await schedule_ready_tasks(str(updated.get("batch_id") or ""))
                    elif kind == "task_input":
                        values = control.get("values")
                        updated = task_service.provide_input(
                            owner_id, task_ref,
                            values if isinstance(values, dict) else {},
                            expected_revision=expected_revision,
                        )
                        await clear_draft_confirmation(task_ref)
                        await emit_task_update(updated)
                        await schedule_ready_tasks(str(updated.get("batch_id") or ""))
                    elif kind == "task_dismiss":
                        updated = task_service.dismiss(
                            owner_id, task_ref,
                            expected_revision=expected_revision,
                        )
                        await emit_task_update(updated)
                        await schedule_ready_tasks(str(updated.get("batch_id") or ""))
                    else:
                        created = task_service.create_undo_batch(
                            owner_id, task_ref, session_id=session_id,
                            expected_revision=expected_revision,
                        )
                        original = task_service.get_by_ref(owner_id, task_ref)
                        await emit_task_update(original)
                        for row in created.tasks:
                            await emit_task_update(row)
                        await schedule_ready_tasks(
                            str(created.batch.get("batch_id") or ""),
                        )
                except (TypeError, ValueError, RuntimeError) as error:
                    try:
                        rows = task_service.list_tasks(owner_id, "all", limit=20)
                    except RuntimeError:
                        rows = []
                    await send_event({
                        "type": "task_snapshot",
                        "task_protocol_version": 1,
                        "tasks": rows,
                        "error_code": str(error)[:80],
                    })
            elif kind == "stop":
                break
    finally:
        if quota_watcher_task is not None:
            quota_watcher_task.cancel()
            await asyncio.gather(quota_watcher_task, return_exceptions=True)
        if screen_sync_task is not None:
            screen_sync_task.cancel()
            await asyncio.gather(screen_sync_task, return_exceptions=True)
        remember("user", last_user_transcript)
        await cancel_non_blocking_tools("session_closed")
        if pending is not None and pending.task_id and task_service is not None:
            task_service.update(
                pending.task_id, status="waiting_input", stage="session_closed",
                error_code="voice_session_closed",
                result_summary="語音模式已關閉，請下次開啟後再確認。",
                expected_statuses={"waiting_confirmation"},
                expected_action_id=pending.confirmation_id,
            )
        cancel_confirmation_timeout()
        if background_flush_task is not None:
            background_flush_task.cancel()
        if task_watcher_task is not None:
            task_watcher_task.cancel()
            await asyncio.gather(task_watcher_task, return_exceptions=True)
        stopped = True
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await live.close()
