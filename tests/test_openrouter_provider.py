"""Offline OpenRouter adapter tests; all HTTP is mocked, no sockets are used."""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from llm_providers import openrouter_provider as ops


PROVIDER_ENV = {"AYUE_LLM_PROVIDER": "openrouter", "OPENROUTER_API_KEY": "test-key"}


def _env(monkeypatch, **overrides):
    base = dict(PROVIDER_ENV)
    base.update(overrides)
    for key in ("AYUE_LLM_PROVIDER", "OPENROUTER_API_KEY", "AYUE_OPENROUTER_MODEL",
                "AYUE_OPENROUTER_FAST_MODEL", "AYUE_OPENROUTER_TIMEOUT_SECONDS",
                "AYUE_OPENROUTER_SERVICE_TIER"):
        monkeypatch.delenv(key, raising=False)
    for key, value in base.items():
        if value is not None:
            monkeypatch.setenv(key, value)
    return base


def _response(status=200, payload=None, lines=None):
    response = MagicMock()
    response.status_code = status
    if payload is not None:
        response.json.return_value = payload
    if lines is not None:
        response.iter_lines.return_value = iter(lines)
    else:
        response.iter_lines.side_effect = AssertionError("stream not requested")
    return response


def test_selected_provider_whitelist(monkeypatch):
    _env(monkeypatch)
    assert ops.selected_provider() == "openrouter"
    monkeypatch.setenv("AYUE_LLM_PROVIDER", "ollama")
    assert ops.selected_provider() == "ollama"
    monkeypatch.setenv("AYUE_LLM_PROVIDER", "gpt")
    assert ops.selected_provider() == "gpt"
    monkeypatch.setenv("AYUE_LLM_PROVIDER", "unknown")
    with pytest.raises(ops.OpenRouterProviderError, match="ollama, gpt, openrouter"):
        ops.selected_provider()


def test_selected_model_defaults_and_overrides(monkeypatch):
    _env(monkeypatch)
    assert ops.selected_model() == "deepseek/deepseek-v4.1-flash"
    assert ops.selected_model(fast=True) == "deepseek/deepseek-v4.1-flash"
    monkeypatch.setenv("AYUE_OPENROUTER_MODEL", "other/model")
    monkeypatch.setenv("AYUE_OPENROUTER_FAST_MODEL", "fast/model")
    assert ops.selected_model() == "other/model"
    assert ops.selected_model(fast=True) == "fast/model"
    monkeypatch.setenv("AYUE_OPENROUTER_FAST_MODEL", "  ")
    assert ops.selected_model(fast=True) == "other/model"


def test_missing_api_key_fails_sanitized(monkeypatch):
    _env(monkeypatch, OPENROUTER_API_KEY="  ")
    with pytest.raises(ops.OpenRouterProviderError, match="OPENROUTER_API_KEY"):
        ops.generate("hello")


def test_provider_routing_body_requests_latency():
    assert ops.provider_routing_body() == {"provider": {"sort": "latency"}}


def test_service_tier_defaults_to_standard_pool(monkeypatch):
    _env(monkeypatch)
    assert ops.service_tier() == "default"
    assert ops.routing_extra_body() == {
        "provider": {"sort": "latency"}, "service_tier": "default"}


def test_service_tier_env_override_and_invalid(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("AYUE_OPENROUTER_SERVICE_TIER", "priority")
    assert ops.service_tier() == "priority"
    monkeypatch.setenv("AYUE_OPENROUTER_SERVICE_TIER", "bogus")
    with pytest.raises(ops.OpenRouterProviderError, match="SERVICE_TIER"):
        ops.service_tier()


def test_openai_tool_conversion(monkeypatch):
    _env(monkeypatch)
    native = {"name": "t1", "description": "d", "parameters": {"type": "object"}}
    converted = ops._to_openai_tool(native)
    assert converted == {
        "type": "function",
        "function": {"name": "t1", "parameters": {"type": "object"}, "description": "d"},
    }
    already = {"type": "function", "function": {"name": "x", "parameters": {}}}
    assert ops._to_openai_tool(already) is already


def test_normalize_conversation_shapes(monkeypatch):
    _env(monkeypatch)
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "t", "arguments": {"k": "v"}}},
        ]},
        {"role": "tool", "tool_call_id": "c1", "tool_name": "t", "content": "ok"},
    ]
    normalized = ops._normalize_conversation(messages)
    assert normalized[1]["tool_calls"][0]["function"]["arguments"] == '{"k": "v"}'
    assert "tool_name" not in normalized[2]
    assert normalized[2]["tool_call_id"] == "c1"


def test_decode_tool_calls_parses_and_fails_closed(monkeypatch):
    _env(monkeypatch)
    decoded = ops._decode_tool_calls([
        {"function": {"name": "t", "arguments": '{"a": 1}'}},
        {"function": {"name": "u", "arguments": {"b": 2}}},
    ])
    assert decoded == [{"name": "t", "arguments": {"a": 1}}, {"name": "u", "arguments": {"b": 2}}]
    with pytest.raises(ops.OpenRouterProviderError, match="invalid_tool_arguments"):
        ops._decode_tool_calls([{"function": {"name": "t", "arguments": "{bad"}}])


def test_accumulate_tool_calls_merges_deltas(monkeypatch):
    _env(monkeypatch)
    accumulated = ops._accumulate_tool_calls([], [
        {"index": 0, "id": "c1", "function": {"name": "t", "arguments": '{"a"'}},
        {"index": 0, "function": {"arguments": ': 1}'}},
        {"index": 1, "id": "c2", "function": {"name": "u", "arguments": '{}'}},
    ])
    assert accumulated[0]["function"]["arguments"] == '{"a": 1}'
    assert accumulated[1] == {"id": "c2", "function": {"name": "u", "arguments": "{}"}}


def test_generate_non_stream_payload_and_usage(monkeypatch):
    _env(monkeypatch)
    recorded = []
    monkeypatch.setattr("agent_quota.service.record_usage", lambda *a: recorded.append(a))
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        seen.update(url=url, json=json, headers=headers, stream=stream)
        return _response(payload={
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            "choices": [{"message": {"content": "hi", "tool_calls": []}}],
        })

    monkeypatch.setattr(ops.requests, "post", fake_post)
    result = ops.generate("hello", system_prompt="sys", temperature=0.2, max_tokens=99)
    assert seen["url"].startswith("https://openrouter.ai/api/v1")
    body = seen["json"]
    assert body["provider"] == {"sort": "latency"}
    assert body["service_tier"] == "default"
    assert body["model"] == "deepseek/deepseek-v4.1-flash"
    assert body["temperature"] == 0.2 and body["max_tokens"] == 99
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hello"},
    ]
    assert "stream" not in body
    assert seen["headers"]["Authorization"] == "Bearer test-key"
    assert seen["headers"]["HTTP-Referer"]
    assert result["content"] == "hi"
    assert result["input_tokens"] == 10 and result["output_tokens"] == 5
    assert result["tool_calls"] == [] and result["ttft_ms"] == 0
    assert len(recorded) == 1
    assert recorded[0][1:] == (10, 5)


def test_generate_json_output_uses_response_format_and_tools(monkeypatch):
    _env(monkeypatch)
    seen = {}

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        seen.update(json=json)
        return _response(payload={
            "usage": {},
            "choices": [{"message": {
                "content": '{"x": 1}',
                "tool_calls": [{"function": {"name": "t", "arguments": '{"a": 1}'}}],
            }}],
        })

    monkeypatch.setattr(ops.requests, "post", fake_post)
    result = ops.generate(
        "hello", json_output=True,
        tools=[{"name": "t", "parameters": {"type": "object"}}],
        conversation_messages=[
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "t", "arguments": {"k": 1}}}]},
        ])
    assert seen["json"]["response_format"] == {"type": "json_object"}
    assert seen["json"]["tools"] == [{
        "type": "function", "function": {"name": "t", "parameters": {"type": "object"}},
    }]
    assert seen["json"]["messages"][0]["tool_calls"][0]["function"]["arguments"] == '{"k": 1}'
    assert result["tool_calls"] == [{"name": "t", "arguments": {"a": 1}}]


def test_generate_stream_delivers_tokens_and_usage(monkeypatch):
    _env(monkeypatch)
    recorded = []
    monkeypatch.setattr("agent_quota.service.record_usage", lambda *a: recorded.append(a))
    events = [
        b'data: {"choices": [{"delta": {"content": "he"}}]}',
        b'data: {"choices": [{"delta": {"content": "llo"}}]}',
        b'data: {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}',
        b"data: [DONE]",
    ]

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        assert json["stream"] is True
        return _response(lines=events)

    tokens = []

    def on_token(fragment):
        tokens.append(fragment)

    monkeypatch.setattr(ops.requests, "post", fake_post)
    result = ops.generate("hello", on_token=on_token)
    assert tokens == ["he", "llo"]
    assert result["content"] == "hello"
    assert result["input_tokens"] == 3 and result["output_tokens"] == 2
    assert result["ttft_ms"] >= 0 and result["tps"] > 0
    assert recorded == [(recorded[0][0], 3, 2)]


def test_generate_deadline_exhausted(monkeypatch):
    _env(monkeypatch)
    deadline = 0.0
    with pytest.raises(TimeoutError):
        ops.generate("hello", deadline_monotonic=deadline)


def test_generate_handles_stream_tool_call_deltas(monkeypatch):
    """Streaming tool calls accumulate across delta chunks and decode once."""
    _env(monkeypatch)
    monkeypatch.setattr("agent_quota.service.record_usage", lambda *a: None)
    lines = [
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "t", "arguments": "{\\"a\\""}}]}}]}',
        'data: {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ": 1}"}}]}}]}',
        "data: [DONE]",
    ]
    lines = [line.encode() for line in lines]

    def fake_post(url, headers=None, json=None, timeout=None, stream=False):
        return _response(lines=lines)

    monkeypatch.setattr(ops.requests, "post", fake_post)
    result = ops.generate("hello", tools=[{"name": "t", "parameters": {}}], on_token=lambda f: None)
    assert result["tool_calls"] == [{"name": "t", "arguments": {"a": 1}}]


@pytest.mark.parametrize("status,detail", [(401, "unauthorized"), (402, "payment_required"), (403, "forbidden"), (429, "rate_limited"), (500, "HTTP 500")])
def test_generate_sanitized_status_errors(monkeypatch, status, detail):
    _env(monkeypatch)
    monkeypatch.setattr(ops.requests, "post", lambda *a, **k: _response(status=status))
    with pytest.raises(ops.OpenRouterProviderError, match=detail):
        ops.generate("hello")


def test_verify_key_and_models_success(monkeypatch):
    _env(monkeypatch)

    def fake_get(url, headers=None, timeout=None):
        assert url.startswith("https://openrouter.ai/api/v1/models")
        assert headers["Authorization"] == "Bearer test-key"
        return _response(payload={"data": [{"id": "deepseek/deepseek-v4.1-flash"}]})

    monkeypatch.setattr(ops.requests, "get", fake_get)
    result = ops.verify_key_and_models()
    assert result["models"] == ["deepseek/deepseek-v4.1-flash"]


def test_verify_key_and_models_rejects_missing_model(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setenv("AYUE_OPENROUTER_MODEL", "not/a-model")
    monkeypatch.setattr(
        ops.requests, "get",
        lambda *a, **k: _response(payload={"data": [{"id": "deepseek/deepseek-v4.1-flash"}]}))
    with pytest.raises(ops.OpenRouterProviderError, match="not/a-model"):
        ops.verify_key_and_models()


def test_verify_key_and_models_rejects_bad_key(monkeypatch):
    _env(monkeypatch)
    monkeypatch.setattr(ops.requests, "get", lambda *a, **k: _response(status=401))
    with pytest.raises(ops.OpenRouterProviderError, match="unauthorized"):
        ops.verify_key_and_models()


def test_load_settings_respects_explicit_env(monkeypatch, tmp_path):
    env = {}
    env["OPENROUTER_API_KEY"] = "explicit-key"
    env["AYUE_SKIP_DOTENV"] = "1"
    ops.load_openrouter_settings(tmp_path / "missing.env", environ=env)
    assert env["OPENROUTER_API_KEY"] == "explicit-key"
    env2 = {}
    env2["OPENROUTER_API_KEY"] = "explicit-key"
    (tmp_path / ".env").write_text('OPENROUTER_API_KEY="dotenv-key"\nAYUE_OPENROUTER_MODEL=model-from-dotenv\n')
    ops.load_openrouter_settings(tmp_path / ".env", environ=env2)
    assert env2["OPENROUTER_API_KEY"] == "explicit-key"
    assert env2["AYUE_OPENROUTER_MODEL"] == "model-from-dotenv"