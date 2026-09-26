"""Synthetic-only Phase 2 observations and operational kill switch tests."""
import json
from unittest.mock import Mock

import pytest

from matchmaker_agent.concept_identity import canonicalize_concept
from matchmaker_agent.related_interest_contract import POLICY, bounded_ann_observations
from services import related_interest_pilot_monitor as monitor
from services.related_interest_telemetry import summarize, record_search
from .test_related_interest_pilot import document, evidence


def job(*, end=1000, status="failed", code="semantic_validator_unavailable", triggered=True):
    return {"user_id": "synthetic-a", "status": status, "error_code": code,
        "started_at": end-20, "completed_at": end,
        "related_interest_pilot": {"policy_version": POLICY, "telemetry_version": 2, "triggered": triggered}}


def test_ann_observations_are_bounded_hashes_and_scores_not_labels_or_ids():
    key = canonicalize_concept("Football").key
    row = {"concept_key": key, "similarity": .987654, "semantic_text": "PRIVATE", "candidate_id": "SECRET"}
    assert bounded_ann_observations([row, row]) == [{"concept_key": key, "similarity": .9877}]
    assert bounded_ann_observations([{}, {**row, "concept_key": "candidate-id"},
        {**row, "similarity": True}, {**row, "similarity": float("nan")}, {**row, "similarity": 1.01}]) == []
    assert len(bounded_ann_observations([{**row, "concept_key": canonicalize_concept(f"Sport {i}").key}
        for i in range(20)])) == 12
    assert "PRIVATE" not in str(bounded_ann_observations([row]))


def test_telemetry_exact_observation_never_resets_a_prior_trigger(monkeypatch):
    import database
    collection = Mock()
    monkeypatch.setattr(database, "db", {"match_search_jobs": collection})
    record_search("job", "owner", triggered=False)
    query, update = collection.update_one.call_args.args
    assert query == {"job_id": "job", "user_id": "owner"}
    assert "related_interest_pilot.triggered" not in update["$set"]
    assert not collection.update_one.call_args.kwargs["upsert"]


def test_report_separates_attempt_errors_retry_unavailability_and_latencies():
    failed, success, exact = job(), job(end=1030, status="completed", code=""), job(end=1060, triggered=False)
    exact["retrieval_diagnostics"] = {"qualified_exact_count": 1}
    exact["started_at"] = 1050
    failed["related_interest_pilot"]["validator_counts"] = {
        "attempts": 2, "attempt_errors": 2, "retries": 1, "error": 2}
    success["related_interest_pilot"]["validator_counts"] = {
        "attempts": 1, "attempt_errors": 0, "retries": 0, "accepted": 1, "role_mismatch": 1}
    success["related_interest_pilot"]["ann_observations"] = [
        {"concept_key": canonicalize_concept("Football").key, "similarity": .91}]
    result = summarize([failed, success, exact], [])
    assert result["fallback_trigger_jobs"] == 2 and result["preference_search_jobs"] == 3
    assert result["exact_hit_jobs"] == result["exact_only_jobs"] == 1
    assert result["validator_unavailable_rate"] == .5
    assert result["validator_retry_rate"] == .3333 and result["validator_attempt_error_rate"] == .6667
    assert result["validator_concept_error_rate"] == .6667
    assert result["ann"] == {"hits": 1, "unique_concepts": 1, "score_min": .91, "score_median": .91, "score_max": .91}
    assert result["latency_seconds"] == {"samples": 3, "p50": 20, "p95": 20}
    assert "synthetic-a" not in json.dumps(result)
    failed["related_interest_pilot"].pop("telemetry_version")
    assert summarize([failed], [])["validator_retry_rate"] is None


def test_renderer_observations_do_not_change_copy_and_account_for_safe_fallback():
    from services.pair_opening_service import compose_pair_opening
    regular = document()
    neutral = document(packet=evidence(c="a"*80+" wheelchair accessible"))
    for doc, expected in [(regular, "fact_bound"), (neutral, "neutral_fallback")]:
        doc["related_interest_pilot"] = {"policy_version": POLICY}
        assert {e["reason_render_mode"] for e in doc["friend_intro_v4"].values()} == {expected}
        text, outcome = compose_pair_opening(doc, "小宇", "小晴")
        assert outcome == "related_interest_"+expected
        assert "你們都喜歡" not in text
    result = summarize([], [regular, neutral], [
        {"metadata": {"opening_outcome": "related_interest_fact_bound"}},
        {"metadata": {"opening_outcome": "related_interest_neutral_fallback"}}])
    assert result["reason_rendering"] == {"known": 4, "unknown": 0, "neutral_fallback": 2, "fallback_rate": .5}
    assert result["opening_rendering"]["fallback_rate"] == .5


def test_systemic_failure_uses_recent_consecutive_typed_failures_not_semantic_no():
    rows = [job(end=1000-i) for i in range(3)]
    assert monitor.systemic_failure(rows, now=1000)
    assert not monitor.systemic_failure(rows[:2], now=1000)
    assert not monitor.systemic_failure(rows, now=2000)
    assert not monitor.systemic_failure([*rows, job(end=1000, status="completed", code="")], now=1000)
    assert not monitor.systemic_failure([*rows[:2], job(end=998, code="insufficient_semantic_ground")], now=1000)
    assert not monitor.systemic_failure([*rows[:2], job(end=998, triggered=False)], now=1000)
    rows[2]["related_interest_pilot"].pop("telemetry_version")
    assert not monitor.systemic_failure(rows, now=1000)


def test_systemic_failure_sets_shared_kill_file_with_bounded_query(monkeypatch, tmp_path):
    import database
    path = tmp_path / "kill"
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "active")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", '["synthetic-a","synthetic-b"]')
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(path))
    monkeypatch.setattr(monitor.time, "time", lambda: 1000)
    collection = Mock()
    collection.find.return_value.sort.return_value.limit.return_value = [job(end=1000-i) for i in range(3)]
    monkeypatch.setattr(database, "db", {"match_search_jobs": collection})
    assert monitor.on_job_finished("synthetic-a", "failed", "semantic_validator_unavailable")
    assert path.exists()
    query = collection.find.call_args.args[0]
    assert query["user_id"] == {"$in": ["synthetic-a", "synthetic-b"]}
    assert query["completed_at"] == {"$gte": 100}
    collection.find.return_value.sort.return_value.limit.assert_called_once_with(3)
    collection.reset_mock()
    assert not monitor.on_job_finished("synthetic-a", "failed", "semantic_validator_unavailable")
    collection.find.assert_not_called()


@pytest.mark.parametrize("status,code", [("completed", ""), ("insufficient_common_ground", "insufficient_semantic_ground")])
def test_normal_outcome_never_queries_monitor_database(monkeypatch, status, code):
    enabled = Mock(side_effect=AssertionError("no monitor work for ordinary completion"))
    monkeypatch.setattr(monitor, "canary_requester_enabled", enabled)
    assert not monitor.on_job_finished("synthetic-a", status, code)
    enabled.assert_not_called()


def test_monitor_failure_is_secret_safe_and_does_not_change_completed_job(monkeypatch, caplog):
    import database
    monkeypatch.setattr(monitor, "canary_requester_enabled", lambda _: True)
    monkeypatch.setattr(monitor, "canary_cohort", lambda: frozenset({"a", "b"}))
    collection = Mock(); collection.find.side_effect = RuntimeError("SECRET provider data")
    monkeypatch.setattr(database, "db", {"match_search_jobs": collection})
    assert not monitor.on_job_finished("a", "failed", "semantic_validator_unavailable")
    assert "SECRET" not in caplog.text and "RuntimeError" in caplog.text


def test_report_scope_is_bounded_and_leakage_is_not_hidden():
    from scripts.report_related_interest_pilot import collect_report
    inside, outside = job(), job()
    outside["user_id"] = "outside-owner"
    proposal = {"_id": "private-id", "from_user": "synthetic-a", "to_user": "synthetic-b",
        "related_interest_pilot": {"policy_version": POLICY, "relations": ["role_mismatch"]}, "status": "draft"}
    leaked = {**proposal, "to_user": "outside-owner", "related_interest_pilot": {
        "policy_version": POLICY, "relations": ["constraint_conflict"]}}
    collections = {name: Mock() for name in ("match_search_jobs", "matches", "messages")}
    def rows(name, values, ordered=True):
        cursor = collections[name].find.return_value
        if ordered:
            cursor = cursor.sort.return_value
        cursor.limit.return_value.max_time_ms.return_value = values
    rows("match_search_jobs", [inside, outside])
    rows("matches", [proposal, leaked])
    rows("messages", [], ordered=False)
    result = collect_report(collections, frozenset({"synthetic-a", "synthetic-b"}), since=900, limit=10)
    assert result["safety_findings"] == {"outside_cohort_semantic_jobs": 1,
        "outside_cohort_proposals": 1, "rejected_relation_proposals": 1}
    assert result["preference_search_jobs"] == result["semantic_proposals"] == 1
    assert not result["truncated"] and "outside-owner" not in json.dumps(result)
    assert "private-id" not in json.dumps(result)
    assert collections["messages"].find.call_args.args[0]["metadata.match_id"] == {"$in": ["private-id"]}


def test_social_transport_keeps_failed_ann_diagnostics_but_not_provider_text(monkeypatch):
    from services import preference_semantic_service as service
    from agent_quota import internal
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "active")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", '["owner","candidate"]')
    monkeypatch.setattr(internal, "signed_headers", lambda: {})
    key = canonicalize_concept("Football").key
    response = Mock(); response.json.return_value = {"status": "error", "error_code": "semantic_validator_unavailable",
        "canonical_key": canonicalize_concept("Basketball").key,
        "validator_counts": {"attempts": 2, "retries": 1, "attempt_errors": 2},
        "ann_observations": [{"concept_key": key, "similarity": .91, "semantic_text": "SECRET"}]}
    monkeypatch.setattr(service.requests, "post", Mock(return_value=response))
    with pytest.raises(service.PreferenceSemanticRetrievalError) as caught:
        service.retrieve_semantic_preference_candidates("owner", "Basketball", excluded_user_ids=set())
    assert caught.value.validator_counts["retries"] == 1
    assert caught.value.ann_observations == [{"concept_key": key, "similarity": .91}]
    assert "SECRET" not in str(caught.value.__dict__)
