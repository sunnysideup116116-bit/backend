"""Offline breaker diagnostics: unchanged safety predicate and secret boundary."""
import json
import logging
from unittest.mock import Mock

import pytest

from matchmaker_agent import validator_diagnostics as diagnostics
from services import event_semantic_monitor as monitor
from tests.match_flow_store import Collection, matches

PRIVATE = "PRIVATE_SENTINEL_owner_prompt_jwt_provider_response"


class Observations(Collection):
    def __init__(self):
        super().__init__()
        self.reads = 0

    def delete_many(self, query):
        self.rows = [row for row in self.rows if not matches(row, query)]

    def find(self, *args, **kwargs):
        self.reads += 1
        return super().find(*args, **kwargs)


@pytest.fixture(autouse=True)
def safe_logs(caplog):
    caplog.set_level(logging.INFO, logger=diagnostics.LOG.name)


def transitions(caplog):
    return [json.loads(record.getMessage().removeprefix("validator_diagnostic "))
        for record in caplog.records if record.getMessage().startswith("validator_diagnostic ")
        and json.loads(record.getMessage().removeprefix("validator_diagnostic "))["event"] == "breaker_transition"]


def telemetry(reference="a" * 32):
    return {"semantic_triggered": True, "diagnostic_id": reference,
        "validator": {"accepted": 1, "error": 2, "attempts": 3, "retries": 1},
        "owner_id": PRIVATE, "prompt": PRIVATE, "provider_response": PRIVATE,
        "authorization": PRIVATE, "raw": {"nested": PRIVATE}}


def test_three_terminal_failures_emit_one_transition_each_and_engage_at_three(caplog):
    store, engage = Observations(), Mock()
    assert monitor.WINDOW_SECONDS == 900 and monitor.CONSECUTIVE_FAILURE_LIMIT == 3
    for index in range(3):
        stopped = monitor.on_result("unavailable", "semantic_validator_unavailable", telemetry(),
            collection=store, now=lambda: 1000 + index, engage=engage)
        assert stopped is (index == 2)
    rows = transitions(caplog)
    assert len(rows) == 3
    assert [row["before_capped2"] for row in rows] == [0, 1, 2]
    assert [row["after_capped3"] for row in rows] == [1, 2, 3]
    assert [row["breaker_engaged"] for row in rows] == [False, False, True]
    assert rows[-1]["counter_before_is_lower_bound"] is True
    assert all(row["correlation_id"] == "a" * 32 for row in rows)
    assert store.reads == 3, "diagnostics must not add database reads"
    assert len(store.rows) == 3 and engage.call_count == 1
    assert all(row["diagnostic_id"] == "a" * 32 for row in store.rows)
    assert PRIVATE not in caplog.text and PRIVATE not in repr(store.rows)


@pytest.mark.parametrize("middle_status,middle_code", [
    ("no_match", ""), ("unavailable", "event_negative_unavailable"),
])
def test_success_and_non_target_unavailable_reset_same_consecutive_predicate(
        caplog, middle_status, middle_code):
    store, engage = Observations(), Mock()
    for index, (status, code) in enumerate([
        ("unavailable", "semantic_validator_unavailable"),
        (middle_status, middle_code),
        ("unavailable", "semantic_validator_unavailable"),
    ]):
        assert not monitor.on_result(status, code, telemetry(), collection=store,
            now=lambda: 1000 + index, engage=engage)
    rows = transitions(caplog)
    assert [row["after_capped3"] for row in rows] == [1, 0, 1]
    assert store.reads == 3 and not engage.called


def test_old_observations_outside_fifteen_minutes_do_not_count(caplog):
    store, engage = Observations(), Mock()
    for stamp in (1000, 1001, 1902):
        assert not monitor.on_result("unavailable", "semantic_validator_unavailable", telemetry(),
            collection=store, now=lambda: stamp, engage=engage)
    assert [row["after_capped3"] for row in transitions(caplog)] == [1, 2, 1]
    assert store.reads == 3 and not engage.called


@pytest.mark.parametrize("reference", [PRIVATE, "b" * 31, "C" * 32, "g" * 32, 123, None])
def test_untrusted_correlation_is_not_persisted_or_logged(caplog, reference):
    store = Observations()
    assert not monitor.on_result("no_match", "", telemetry(reference),
        collection=store, now=lambda: 1000, engage=Mock())
    assert "diagnostic_id" not in store.rows[0]
    assert PRIVATE not in caplog.text and PRIVATE not in repr(store.rows)


def test_log_sink_failure_does_not_change_breaker_accounting_or_engagement(monkeypatch):
    store, engage = Observations(), Mock()
    monkeypatch.setattr(diagnostics.LOG, "info", Mock(side_effect=RuntimeError(PRIVATE)))
    for index in range(3):
        assert monitor.on_result("unavailable", "semantic_validator_unavailable", telemetry(),
            collection=store, now=lambda: 1000 + index, engage=engage) is (index == 2)
    assert engage.call_count == 1 and store.reads == 3 and len(store.rows) == 3
    assert PRIVATE not in repr(store.rows)


def test_primary_timeout_secondary_success_is_not_terminal_unavailable(caplog):
    store, engage = Observations(), Mock()
    telemetry_row = {**telemetry(), "validator": {"accepted": 1, "error": 0, "attempts": 2,
        "retries": 1, "failover_attempts": 1}}
    stopped = monitor.on_result("success", "", telemetry_row,
        collection=store, now=lambda: 1000, engage=engage)
    assert stopped is False and engage.call_count == 0
    row = store.rows[-1]
    assert row["status"] == "completed" and row["error_code"] == ""
    assert row["counts"]["failover_attempts"] == 1
    assert not monitor.systemic_failure(store.rows, now=1001)
    rows = transitions(caplog)
    assert len(rows) == 1 and rows[0]["breaker_engaged"] is False


def test_both_unavailable_with_failover_still_engages_breaker(caplog):
    store, engage = Observations(), Mock()
    for index in range(3):
        stopped = monitor.on_result("unavailable", "semantic_validator_unavailable",
            {**telemetry(), "validator": {"error": 2, "attempts": 2, "retries": 1, "failover_attempts": 1}},
            collection=store, now=lambda index=index: 2000 + index, engage=engage)
        assert stopped is (index == 2)
    assert engage.call_count == 1
    rows = transitions(caplog)
    assert [row["after_capped3"] for row in rows] == [1, 2, 3]
    assert rows[-1]["breaker_engaged"] is True
