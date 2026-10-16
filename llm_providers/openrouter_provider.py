"""Shared OpenRouter chat-completion adapter for AYUE_LLM_PROVIDER=openrouter.

Scope contract: only Social shared completions, the Matchmaker OpenAI-SDK calls
and the Risk background judge switch to OpenRouter in this mode. Gemini NLP and
summary adapters, the local Guardrail llama-server and Google embeddings never
change provider. Every request carries {"provider": {"sort": "latency"}} so
OpenRouter routes to the lowest-latency endpoints first with its default
endpoint fallback enabled. Model-level fallback stays disabled; generations
fail explicitly. Ollama thinking levels are not forwarded because OpenRouter
has no equivalent parameter. Error messages never carry remote payloads or the
API key value.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import requests

DEFAULT_MODEL = "deepseek/deepseek-v4.1-flash"
DEFAULT_TIMEOUT_SECONDS = 120.0
BASE_URL = "https://openrouter.ai/api/v1"
_COMPLETIONS_URL = BASE_URL + "/chat/completions"
_MODELS_URL = BASE_URL + "/models"
PROVIDERS = ("ollama", "gpt", "openrouter")
SERVICE_TIERS = ("default", "flex", "priority", "fast", "ultrafast")
DEFAULT_SERVICE_TIER = "default"
SERVER_ENV = Path(__file__).resolve().parents[1] / ".env"
OPENROUTER_ENV_KEYS = frozenset({
    "OPENROUTER_API_KEY", "AYUE_OPENROUTER_MODEL",
    "AYUE_OPENROUTER_FAST_MODEL", "AYUE_OPENROUTER_TIMEOUT_SECONDS",
    "AYUE_OPENROUTER_SERVICE_TIER",
})
APP_HEADERS = {
    "HTTP-Referer": "https://service.misproject.us.ci/",
    "X-OpenRouter-Title": "ayue-dating-backend",
}


class OpenRouterProviderError(RuntimeError):
    """Sanitized provider failure; never carries remote payloads or credentials."""


def load_openrouter_settings(path=SERVER_ENV, *, environ=None):
    """Load allowlisted OpenRouter keys from Server/.env; explicit env wins."""
    from dotenv import dotenv_values

    env = os.environ if environ is None else environ
    if env.get("AYUE_SKIP_DOTENV", "").strip().lower() in {"1", "true", "on"}:
        return
    for key, value in dotenv_values(path, interpolate=False).items():
        if key in OPENROUTER_ENV_KEYS and value is not None:
            env.setdefault(key, value)


load_openrouter_settings()


def _provider_from_environ(environ):
    return environ.get("AYUE_LLM_PROVIDER", "ollama").strip().lower()


def selected_provider(*, environ=None) -> str:
    provider = _provider_from_environ(os.environ if environ is None else environ)
    if provider not in PROVIDERS:
        raise OpenRouterProviderError(
            "AYUE_LLM_PROVIDER must be one of: " + ", ".join(PROVIDERS))
    return provider


def is_openrouter(*, environ=None) -> bool:
    return _provider_from_environ(os.environ if environ is None else environ) == "openrouter"


def api_key(*, environ=None):
    env = os.environ if environ is None else environ
    key = env.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise OpenRouterProviderError("Set OPENROUTER_API_KEY to use the openrouter provider")
    return key


def selected_model(*, fast: bool = False, environ=None) -> str:
    env = os.environ if environ is None else environ
    main = env.get("AYUE_OPENROUTER_MODEL", "").strip() or DEFAULT_MODEL
    if fast:
        return env.get("AYUE_OPENROUTER_FAST_MODEL", "").strip() or main
    return main


def provider_routing_body() -> dict:
    return {"provider": {"sort": "latency"}}


def service_tier(*, environ=None) -> str:
    """Standard tier by default; open-weight endpoints never upcharge silently.

    Open-weight models (DeepSeek included) admit priority/flex endpoints into
    default routing, so latency sorting alone can land on an upcharged tier.
    An explicit request-level tier pins the billing pool at that tier.
    """
    env = os.environ if environ is None else environ
    tier = env.get("AYUE_OPENROUTER_SERVICE_TIER", "").strip().lower()
    tier = tier or DEFAULT_SERVICE_TIER
    if tier not in SERVICE_TIERS:
        raise OpenRouterProviderError(
            "AYUE_OPENROUTER_SERVICE_TIER must be one of: " + ", ".join(SERVICE_TIERS))
    return tier


def routing_extra_body(*, environ=None) -> dict:
    """Routing body for OpenAI-SDK callers (Matchmaker, Risk judge)."""
    return {
        "provider": {"sort": "latency"},
        "service_tier": service_tier(environ=environ),
    }


def request_deadline(deadline_monotonic: float | None = None) -> float:
    """Resolve the local deadline: env timeout capped by the caller deadline."""
    try:
        seconds = float(
            os.getenv("AYUE_OPENROUTER_TIMEOUT_SECONDS", "") or DEFAULT_TIMEOUT_SECONDS)
        if not seconds > 0:
            raise ValueError
    except ValueError:
        raise OpenRouterProviderError(
            "AYUE_OPENROUTER_TIMEOUT_SECONDS must be positive and finite") from None
    deadline = time.monotonic() + seconds
    if deadline_monotonic is not None:
        deadline = min(deadline, deadline_monotonic)
    if deadline <= time.monotonic():
        raise TimeoutError("OpenRouter request deadline exhausted")
    return deadline


def _headers(api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    headers.update(APP_HEADERS)
    return headers


def _raise_for_status(status_code: int) -> None:
    detail = {
        401: "openrouter_unauthorized: OPENROUTER_API_KEY 驗證失敗（Key 無效或未啟用）",
        402: "openrouter_payment_required: OpenRouter 帳戶餘額或額度不足",
        403: "openrouter_forbidden: 此 Key 沒有存取該模型的權限",
        429: "openrouter_rate_limited: 超出 OpenRouter 頻率或額度限制",
    }.get(status_code)
    if detail:
        raise OpenRouterProviderError(detail)
    raise OpenRouterProviderError(f"openrouter_request_failed: HTTP {status_code}")


def _transport(timeout: float) -> float:
    return max(1.0, timeout)


def _post(payload: dict, timeout: float, *, stream: bool = False) -> requests.Response:
    try:
        response = requests.post(
            _COMPLETIONS_URL, headers=_headers(api_key()), json=payload,
            timeout=(min(10.0, _transport(timeout)), _transport(timeout)),
            stream=stream,
        )
    except requests.Timeout:
        raise TimeoutError("OpenRouter request deadline exhausted") from None
    except requests.RequestException as exc:
        raise OpenRouterProviderError("openrouter_transport_failed") from None
    if response.status_code != 200:
        response.close()
        _raise_for_status(response.status_code)
    return response


def _single_turn_messages(prompt: str, system_prompt: str | None) -> list[dict]:
    system_message = str(system_prompt or "").strip()
    if system_message:
        return [
            {"role": "system", "content": system_message},
            {"role": "user", "content": str(prompt or "")},
        ]
    return [{"role": "user", "content": str(prompt or "")}]


def _normalize_conversation(messages: list[dict]) -> list[dict]:
    """Convert Social's ollama-shaped provider messages to strict OpenAI payloads."""
    normalized = []
    for message in messages:
        value = dict(message)
        raw_calls = value.get("tool_calls")
        if raw_calls:
            packed = []
            for call in raw_calls:
                function = call.get("function") or {}
                arguments = function.get("arguments")
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments or {}, ensure_ascii=False)
                packed.append({
                    "id": call.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": function.get("name", ""),
                        "arguments": arguments,
                    },
                })
            value["tool_calls"] = packed
        if value.get("role") == "tool":
            value.pop("tool_name", None)
        normalized.append(value)
    return normalized


def _to_openai_tool(schema: dict) -> dict:
    if schema.get("function"):
        return schema
    function = {
        "name": schema.get("name", ""),
        "parameters": schema.get("parameters") or {},
    }
    description = schema.get("description")
    if description:
        function["description"] = description
    return {"type": "function", "function": function}


def _decode_tool_calls(raw_calls) -> list[dict]:
    calls = []
    for call in raw_calls or []:
        function = call.get("function") or {}
        arguments = function.get("arguments")
        if arguments is None or arguments == "":
            arguments = {}
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                raise OpenRouterProviderError("openrouter_invalid_tool_arguments") from None
        if not isinstance(arguments, dict):
            raise OpenRouterProviderError("openrouter_invalid_tool_arguments")
        calls.append({
            "name": function.get("name", ""),
            "arguments": arguments,
        })
    return calls


def _accumulate_tool_calls(accumulated: list[dict], deltas) -> list[dict]:
    for delta in deltas:
        try:
            index = int(delta.get("index") or 0)
        except (TypeError, ValueError):
            continue
        while len(accumulated) <= index:
            accumulated.append({"id": "", "function": {"name": "", "arguments": ""}})
        entry = accumulated[index]
        if delta.get("id"):
            entry["id"] = delta["id"]
        function = delta.get("function") or {}
        if function.get("name"):
            entry["function"]["name"] += function["name"]
        fragment = function.get("arguments")
        if fragment is not None:
            entry["function"]["arguments"] += str(fragment)
    return accumulated


def _build_payload(
    *, model: str, messages: list[dict], temperature: float, max_tokens: int,
    tools: list[dict] | None, json_output: bool, stream: bool,
) -> dict:
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "usage": {"include": True},
        "service_tier": service_tier(),
        **provider_routing_body(),
    }
    if tools:
        payload["tools"] = [_to_openai_tool(tool) for tool in tools]
    if json_output:
        payload["response_format"] = {"type": "json_object"}
    if stream:
        payload["stream"] = True
    return payload


def generate(
    prompt: str,
    *,
    model: str | None = None,
    system_prompt: str | None = None,
    tools: list[dict] | None = None,
    json_output: bool = False,
    temperature: float = 0.5,
    max_tokens: int = 8192,
    deadline_monotonic: float | None = None,
    on_token: "Callable[[str], None] | None" = None,
    conversation_messages: list[dict] | None = None,
) -> dict:
    """Run one chat completion; mirrors the Social completion result contract.

    Returns {"content", "tool_calls", "input_tokens", "output_tokens",
    "duration_ms", "ttft_ms", "tps"}; ttft_ms/tps stay zero when
    not observable (non-stream). Tool calls decode to {"name", "arguments"}.
    """
    from agent_quota.service import record_usage

    call_id = uuid.uuid4().hex
    effective_model = model or selected_model()
    if conversation_messages is not None:
        provider_messages = [
            {"role": "system", "content": system_prompt},
            *_normalize_conversation(conversation_messages),
        ] if system_prompt else _normalize_conversation(conversation_messages)
    else:
        provider_messages = _single_turn_messages(prompt, system_prompt)
    stream = on_token is not None
    payload = _build_payload(
        model=effective_model, messages=provider_messages,
        temperature=temperature, max_tokens=max_tokens,
        tools=tools, json_output=json_output, stream=stream,
    )

    deadline = request_deadline(deadline_monotonic)
    started = time.perf_counter()
    if not stream:
        response = _post(payload, deadline - time.monotonic())
        try:
            data = response.json()
        except ValueError:
            raise OpenRouterProviderError("openrouter_invalid_response") from None
        finally:
            response.close()
        usage = data.get("usage") or {}
        input_tokens = int(usage.get("prompt_tokens") or 0)
        output_tokens = int(usage.get("completion_tokens") or 0)
        try:
            message = (data.get("choices") or [{}])[0].get("message") or {}
        except (IndexError, TypeError):
            raise OpenRouterProviderError("openrouter_invalid_response") from None
        content = (message.get("content") or "").strip()
        tool_calls = _decode_tool_calls(message.get("tool_calls"))
        if input_tokens or output_tokens:
            record_usage(call_id, input_tokens, output_tokens)
        return {
            "content": content, "tool_calls": tool_calls,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "duration_ms": round((time.perf_counter() - started) * 1000),
            "ttft_ms": 0, "tps": 0.0,
        }

    content_parts: list[str] = []
    accumulated: list[dict] = []
    input_tokens = output_tokens = 0
    first_token_at: float | None = None
    response = _post(payload, deadline - time.monotonic(), stream=True)
    try:
        for line in response.iter_lines(decode_unicode=False):
            if time.monotonic() >= deadline:
                raise TimeoutError("OpenRouter request deadline exhausted")
            if not line:
                continue
            text = line.decode("utf-8", "replace") if isinstance(line, bytes) else line
            if not text.startswith("data:"):
                continue
            chunk_text = text[5:].strip()
            if chunk_text == "[DONE]":
                break
            try:
                event = json.loads(chunk_text)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            usage = event.get("usage")
            if isinstance(usage, dict):
                input_tokens = int(usage.get("prompt_tokens") or 0) or input_tokens
                output_tokens = int(usage.get("completion_tokens") or 0) or output_tokens
            choices = event.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            fragment = delta.get("content") or ""
            raw_calls = delta.get("tool_calls")
            if raw_calls:
                accumulated = _accumulate_tool_calls(accumulated, raw_calls)
            if fragment or raw_calls:
                if first_token_at is None:
                    first_token_at = time.perf_counter()
            if fragment:
                content_parts.append(fragment)
                on_token(fragment)
    finally:
        response.close()
    if input_tokens or output_tokens:
        record_usage(call_id, input_tokens, output_tokens)
    content = "".join(content_parts).strip()
    tool_calls = _decode_tool_calls(accumulated)
    finished = time.perf_counter()
    duration_ms = round((finished - started) * 1000)
    ttft_ms = round((first_token_at - started) * 1000) if first_token_at is not None else 0
    decode_seconds = finished - first_token_at if first_token_at is not None else 0.0
    tps = round(output_tokens / decode_seconds, 3) if output_tokens and decode_seconds > 0 else 0.0
    return {
        "content": content, "tool_calls": tool_calls,
        "input_tokens": input_tokens, "output_tokens": output_tokens,
        "duration_ms": duration_ms, "ttft_ms": ttft_ms, "tps": tps,
    }


def verify_key_and_models(timeout_seconds: float = 15.0) -> dict:
    """Fail-fast key and model-catalog check used by the start_all.sh preflight."""
    main_model = selected_model()
    fast_model = selected_model(fast=True)
    required = [main_model] if fast_model == main_model else [main_model, fast_model]
    try:
        response = requests.get(
            _MODELS_URL, headers=_headers(api_key()), timeout=timeout_seconds)
    except requests.Timeout:
        raise OpenRouterProviderError("openrouter_models_unreachable: 驗證請求逾時") from None
    except requests.RequestException as exc:
        raise OpenRouterProviderError("openrouter_models_unreachable: 無法連線 OpenRouter") from None
    if response.status_code != 200:
        response.close()
        _raise_for_status(response.status_code)
    response.close()
    try:
        ids = {item.get("id") for item in (response.json().get("data") or [])}
    except ValueError:
        raise OpenRouterProviderError("openrouter_invalid_response") from None
    missing = [model for model in required if model not in ids]
    if missing:
        raise OpenRouterProviderError(
            "openrouter_model_unavailable: " + ", ".join(missing) +
            " 不在此 Key 可用的模型清單內")
    return {"models": required}


if __name__ == "__main__":
    try:
        result = verify_key_and_models()
    except (OpenRouterProviderError, TimeoutError) as exc:
        raise SystemExit(
            f"OpenRouter preflight failed: {exc}. No services were started.") from None
    print(
        "OpenRouter preflight passed (models: " + ", ".join(result["models"]) +
        ", routing: latency, service tier: " + service_tier() + ").")