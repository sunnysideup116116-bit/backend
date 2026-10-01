"""Safe attempt diagnostics without changing accepted validator semantics.

All inputs/transports/accounting are disposable stubs; no provider or database
connections are made. Secret sentinels deliberately exercise log redaction.
"""
import asyncio
from contextlib import contextmanager
import importlib
import json
import logging
import os
import re
import subprocess
import sys
import socket
import ssl
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

import related_interest_validator as validator


MODEL = "deepseek-v4.1-flash:cloud"
PRIVATE = "PRIVATE_SENTINEL_prompt_owner_token_password_provider_body"
FINAL_SUCCESS = {"success", "completed", "accepted", "normal_no_match", "no_match"}


@pytest.fixture
def diagnostics(caplog):
    # A missing API is a clear runtime RED, not an unrelated collection failure.
    try:
        result = importlib.import_module("validator_diagnostics")
    except ModuleNotFoundError:
        pytest.fail("safe validator diagnostic module/API is absent")
    for name in ("diagnostic_scope", "current_trace", "emit", "LOG"):
        assert hasattr(result, name), f"safe validator diagnostic API missing: {name}"
    caplog.set_level(logging.INFO, logger=result.LOG.name)
    return result


def completion(*relations, model=MODEL, text=None, finish="stop", usage=None):
    if text is None:
        text = json.dumps({"results": [{"id": str(i), "relation": relation}
            for i, relation in enumerate(relations)]})
    return SimpleNamespace(model=model, usage=usage,
        choices=[SimpleNamespace(finish_reason=finish,
            message=SimpleNamespace(content=text))])


def records(caplog):
    result = []
    for record in caplog.records:
        message = record.getMessage()
        if message.startswith("validator_diagnostic "):
            result.append(json.loads(message.removeprefix("validator_diagnostic ")))
    return result


def validate(monkeypatch, diagnostics, outcomes, *, count=2, deadline=18., clock=lambda: 0.):
    outcomes = iter(outcomes)
    timeouts = []

    def complete(_client, *, timeout, **_request):
        timeouts.append(timeout)
        outcome = next(outcomes)
        if callable(outcome):
            return outcome()
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(validator, "_completion", complete)
    with diagnostics.diagnostic_scope("semantic_validator") as trace:
        result = validator.validate_concepts(PRIVATE,
            [{"semantic_text": PRIVATE + str(i)} for i in range(count)],
            SimpleNamespace(api_key=PRIVATE, base_url="https://provider.invalid/v1"),
            MODEL, deadline=deadline, clock=clock)
    return result, timeouts, trace


def final_record(caplog):
    finals = [record for record in records(caplog) if record["event"] == "validator_result"]
    assert len(finals) == 1, "every validator call needs exactly one final diagnostic"
    return finals[0]


def assert_safe(caplog):
    assert PRIVATE not in caplog.text
    for record in records(caplog):
        assert re.fullmatch(r"[a-f0-9]{32}", record["correlation_id"])
        assert re.fullmatch(r"[a-f0-9]{32}", record["call_id"])
        assert not {"prompt", "query", "owner", "owner_id", "preference", "message",
            "response", "authorization", "api_key", "url", "headers", "body"} & record.keys()


def test_scope_is_server_owned_nested_and_reset_after_exception(diagnostics, caplog):
    assert diagnostics.current_trace() is None
    with diagnostics.diagnostic_scope("event_request") as outer:
        assert diagnostics.current_trace() is outer
        with pytest.raises(RuntimeError):
            with diagnostics.diagnostic_scope("semantic_validator") as inner:
                assert inner.correlation_id == outer.correlation_id
                assert inner.call_id != outer.call_id
                assert diagnostics.current_trace() is inner
                inner.emit("attempt_result", stage="transport", category="provider_timeout",
                    elapsed_ms=12., owner=PRIVATE, api_key=PRIVATE, prompt=PRIVATE,
                    response=PRIVATE, provider_code=PRIVATE, exception_class=PRIVATE,
                    arbitrary_field=PRIVATE)
                raise RuntimeError(PRIVATE)
        assert diagnostics.current_trace() is outer
    assert diagnostics.current_trace() is None
    assert_safe(caplog)


def test_success_emits_bounded_attempt_and_final_metadata(monkeypatch, diagnostics, caplog):
    (accepted, counts), timeouts, trace = validate(monkeypatch, diagnostics,
        [completion("candidate_more_specific", "unrelated")])
    assert len(accepted) == 1 and counts["accepted"] == counts["rejected"] == 1
    assert counts["attempts"] == 1 and counts["retries"] == counts["error"] == 0
    assert timeouts == [6.]
    emitted = records(caplog)
    assert [row["event"] for row in emitted].count("validator_start") == 1
    starts = [row for row in emitted if row["event"] == "attempt_start"]
    attempts = [row for row in emitted if row["event"] == "attempt_result"]
    assert len(starts) == len(attempts) == 1
    assert starts[0]["timeout_budget_ms"] == 6000.
    assert starts[0]["remaining_shared_ms"] == 18000.
    assert attempts[0]["category"] == "success"
    assert attempts[0]["parser_result"] in {"accepted", "valid", "success"}
    assert final_record(caplog)["final_typed_outcome"] in FINAL_SUCCESS
    assert all(row["correlation_id"] == trace.correlation_id for row in emitted)
    assert_safe(caplog)


def status_error(status):
    request = httpx.Request("POST", "https://provider.invalid/v1")
    return APIStatusError(PRIVATE,
        response=httpx.Response(status, request=request),
        body={"error": {"message": PRIVATE, "code": PRIVATE}})


@pytest.mark.parametrize("status,category", [
    (401, "provider_account_or_quota"), (403, "provider_account_or_quota"),
    (429, "provider_rate_limit"), (500, "provider_5xx"),
    (502, "provider_5xx"), (503, "provider_5xx"),
])
def test_systemic_status_is_diagnosed_and_overrides_prior_trusted_decisions(
        monkeypatch, diagnostics, caplog, status, category):
    (accepted, counts), timeouts, _ = validate(monkeypatch, diagnostics,
        [completion("candidate_more_specific", "unrelated"), status_error(status), status_error(status)], count=4)
    assert not accepted and counts["job_unavailable"] == 1
    assert counts["accepted"] == counts["rejected"] == 1 and counts["error"] == 2
    assert counts["attempts"] == 3 and counts["retries"] == 1
    assert timeouts == [6., 6., 6.]
    attempts = [row for row in records(caplog)
        if row["event"] == "attempt_result" and row.get("category") == category]
    assert len(attempts) == 2
    assert all(row["stage"] == "transport" and row["http_status"] == status
        and row["http_status_class"] == str(status // 100) + "xx"
        and row["exception_class"] == "APIStatusError" for row in attempts)
    assert final_record(caplog)["final_typed_outcome"] == "unavailable"
    assert_safe(caplog)


@pytest.mark.parametrize("fault", ["timeout", "connection_reset", "connection"])
def test_transport_failures_have_typed_safe_category(monkeypatch, diagnostics, caplog, fault):
    request = httpx.Request("POST", "https://provider.invalid/v1")
    if fault == "timeout":
        error = APITimeoutError(request=request)
        category, exception = "provider_timeout", "APITimeoutError"
    else:
        error = APIConnectionError(message=PRIVATE, request=request)
        error.__cause__ = (httpx.ReadError(PRIVATE, request=request) if fault == "connection_reset"
            else httpx.ConnectError(PRIVATE, request=request))
        category, exception = "provider_connection", "APIConnectionError"
    (accepted, counts), _, _ = validate(monkeypatch, diagnostics, [error, error])
    assert not accepted and counts["error"] == 2 and counts["attempts"] == 2
    assert counts["retries"] == 1
    assert bool(counts.get("job_unavailable")) is (fault != "timeout")
    attempts = [row for row in records(caplog) if row["event"] == "attempt_result"]
    assert len(attempts) == 2
    assert all(row["category"] == category and row["exception_class"] == exception for row in attempts)
    assert final_record(caplog)["final_typed_outcome"] == "unavailable"
    assert_safe(caplog)


@pytest.mark.parametrize("text,finish", [
    ("{" + PRIVATE, "stop"), ("", "stop"), ("{}", "stop"),
    ('{"results":[{"id":"0","relation":"UNKNOWN-TAXONOMY"}]}', "stop"),
    ('{"results":[{"id":"0","relation":"equivalent","extra":1}]}', "stop"),
    ('{"results":[{"id":"0","relation":"equivalent"}]}', "length"),
])
def test_parser_failures_remain_error_and_have_only_category(
        monkeypatch, diagnostics, caplog, text, finish):
    bad = completion(text=text, finish=finish)
    (accepted, counts), _, _ = validate(monkeypatch, diagnostics, [bad, bad])
    assert not accepted and counts["error"] == 2 and counts["attempts"] == 2
    assert counts["retries"] == 1 and not counts.get("job_unavailable")
    attempts = [row for row in records(caplog) if row["event"] == "attempt_result"]
    assert len(attempts) == 2
    assert all(row["stage"] == "parser" and row["category"] == "parser_failure"
        and row.get("parser_result") not in {None, "accepted", "valid", "success"} for row in attempts)
    assert final_record(caplog)["final_typed_outcome"] == "unavailable"
    assert_safe(caplog)


@pytest.mark.parametrize("prefix", ["candidate_more_specific", "unrelated"])
def test_partial_local_error_does_not_escalate_trusted_results(monkeypatch, diagnostics, caplog, prefix):
    invalid = completion("bad-taxonomy", "bad-taxonomy")
    (accepted, counts), _, _ = validate(monkeypatch, diagnostics,
        [completion(prefix, prefix), invalid, invalid], count=4)
    assert len(accepted) == (2 if prefix == "candidate_more_specific" else 0)
    assert counts["accepted"] + counts["rejected"] == 2 and counts["error"] == 2
    assert counts["attempts"] == 3 and counts["retries"] == 1
    assert not counts.get("job_unavailable")
    assert final_record(caplog)["final_typed_outcome"] in FINAL_SUCCESS
    assert_safe(caplog)


def test_six_reject_two_error_is_normal_no_match_diagnostic(monkeypatch, diagnostics, caplog):
    invalid = completion("bad-taxonomy", "bad-taxonomy")
    (accepted, counts), _, _ = validate(monkeypatch, diagnostics,
        [completion("unrelated", "unrelated")] * 3 + [invalid, invalid], count=8)
    assert not accepted and counts["rejected"] == 6 and counts["error"] == 2
    assert counts["attempts"] == 5 and counts["retries"] == 1
    assert final_record(caplog)["final_typed_outcome"] in FINAL_SUCCESS
    assert_safe(caplog)


def test_model_mismatch_is_not_relation_evidence(monkeypatch, diagnostics, caplog):
    bad = completion("equivalent", "equivalent", model="private-other-model")
    (accepted, counts), _, _ = validate(monkeypatch, diagnostics, [bad, bad])
    assert not accepted and counts["job_unavailable"] == 1 and counts["error"] == 2
    attempts = [row for row in records(caplog) if row["event"] == "attempt_result"]
    assert len(attempts) == 2 and all(row["stage"] == "model" for row in attempts)
    assert final_record(caplog)["final_typed_outcome"] == "unavailable"
    assert "private-other-model" not in caplog.text
    assert_safe(caplog)


def test_accounting_failure_is_diagnosed_without_paid_retry(monkeypatch, diagnostics, caplog):
    from agent_quota import service
    monkeypatch.setattr(service, "record_usage_deferred", Mock(side_effect=RuntimeError(PRIVATE)))
    (accepted, counts), _, _ = validate(monkeypatch, diagnostics,
        [completion("equivalent", "equivalent", usage=SimpleNamespace(prompt_tokens=2, completion_tokens=2))])
    assert not accepted and counts["job_unavailable"] == 1 and counts["attempts"] == 1
    assert counts["retries"] == 0
    failures = [row for row in records(caplog) if row.get("category") == "accounting_failure"]
    assert len(failures) >= 1 and failures[0]["stage"] == "accounting"
    assert final_record(caplog)["final_typed_outcome"] == "unavailable"
    assert_safe(caplog)


def test_expired_shared_deadline_does_not_start_transport(monkeypatch, diagnostics, caplog):
    (accepted, counts), timeouts, _ = validate(monkeypatch, diagnostics, [], deadline=0.)
    assert not accepted and counts["attempts"] == 0 and counts["error"] == 2
    assert not timeouts and not any(row["event"] == "attempt_start" for row in records(caplog))
    final = final_record(caplog)
    assert final["stage"] == "deadline" and final["category"] == "shared_deadline_exhaustion"
    assert final["remaining_shared_ms"] == 0. and final["final_typed_outcome"] == "unavailable"
    assert_safe(caplog)


def test_retry_budget_is_remaining_absolute_budget(monkeypatch, diagnostics, caplog):
    now = [0.]
    def first():
        now[0] = 6.
        raise TimeoutError(PRIVATE)
    (accepted, counts), timeouts, _ = validate(monkeypatch, diagnostics,
        [first, completion("equivalent", "equivalent")], deadline=7., clock=lambda: now[0])
    assert len(accepted) == 2 and counts["attempts"] == 2 and counts["retries"] == 1
    assert timeouts == [6., 1.]
    starts = [row for row in records(caplog) if row["event"] == "attempt_start"]
    assert [row["timeout_budget_ms"] for row in starts] == [6000., 1000.]
    assert [row["remaining_shared_ms"] for row in starts] == [7000., 1000.]
    assert_safe(caplog)


def test_logging_failure_never_changes_validator_decision(monkeypatch, diagnostics):
    monkeypatch.setattr(diagnostics.LOG, "info", Mock(side_effect=RuntimeError(PRIVATE)))
    (accepted, counts), _, _ = validate(monkeypatch, diagnostics,
        [completion("candidate_more_specific", "unrelated")])
    assert len(accepted) == 1 and counts["accepted"] == counts["rejected"] == 1
    assert counts["attempt_errors"] == counts["retries"] == counts["error"] == 0


@pytest.mark.parametrize("stop", ["deadline", "external_cancel"])
def test_validator_transport_cancellation_closes_client_and_has_no_orphan(
        monkeypatch, diagnostics, caplog, stop):
    clients, cancellations, active, pending = [], [], set(), []
    original = validator.AsyncOpenAI
    original_timeout = asyncio.timeout
    scopes = []

    async def transport(request):
        active.add(id(request))
        try:
            # The real transport is active before expiry/cancellation delivery.
            # Invoke the same Timeout callback asyncio would schedule, without
            # forcing SDK cold start to fit a tiny real-time margin.
            assert scopes and scopes[0].when() is not None
            if stop == "deadline":
                scopes[0]._on_timeout()
            else:
                asyncio.current_task().cancel()
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellations.append(True)
            raise
        finally:
            active.remove(id(request))
            pending.extend(task for task in asyncio.all_tasks()
                if task is not asyncio.current_task() and not task.done())

    def factory(**kwargs):
        assert kwargs["max_retries"] == 0
        client = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        clients.append(client)
        return original(**kwargs, http_client=client)

    monkeypatch.setattr(validator, "AsyncOpenAI", factory)

    def observe_timeout(delay):
        scope = original_timeout(delay)
        scopes.append(scope)
        return scope

    with diagnostics.diagnostic_scope("semantic_validator"), \
         patch.object(asyncio, "timeout", observe_timeout):
        expected = TimeoutError if stop == "deadline" else asyncio.CancelledError
        with pytest.raises(expected):
            validator._completion(SimpleNamespace(api_key="synthetic", base_url="http://provider.invalid/v1"),
                timeout=6., model=MODEL, messages=[])
    assert cancellations == [True] and not active and not pending
    assert clients and all(client.is_closed for client in clients)
    assert_safe(caplog)


@pytest.fixture
def event_diagnostics(caplog, monkeypatch):
    # Event imports the package-qualified runtime; don't mix ContextVars from
    # the standalone Matchmaker test import with the Event module instance.
    from matchmaker_agent import validator_diagnostics as diagnostics
    caplog.set_level(logging.INFO, logger=diagnostics.LOG.name)
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY", ".90")
    return diagnostics


def test_event_adapter_emits_stage_timings_and_only_opaque_correlation(event_diagnostics, caplog):
    from matchmaker_agent.test_event_v2_adapter import Graph, preference, adapter
    graph = Graph({PRIVATE: [preference("投籃")]})
    with event_diagnostics.diagnostic_scope("event_request") as trace:
        service = adapter(graph)
        assert service.relevance(PRIVATE)
        telemetry = service.telemetry()
        assert telemetry["diagnostic_id"] == trace.correlation_id
    stages = {row["stage"] for row in records(caplog) if row["event"] == "event_stage"}
    assert {"event_load", "event_eligibility", "event_owner_signals", "event_exact",
        "event_vectors", "event_ann", "event_relevance"} <= stages
    for row in records(caplog):
        assert row["correlation_id"] == trace.correlation_id
        if row["event"] == "event_stage":
            assert isinstance(row["elapsed_ms"], (int, float))
            assert isinstance(row["remaining_event_ms"], (int, float))
    assert_safe(caplog)


def test_event_route_joins_validator_attempts_with_one_server_correlation(
        monkeypatch, event_diagnostics, caplog):
    from matchmaker_agent import event_v2_api as api, related_interest_validator as event_validator

    @contextmanager
    def session_for(_agent):
        yield object()

    class InspectAdapter:
        def __init__(self, *_args, **_kwargs):
            self.trace = event_diagnostics.current_trace()
            assert self.trace is not None, "Event API must establish the diagnostic scope"

        def select(self, owner, _excluded):
            assert owner == PRIVATE
            accepted, counts = event_validator.validate_concepts("打籃球",
                [{"semantic_text": "三對三籃球"}], None, MODEL, deadline=18., clock=lambda: 0.)
            assert accepted and counts["error"] == 0
            return []

        def telemetry(self):
            return {"semantic_triggered": True, "validator": {"accepted": 1},
                "diagnostic_id": self.trace.correlation_id}

    monkeypatch.setattr(api, "session_for", session_for)
    monkeypatch.setattr(api, "Adapter", InspectAdapter)
    monkeypatch.setattr(event_validator, "_completion", lambda *_a, **_k: completion("candidate_more_specific"))
    result = api.find_matches(SimpleNamespace(client=None, model=MODEL), PRIVATE, [])
    assert result == [] and re.fullmatch(r"[a-f0-9]{32}", result.telemetry["diagnostic_id"])
    rows = records(caplog)
    assert any(row["event"] == "attempt_result" for row in rows)
    assert any(row["event"] == "event_result" for row in rows)
    assert {row["correlation_id"] for row in rows} == {result.telemetry["diagnostic_id"]}
    assert len({row["call_id"] for row in rows}) >= 2
    assert event_diagnostics.current_trace() is None
    assert_safe(caplog)


def test_event_outer_deadline_is_distinct_from_local_timeout(event_diagnostics, caplog):
    from matchmaker_agent.event_v2_adapter import Adapter
    from matchmaker_agent.event_v2_contract import EventUnavailable
    from matchmaker_agent.test_event_v2_adapter import Graph, preference, VECTOR, NOW
    clock = [0.]

    def late_validate(_query, concepts, *_args, **_kwargs):
        clock[0] = 10.
        return [{**concepts[0], "relation": "candidate_more_specific"}], {"accepted": 1, "error": 0}

    with event_diagnostics.diagnostic_scope("event_request"):
        service = Adapter(Graph({PRIVATE: [preference("投籃")]}), None, MODEL,
            deadline=10., clock=lambda: clock[0], now=lambda: NOW,
            eligible=lambda _: True, allowed=lambda _: True,
            vectors=lambda signals, **_: {signal.source_hash: VECTOR[:] for signal in signals},
            validate=late_validate)
        with pytest.raises(EventUnavailable, match="^semantic_validator_unavailable$"):
            service.relevance(PRIVATE)
    deadline = [row for row in records(caplog)
        if row.get("category") == "shared_deadline_exhaustion"]
    assert deadline and deadline[-1]["stage"] == "deadline"
    assert deadline[-1]["remaining_event_ms"] <= 0.
    assert_safe(caplog)


def test_diagnostics_reach_default_uvicorn_sink_without_enabling_sdk_info():
    # Isolated logging-only Uvicorn setup. This does not load an application,
    # start services, access dotenv, or perform any network/provider operation.
    code = """
import io, json, logging
import uvicorn
uvicorn.Config('synthetic:app', reload=True)
stream = io.StringIO()
for name in ('uvicorn', 'uvicorn.error'):
    for handler in logging.getLogger(name).handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(stream)
import validator_diagnostics as diagnostics
with diagnostics.diagnostic_scope('semantic_validator'):
    diagnostics.emit('validator_start', stage='validator', concept_count=1)
print(json.dumps({
    'diagnostic_visible': 'validator_diagnostic ' in stream.getvalue(),
    'root_info_enabled': logging.getLogger().isEnabledFor(logging.INFO),
    'httpx_info_enabled': logging.getLogger('httpx').isEnabledFor(logging.INFO),
    'openai_info_enabled': logging.getLogger('openai').isEnabledFor(logging.INFO),
}))
"""
    environment = {key: value for key, value in os.environ.items()
        if key in {"PATH", "LANG", "TZ", "PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}}
    result = subprocess.run([sys.executable, "-c", code], cwd=Path(__file__).parent,
        env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["diagnostic_visible"], "default production Uvicorn drops diagnostic INFO records"
    assert state["root_info_enabled"] is False
    assert state["httpx_info_enabled"] is False and state["openai_info_enabled"] is False


@pytest.mark.parametrize("stage,category", [
    ("deadline", "shared_deadline_exhaustion"), ("transport", "provider_timeout"),
])
def test_timeout_origin_classification_preserves_deadline_stage(diagnostics, caplog, stage, category):
    fields = diagnostics.exception_metadata(TimeoutError(PRIVATE), stage)
    assert fields["category"] == category and fields["stage"] == stage
    with diagnostics.diagnostic_scope("semantic_validator"):
        diagnostics.emit("attempt_result", **fields)
    assert records(caplog)[-1]["category"] == category
    assert_safe(caplog)


def sdk_transport(monkeypatch, handler, clients, prior_hooks):
    """Use the real approved SDK with only its HTTP network replaced."""
    original = validator.AsyncOpenAI

    async def prior_hook(response):
        prior_hooks.append(response.status_code)

    def factory(**kwargs):
        assert kwargs["max_retries"] == 0
        assert 0 < kwargs["timeout"] <= 6.
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler),
            event_hooks={"response": [prior_hook]})
        clients.append(http)
        return original(**kwargs, http_client=http)

    monkeypatch.setattr(validator, "AsyncOpenAI", factory)


@pytest.mark.parametrize("status,category", [
    (429, "provider_rate_limit"), (500, "provider_5xx"),
    (502, "provider_5xx"), (503, "provider_5xx"),
])
def test_real_sdk_http_failure_preserves_attempt_cap_and_status_hook(
        monkeypatch, diagnostics, caplog, status, category):
    calls, clients, prior_hooks = [], [], []

    async def handler(request):
        calls.append(True)
        return httpx.Response(status, json={"error": {"message": PRIVATE,
            "code": "rate_limit_exceeded" if status == 429 else "server_error"}})

    sdk_transport(monkeypatch, handler, clients, prior_hooks)
    with diagnostics.diagnostic_scope("semantic_validator"):
        accepted, counts = validator.validate_concepts(PRIVATE,
            [{"semantic_text": PRIVATE}] * 2,
            SimpleNamespace(api_key="synthetic", base_url="https://provider.invalid/v1"),
            MODEL, deadline=time.monotonic() + 18)
    assert not accepted and counts["job_unavailable"] == 1 and counts["error"] == 2
    assert counts["attempts"] == len(calls) == 2 and counts["retries"] == 1
    assert prior_hooks == [status, status], "observer must append, not replace existing response hooks"
    assert len(clients) == 2 and all(client.is_closed for client in clients)
    http = [row for row in records(caplog) if row["event"] == "transport_http"]
    assert len(http) == 2
    assert [row["attempt"] for row in http] == [1, 2]
    assert all(row["http_status"] == status and row["http_status_class"] == str(status // 100) + "xx"
        and row["http_status_source"] == "response_hook" and row["batch_id"] == 1 for row in http)
    failures = [row for row in records(caplog) if row["event"] == "attempt_result"]
    assert len(failures) == 2 and all(row["category"] == category for row in failures)
    assert final_record(caplog)["final_typed_outcome"] == "unavailable"
    assert_safe(caplog)


def test_malformed_http_200_is_recorded_before_sdk_parse_and_not_taxonomy_failure(
        monkeypatch, diagnostics, caplog):
    calls, clients, prior_hooks = [], [], []

    async def handler(request):
        calls.append(True)
        return httpx.Response(200, headers={"Content-Type": "application/json"}, text="{" + PRIVATE)

    sdk_transport(monkeypatch, handler, clients, prior_hooks)
    with diagnostics.diagnostic_scope("semantic_validator"):
        accepted, counts = validator.validate_concepts(PRIVATE,
            [{"semantic_text": PRIVATE}] * 2,
            SimpleNamespace(api_key="synthetic", base_url="https://provider.invalid/v1"),
            MODEL, deadline=time.monotonic() + 18)
    assert not accepted and counts["error"] == 2 and not counts.get("job_unavailable")
    assert counts["attempts"] == len(calls) == 2 and counts["retries"] == 1
    assert prior_hooks == [200, 200] and all(client.is_closed for client in clients)
    http = [row for row in records(caplog) if row["event"] == "transport_http"]
    assert len(http) == 2 and all(row["http_status"] == 200
        and row["http_status_source"] == "response_hook" for row in http)
    failures = [row for row in records(caplog) if row["event"] == "attempt_result"]
    assert len(failures) == 2
    assert all(row["stage"] == "transport" and row["category"] == "provider_payload_failure"
        and row["parser_result"] == "not_reached" for row in failures)
    assert final_record(caplog)["final_typed_outcome"] == "unavailable"
    assert_safe(caplog)


@pytest.mark.parametrize("leaf,category", [
    (lambda: socket.gaierror(socket.EAI_AGAIN, PRIVATE), "dns_failure"),
    (lambda: ssl.SSLCertVerificationError(1, PRIVATE), "tls_verification"),
    (lambda: httpx.PoolTimeout(PRIVATE), "client_connection_pool"),
    (lambda: ConnectionResetError(104, PRIVATE), "connection_reset"),
])
def test_nested_transport_cause_has_only_typed_safe_category(diagnostics, caplog, leaf, category):
    error = APIConnectionError(message=PRIVATE,
        request=httpx.Request("POST", "https://provider.invalid/v1"))
    bridge = RuntimeError(PRIVATE)
    bridge.__cause__ = leaf()
    error.__cause__ = bridge
    fields = diagnostics.exception_metadata(error, "transport")
    assert fields["cause_category"] == category
    with diagnostics.diagnostic_scope("semantic_validator"):
        diagnostics.emit("attempt_result", **fields)
    assert records(caplog)[-1]["cause_category"] == category
    assert_safe(caplog)
