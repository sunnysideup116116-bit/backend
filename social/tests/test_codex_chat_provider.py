"""Offline only: every process launch is blocked or replaced with a fake."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from services import codex_chat_provider as provider


@pytest.fixture(autouse=True)
def no_real_codex(monkeypatch):
    monkeypatch.setattr(provider.subprocess, "Popen", Mock(side_effect=AssertionError("No real child allowed")))
    monkeypatch.setenv("AYUE_LLM_PROVIDER", "ollama")
    monkeypatch.setenv("AYUE_GPT_MODEL", "test-gpt")
    monkeypatch.setenv("AYUE_CODEX_HOME", "/tmp/test-managed-home")
    monkeypatch.delenv("AYUE_GPT_FAST_MODEL", raising=False)
    monkeypatch.delenv("AYUE_GPT_TIMEOUT_SECONDS", raising=False)
    monkeypatch.delenv("AYUE_GPT_SERVICE_TIER", raising=False)


class FakeRpc:
    def __init__(self, output=None, status="completed", account="chatgpt"):
        self.calls = []
        self.output = output if output is not None else {"content": "hello"}
        self.account = account
        self.events = [
            {"method": "item/completed", "params": {"threadId": "t", "turnId": "u", "item": {
                "type": "agentMessage", "text": json.dumps(self.output), "phase": "final_answer"}}},
            {"method": "turn/completed", "params": {"threadId": "t", "turn": {"id": "u", "status": status}}},
        ]

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "account/read":
            return {"account": {"type": self.account}}
        if method == "model/list":
            return {"data": [{"model": "test-gpt"}], "nextCursor": None}
        if method == "mcpServerStatus/list":
            return {"data": []}
        if method == "thread/start":
            return {"thread": {"id": "t"}, "model": "test-gpt", "modelProvider": "openai",
                    "instructionSources": [], "sandbox": {"type": "readOnly"}}
        if method == "turn/start":
            if "outputSchema" not in params:
                for event in self.events:
                    item = event.get("params", {}).get("item", {})
                    if item.get("type") == "agentMessage":
                        item["text"] = self.output.get("content")
            return {"turn": {"id": "u"}}
        if method == "thread/unsubscribe":
            return {"status": "unsubscribed"}
        raise AssertionError(method)

    def event(self):
        return self.events.pop(0)


def mock_session(monkeypatch, rpc):
    closed = []

    @contextmanager
    def fake(deadline):
        try:
            yield rpc, "/tmp/empty-call"
        finally:
            closed.append(True)

    monkeypatch.setattr(provider, "session", fake)
    return closed


TOOL = {"type": "function", "function": {"name": "propose", "parameters": {
    "type": "object", "properties": {"optional": {"type": "string"}}, "additionalProperties": False,
}}}


def test_proposals_are_validated_and_not_registered_or_executed(monkeypatch):
    rpc = FakeRpc({"content": "", "tool_calls": [{"name": "propose", "arguments_json": "{}"}]})
    closed = mock_session(monkeypatch, rpc)
    result = provider.generate("slice only", model="test-gpt", tools=[TOOL])
    assert result["tool_calls"] == [{"name": "propose", "arguments": {}}]
    thread = dict(rpc.calls)["thread/start"]
    assert thread["ephemeral"] is True
    assert thread["environments"] == thread["dynamicTools"] == thread["selectedCapabilityRoots"] == []
    assert thread["allowProviderModelFallback"] is False
    assert thread["approvalPolicy"] == "never"
    assert dict(rpc.calls)["turn/start"]["input"][0]["text"] == "slice only"
    assert closed == [True]


@pytest.mark.parametrize("output", [
    {"content": "", "tool_calls": [{"name": "unknown", "arguments_json": "{}"}]},
    {"content": "", "tool_calls": [{"name": "propose", "arguments_json": '{"optional":3}'}]},
    {"content": "", "tool_calls": [{"name": "propose", "arguments_json": "[]"}]},
    {"content": "", "tool_calls": [{"name": "propose", "arguments_json": "invalid"}]},
    {"content": "", "tool_calls": [], "unexpected": True},
])
def test_bad_proposals_never_reach_callbacks(monkeypatch, output):
    closed = mock_session(monkeypatch, FakeRpc(output))
    callback = Mock()
    with pytest.raises(provider.CodexProviderError, match="Invalid Codex proposal"):
        provider.generate("x", model="test-gpt", tools=[TOOL], on_token=callback)
    callback.assert_not_called()
    assert closed


@pytest.mark.parametrize("status", ["failed", "interrupted", "inProgress"])
def test_failed_turn_discards_final_output(monkeypatch, status):
    mock_session(monkeypatch, FakeRpc(status=status))
    with pytest.raises(provider.CodexProviderError, match="did not complete"):
        provider.generate("x", model="test-gpt")


def test_json_content_and_final_only_callback(monkeypatch):
    mock_session(monkeypatch, FakeRpc({"content": '{"ok":true}'}))
    emitted = []
    result = provider.generate("x", model="test-gpt", json_output=True, on_token=emitted.append)
    assert emitted == [result["content"]] == ['{"ok":true}']
    mock_session(monkeypatch, FakeRpc())
    with pytest.raises(provider.CodexProviderError):
        provider.generate("x", model="test-gpt", json_output=True)


@pytest.mark.parametrize("account", ["apiKey", "chatgptAuthTokens", None])
def test_preflight_rejects_other_auth_without_turn(monkeypatch, account):
    rpc = FakeRpc(account=account)
    mock_session(monkeypatch, rpc)
    with pytest.raises(provider.CodexProviderError, match="Managed ChatGPT"):
        provider.preflight()
    assert [name for name, _ in rpc.calls] == ["account/read"]


def test_preflight_is_non_generative_and_checks_fast_model(monkeypatch):
    rpc = FakeRpc()
    mock_session(monkeypatch, rpc)
    provider.preflight()
    assert [name for name, _ in rpc.calls] == ["account/read", "model/list", "mcpServerStatus/list"]
    monkeypatch.setenv("AYUE_GPT_FAST_MODEL", "missing")
    with pytest.raises(provider.CodexProviderError, match="unavailable"):
        provider.preflight()


def test_model_pagination():
    rpc = Mock()
    rpc.call.side_effect = [
        {"account": {"type": "chatgpt"}},
        {"data": [], "nextCursor": "next"},
        {"data": [{"model": "test-gpt"}]}, {"data": []},
    ]
    provider.verify_account_and_models(rpc, ["test-gpt"])
    assert rpc.call.call_args_list[2].args[1]["cursor"] == "next"


def test_selection_and_deadline(monkeypatch):
    assert provider.selected_provider() == "ollama"
    monkeypatch.setenv("AYUE_LLM_PROVIDER", "gpt")
    assert provider.selected_provider() == "gpt"
    assert provider.selected_model(fast=True) == "test-gpt"
    monkeypatch.setenv("AYUE_GPT_PLANNER_MODEL", "planner-gpt")
    assert provider.selected_model(fast=True, owner="planner") == "planner-gpt"
    monkeypatch.setenv("AYUE_GPT_PLANNER_MODEL", "  ")
    assert provider.selected_model(fast=True, owner="planner") == "test-gpt"
    with pytest.raises(provider.CodexProviderError, match="Unknown LLM owner"):
        provider.selected_model(owner="unknown")
    monkeypatch.setenv("AYUE_LLM_PROVIDER", "other")
    with pytest.raises(provider.CodexProviderError):
        provider.selected_provider()
    with pytest.raises(TimeoutError):
        provider.request_deadline(1.0)
    for invalid in ("0", "-1", "nan", "inf", "bad"):
        monkeypatch.setenv("AYUE_GPT_TIMEOUT_SECONDS", invalid)
        with pytest.raises(provider.CodexProviderError):
            provider.request_deadline()


@contextmanager
def pipe_rpc(data=b"", timeout=1):
    read_fd, write_fd = os.pipe()
    input_read, input_write = os.pipe()
    with os.fdopen(read_fd, "rb", buffering=0) as stdout, os.fdopen(input_write, "wb", buffering=0) as stdin:
        try:
            if data:
                os.write(write_fd, data)
            yield provider.StdioRpc(SimpleNamespace(stdout=stdout, stdin=stdin), time.monotonic() + timeout), write_fd, input_read
        finally:
            os.close(write_fd)
            os.close(input_read)


def test_rpc_interleaved_notification():
    frames = b'{"method":"thread/started","params":{}}\n{"id":1,"result":{"ok":true}}\n'
    with pipe_rpc(frames) as (rpc, _, __):
        assert rpc.call("example", {}) == {"ok": True}
        assert rpc.event()["method"] == "thread/started"


@pytest.mark.parametrize("frame", [b'bad\n', b'[]\n', b'{"id":1,"error":{"message":"PRIVATE"}}\n', b'{"id":9,"result":{}}\n'])
def test_protocol_failures_are_sanitized(frame):
    with pipe_rpc(frame) as (rpc, _, __):
        with pytest.raises(provider.CodexProviderError) as exc:
            rpc.call("example", {})
        assert "PRIVATE" not in str(exc.value)


def test_client_action_is_denied():
    with pipe_rpc(b'{"id":55,"method":"item/tool/call","params":{}}\n') as (rpc, _, input_read):
        with pytest.raises(provider.CodexProviderError, match="forbidden"):
            rpc.receive()
        assert json.loads(os.read(input_read, 4096))["error"]["code"] == -32601


def test_silent_child_times_out():
    with pipe_rpc(timeout=0.01) as (rpc, _, __):
        with pytest.raises(TimeoutError):
            rpc.receive()


def test_session_scrubs_environment_and_reaps_on_timeout(monkeypatch):
    process = Mock(pid=987654)
    process.wait.side_effect = [provider.subprocess.TimeoutExpired("mock", 1), 0]
    launch = Mock(return_value=process)
    monkeypatch.setattr(provider.subprocess, "Popen", launch)
    monkeypatch.setattr(provider, "StdioRpc", Mock(return_value=Mock()))
    kill = Mock()
    monkeypatch.setattr(provider.os, "killpg", kill)
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-propagate")
    with pytest.raises(TimeoutError):
        with provider.session(time.monotonic() + 1):
            raise TimeoutError
    env = launch.call_args.kwargs["env"]
    assert "OPENAI_API_KEY" not in env
    assert env["CODEX_HOME"] == "/tmp/test-managed-home"
    assert launch.call_args.kwargs["start_new_session"] is True
    assert kill.call_count == 2
    assert not Path(launch.call_args.kwargs["cwd"]).exists()


def test_shared_entrypoints_route_gpt_without_ollama_key(monkeypatch):
    from services import ai_service
    monkeypatch.setenv("AYUE_LLM_PROVIDER", "gpt")
    monkeypatch.setenv("AYUE_GPT_FAST_MODEL", "test-fast")
    monkeypatch.setattr(ai_service, "OLLAMA_API_KEY", "")
    ollama = Mock(side_effect=AssertionError("No Ollama fallback"))
    monkeypatch.setattr(ai_service.ollama_client, "chat", ollama)
    generate = Mock(return_value={"content": "hello", "tool_calls": [], "duration_ms": 12})
    monkeypatch.setattr(provider, "generate", generate)
    assert ai_service.generate_chat_completion("x", model="old-ollama").content == "hello"
    assert generate.call_args.kwargs["model"] == "test-gpt"
    result = ai_service.generate_chat_completion_with_tools("slice", [], prefer_fast_model=True, deadline_monotonic=1.0)
    assert result.model_name == "test-fast"
    assert result.ttft_ms == result.input_tokens == result.output_tokens == 0
    assert generate.call_args.kwargs["deadline_monotonic"] == 1.0
    generate.side_effect = provider.CodexProviderError("failed")
    with pytest.raises(provider.CodexProviderError):
        ai_service.generate_chat_completion("x")
    ollama.assert_not_called()


def test_empty_fast_model_uses_main(monkeypatch):
    monkeypatch.setenv("AYUE_GPT_FAST_MODEL", "  ")
    assert provider.selected_model(fast=True) == "test-gpt"


def test_expired_deadline_does_not_create_session(monkeypatch):
    launch = Mock(side_effect=AssertionError("expired request launched"))
    monkeypatch.setattr(provider, "session", launch)
    with pytest.raises(TimeoutError):
        provider.generate("x", model="test-gpt", deadline_monotonic=1.0)
    launch.assert_not_called()


def test_server_retry_and_usage_and_bounded_callback(monkeypatch):
    rpc = FakeRpc({"content": "x" * 701})
    rpc.events[:0] = [
        {"method": "error", "params": {"threadId": "t", "turnId": "u", "willRetry": True}},
        {"method": "error", "params": {"threadId": "other", "turnId": "u", "willRetry": False}},
        {"method": "thread/tokenUsage/updated", "params": {"threadId": "t", "turnId": "u", "tokenUsage": {
            "last": {"inputTokens": 31, "outputTokens": 14}}}},
    ]
    mock_session(monkeypatch, rpc)
    chunks = []
    result = provider.generate("x", model="test-gpt", on_token=chunks.append)
    assert result["input_tokens"] == 31 and result["output_tokens"] == 14
    assert "".join(chunks) == "x" * 701
    assert max(map(len, chunks)) <= 120
