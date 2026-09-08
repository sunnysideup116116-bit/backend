"""No-network coverage of retry eligibility and one shared Planner budget."""

from unittest.mock import patch

import httpx
import pytest
from ollama import Client, ResponseError

from services import ai_service
from services.ayue_agent.contracts import PublicAgentTurnContext, TurnClockV1
from services.ayue_agent.v3 import planner


def _turn():
    return PublicAgentTurnContext(
        user_id="owner", room_id="room", message="你好",
        clock=TurnClockV1(
            timezone="Asia/Taipei", utc_iso="2026-09-08T00:00:00Z",
            local_iso="2026-09-08T08:00:00+08:00", local_date="2026-09-08",
            local_time="08:00", weekday_zh_tw="星期二",
        ),
    )


def _valid_result():
    return ai_service.ToolCallResult(content="", tool_calls=[{
        "name": "decompose_tasks",
        "arguments": {
            "mode": "direct_chat", "write_intent": "none",
            "tasks": [], "direct_reply": "你好，今天過得如何？",
        },
    }])


@pytest.mark.parametrize("error, code", [
    (TimeoutError("timeout"), "provider_timeout"),
    (httpx.ReadTimeout("timeout"), "provider_timeout"),
    (ResponseError("unauthorized", 401), "provider_auth_error"),
    (ResponseError("forbidden", 403), "provider_auth_error"),
    (ResponseError("throttled", 429), "provider_rate_limited"),
    (ResponseError("bad request", 400), "provider_error"),
    (RuntimeError("missing configuration"), "provider_error"),
])
def test_nonretryable_provider_error_does_not_make_second_call(error, code):
    with patch.object(planner, "generate_chat_completion_with_tools", side_effect=[error, _valid_result()]) as provider:
        plan, metrics = planner.plan_turn(_turn())

    assert plan is None
    assert provider.call_count == metrics.llm_call_count == 1
    assert metrics.retry_count == 0
    assert metrics.failure_code == code


@pytest.mark.parametrize("error", [ConnectionError("offline"), ResponseError("unavailable", 503)])
def test_transient_provider_error_recovers_inside_same_deadline(error):
    now = [100.0]

    def provider(*_args, **_kwargs):
        if now[0] == 100.0:
            now[0] += 8
            raise error
        return _valid_result()

    with patch.object(planner, "OLLAMA_REQUEST_TIMEOUT_SECONDS", 30), \
         patch.object(planner.time, "monotonic", side_effect=lambda: now[0]), \
         patch.object(planner, "generate_chat_completion_with_tools", side_effect=provider) as call:
        plan, metrics = planner.plan_turn(_turn())

    assert plan is not None
    assert metrics.llm_call_count == 2
    assert metrics.retry_count == 1
    assert metrics.retry_reason == "provider_error"
    assert [item.kwargs["deadline_monotonic"] for item in call.call_args_list] == [130, 130]


def test_schema_repair_keeps_retry_but_does_not_reset_budget():
    invalid = ai_service.ToolCallResult(content="not a tool call", tool_calls=[])
    with patch.object(planner.time, "monotonic", return_value=100), \
         patch.object(planner, "OLLAMA_REQUEST_TIMEOUT_SECONDS", 30), \
         patch.object(planner, "generate_chat_completion_with_tools", side_effect=[invalid, _valid_result()]) as provider:
        plan, metrics = planner.plan_turn(_turn())

    assert plan is not None
    assert metrics.retry_count == 1
    assert metrics.retry_reason == "missing_tool_call"
    assert [item.kwargs["deadline_monotonic"] for item in provider.call_args_list] == [130, 130]


def test_expired_budget_prevents_retry_even_for_repairable_protocol_error():
    now = [100.0]

    def retry_prompt(*_args):
        now[0] = 131.0
        return "repair"

    with patch.object(planner.time, "monotonic", side_effect=lambda: now[0]), \
         patch.object(planner, "OLLAMA_REQUEST_TIMEOUT_SECONDS", 30), \
         patch.object(planner, "_planner_retry_prompt", side_effect=retry_prompt), \
         patch.object(planner, "generate_chat_completion_with_tools", return_value=ai_service.ToolCallResult("", [])) as provider:
        plan, metrics = planner.plan_turn(_turn())

    assert plan is None
    assert provider.call_count == 1
    assert metrics.retry_count == 0
    assert metrics.failure_code == "planner_deadline_exceeded"


def test_transport_uses_remaining_budget_and_restores_context_after_failure():
    now = [100.0]
    timeouts = []

    def transport(request):
        timeouts.append(request.extensions["timeout"])
        if len(timeouts) == 1:
            now[0] += 8
            return httpx.Response(503, json={"error": "temporarily unavailable"})
        return httpx.Response(200, json={
            "model": "mock", "message": {"role": "assistant", "content": "hello"},
            "done": True,
        })

    client = Client(
        host="http://provider.invalid", timeout=30,
        transport=httpx.MockTransport(transport),
        event_hooks={"request": [ai_service._apply_ollama_deadline]},
    )
    try:
        with patch.object(ai_service, "ollama_client", client), \
             patch.object(ai_service, "OLLAMA_API_KEY", "mock"), \
             patch.object(ai_service.time, "monotonic", side_effect=lambda: now[0]):
            with pytest.raises(ResponseError):
                ai_service.generate_chat_completion_with_tools("mock", [], deadline_monotonic=130)
            assert ai_service._OLLAMA_DEADLINE.get() is None
            ai_service.generate_chat_completion_with_tools("mock", [], deadline_monotonic=130)
            ai_service.generate_chat_completion_with_tools("mock", [])
    finally:
        client._client.close()

    assert [item["read"] for item in timeouts] == [30, 22, 30]
    assert all(item["connect"] == item["read"] for item in timeouts)


def test_late_provider_result_is_not_accepted_or_retried():
    now = [100.0]

    def provider(*_args, **_kwargs):
        now[0] = 131.0
        return _valid_result()

    with patch.object(planner.time, "monotonic", side_effect=lambda: now[0]), \
         patch.object(planner, "OLLAMA_REQUEST_TIMEOUT_SECONDS", 30), \
         patch.object(planner, "generate_chat_completion_with_tools", side_effect=provider) as call:
        plan, metrics = planner.plan_turn(_turn())

    assert plan is None
    assert call.call_count == 1
    assert metrics.failure_code == "provider_timeout"
