from __future__ import annotations

import asyncio
import base64
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import WebSocket
from .language import display_transcript
from .capabilities import CATALOG, ACTIONS, available_actions
from .contextual import bind_target, safe_result

from .contracts import (
    VoiceProposal,
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
)


SendEvent = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass
class DuplexConfirmation:
    confirmation_id: str
    proposal: VoiceProposal
    phrase: str
    scope: str
    revision: int
    expires_at: float


@dataclass(frozen=True)
class PendingToolResult:
    call_id: str
    name: str


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
        arguments["count"] = raw.get("count")
    elif name == "read_calendar":
        intent = "calendar.query"
        arguments["range"] = raw.get("range")
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
        arguments["target"] = raw.get("target")
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
        intent = "contacts.query"
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
        intent = "chat.open"
        arguments["contact_name"] = raw.get("contact_name")
    elif name == "send_chat_message":
        intent = "chat.request_send"
        arguments = {
            "contact_name": raw.get("contact_name"),
            "message": raw.get("message"),
        }
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
) -> None:
    """Bridge one app WebSocket to one persistent, full-duplex Gemini session."""

    context = safe_context(initial_context)
    live = provider.create_duplex_session(
        voice_config=context.get("voice_config"),
    )
    await live.connect()
    pending: DuplexConfirmation | None = None
    awaiting_results: dict[str, PendingToolResult] = {}
    state_lock = asyncio.Lock()
    stopped = False
    current_response_id: str | None = None
    current_sequence = 0
    current_transcript = ""
    current_audio_allowed: bool | None = None
    last_user_transcript = ""
    next_reply_code = "conversation"
    close_after_turn = False
    first_chunk: bytes | None = None
    last_completed_first_chunk: bytes | None = None
    checking_replayed_turn = False
    suppress_current_turn = False
    seen_tool_calls: set[str] = set()
    tool_response_cache: dict[str, tuple[str, dict[str, Any]]] = {}
    background_actions: dict[str, VoiceProposal] = {}
    queued_background_results: list[tuple[str | dict[str, Any], str]] = []
    progress_turn_pending = False
    private_read_authorized = False
    last_delegated_proposal: VoiceProposal | None = None
    last_delegated_expires_at = 0.0
    started = time.monotonic()

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
        if call_id:
            tool_response_cache[call_id] = (name, dict(response))
        await live.send_tool_response(
            call_id=call_id,
            name=name,
            response=response,
        )

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
            "private_confirm": {
                "zh-TW": "這次會讀取你看得到的聊天內容，要繼續請說「確認」。",
                "zh-CN": "这次会读取你可以看到的聊天内容，要继续请说“确认”。",
                "en-US": 'This will read chat content you can access. To continue, say "confirm".',
            },
        }
        return prompts[kind].get(language, prompts[kind]["zh-TW"])

    async def handle_function(call: Any) -> None:
        nonlocal pending, next_reply_code, close_after_turn, progress_turn_pending
        nonlocal private_read_authorized
        nonlocal last_delegated_proposal, last_delegated_expires_at
        name = str(call.name or "")
        call_id = str(call.id or "")
        if call_id in tool_response_cache:
            cached_name, cached_response = tool_response_cache[call_id]
            await live.send_tool_response(
                call_id=call_id,
                name=cached_name,
                response=cached_response,
            )
            return
        if call_id and call_id in seen_tool_calls:
            return
        if call_id:
            seen_tool_calls.add(call_id)
        visible_action = visible_choice_action(last_user_transcript)
        visible_status = dict(context.get("feature_status") or {})
        if (
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
            await tool_response(call, {
                "status": "ok",
                "scope": str(context.get("scope") or "global"),
                "revision": context.get("revision", 0),
                "screen": context.get("screen", {}),
                "media_count": int(context.get("media_count") or 0),
                "can_publish": context.get("can_publish") is True,
                "message": "只描述安全畫面狀態，不推測畫面文字或私人內容。",
            })
            return
        if name in {
            "select_screen_target",
            "read_shared_dates", "respond_date_invitation", "update_shared_date", "confirm_shared_date",
            "navigate_app",
            "open_app_page",
            "patch_profile",
            "request_profile_save",
            "set_app_setting",
            "write_post_caption",
            "select_recent_post_photos",
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
            "activate_visible_choice",
            "list_contacts",
            "read_self_profile",
            "read_memories",
            "add_memory",
            "open_chat",
            "send_chat_message",
        }:
            if pending is not None:
                await tool_response(call, {
                    "status": "awaiting_confirmation",
                    "phrase": pending.phrase,
                    "spoken_prompt": localized_prompt("confirm"),
                    "message": (
                        "不得重新建立原操作。若使用者剛說確認，"
                        "只呼叫 confirm_pending_action。"
                    ),
                })
                return
            proposal = _proposal_from_function(
                call,
                revision=int(context.get("revision") or 0),
            )
            # Correct routing for a current, explicit memory question even if
            # Live mistakenly delegates it to the public persona.
            direct = deterministic_proposal(last_user_transcript, context=context)
            if direct is not None and direct.intent == "memory.query" and name in {
                "ask_public_ayue", "ask_matching_ayue", "read_self_profile", "read_memories",
            }:
                proposal = direct
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
            bound, target_error = bind_target(proposal.intent, proposal.arguments, context)
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
            }:
                action_id = str(call.id or uuid.uuid4().hex)
                background_actions[action_id] = proposal
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
                    "scope": pending.scope,
                    "base_revision": pending.revision,
                    "expires_at": pending.expires_at,
                })
                await tool_response(call, {
                    "status": "confirmation_required",
                    "phrase": phrase,
                    "spoken_prompt": localized_prompt("confirm"),
                    "message": "只能逐字朗讀 spoken_prompt 一次。",
                })
                return
            action_id = str(call.id or uuid.uuid4().hex)
            awaiting_results[action_id] = PendingToolResult(
                call_id=str(call.id or action_id),
                name=name,
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
            spoken = str((call.args or {}).get("spoken_phrase") or "")[:200]
            if pending is None:
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
                pending = None
                await tool_response(call, {
                    "status": "confirmation_expired",
                    "message": "確認已逾時，沒有執行變更。",
                })
                return
            if (
                context.get("scope") != pending.scope
                or context.get("revision") != pending.revision
            ):
                pending = None
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
                    "message": localized_prompt("confirm"),
                })
                return
            confirmed = pending
            pending = None
            if confirmed.proposal.intent == "ayue.private_query":
                private_read_authorized = True
            action_id = confirmed.confirmation_id
            if confirmed.proposal.intent == "ayue.private_query":
                background_actions[action_id] = confirmed.proposal
                progress_turn_pending = True
                next_reply_code = "ayue_delegated"
            else:
                awaiting_results[action_id] = PendingToolResult(
                    call_id=str(call.id or action_id),
                    name=name,
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
            })
            if confirmed.proposal.intent == "ayue.private_query":
                await tool_response(call, {
                    "status": "working",
                    "spoken_prompt": localized_prompt("working"),
                    "message": "私人聊天讀取已在本 session 授權。",
                })
            return

        if name == "cancel_current_action":
            pending = None
            next_reply_code = "cancelled"
            await tool_response(call, {
                "status": "cancelled",
                "message": "待確認操作已取消。",
            })
            return

        if name == "close_voice_mode":
            pending = None
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

    async def deliver_background_result(message: str | dict[str, Any], source: str) -> None:
        rendered = json.dumps(message, ensure_ascii=False) if isinstance(message, dict) else safe_reply(message)[:1200]
        await live.send_text(
            "[DELEGATED_AYUE_RESULT]\n"
            f"source={source}\n"
            f"{rendered}\n"
            "這是已完成的工具結果。請理解重點後，直接以阿月第一人稱自然回答使用者，"
            "不要逐字照念、不要說你在轉述、不要提到有多個阿月，"
            "也不要增加結果沒有的事實或承諾。",
        )

    async def handle_live_message(response: Any) -> bool:
        nonlocal current_response_id, current_sequence
        nonlocal current_transcript, current_audio_allowed
        nonlocal last_user_transcript, next_reply_code, close_after_turn
        nonlocal first_chunk, last_completed_first_chunk
        nonlocal checking_replayed_turn, suppress_current_turn
        nonlocal progress_turn_pending

        if response.go_away:
            await send_event({"type": "state", "state": "reconnecting"})
            return True

        if response.voice_activity:
            activity = str(response.voice_activity.voice_activity_type or "")
            if activity.endswith("ACTIVITY_START"):
                last_user_transcript = ""
                await send_event({
                    "type": "microphone_state",
                    "state": "speech_started",
                })
            elif activity.endswith("ACTIVITY_END"):
                await send_event({
                    "type": "microphone_state",
                    "state": "speech_ended",
                })

        content = response.server_content
        if content:
            if checking_replayed_turn and content.turn_complete and not content.model_turn:
                checking_replayed_turn = False
            interim = content.interim_input_transcription
            if interim and interim.text and not suppress_current_turn:
                await send_event({
                    "type": "user_transcript",
                    "text": display_transcript(str(interim.text)),
                    "final": False,
                })
            incoming = content.input_transcription
            if incoming and incoming.text and not suppress_current_turn:
                last_user_transcript = _append_transcript(
                    last_user_transcript,
                    str(incoming.text),
                )
                await send_event({
                    "type": "user_transcript",
                    "text": display_transcript(last_user_transcript),
                    "final": incoming.finished is True,
                })
            if content.interrupted:
                await reset_output(interrupted=True)
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
                    if queued_background_results:
                        queued_message, queued_source = queued_background_results.pop(0)
                        await deliver_background_result(queued_message, queued_source)
                if close_after_turn:
                    close_after_turn = False
                    await send_event({"type": "state", "state": "close_requested"})

        if response.tool_call:
            for call in response.tool_call.function_calls or []:
                async with state_lock:
                    await handle_function(call)
        if response.tool_call_cancellation:
            cancelled = set(response.tool_call_cancellation.ids or [])
            for action_id, tool in list(awaiting_results.items()):
                if tool.call_id in cancelled:
                    awaiting_results.pop(action_id, None)
        return False

    async def reconnect_live() -> None:
        nonlocal pending, checking_replayed_turn
        resumed = await live.reconnect()
        checking_replayed_turn = resumed
        if not resumed:
            async with state_lock:
                pending = None
                awaiting_results.clear()
        await send_event({
            "type": "state",
            "state": "duplex_ready",
            "microphone": "streaming",
            "resumed": resumed,
            "confirmations_preserved": resumed,
        })

    async def receive_live() -> None:
        reconnect_attempts = 0
        while not stopped:
            try:
                should_reconnect = False
                async for response in live.receive_turn():
                    should_reconnect = await handle_live_message(response)
                    if should_reconnect:
                        break
                if should_reconnect:
                    await reconnect_live()
                reconnect_attempts = 0
            except asyncio.CancelledError:
                raise
            except Exception:
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
                try:
                    await reconnect_live()
                except Exception:
                    continue

    receiver = asyncio.create_task(receive_live())
    try:
        await live.send_text(
            "[VOICE_SESSION_STARTED]\n"
            f"user_display_name={str((context.get('voice_config') or {}).get('self_name') or '')[:40]}\n"
            "語音模式已就緒，請用一個很短的句子主動打招呼；有安全名稱時可以自然稱呼。",
        )
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
            if kind == "context_changed":
                next_context = safe_context(control.get("context"))
                async with state_lock:
                    if pending and (
                        next_context["scope"] != pending.scope
                        or next_context["revision"] != pending.revision
                        or (
                            pending.proposal.arguments.get("target_ref")
                            and bind_target(pending.proposal.intent, pending.proposal.arguments, next_context)[1] is not None
                        )
                    ):
                        pending = None
                    context = next_context
            elif kind == "action_result":
                action_id = str(control.get("action_id") or "")
                if action_id in background_actions:
                    proposal = background_actions.pop(action_id)
                    source = "private" if proposal.intent == "ayue.private_query" else "public"
                    result_text = str(control.get("message") or "")[:1200]
                    if control.get("success") is not True:
                        result_text = result_text or "目前暫時無法回覆。"
                    else:
                        last_delegated_proposal = proposal
                        last_delegated_expires_at = time.time() + 120
                    if control.get("result_version") == 1:
                        result_text = safe_result(control)
                    if progress_turn_pending or current_response_id is not None:
                        queued_background_results.append((result_text, source))
                    else:
                        await deliver_background_result(result_text, source)
                    continue
                async with state_lock:
                    target = awaiting_results.pop(action_id, None)
                if target:
                    success = control.get("success") is True
                    next_reply_code = "action_completed" if success else "action_failed"
                    result_response = safe_result(control)
                    tool_response_cache[target.call_id] = (
                        target.name,
                        dict(result_response),
                    )
                    await live.send_tool_response(
                        call_id=target.call_id,
                        name=target.name,
                        response=result_response,
                    )
            elif kind == "stop":
                break
    finally:
        stopped = True
        receiver.cancel()
        await asyncio.gather(receiver, return_exceptions=True)
        await live.close()
