"""Deterministic validator primary -> secondary failover contract; no providers."""
import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

import related_interest_validator as validator
from related_interest_contract import ACCEPTED, RELATIONS

PRIMARY = "deepseek-v4.1-flash:cloud"
SECONDARY = "glm-5.3-flash:cloud"


class Clock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def reply(relations, *, model=PRIMARY, finish="stop"):
    return SimpleNamespace(model=model, usage=None, choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=json.dumps({"results": [
            {"id": str(i), "relation": r} for i, r in enumerate(relations)]})))])


def provider_error(code):
    return APIStatusError("synthetic-body", response=httpx.Response(code,
        request=httpx.Request("POST", "http://provider.invalid/v1")), body=None)


def scripted(clock, script, calls):
    pending = list(script)

    def complete(_client, *, timeout, model, **_request):
        step = pending.pop(0)
        calls.append({"model": model, "timeout": timeout,
            "role": step.get("role"), "kind": step["kind"]})
        clock.advance(step.get("advance", timeout if step["kind"] == "raise" else .5))
        if step["kind"] == "raise":
            raise step["exc"]
        return step["response"]
    return complete


def run(monkeypatch, script, *, concepts=2, deadline=18.0, fallback=SECONDARY):
    clock = Clock()
    calls = []
    monkeypatch.setattr(validator, "_completion", scripted(clock, script, calls))
    items = [{"concept_key": f"k{index}", "semantic_text": f"C{index}"} for index in range(concepts)]
    accepted, counts = validator.validate_concepts("Q", items, object(), PRIMARY,
        deadline=deadline, clock=clock, fallback_model=fallback)
    return accepted, counts, calls, clock


def test_primary_success_never_calls_secondary(monkeypatch):
    accepted, counts, calls, _ = run(monkeypatch, [
        {"kind": "reply", "role": "primary", "response": reply(["role_mismatch", "unrelated"])}])
    assert len(accepted) == 1
    assert [c["model"] for c in calls] == [PRIMARY]
    assert counts["attempts"] == 1 and counts["retries"] == 0
    assert counts["failover_attempts"] == 0 and counts["error"] == 0
    assert not counts.get("job_unavailable")


@pytest.mark.parametrize("relation", ["constraint_conflict", "unrelated", "candidate_more_broad"])
def test_primary_valid_reject_never_calls_secondary(monkeypatch, relation):
    accepted, counts, calls, _ = run(monkeypatch, [
        {"kind": "reply", "role": "primary", "response": reply([relation])}], concepts=1)
    assert accepted == []
    assert [c["model"] for c in calls] == [PRIMARY]
    assert counts[relation] == 1 and counts["rejected"] == 1
    assert counts["failover_attempts"] == 0 and not counts.get("job_unavailable")


@pytest.mark.parametrize("fault", ["timeout", "api_timeout", "429", "500", "503", "connection"])
def test_primary_availability_failure_fails_over_to_secondary(monkeypatch, fault):
    error = {
        "timeout": lambda: TimeoutError(),
        "api_timeout": lambda: APITimeoutError(request=httpx.Request("POST", "http://provider.invalid/v1")),
        "429": lambda: provider_error(429),
        "500": lambda: provider_error(500),
        "503": lambda: provider_error(503),
        "connection": lambda: APIConnectionError(request=httpx.Request("POST", "http://provider.invalid/v1")),
    }[fault]()
    accepted, counts, calls, clock = run(monkeypatch, [
        {"kind": "raise", "role": "primary", "exc": error},
        {"kind": "reply", "role": "secondary", "response": reply(["sibling_related"], model=SECONDARY),
         "advance": .6}], concepts=1)
    assert len(accepted) == 1 and accepted[0]["relation"] == "sibling_related"
    assert [c["model"] for c in calls] == [PRIMARY, SECONDARY]
    assert counts["attempts"] == 2 and counts["retries"] == 1
    assert counts["failover_attempts"] == 1
    assert counts["error"] == 0 and not counts.get("job_unavailable")
    assert clock.value <= 18.0


def test_primary_401_and_403_never_fail_over(monkeypatch):
    for code in (401, 403):
        accepted, counts, calls, _ = run(monkeypatch, [
            {"kind": "raise", "role": "primary", "exc": provider_error(code)},
            {"kind": "raise", "role": "primary", "exc": provider_error(code)}], concepts=1)
        assert [c["model"] for c in calls] == [PRIMARY, PRIMARY]
        assert counts["failover_attempts"] == 0
        assert counts["job_unavailable"] == 1 and accepted == []


def test_parser_failure_retries_primary_not_secondary(monkeypatch):
    accepted, counts, calls, _ = run(monkeypatch, [
        {"kind": "reply", "role": "primary", "response": reply(["unrelated"], finish="length")},
        {"kind": "reply", "role": "primary", "response": reply(["unrelated"])}], concepts=1)
    assert [c["model"] for c in calls] == [PRIMARY, PRIMARY]
    assert counts["attempts"] == 2 and counts["failover_attempts"] == 0
    assert counts["rejected"] == 1 and counts["error"] == 0


def test_both_unavailable_fails_closed(monkeypatch):
    accepted, counts, calls, _ = run(monkeypatch, [
        {"kind": "raise", "role": "primary", "exc": TimeoutError()},
        {"kind": "raise", "role": "secondary", "exc": TimeoutError(), "advance": 6.0}])
    assert [c["model"] for c in calls] == [PRIMARY, SECONDARY]
    assert accepted == [] and counts["error"] == 2
    assert counts["attempts"] == 2 and counts["failover_attempts"] == 1
    assert not counts.get("job_unavailable")  # per-attempt timeouts stay batch-local
    accepted, counts, calls, _ = run(monkeypatch, [
        {"kind": "raise", "role": "primary", "exc": provider_error(429)},
        {"kind": "raise", "role": "secondary", "exc": provider_error(503)}], concepts=1)
    assert accepted == [] and counts["job_unavailable"] == 1
    assert counts["failover_attempts"] == 1 and counts["error"] == 1


def test_insufficient_shared_budget_never_starts_secondary(monkeypatch):
    accepted, counts, calls, clock = run(monkeypatch, [
        {"kind": "raise", "role": "primary", "exc": TimeoutError(), "advance": 16.5}], concepts=2)
    assert [c["model"] for c in calls] == [PRIMARY]
    assert accepted == [] and counts["attempts"] == 1 and counts["failover_attempts"] == 0
    assert counts["error"] == 2 and counts["retries"] == 0


def test_shared_deadline_is_not_extended_for_secondary(monkeypatch):
    _, counts, calls, clock = run(monkeypatch, [
        {"kind": "raise", "role": "primary", "exc": TimeoutError(), "advance": 6.0},
        {"kind": "raise", "role": "secondary", "exc": TimeoutError(), "advance": 4.0}],
        concepts=1, deadline=10.0)
    assert [c["timeout"] for c in calls] == [6.0, 4.0]
    assert clock.value == 10.0 and counts["failover_attempts"] == 1


def test_cancellation_never_fails_over_and_propagates(monkeypatch):
    clock = Clock()
    calls = []
    def cancelled(_client, *, timeout, model, **_request):
        calls.append(model)
        raise asyncio.CancelledError()
    monkeypatch.setattr(validator, "_completion", cancelled)
    with pytest.raises(asyncio.CancelledError):
        validator.validate_concepts("Q", [{"concept_key": "k", "semantic_text": "C"}], object(), PRIMARY,
            deadline=18.0, clock=clock, fallback_model=SECONDARY)
    assert calls == [PRIMARY]


def test_invalid_fallback_model_fails_closed_without_starting_it(monkeypatch):
    accepted, counts, calls, _ = run(monkeypatch, [
        {"kind": "raise", "role": "primary", "exc": TimeoutError()}], concepts=1,
        fallback="untrusted-model")
    assert accepted == [] and counts["job_unavailable"] == 1
    assert [c["model"] for c in calls] == [PRIMARY]
    assert counts["failover_attempts"] == 1 and counts["attempts"] == 2


def test_fallback_equal_to_primary_is_ignored(monkeypatch):
    _, counts, calls, _ = run(monkeypatch, [
        {"kind": "raise", "role": "primary", "exc": TimeoutError()},
        {"kind": "reply", "role": "primary", "response": reply(["unrelated"])}], concepts=1,
        fallback=PRIMARY)
    assert [c["model"] for c in calls] == [PRIMARY, PRIMARY]
    assert counts["failover_attempts"] == 0


class Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.rows = []

    def emit(self, record):
        text = record.getMessage()
        index = text.find("validator_diagnostic ")
        if index >= 0:
            self.rows.append(json.loads(text[index + len("validator_diagnostic "):]))


def test_diagnostics_distinguish_primary_secondary_and_hide_content(monkeypatch):
    capture = Capture()
    logger = logging.getLogger("uvicorn.error.validator_diagnostics")
    logger.addHandler(capture)
    try:
        accepted, counts, calls, _ = run(monkeypatch, [
            {"kind": "raise", "role": "primary", "exc": TimeoutError()},
            {"kind": "reply", "role": "secondary", "response": reply(["role_mismatch"], model=SECONDARY)}],
            concepts=1)
    finally:
        logger.removeHandler(capture)
    assert len(accepted) == 1
    starts = [r for r in capture.rows if r.get("event") == "attempt_start"]
    assert [r.get("provider_role") for r in starts] == ["primary", "secondary"]
    assert [r.get("model_id") for r in starts] == [PRIMARY, SECONDARY]
    assert starts[1].get("retry_decision") == "failover_secondary"
    assert starts[1].get("failover_elapsed_ms") >= 0
    failures = [r for r in capture.rows if r.get("event") == "attempt_result"
        and r.get("category") == "provider_timeout"]
    assert failures and failures[0].get("provider_role") == "primary"
    success = [r for r in capture.rows if r.get("event") == "attempt_result"
        and r.get("parser_result") == "valid"]
    assert success and success[0].get("provider_role") == "secondary"
    final = [r for r in capture.rows if r.get("event") == "validator_result"]
    assert final[0].get("failover_used") is True and final[0].get("failover_attempts") == 1
    assert final[0].get("final_typed_outcome") == "accepted"
    assert "PRIVATE" not in json.dumps(capture.rows)


def test_agent_reads_optional_fallback_model_from_environment(monkeypatch):
    import matchmaker
    monkeypatch.setenv("LLM_API_KEY", "offline-test")
    monkeypatch.setenv("LLM_BASE_URL", "http://provider.invalid/v1")
    monkeypatch.setenv("LLM_MODEL_ID", "deepseek-v4.1-flash:cloud")
    monkeypatch.setenv("EVENT_VALIDATOR_FALLBACK_MODEL_ID", "glm-5.3-flash:cloud")
    assert matchmaker.MatchmakerAgent().validator_fallback_model == "glm-5.3-flash:cloud"
    monkeypatch.delenv("EVENT_VALIDATOR_FALLBACK_MODEL_ID")
    assert matchmaker.MatchmakerAgent().validator_fallback_model is None
