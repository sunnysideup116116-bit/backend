from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from registration_voice.settings import collect_google_api_keys

from .contracts import (
    VoiceProposal,
    confirmation_matches,
    confirmation_phrase,
    context_allows_proposal,
    normalized_phrase,
    requires_confirmation,
    safe_context,
)
from .duplex_runtime import run_duplex_session
from .contextual import bind_target
from .capabilities import CATALOG
from .limiter import (
    AppVoiceLimiter,
    AppVoiceTicket,
    AppVoiceTicketStore,
    fingerprint,
    new_process_secret,
)
from .memory import (
    AppwriteVoiceMemoryService,
    VoiceMemoryError,
    VoiceMemoryRecord,
    VoiceOwner,
)
from .provider import AppVoiceProvider
from .settings import AppVoiceSettings
from .weather import GoogleVoiceWeatherService, resolve_weather_location
from .task_service import (
    VoiceTaskService,
    start_voice_task_worker,
    stop_voice_task_worker,
)
from .template_dispatcher import TEMPLATE_TOOL_COUNT


LOGGER = logging.getLogger("app_voice.runtime")


def _saved_weather_location(user_id: str) -> str:
    """Read the coarse manual profile location from the in-process Social DB."""
    database = sys.modules.get("database")
    profiles = getattr(database, "profiles_coll", None) if database else None
    if profiles is None:
        return ""
    try:
        profile = profiles.find_one(
            {"user_id": user_id}, {"_id": 0, "profile_location": 1},
        ) or {}
    except Exception:
        return ""
    raw = profile.get("profile_location")
    if not isinstance(raw, dict):
        return ""
    city = re.sub(r"\s+", "", str(raw.get("city") or "")).strip("，,。")[:20]
    district = re.sub(
        r"\s+", "", str(raw.get("district") or ""),
    ).strip("，,。")[:20]
    location = f"{city}{district}".strip()
    return location[:80] if len(location) >= 2 else ""


def _authenticate_appwrite_owner(authorization: str) -> str:
    try:
        from services.appwrite_identity_service import authenticate_owner
    except ImportError:
        # Isolated app-voice tests use protocol v3 unless an authenticator is injected.
        raise VoiceMemoryError("voice_identity_unavailable")
    try:
        return str(authenticate_owner(authorization) or "")
    except Exception as error:
        code = str(getattr(error, "code", "voice_identity_unavailable"))
        raise VoiceMemoryError(code) from error


class AppVoiceSessionRequest(BaseModel):
    installation_id: str = Field(min_length=16, max_length=128)
    user_id: str = Field(min_length=1, max_length=128)
    consent_version: str = Field(min_length=1, max_length=80)
    consent_accepted_at: str = Field(min_length=10, max_length=64)
    input_mode: str = Field(default="on_device_text", max_length=40)
    output_mode: str = Field(default="gemini_live_duplex", max_length=40)
    client_protocol_version: int = Field(default=3, ge=1, le=4)


class AppVoiceTaskCancelRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)


class AppVoiceTaskInputRequest(BaseModel):
    expected_revision: int | None = Field(default=None, ge=0)
    values: dict[str, Any] = Field(min_length=1, max_length=8)


@dataclass
class PendingConfirmation:
    confirmation_id: str
    proposal: VoiceProposal
    phrase: str
    scope: str
    revision: int
    expires_at: float


class AppVoiceRuntime:
    def __init__(
        self,
        *,
        settings: AppVoiceSettings | None = None,
        keys: list[str] | None = None,
        provider: AppVoiceProvider | None = None,
        memory_service: Any | None = None,
        weather_service: Any | None = None,
        weather_location_provider: Any | None = None,
        task_service: VoiceTaskService | None = None,
        defer_task_service: bool = False,
        identity_authenticator: Any | None = None,
    ):
        self.settings = settings or AppVoiceSettings.from_env()
        resolved_keys = collect_google_api_keys() if keys is None else keys
        self.provider = provider or AppVoiceProvider(self.settings, list(resolved_keys))
        self.tickets = AppVoiceTicketStore(self.settings.ticket_ttl_seconds)
        self.limiter = AppVoiceLimiter(self.settings)
        self.secret = new_process_secret()
        self.memory = (
            memory_service
            if memory_service is not None
            else AppwriteVoiceMemoryService.from_env()
            if self.settings.memory_enabled
            else None
        )
        self._memory_tasks: set[asyncio.Task[Any]] = set()
        self.weather = (
            weather_service
            if weather_service is not None
            else GoogleVoiceWeatherService.from_env_or_none()
        )
        self.weather_location_provider = weather_location_provider
        self.task_service_injected = task_service is not None
        self._task_service_deferred = bool(defer_task_service and task_service is None)
        if task_service is not None:
            self.tasks = task_service
        elif self._task_service_deferred:
            self.tasks = VoiceTaskService(
                None,
                None,
                enabled=False,
                worker_count=self.settings.task_workers,
                per_user_concurrency=self.settings.task_per_user_concurrency,
            )
        else:
            self.tasks = VoiceTaskService.from_database(
                enabled=self.settings.tasks_enabled,
                worker_count=self.settings.task_workers,
                per_user_concurrency=self.settings.task_per_user_concurrency,
            )
        self.identity_authenticator = identity_authenticator or _authenticate_appwrite_owner
        self.identity_authenticator_injected = identity_authenticator is not None

    def initialize_task_service(self) -> None:
        if not self._task_service_deferred:
            return
        self.tasks = VoiceTaskService.from_database(
            enabled=self.settings.tasks_enabled,
            worker_count=self.settings.task_workers,
            per_user_concurrency=self.settings.task_per_user_concurrency,
        )
        self._task_service_deferred = False

    def queue_memory_finalize(
        self, ticket: AppVoiceTicket, turns: list[dict[str, str]],
    ) -> None:
        if self.memory is None or not ticket.session_id or not ticket.username:
            return
        owner = VoiceOwner(ticket.user_id, ticket.username)
        turn_count = len(turns)
        task = asyncio.create_task(
            asyncio.to_thread(
                self.memory.finalize_session,
                owner,
                ticket.session_id,
                list(turns),
            )
        )
        self._memory_tasks.add(task)

        def finished(done: asyncio.Task[Any]) -> None:
            self._memory_tasks.discard(done)
            if done.cancelled():
                return
            try:
                result = done.result()
                saved = result.last_session_id == ticket.session_id
                print(
                    "[APP_VOICE_MEMORY] "
                    f"{'saved' if saved else 'skipped'} "
                    f"revision={result.revision} turns={turn_count}"
                )
            except Exception as error:
                code = getattr(error, "code", type(error).__name__)
                print(f"[APP_VOICE_MEMORY] finalize failed: {code}")

        task.add_done_callback(finished)

    async def wait_memory_tasks(self) -> None:
        if self._memory_tasks:
            await asyncio.gather(*tuple(self._memory_tasks), return_exceptions=True)

    def client_ip(self, connection: Request | WebSocket) -> str:
        forwarded = connection.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()[:80]
        return str(connection.client.host if connection.client else "unknown")[:80]

    def origin_allowed(self, websocket: WebSocket) -> bool:
        origin = (websocket.headers.get("origin") or "").strip().lower()
        if not origin:
            return True
        if origin in {
            "https://service.misproject.us.ci",
            "http://127.0.0.1:4173",
            "http://localhost:4173",
        }:
            return True
        configured = {
            item.strip().lower()
            for item in os.getenv("CORS_ORIGINS", "").split(",")
            if item.strip()
        }
        return origin in configured or bool(re.fullmatch(r"https://[a-z0-9.-]+\.github\.io", origin))


def _setting_reply(proposal: VoiceProposal) -> str:
    key = proposal.arguments.get("key")
    enabled = proposal.arguments.get("enabled") is True
    label = {
        "notifications.global": "訊息通知",
        "location.enabled": "定位",
        "ai.proactive_care": "AI 主動關心",
        "ui.liquid_glass": "流體玻璃導覽列",
        "ui.dark_mode": "深色模式",
    }.get(key, "設定")
    return f"{label}將{'開啟' if enabled else '關閉'}。"


def _bearer_token(request: Request) -> str:
    value = str(request.headers.get("authorization") or "").strip()
    scheme, separator, token = value.partition(" ")
    if separator and scheme.lower() == "bearer" and token.strip():
        return token.strip()
    return ""


def _confirmation_preview(proposal: VoiceProposal, context: dict[str, Any]) -> str:
    if proposal.intent == "date.confirm":
        contact = str(proposal.arguments.get("contact_name") or "目前對象")[:40]
        return f"要確認你和{contact}的共同約會安排，請說「確認安排」。"
    if proposal.intent == "date.update":
        contact = str(proposal.arguments.get("contact_name") or "目前對象")[:40]
        return f"要套用你和{contact}的共同約會修改，請說「確認修改共同約會」。"
    if proposal.intent == "date.respond":
        contact = str(proposal.arguments.get("contact_name") or "目前對象")[:40]
        decision = "接受" if proposal.arguments.get("accepted") is True else "拒絕"
        return f"要{decision}{contact}的約會邀請，請說「{confirmation_phrase(proposal)}」。"
    if proposal.intent == "calendar.create":
        title = str(proposal.arguments.get("title") or "行程")[:80]
        event_date = str(proposal.arguments.get("date") or "")
        start_time = str(proposal.arguments.get("start_time") or "")
        return f"要新增「{title}」，時間 {event_date} {start_time}，請說「確認新增行程」。"
    if proposal.intent == "calendar.update":
        target = str(proposal.arguments.get("target") or "這個行程")[:80]
        return f"要修改「{target}」，請說「確認修改行程」。"
    if proposal.intent == "calendar.cancel":
        target = str(proposal.arguments.get("target") or "這個行程")[:80]
        return f"要取消「{target}」，請說「確認取消行程」。"
    if proposal.intent == "post.request_publish":
        count = int(context.get("media_count") or 0)
        return (
            "你還沒有選擇圖片，請先自行選擇一到五張圖片。"
            if count == 0
            else f"目前有 {count} 張圖片。要發布請說「確認發布」。"
        )
    if proposal.intent == "profile.request_commit":
        return "要儲存目前的個人資料變更，請說「確認儲存個人資料」。"
    if proposal.intent == "chat.request_send":
        contact = str(proposal.arguments.get("contact_name") or "對方")[:40]
        return f"要把目前訊息傳給{contact}，請說「確認傳送訊息」。"
    if proposal.intent == "memory.add":
        label = str(proposal.arguments.get("label") or "這件事")[:40]
        return f"要把「{label}」加入阿月記憶，請說「確認新增阿月記憶」。"
    return f"{_setting_reply(proposal)}請說「{confirmation_phrase(proposal)}」確認。"


def create_router(runtime: AppVoiceRuntime) -> APIRouter:
    app_router = APIRouter(prefix="/api/app-voice", tags=["App Voice Assistant"])

    async def verify_owner(authorization: str) -> str:
        if runtime.identity_authenticator_injected:
            return str(runtime.identity_authenticator(authorization) or "")
        return str(await asyncio.to_thread(runtime.identity_authenticator, authorization) or "")

    async def run_task_call(function: Any, *args: Any, **kwargs: Any) -> Any:
        if runtime.task_service_injected:
            return function(*args, **kwargs)
        return await asyncio.to_thread(function, *args, **kwargs)

    @app_router.get("/capability")
    async def capability() -> dict[str, Any]:
        configured_routing_mode = runtime.settings.tool_routing_mode
        max_model_tools = (
            TEMPLATE_TOOL_COUNT
            if configured_routing_mode == "template"
            else 7
            if configured_routing_mode == "proxy"
            else 36
        )
        return {
            "enabled": runtime.settings.enabled,
            "protocol_version": 4,
            "action_catalog_version": CATALOG["version"],
            "screen_context_version": 1,
            "structured_action_results": True,
            "tool_routing_version": 2,
            "task_protocol_version": 1,
            "task_interactions": True,
            "guide_protocol_version": 1,
            "personal_routines": True,
            "authenticated_session_required": True,
            "max_model_tools": max_model_tools,
            "voice_runtime_template": configured_routing_mode == "template",
            "max_operations_per_request": 8,
            "task_service": runtime.tasks.enabled,
            "demo_only": runtime.settings.demo_only,
            "session_memory": runtime.memory is not None,
            "session_memory_storage": "appwrite_internal" if runtime.memory else "disabled",
            "session_memory_max_chars": 200 if runtime.memory else 0,
            "session_memory_recent_chars": 120 if runtime.memory else 0,
            "voice_weather": runtime.weather is not None,
            "voice_weather_sources": (
                ["google_weather", "google_air_quality"]
                if runtime.weather is not None
                else []
            ),
            "android_on_device_stt": True,
            "gemini_fallback_available": runtime.provider.gemini_available,
            "gemini_live_audio": runtime.provider.gemini_available,
            "full_duplex_live": runtime.provider.gemini_available,
            "persistent_live_session": runtime.provider.gemini_available,
            "server_vad": runtime.provider.gemini_available,
            "session_resumption": runtime.provider.gemini_available,
            "live_function_calling": runtime.provider.gemini_available,
            "wake_word": "android_foreground",
            "voice_options": [
                "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda",
                "Orus", "Aoede", "Callirrhoe", "Autonoe", "Enceladus",
                "Iapetus", "Umbriel", "Algieba", "Despina", "Erinome",
                "Algenib", "Rasalgethi", "Laomedeia", "Achernar", "Alnilam",
                "Schedar", "Gacrux", "Pulcherrima", "Achird",
                "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager",
                "Sulafat",
            ],
            "speech_speed_options": ["slow", "normal", "fast"],
            "response_language_options": ["zh-TW", "zh-CN", "en-US"],
            "structured_confirmation": True,
            "barge_in": True,
            "tts_modes": ["gemini_live_duplex", "gemini_live", "gemini_tts"],
            "default_tts_mode": "gemini_live_duplex",
            "supported_languages": ["zh-TW"],
            "max_session_seconds": runtime.settings.max_session_seconds,
            "consent_version": runtime.settings.consent_version,
            "supported_intents": [
                "animated_navigation", "screen_read", "profile", "settings",
                "post_draft", "post_publish_confirmation", "recent_gallery",
                "public_ayue_matching", "public_ayue_calendar",
                "public_ayue_web", "public_ayue_places", "public_ayue_memory",
                "private_ayue_relationship", "chat_open", "chat_history_search",
                "contact_list", "chat_send",
                "direct_calendar_read", "direct_match_status",
                "direct_calendar_create", "direct_calendar_update",
                "direct_calendar_cancel",
                "direct_match_hub_read", "voice_match_hub_decision",
                "voice_personality_exploration", "visible_choice_action",
                "direct_self_profile", "direct_memory_read",
                "direct_memory_add",
                "direct_shared_dates", "date_invitation_response", "shared_date_form_update",
                "shared_date_form_confirm",
                "current_weather_and_air_quality",
                "guided_app_help", "voice_permission_repair",
                "unified_pending_digest", "authorized_app_search",
                "fixed_workflows", "personal_voice_routines",
                "structured_task_interactions",
            ],
        }

    @app_router.post("/session")
    async def create_session(req: AppVoiceSessionRequest, request: Request) -> dict[str, Any]:
        if not runtime.settings.enabled:
            raise HTTPException(status_code=503, detail="app_voice_unavailable")
        if req.consent_version != runtime.settings.consent_version:
            raise HTTPException(status_code=409, detail="app_voice_consent_mismatch")
        user_id = req.user_id
        username = ""
        memory = VoiceMemoryRecord()
        authenticated_owner = ""
        if runtime.memory is not None:
            try:
                owner, memory = await asyncio.to_thread(
                    runtime.memory.authenticate_and_load,
                    _bearer_token(request),
                    req.user_id,
                )
            except VoiceMemoryError as error:
                status = 401 if error.code in {
                    "voice_memory_jwt_required",
                    "voice_memory_authentication_failed",
                    "voice_memory_user_mismatch",
                } else 503
                raise HTTPException(status_code=status, detail=error.code) from error
            user_id = owner.user_id
            authenticated_owner = owner.user_id
            username = owner.username
            print(
                "[APP_VOICE_MEMORY] loaded "
                f"revision={memory.revision} "
                f"chars={len(memory.older_summary) + len(memory.recent_summary)}"
            )
        elif req.client_protocol_version >= 4:
            try:
                authenticated_owner = await verify_owner(
                    str(request.headers.get("authorization") or ""),
                )
            except VoiceMemoryError as error:
                code = str(getattr(error, "code", "voice_identity_unavailable"))
                status = 401 if code in {
                    "appwrite_jwt_required", "appwrite_authentication_failed",
                    "voice_memory_jwt_required", "voice_memory_authentication_failed",
                } else 503
                raise HTTPException(status_code=status, detail=code) from error
            if not authenticated_owner or not hmac.compare_digest(
                authenticated_owner, str(req.user_id),
            ):
                raise HTTPException(status_code=401, detail="voice_identity_user_mismatch")
            user_id = authenticated_owner
        if not runtime.settings.allows_user(user_id):
            raise HTTPException(status_code=403, detail="app_voice_demo_account_required")
        ip = runtime.client_ip(request)
        identity = fingerprint(runtime.secret, user_id, req.installation_id, ip)
        if not runtime.limiter.allow_session(identity):
            raise HTTPException(status_code=429, detail="app_voice_rate_limit")
        ip_hash = fingerprint(runtime.secret, "app-voice-ip", ip)
        protocol_version = 4 if req.client_protocol_version >= 4 else 3
        routing_mode = (
            runtime.settings.routing_mode_for_user(user_id)
            if protocol_version >= 4 else "legacy"
        )
        ticket, ttl = runtime.tickets.issue(
            identity,
            user_id,
            ip_hash,
            username=username,
            session_id=uuid.uuid4().hex,
            memory_older_summary=memory.older_summary,
            memory_recent_summary=memory.recent_summary,
            protocol_version=protocol_version,
            routing_mode=routing_mode,
        )
        return {
            "ticket": ticket,
            "expires_in_seconds": ttl,
            "websocket_url": "wss://service.misproject.us.ci/api/app-voice",
            "max_duration_seconds": runtime.settings.max_session_seconds,
            "demo_only": runtime.settings.demo_only,
            "protocol_version": protocol_version,
            "tool_routing_mode": routing_mode,
        }

    async def authenticated_task_owner(request: Request) -> str:
        try:
            owner = await verify_owner(str(request.headers.get("authorization") or ""))
        except VoiceMemoryError as error:
            code = str(getattr(error, "code", "voice_identity_unavailable"))
            status = 401 if code in {
                "appwrite_jwt_required", "appwrite_authentication_failed",
                "voice_memory_jwt_required", "voice_memory_authentication_failed",
            } else 503
            raise HTTPException(status_code=status, detail=code) from error
        if not owner:
            raise HTTPException(status_code=401, detail="voice_identity_invalid")
        return owner

    @app_router.get("/tasks")
    async def list_voice_tasks(
        request: Request, filter: str = "active", limit: int = 20,
    ) -> dict[str, Any]:
        owner = await authenticated_task_owner(request)
        if filter not in {"active", "recent", "all"}:
            raise HTTPException(status_code=400, detail="voice_task_filter_invalid")
        if not runtime.tasks.enabled:
            return {"task_protocol_version": 1, "tasks": []}
        try:
            tasks = await run_task_call(
                runtime.tasks.list_tasks, owner, filter, limit=max(1, min(limit, 20)),
            )
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"task_protocol_version": 1, "tasks": tasks}

    @app_router.post("/tasks/{task_ref}/cancel")
    async def cancel_voice_task(
        task_ref: str, body: AppVoiceTaskCancelRequest, request: Request,
    ) -> dict[str, Any]:
        owner = await authenticated_task_owner(request)
        if not re.fullmatch(r"(?:task|batch)_[0-9a-f]{32}", task_ref):
            raise HTTPException(status_code=404, detail="voice_task_not_found")
        try:
            tasks = await run_task_call(
                runtime.tasks.cancel,
                owner,
                task_ref,
                expected_revision=body.expected_revision,
            )
        except ValueError as error:
            code = str(error)
            status = 409 if code == "voice_task_revision_stale" else 404
            raise HTTPException(status_code=status, detail=code) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"status": "ok", "tasks": tasks}

    def task_error(error: ValueError) -> HTTPException:
        code = str(error)
        if code == "voice_task_not_found":
            return HTTPException(status_code=404, detail=code)
        if code == "voice_task_revision_stale":
            return HTTPException(status_code=409, detail=code)
        return HTTPException(status_code=400, detail=code)

    @app_router.post("/tasks/{task_ref}/retry")
    async def retry_voice_task(
        task_ref: str, body: AppVoiceTaskCancelRequest, request: Request,
    ) -> dict[str, Any]:
        owner = await authenticated_task_owner(request)
        try:
            row = await run_task_call(
                runtime.tasks.retry, owner, task_ref,
                expected_revision=body.expected_revision,
            )
        except ValueError as error:
            raise task_error(error) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"status": "ok", "task": runtime.tasks.project(row)}

    @app_router.post("/tasks/{task_ref}/input")
    async def provide_voice_task_input(
        task_ref: str, body: AppVoiceTaskInputRequest, request: Request,
    ) -> dict[str, Any]:
        owner = await authenticated_task_owner(request)
        try:
            row = await run_task_call(
                runtime.tasks.provide_input, owner, task_ref, body.values,
                expected_revision=body.expected_revision,
            )
        except ValueError as error:
            raise task_error(error) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"status": "ok", "task": runtime.tasks.project(row)}

    @app_router.post("/tasks/{task_ref}/dismiss")
    async def dismiss_voice_task(
        task_ref: str, body: AppVoiceTaskCancelRequest, request: Request,
    ) -> dict[str, Any]:
        owner = await authenticated_task_owner(request)
        try:
            row = await run_task_call(
                runtime.tasks.dismiss, owner, task_ref,
                expected_revision=body.expected_revision,
            )
        except ValueError as error:
            raise task_error(error) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {"status": "ok", "task": runtime.tasks.project(row)}

    @app_router.post("/tasks/{task_ref}/undo")
    async def undo_voice_task(
        task_ref: str, body: AppVoiceTaskCancelRequest, request: Request,
    ) -> dict[str, Any]:
        owner = await authenticated_task_owner(request)
        try:
            created = await run_task_call(
                runtime.tasks.create_undo_batch,
                owner,
                task_ref,
                session_id=f"rest-{uuid.uuid4().hex}",
                expected_revision=body.expected_revision,
            )
        except ValueError as error:
            raise task_error(error) from error
        except RuntimeError as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return {
            "status": "queued",
            "batch_ref": created.batch.get("batch_ref"),
            "tasks": [runtime.tasks.project(row) for row in created.tasks],
        }

    @app_router.websocket("")
    async def app_voice(websocket: WebSocket) -> None:
        if not runtime.origin_allowed(websocket):
            await websocket.close(code=1008, reason="origin_not_allowed")
            return
        await websocket.accept()
        acquired = False
        live_audio_task: asyncio.Task[None] | None = None
        ticket: AppVoiceTicket | None = None
        memory_turns: list[dict[str, str]] = []
        try:
            try:
                hello = json.loads(await asyncio.wait_for(websocket.receive_text(), timeout=5))
            except (asyncio.TimeoutError, TypeError, ValueError, WebSocketDisconnect):
                await websocket.close(code=1008, reason="invalid_hello")
                return
            if hello.get("type") != "hello":
                await websocket.close(code=1008, reason="invalid_hello")
                return
            ticket = runtime.tickets.consume(str(hello.get("ticket") or ""))
            if ticket is None:
                await websocket.close(code=1008, reason="invalid_ticket")
                return
            try:
                client_protocol_version = int(
                    hello.get("client_protocol_version") or 3,
                )
            except (TypeError, ValueError):
                await websocket.close(code=1008, reason="protocol_mismatch")
                return
            if client_protocol_version != ticket.protocol_version:
                await websocket.close(code=1008, reason="protocol_mismatch")
                return
            current_ip = fingerprint(runtime.secret, "app-voice-ip", runtime.client_ip(websocket))
            if not hmac.compare_digest(ticket.ip_fingerprint, current_ip):
                await websocket.close(code=1008, reason="ticket_client_mismatch")
                return
            if not runtime.limiter.acquire():
                await websocket.send_json({"type": "error", "code": "app_voice_concurrency_limit"})
                await websocket.close(code=1013)
                return
            acquired = True
            output_mode = str(hello.get("output_mode") or "gemini_tts")
            input_mode = str(hello.get("input_mode") or "on_device_text")
            memory_record = VoiceMemoryRecord(
                older_summary=ticket.memory_older_summary,
                recent_summary=ticket.memory_recent_summary,
            )
            memory_prompt = memory_record.prompt_text()

            def session_context(value: Any) -> dict[str, Any]:
                result = safe_context(value)
                voice_config = dict(result.get("voice_config") or {})
                if ticket and ticket.username:
                    voice_config["self_name"] = ticket.username
                result["voice_config"] = voice_config
                if memory_prompt:
                    result["conversation_memory"] = memory_prompt
                return result

            def remember(role: str, value: Any) -> None:
                text = str(value or "").strip()[:2000]
                if role not in {"user", "assistant"} or not text:
                    return
                item = {"role": role, "content": text}
                if memory_turns and memory_turns[-1] == item:
                    return
                memory_turns.append(item)
                if len(memory_turns) > 100:
                    del memory_turns[0]

            context = session_context(hello.get("context"))
            pending: PendingConfirmation | None = None
            delegated_actions: dict[str, VoiceProposal] = {}
            last_delegated_proposal: VoiceProposal | None = None
            last_delegated_expires_at = 0.0
            audio_buffer = bytearray()
            send_lock = asyncio.Lock()
            live_audio_task = None
            live_response_id: str | None = None
            started = time.monotonic()

            async def send_event(payload: dict[str, Any]) -> None:
                async with send_lock:
                    await websocket.send_json(payload)

            await send_event({
                "type": "ready",
                "user_id": ticket.user_id,
                "max_duration_seconds": runtime.settings.max_session_seconds,
                "protocol_version": ticket.protocol_version,
                "tool_routing_mode": ticket.routing_mode,
            })
            if ticket.protocol_version >= 4 and runtime.tasks.enabled:
                try:
                    await send_event({
                        "type": "task_snapshot",
                        "task_protocol_version": 1,
                        "tasks": await run_task_call(
                            runtime.tasks.list_tasks, ticket.user_id, "all", limit=20,
                        ),
                    })
                except RuntimeError:
                    await send_event({"type": "error", "code": "voice_task_storage_unavailable"})

            if (
                output_mode == "gemini_live_duplex"
                and input_mode == "gemini_live_audio"
            ):
                if not runtime.provider.gemini_available:
                    await send_event({
                        "type": "error",
                        "code": "gemini_live_unavailable",
                    })
                    await websocket.close(code=1013)
                    return
                try:
                    await run_duplex_session(
                        websocket,
                        provider=runtime.provider,
                        limiter=runtime.limiter,
                        identity=ticket.identity,
                        initial_context=context,
                        max_session_seconds=runtime.settings.max_session_seconds,
                        send_event=send_event,
                        conversation_memory=memory_prompt,
                        memory_turns=memory_turns,
                        weather_service=runtime.weather,
                        weather_location_provider=runtime.weather_location_provider,
                        weather_user_id=ticket.user_id,
                        user_id=ticket.user_id,
                        voice_session_id=ticket.session_id,
                        capability_secret=runtime.secret,
                        routing_mode=ticket.routing_mode,
                        task_service=(
                            runtime.tasks if ticket.protocol_version >= 4 else None
                        ),
                    )
                    await send_event({"type": "closed", "reconnect": True})
                    await websocket.close(code=1000)
                except WebSocketDisconnect:
                    pass
                except Exception as error:
                    LOGGER.exception(
                        "App Voice duplex session failed (error_type=%s)",
                        type(error).__name__,
                    )
                    try:
                        await send_event({
                            "type": "error",
                            "code": "gemini_live_connect_failed",
                        })
                        await websocket.close(code=1011)
                    except Exception:
                        pass
                return

            async def cancel_live_audio(*, notify: bool) -> None:
                nonlocal live_audio_task, live_response_id
                task = live_audio_task
                response_id = live_response_id
                live_audio_task = None
                live_response_id = None
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                if notify and response_id:
                    await send_event({
                        "type": "audio_interrupted",
                        "response_id": response_id,
                    })

            async def stream_live_audio(response_id: str, text: str) -> None:
                nonlocal live_audio_task, live_response_id
                sequence = 0
                try:
                    async for chunk in runtime.provider.stream_synthesize(text):
                        await send_event({
                            "type": "audio_chunk",
                            "response_id": response_id,
                            "sequence": sequence,
                            "mime_type": "audio/pcm;rate=24000",
                            "base64": base64.b64encode(chunk).decode("ascii"),
                        })
                        sequence += 1
                    await send_event({
                        "type": "audio_complete" if sequence else "audio_unavailable",
                        "response_id": response_id,
                        "chunks": sequence,
                    })
                except asyncio.CancelledError:
                    raise
                except Exception:
                    await send_event({
                        "type": "audio_unavailable",
                        "response_id": response_id,
                    })
                finally:
                    if live_response_id == response_id:
                        live_response_id = None
                        live_audio_task = None

            async def reply(text: str, *, code: str = "ok") -> None:
                nonlocal live_audio_task, live_response_id
                await cancel_live_audio(notify=True)
                remember("assistant", text)
                response_id = uuid.uuid4().hex
                await send_event({
                    "type": "assistant_reply", "text": text[:160], "code": code,
                    "response_id": response_id,
                    "speech_mode": output_mode,
                })
                if output_mode == "gemini_live" and runtime.limiter.allow_tts(ticket.identity):
                    live_response_id = response_id
                    live_audio_task = asyncio.create_task(
                        stream_live_audio(response_id, text[:160]),
                    )
                    return
                if output_mode == "gemini_tts" and runtime.limiter.allow_tts(ticket.identity):
                    audio = await runtime.provider.synthesize(text)
                    if audio:
                        await send_event({
                            "type": "audio", "response_id": response_id, **audio,
                        })
                        return
                if output_mode in {"gemini_live", "gemini_tts"}:
                    await send_event({
                        "type": "audio_unavailable", "response_id": response_id,
                    })

            async def handle_proposal(proposal: VoiceProposal | None) -> None:
                nonlocal pending
                if proposal is None:
                    await reply("我還不確定你要做什麼。你可以請我編輯個人資料、調整設定或撰寫貼文。", code="clarification")
                    return
                if proposal.intent == "assistant.cancel":
                    pending = None
                    await reply(proposal.reply, code="cancelled")
                    return
                if proposal.intent == "assistant.reply":
                    await reply(proposal.reply, code="conversation")
                    return
                bound, target_error = bind_target(proposal.intent, proposal.arguments, context)
                if target_error:
                    await reply("請重新讀取目前畫面並指定項目。", code=target_error)
                    return
                proposal = VoiceProposal(proposal.intent, bound, proposal.reply, proposal.base_revision)
                if not context_allows_proposal(context, proposal):
                    await reply(
                        "這項功能沒有被你授權，可以在「阿月語音助理」設定裡調整。",
                        code="permission_denied",
                    )
                    return
                if proposal.intent == "weather.query":
                    if runtime.weather is None:
                        await reply(
                            "目前天氣與空氣品質服務尚未啟用。",
                            code="weather_unavailable",
                        )
                        return
                    location = await resolve_weather_location(
                        proposal.arguments.get("location"),
                        ticket.user_id,
                        runtime.weather_location_provider,
                    )
                    result = await asyncio.to_thread(
                        runtime.weather.query,
                        location,
                    )
                    status = str(result.get("status") or "failed")
                    await reply(
                        str(result.get("message") or "目前暫時查不到氣象資料。")[:900],
                        code=(
                            "weather"
                            if status == "ok"
                            else "weather_partial"
                            if status == "partial"
                            else "clarification"
                            if status == "needs_input"
                            else "weather_unavailable"
                        ),
                    )
                    return
                if proposal.intent == "assistant.close":
                    pending = None
                    await reply(proposal.reply, code="voice_mode_closed")
                    return
                if proposal.intent == "post.request_publish" and not context.get("can_publish"):
                    pending = None
                    await reply("你還沒有完成圖片選擇，請先自行選擇一到五張圖片。", code="post_not_ready")
                    return
                if requires_confirmation(proposal):
                    phrase = confirmation_phrase(proposal)
                    pending = PendingConfirmation(
                        confirmation_id=uuid.uuid4().hex,
                        proposal=proposal,
                        phrase=phrase,
                        scope=str(context.get("scope") or "global"),
                        revision=int(context.get("revision") or 0),
                        expires_at=time.time() + 30,
                    )
                    await reply(_confirmation_preview(proposal, context), code="confirmation_required")
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
                    return
                action_id = uuid.uuid4().hex
                if proposal.intent in {
                    "match.ayue_query", "ayue.public_query",
                    "ayue.private_query", "personality.explore",
                }:
                    delegated_actions[action_id] = proposal
                await send_event({
                    "type": "action_proposal",
                    "action_id": action_id,
                    "intent": proposal.intent,
                    "arguments": proposal.arguments,
                    "confirmed": True,
                    "scope": str(context.get("scope") or "global"),
                    "base_revision": proposal.base_revision,
                })

            async def confirm_pending(
                *, confirmation_id: str, spoken_phrase: str,
            ) -> bool:
                nonlocal pending
                if pending is None:
                    await reply("這個確認已經失效，請重新提出操作。", code="stale_confirmation")
                    return False
                if not hmac.compare_digest(confirmation_id, pending.confirmation_id):
                    await reply("這個確認已經失效，請重新提出操作。", code="stale_confirmation")
                    return False
                if pending.expires_at < time.time():
                    pending = None
                    await reply("確認已逾時，這次沒有執行變更。", code="confirmation_expired")
                    return False
                if (
                    context.get("scope") != pending.scope
                    or context.get("revision") != pending.revision
                ):
                    pending = None
                    await reply("畫面內容已改變，請重新提出操作。", code="stale_confirmation")
                    return False
                if not confirmation_matches(spoken_phrase, pending.proposal):
                    await reply(
                        f"我還沒收到完整確認。請說「{pending.phrase}」。",
                        code="confirmation_mismatch",
                    )
                    return False
                confirmed = pending
                pending = None
                await cancel_live_audio(notify=True)
                await send_event({
                    "type": "action_proposal",
                    "action_id": confirmed.confirmation_id,
                    "confirmation_id": confirmed.confirmation_id,
                    "intent": confirmed.proposal.intent,
                    "arguments": confirmed.proposal.arguments,
                    "confirmed": True,
                    "scope": confirmed.scope,
                    "base_revision": confirmed.revision,
                })
                return True

            while True:
                remaining = runtime.settings.max_session_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    await reply("這次語音工作階段已結束，需要時再點我。", code="session_timeout")
                    break
                try:
                    message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                if message.get("type") == "websocket.disconnect":
                    return
                binary = message.get("bytes")
                if binary is not None:
                    if binary and len(binary) <= 65536 and len(audio_buffer) + len(binary) <= 16000 * 2 * 30:
                        audio_buffer.extend(binary)
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
                    next_context = session_context(control.get("context"))
                    if pending and (
                        next_context["scope"] != pending.scope
                        or next_context["revision"] != pending.revision
                    ):
                        pending = None
                    context = next_context
                elif kind == "interrupt":
                    await cancel_live_audio(notify=True)
                elif kind == "confirmation_response":
                    await cancel_live_audio(notify=True)
                    if control.get("accepted") is not True:
                        pending = None
                        await reply("好，這次不會執行變更。", code="cancelled")
                        continue
                    await confirm_pending(
                        confirmation_id=str(control.get("confirmation_id") or ""),
                        spoken_phrase=str(control.get("spoken_phrase") or "")[:200],
                    )
                elif kind == "utterance":
                    await cancel_live_audio(notify=True)
                    text = str(control.get("text") or "")[:2000]
                    remember("user", text)
                    if pending:
                        await confirm_pending(
                            confirmation_id=pending.confirmation_id,
                            spoken_phrase=text,
                        )
                        continue
                    compact = normalized_phrase(text)
                    if (
                        compact in {
                            "確認", "確定", "同意", "好", "好的", "取消",
                            "confirm", "confirmed", "cancel", "yes", "no",
                        }
                        and last_delegated_proposal is not None
                        and last_delegated_expires_at >= time.time()
                    ):
                        previous = last_delegated_proposal
                        arguments = dict(previous.arguments)
                        if previous.intent == "personality.explore":
                            arguments["message"] = text
                        else:
                            arguments["question"] = text
                        await handle_proposal(VoiceProposal(
                            previous.intent,
                            arguments,
                            "",
                            int(context.get("revision") or 0),
                        ))
                        continue
                    await handle_proposal(
                        await runtime.provider.interpret_text(text, context=context),
                    )
                elif kind == "audio_end":
                    captured = bytes(audio_buffer)
                    audio_buffer.clear()
                    await handle_proposal(
                        await runtime.provider.interpret_audio(captured, context=context),
                    )
                elif kind == "action_result":
                    success = control.get("success") is True
                    action_id = str(control.get("action_id") or "")
                    delegated = delegated_actions.pop(action_id, None)
                    if success and delegated is not None:
                        last_delegated_proposal = delegated
                        last_delegated_expires_at = time.time() + 120
                    text = str(control.get("message") or "")[:200]
                    await reply(
                        text or ("操作已完成。" if success else "操作沒有完成，請稍後再試。"),
                        code="action_completed" if success else "action_failed",
                    )
                elif kind == "stop":
                    break
            await cancel_live_audio(notify=False)
            await send_event({"type": "closed"})
            await websocket.close(code=1000)
        finally:
            if live_audio_task is not None and not live_audio_task.done():
                live_audio_task.cancel()
                await asyncio.gather(live_audio_task, return_exceptions=True)
            if acquired:
                runtime.limiter.release()
            if ticket is not None and acquired:
                runtime.queue_memory_finalize(ticket, memory_turns)

    return app_router


_runtime = AppVoiceRuntime(
    weather_location_provider=_saved_weather_location,
    defer_task_service=True,
)
router = create_router(_runtime)


def start_app_voice_task_services() -> None:
    _runtime.initialize_task_service()
    start_voice_task_worker(_runtime.tasks)


def stop_app_voice_task_services() -> None:
    stop_voice_task_worker()
