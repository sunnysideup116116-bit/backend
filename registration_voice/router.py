from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field

from .contracts import (
    redact_sensitive_transcript,
    requires_confirmation,
    safe_form,
    validate_function_patch,
)
from .key_pool import GoogleApiKeyPool
from .limiter import (
    LocalVoiceLimiter,
    TicketStore,
    anonymous_identity,
    process_identity_secret,
)
from .provider import GeminiLiveProvider, LiveConnection, LiveProvider, ProviderEvent
from .settings import VoiceRegistrationSettings, collect_google_api_keys


logger = logging.getLogger(__name__)


class VoiceSessionRequest(BaseModel):
    installation_id: str = Field(min_length=16, max_length=128)
    consent_version: str = Field(min_length=1, max_length=80)
    consent_accepted_at: str = Field(min_length=10, max_length=64)


@dataclass
class DraftState:
    form: dict[str, Any]
    revision: int


class VoiceRegistrationRuntime:
    def __init__(
        self,
        *,
        settings: VoiceRegistrationSettings | None = None,
        keys: list[str] | None = None,
        provider: LiveProvider | None = None,
    ):
        self.settings = settings or VoiceRegistrationSettings.from_env()
        self.key_pool = GoogleApiKeyPool(
            collect_google_api_keys() if keys is None else keys,
            cooldown_seconds=self.settings.key_cooldown_seconds,
        )
        self.provider = provider or GeminiLiveProvider(self.settings)
        self.tickets = TicketStore(self.settings.ticket_ttl_seconds)
        self.limiter = LocalVoiceLimiter(self.settings)
        self._identity_secret = process_identity_secret()

    @property
    def available(self) -> bool:
        return self.settings.enabled and self.key_pool.size > 0

    def client_ip(self, connection: Request | WebSocket) -> str:
        forwarded = connection.headers.get("x-forwarded-for", "")
        if forwarded:
            return forwarded.split(",", 1)[0].strip()[:80]
        return str(connection.client.host if connection.client else "unknown")[:80]

    def allowed_origin(self, websocket: WebSocket) -> bool:
        origin = (websocket.headers.get("origin") or "").strip().lower()
        if not origin:  # Native Flutter clients do not send a browser Origin.
            return True
        if origin in {
            "https://service.misproject.us.ci",
            "http://127.0.0.1:4173",
            "http://localhost:4173",
        }:
            return True
        configured_origins = {
            value.strip().lower()
            for value in os.getenv("CORS_ORIGINS", "").split(",")
            if value.strip()
        }
        if origin in configured_origins:
            return True
        return bool(re.fullmatch(r"https://[a-z0-9.-]+\.github\.io", origin))


def _is_retryable_provider_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        marker in text
        for marker in (
            "429", "resourceexhausted", "resource_exhausted", "quota", "rate limit",
            "timeout", "unavailable", "connection", "401", "403", "api key",
        )
    )


def _provider_error_category(exc: Exception) -> str:
    text = f"{type(exc).__name__}: {exc}".lower()
    if any(marker in text for marker in ("429", "quota", "rate limit", "resource_exhausted")):
        return "quota_or_rate_limit"
    if any(marker in text for marker in ("401", "403", "api key", "permission")):
        return "authentication"
    if "timeout" in text:
        return "timeout"
    if any(marker in text for marker in ("connection", "unavailable", "closed")):
        return "connection"
    return "unexpected"


async def _bridge(
    websocket: WebSocket,
    connection: LiveConnection,
    state: DraftState,
    settings: VoiceRegistrationSettings,
) -> str:
    send_lock = asyncio.Lock()
    stop_requested = asyncio.Event()
    max_audio_bytes = settings.max_session_seconds * 16000 * 2 + 65536
    audio_bytes = 0
    started = time.monotonic()

    async def send_event(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    async def client_loop() -> str:
        nonlocal audio_bytes
        while True:
            remaining = settings.max_session_seconds - (time.monotonic() - started)
            if remaining <= 0:
                stop_requested.set()
                await connection.send_audio_stream_end()
                await send_event({"type": "session_timeout"})
                return "stop"
            try:
                message = await asyncio.wait_for(websocket.receive(), timeout=remaining)
            except asyncio.TimeoutError:
                stop_requested.set()
                await connection.send_audio_stream_end()
                await send_event({"type": "session_timeout"})
                return "stop"

            if message.get("type") == "websocket.disconnect":
                return "disconnect"
            data = message.get("bytes")
            if data is not None:
                if not data or len(data) > 65536:
                    continue
                audio_bytes += len(data)
                if audio_bytes > max_audio_bytes:
                    stop_requested.set()
                    await connection.send_audio_stream_end()
                    await send_event({"type": "audio_limit_reached"})
                    return "stop"
                await connection.send_audio(data)
                continue

            raw = message.get("text")
            if not raw:
                continue
            try:
                control = json.loads(raw)
            except (TypeError, ValueError):
                continue
            kind = control.get("type")
            if kind == "activity_start":
                await connection.send_activity_start()
            elif kind == "activity_end":
                await connection.send_activity_end()
            elif kind == "form_state_changed":
                try:
                    revision = int(control.get("revision"))
                except (TypeError, ValueError):
                    continue
                if revision > state.revision:
                    state.form = safe_form(control.get("form"))
                    state.revision = revision
                    await connection.send_form_state(state.form, state.revision)
            elif kind == "stop":
                stop_requested.set()
                await connection.send_activity_end()
                await connection.send_audio_stream_end()
                return "stop"

    async def provider_loop() -> str:
        async for event in connection.events():
            if event.type == "transcript" and event.text:
                await send_event({
                    "type": "transcript",
                    "text": redact_sensitive_transcript(event.text)[:1000],
                    "final": event.final,
                })
            elif event.type == "tool_calls":
                batch_base_revision = state.revision
                accepted = []
                combined_changes: dict[str, Any] = {}
                combined_rejected: list[dict[str, Any]] = []
                combined_warnings: list[str] = []
                for call in event.tool_calls:
                    if call.name != "propose_registration_patch":
                        await connection.send_tool_result(call, {
                            "applied": False,
                            "error": "unsupported_tool",
                            "current_revision": state.revision,
                        })
                        continue
                    decision = validate_function_patch(
                        call.args,
                        current_revision=batch_base_revision,
                    )
                    if decision.stale:
                        await connection.send_tool_result(call, {
                            "applied": False,
                            "error": "stale_revision",
                            "current_revision": state.revision,
                        })
                        continue
                    accepted.append((call, decision))
                    combined_changes.update(decision.changes)
                    combined_rejected.extend(decision.rejected)
                    combined_warnings.extend(decision.warnings)

                if combined_changes:
                    state.form.update(combined_changes)
                    state.revision += 1
                if combined_changes or combined_rejected or combined_warnings:
                    await send_event({
                        "type": "form_patch",
                        "base_revision": batch_base_revision,
                        "revision": state.revision,
                        "changes": combined_changes,
                        "rejected": combined_rejected,
                        "warnings": list(dict.fromkeys(combined_warnings)),
                        "requires_confirmation": requires_confirmation(combined_changes),
                    })
                for call, decision in accepted:
                    await connection.send_tool_result(call, {
                        "applied": bool(decision.changes),
                        "rejected": list(decision.rejected),
                        "warnings": list(decision.warnings),
                        "current_revision": state.revision,
                        "current_form": state.form,
                    })
                if stop_requested.is_set():
                    return "stop"
            elif event.type == "turn_complete" and stop_requested.is_set():
                return "stop"
        return "provider_closed"

    client_task = asyncio.create_task(client_loop())
    provider_task = asyncio.create_task(provider_loop())
    try:
        done, pending = await asyncio.wait(
            {client_task, provider_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        finished = next(iter(done))
        reason = await finished
        if reason == "stop" and provider_task in pending:
            try:
                return await asyncio.wait_for(provider_task, timeout=3.0)
            except asyncio.TimeoutError:
                return "stop"
        return reason
    finally:
        for task in (client_task, provider_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(client_task, provider_task, return_exceptions=True)


def create_router(runtime: VoiceRegistrationRuntime) -> APIRouter:
    registration_router = APIRouter(prefix="/api/registration/voice", tags=["Registration Voice"])

    @registration_router.get("/capability")
    async def capability() -> dict[str, Any]:
        return {
            "enabled": runtime.available,
            "model": runtime.settings.model if runtime.available else None,
            "max_session_seconds": runtime.settings.max_session_seconds,
            "silence_duration_ms": runtime.settings.silence_duration_ms,
            "supported_languages": ["zh-Hant-TW", "en"],
            "local_rate_limit_enabled": runtime.limiter.enabled,
            "consent_version": runtime.settings.consent_version,
        }

    @registration_router.post("/session")
    async def create_session(req: VoiceSessionRequest, request: Request) -> dict[str, Any]:
        if not runtime.available:
            raise HTTPException(status_code=503, detail="voice_registration_unavailable")
        if req.consent_version != runtime.settings.consent_version:
            raise HTTPException(status_code=409, detail="voice_consent_version_mismatch")
        client_ip = runtime.client_ip(request)
        identity = anonymous_identity(
            req.installation_id,
            client_ip,
            runtime._identity_secret,
        )
        if not runtime.limiter.allow_start(identity):
            raise HTTPException(status_code=429, detail="voice_local_rate_limit")
        client_ip_fingerprint = anonymous_identity(
            "voice-ticket-ip",
            client_ip,
            runtime._identity_secret,
        )
        ticket, ttl = runtime.tickets.issue(identity, client_ip_fingerprint)
        return {
            "ticket": ticket,
            "expires_in_seconds": ttl,
            "websocket_url": "wss://service.misproject.us.ci/api/registration/voice",
            "max_duration_seconds": runtime.settings.max_session_seconds,
            "supported_languages": ["zh-Hant-TW", "en"],
            "local_rate_limit_enabled": runtime.limiter.enabled,
        }

    @registration_router.websocket("")
    async def registration_voice(websocket: WebSocket) -> None:
        if not runtime.allowed_origin(websocket):
            await websocket.close(code=1008, reason="origin_not_allowed")
            return
        await websocket.accept()
        if not runtime.available:
            await websocket.send_json({"type": "provider_unavailable"})
            await websocket.close(code=1013)
            return

        acquired = False
        try:
            try:
                hello_message = await asyncio.wait_for(websocket.receive_text(), timeout=5.0)
                hello = json.loads(hello_message)
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
            websocket_ip_fingerprint = anonymous_identity(
                "voice-ticket-ip",
                runtime.client_ip(websocket),
                runtime._identity_secret,
            )
            if not hmac.compare_digest(
                ticket.client_ip_fingerprint,
                websocket_ip_fingerprint,
            ):
                await websocket.close(code=1008, reason="ticket_client_mismatch")
                return
            if not runtime.limiter.acquire():
                await websocket.send_json({"type": "local_rate_limit"})
                await websocket.close(code=1013)
                return
            acquired = True

            try:
                revision = max(0, int(hello.get("revision", 0)))
            except (TypeError, ValueError):
                revision = 0
            state = DraftState(safe_form(hello.get("form")), revision)
            candidates = runtime.key_pool.candidates()
            if not candidates:
                await websocket.send_json({"type": "provider_unavailable"})
                await websocket.close(code=1013)
                return

            for index, candidate in enumerate(candidates):
                if index:
                    await websocket.send_json({"type": "provider_reconnecting"})
                try:
                    async with runtime.provider.connect(
                        candidate.key, state.form, state.revision,
                    ) as connection:
                        runtime.key_pool.mark_success(candidate.key)
                        await websocket.send_json({
                            "type": "ready",
                            "revision": state.revision,
                            "max_duration_seconds": runtime.settings.max_session_seconds,
                        })
                        result = await _bridge(
                            websocket, connection, state, runtime.settings,
                        )
                        if result in {"stop", "disconnect"}:
                            if result == "stop":
                                await websocket.send_json({"type": "closed"})
                                await websocket.close(code=1000)
                            return
                        raise ConnectionError("Gemini Live session closed unexpectedly")
                except WebSocketDisconnect:
                    return
                except Exception as exc:  # Try the next configured key/session.
                    retryable = _is_retryable_provider_error(exc)
                    category = _provider_error_category(exc)
                    logger.warning(
                        "Registration voice provider attempt failed: key_slot=%s category=%s retryable=%s",
                        candidate.position + 1,
                        category,
                        retryable,
                    )
                    if retryable:
                        runtime.key_pool.mark_unavailable(candidate.key)
                    else:
                        break

            await websocket.send_json({"type": "provider_unavailable"})
            await websocket.close(code=1013)
        finally:
            if acquired:
                runtime.limiter.release()

    return registration_router


_runtime = VoiceRegistrationRuntime()
router = create_router(_runtime)
