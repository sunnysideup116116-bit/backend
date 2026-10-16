"""OpenRouter dispatch tests for the Social shared completion entry points."""
import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from services import ai_service


def _openrouter_mode(monkeypatch):
    monkeypatch.setattr(ai_service.codex_chat_provider, "selected_provider", lambda: "openrouter")
    monkeypatch.setattr(ai_service, "OLLAMA_API_KEY", "stub")


FAKE_RESULT = {
    "content": "回覆內容", "tool_calls": [],
    "input_tokens": 7, "output_tokens": 3, "duration_ms": 111,
    "ttft_ms": 0, "tps": 0.0,
}


def test_resolve_chat_model_openrouter(monkeypatch):
    _openrouter_mode(monkeypatch)
    assert ai_service._resolve_chat_model() == "deepseek/deepseek-v4.1-flash"
    assert ai_service._resolve_chat_model(prefer_fast_model=True) == "deepseek/deepseek-v4.1-flash"


def test_generate_chat_completion_dispatch(monkeypatch):
    _openrouter_mode(monkeypatch)
    captured = {}

    def fake_generate(prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return dict(FAKE_RESULT)

    monkeypatch.setattr(ai_service.openrouter_provider, "generate", fake_generate)
    result = ai_service.generate_chat_completion(
        "你好多喂", system_prompt="SYS", temperature=0.3, max_tokens=256,
        model_owner="match")
    assert result.content == "回覆內容"
    assert result.input_tokens == 7 and result.duration_ms == 111
    assert captured["model"] == "deepseek/deepseek-v4.1-flash"
    assert captured["system_prompt"] == "SYS"
    assert captured["temperature"] == 0.3 and captured["max_tokens"] == 256
    assert captured["json_output"] is False


def test_generate_chat_completion_json_strips_fences(monkeypatch):
    _openrouter_mode(monkeypatch)
    monkeypatch.setattr(ai_service.openrouter_provider, "generate", lambda p, **k: {
        **FAKE_RESULT, "content": '```json\n{"a": 1}\n```'})
    result = ai_service.generate_chat_completion("p", json_output=True)
    assert result.content == '{"a": 1}'


def test_generate_chat_completion_plain_normalizes(monkeypatch):
    _openrouter_mode(monkeypatch)
    calls = {}
    monkeypatch.setattr(
        ai_service, "normalize_zh_tw", lambda text: calls.setdefault("t", text) + "!")
    monkeypatch.setattr(ai_service.openrouter_provider, "generate", lambda p, **k: dict(FAKE_RESULT))
    result = ai_service.generate_chat_completion("p")
    assert result.content == "回覆內容!"


def test_tool_completion_dispatch(monkeypatch):
    _openrouter_mode(monkeypatch)
    captured = {}

    def fake_generate(prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return {
            **FAKE_RESULT,
            "tool_calls": [{"name": "t", "arguments": {"k": 1}}],
            "ttft_ms": 20, "tps": 9.5,
        }

    monkeypatch.setattr(ai_service.openrouter_provider, "generate", fake_generate)
    conversation = [{"role": "user", "content": "@USER"}]
    result = ai_service.generate_chat_completion_with_tools(
        "payload", tools=[{"name": "t", "parameters": {}}],
        prefer_fast_model=True, on_token=None,
        conversation_messages=conversation, model_owner="planner")
    assert result.tool_calls == [{"name": "t", "arguments": {"k": 1}}]
    assert result.ttft_ms == 20 and result.tps == 9.5
    assert result.model_name == "deepseek/deepseek-v4.1-flash"
    assert captured["conversation_messages"] == conversation
    assert captured["tools"] == [{"name": "t", "parameters": {}}]


def test_assessment_json_dispatch(monkeypatch):
    _openrouter_mode(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        ai_service, "request_assessment_json",
        lambda call, kind, model: {"model": model, "value": call(0.0), "kind": kind})

    def fake_generate(prompt, **kwargs):
        captured.update(prompt=prompt, **kwargs)
        return {"content": '{"answer": 42}'}

    monkeypatch.setattr(ai_service.openrouter_provider, "generate", fake_generate)
    result = ai_service._generate_assessment_json("assess", "big_five", 0.1)
    assert result["model"] == "deepseek/deepseek-v4.1-flash"
    assert result["kind"] == "big_five"
    assert captured["json_output"] is True
    assert captured["max_tokens"] == 2048


def test_ollama_path_falls_back_when_not_openrouter(monkeypatch):
    """The ollama branch stays intact when the provider is not openrouter."""
    monkeypatch.setattr(ai_service.codex_chat_provider, "selected_provider", lambda: "ollama")

    def boom(*args, **kwargs):
        raise AssertionError("openrouter adapter must not run in ollama mode")

    monkeypatch.setattr(ai_service.openrouter_provider, "generate", boom)
    monkeypatch.setattr(
        ai_service.ollama_client, "chat",
        lambda **payload: {"message": {"content": "hello"},
                           "prompt_eval_count": 5, "eval_count": 2})
    monkeypatch.setattr(
        ai_service, "tracked_ollama",
        lambda response: response)
    result = ai_service.generate_chat_completion("p")
    assert result.content == "hello"
    assert result.input_tokens == 5 and result.output_tokens == 2