from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Protocol

from .contracts import TAIWAN_CITIES
from .settings import VoiceRegistrationSettings


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: dict[str, Any]


@dataclass(frozen=True)
class ProviderEvent:
    type: str
    text: str = ""
    final: bool = False
    tool_calls: tuple[ToolCall, ...] = ()


class LiveConnection(Protocol):
    async def send_audio(self, data: bytes) -> None: ...
    async def send_audio_stream_end(self) -> None: ...
    async def send_activity_start(self) -> None: ...
    async def send_activity_end(self) -> None: ...
    async def send_form_state(self, form: dict[str, Any], revision: int) -> None: ...
    async def send_tool_result(self, call: ToolCall, result: dict[str, Any]) -> None: ...
    def events(self) -> AsyncIterator[ProviderEvent]: ...


class LiveProvider(Protocol):
    def connect(
        self, key: str, form: dict[str, Any], revision: int,
    ): ...


def _function_schema() -> dict[str, Any]:
    properties: dict[str, Any] = {
        "base_revision": {
            "type": "integer",
            "description": "The exact current form revision supplied by the server.",
        },
        "nickname": {"type": "string"},
        "email": {"type": "string"},
        "gender": {"type": "string", "enum": ["male", "female"]},
        "phone": {"type": "string"},
        "age": {"type": "integer"},
        "region": {"type": "string", "enum": list(TAIWAN_CITIES)},
        "interest": {"type": "string"},
        "clear_fields": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["nickname", "email", "gender", "phone", "age", "region", "interest"],
            },
        },
        "warning_codes": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": [
                    "password_spoken",
                    "district_not_stored",
                    "ambiguous_value",
                    "unsupported_language",
                ],
            },
        },
    }
    return {
        "type": "object",
        "properties": properties,
        "required": ["base_revision"],
    }


def _system_instruction(form: dict[str, Any], revision: int) -> str:
    return f"""
You are a tool-only voice form assistant for a Taiwanese registration form.
Accept only Taiwan Mandarin and English speech. Always write Chinese content in
Traditional Chinese. If the utterance is primarily another language, do not
propose any field value; call the tool only with
warning_codes=[unsupported_language]. Never speak to the user.
Whenever the user explicitly provides or corrects a supported field, call
propose_registration_patch. Combine every stable field from one utterance into
one function call rather than splitting it across calls. The newest explicit correction wins. Users may
provide fields in any order. Do not infer gender, age, region, email, phone,
nickname, or interests from tone, name, or background knowledge.

The supported fields are nickname, email, gender, phone, age, region, and
interest. Password is forbidden: never include a password or its value in tool
arguments. If the user says a password, emit only warning_codes=[password_spoken].
Do not submit accounts and do not claim that a profile was saved.

The current safe form state is:
{json.dumps(form, ensure_ascii=False, separators=(",", ":"))}
The current base_revision is {revision}. Always echo that exact revision in the
next tool call. A FORM_STATE_UPDATE message replaces both values and is control
data, not a user request; do not call a tool merely because it arrived.

For a township such as 卑南鄉, map it to the containing county/city supported by
the schema and add district_not_stored. Preserve explicit self-corrections and
negation, such as 不喜歡籃球、改成羽球. Use Traditional Chinese for display text.
After receiving a tool result, silently wait for more user input.
""".strip()


class _GoogleLiveConnection:
    def __init__(self, session: Any, types_module: Any):
        self._session = session
        self._types = types_module

    async def send_audio(self, data: bytes) -> None:
        await self._session.send_realtime_input(
            audio=self._types.Blob(data=data, mime_type="audio/pcm;rate=16000"),
        )

    async def send_audio_stream_end(self) -> None:
        await self._session.send_realtime_input(audio_stream_end=True)

    async def send_activity_start(self) -> None:
        # Automatic server-side VAD is enabled. Explicit activity signals are
        # only valid when automatic activity detection is disabled.
        return None

    async def send_activity_end(self) -> None:
        return None

    async def send_form_state(self, form: dict[str, Any], revision: int) -> None:
        control = {
            "type": "FORM_STATE_UPDATE",
            "revision": revision,
            "form": form,
            "instruction": "Control data only. Do not call a tool for this message.",
        }
        await self._session.send_realtime_input(
            text=json.dumps(control, ensure_ascii=False, separators=(",", ":")),
        )

    async def send_tool_result(self, call: ToolCall, result: dict[str, Any]) -> None:
        await self._session.send_tool_response(
            function_responses=self._types.FunctionResponse(
                id=call.id or None,
                name=call.name,
                response=result,
            ),
        )

    async def events(self) -> AsyncIterator[ProviderEvent]:
        # google-genai's receive() iterator intentionally ends after one model
        # turn. Re-open it for the next turn while keeping the same Live socket.
        while True:
            received_any = False
            async for message in self._session.receive():
                received_any = True
                content = getattr(message, "server_content", None)
                if content is not None:
                    interim = getattr(content, "interim_input_transcription", None)
                    if interim is not None and getattr(interim, "text", None):
                        yield ProviderEvent(
                            type="transcript",
                            text=str(interim.text),
                            final=False,
                        )
                    transcript = getattr(content, "input_transcription", None)
                    if transcript is not None and getattr(transcript, "text", None):
                        yield ProviderEvent(
                            type="transcript",
                            text=str(transcript.text),
                            final=True,
                        )
                    if bool(getattr(content, "turn_complete", False)):
                        yield ProviderEvent(type="turn_complete", final=True)

                tool_call = getattr(message, "tool_call", None)
                function_calls = (
                    getattr(tool_call, "function_calls", None) if tool_call else None
                )
                if function_calls:
                    calls = tuple(
                        ToolCall(
                            id=str(getattr(call, "id", "") or ""),
                            name=str(getattr(call, "name", "") or ""),
                            args=dict(getattr(call, "args", None) or {}),
                        )
                        for call in function_calls
                    )
                    yield ProviderEvent(type="tool_calls", tool_calls=calls)
            if not received_any:
                return


class GeminiLiveProvider:
    def __init__(self, settings: VoiceRegistrationSettings):
        self._settings = settings

    @asynccontextmanager
    async def connect(
        self, key: str, form: dict[str, Any], revision: int,
    ) -> AsyncIterator[LiveConnection]:
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # Kept lazy so offline tests can use a fake provider.
            raise RuntimeError("google-genai is not installed in the Social environment") from exc

        level = {
            "minimal": types.ThinkingLevel.MINIMAL,
            "low": types.ThinkingLevel.LOW,
            "medium": types.ThinkingLevel.MEDIUM,
            "high": types.ThinkingLevel.HIGH,
        }.get(self._settings.thinking_level, types.ThinkingLevel.LOW)
        declaration = types.FunctionDeclaration(
            name="propose_registration_patch",
            description=(
                "Propose explicit user-spoken changes to the registration draft. "
                "Never include passwords and never submit the account."
            ),
            parameters_json_schema=_function_schema(),
        )
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            thinking_config=types.ThinkingConfig(thinking_level=level),
            system_instruction=_system_instruction(form, revision),
            tools=[types.Tool(function_declarations=[declaration])],
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=False,
                    silence_duration_ms=self._settings.silence_duration_ms,
                ),
            ),
        )
        client = genai.Client(
            api_key=key,
            http_options=types.HttpOptions(api_version="v1beta"),
        )
        try:
            async with client.aio.live.connect(
                model=self._settings.model,
                config=config,
            ) as session:
                yield _GoogleLiveConnection(session, types)
        finally:
            client.close()
