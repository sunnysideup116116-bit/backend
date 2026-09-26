import json
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

import agent_api
import related_interest_retrieval as retrieval
from agent_quota import internal
from agent_quota.service import task_scope
from test_related_interest import setup_graph, MODEL, Result, synthetic_completion_boundary


@pytest.fixture(autouse=True)
def canary_config(monkeypatch, tmp_path):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "active")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", '["owner","person"]')
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(tmp_path / "kill"))


@pytest.mark.parametrize("size", [2, 5, 10])
def test_expansion_server_bound_before_limit_and_defensive_outside_drop(monkeypatch, size):
    cohort = ["owner", "person", *[f"synthetic-{i}" for i in range(size-2)]]
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", json.dumps(cohort))
    session, req, query, candidate, client, events = setup_graph()
    original = session.run
    def execute(statement, **params):
        text = str(statement)
        if "db.index.vector.queryNodes" in text or "UNWIND $concepts" in text:
            assert params["allowed_owner_ids"] == sorted(cohort)
        if "db.index.vector.queryNodes" in text:
            assert "owner.id IN $allowed_owner_ids" in text
        if "UNWIND $concepts" in text:
            assert text.index("UNWIND $allowed_owner_ids") < text.index("LIMIT $per_concept_limit")
            assert "(candidate:User {id:allowed_owner_id})-[:PREFERS]->(c)" in text
            rows = original(statement, **params)
            return Result([*rows, {"candidate_id": "outsider", "evidence": [{"concept_key": candidate.key}]}])
        return original(statement, **params)
    session.run = execute
    result = retrieval.retrieve(session, req, query, client, "deepseek-test", MODEL, clock=lambda: 0)
    assert [c["candidate_id"] for c in result["candidates"]] == ["person"]
    assert events == ["ann", "validate", "expand"]


@pytest.mark.parametrize("cohort,requester", [('', 'owner'), ('invalid', 'owner'), ('["owner","person"]', 'outsider')])
def test_bad_cohort_or_outsider_never_reads_graph_or_calls_validator(monkeypatch, cohort, requester):
    _session, req, query, _candidate, client, _events = setup_graph()
    req.requester_user_id = requester
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", cohort)
    session = Mock()
    result = retrieval.retrieve(session, req, query, client, "deepseek-test", MODEL)
    assert result["error_code"] == "semantic_policy_disabled"
    session.run.assert_not_called(); client.with_options.assert_not_called()


def test_missing_user_id_index_stops_before_ann():
    session, req, query, _candidate, client, events = setup_graph()
    original = session.run
    session.run = lambda statement, **params: Result([{"count": 1, "user_count": 0}]) \
        if "SHOW INDEXES" in str(statement) else original(statement, **params)
    result = retrieval.retrieve(session, req, query, client, "deepseek-test", MODEL)
    assert result["error_code"] == "semantic_index_unavailable" and events == []
    client.with_options.assert_not_called()


@pytest.mark.parametrize("phase", ["before-ann", "before-validator", "before-expansion"])
def test_kill_rechecked_between_work_boundaries(monkeypatch, tmp_path, phase):
    session, req, query, _candidate, client, events = setup_graph()
    path = tmp_path / "stop"
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(path))
    original = session.run
    def execute(statement, **params):
        rows = original(statement, **params)
        text = str(statement)
        if (phase == "before-ann" and "SHOW INDEXES" in text
                or phase == "before-validator" and "db.index.vector.queryNodes" in text):
            path.touch()
        return rows
    session.run = execute
    create = client.chat.completions.create.side_effect
    def completion(**kwargs):
        result = create(**kwargs)
        if phase == "before-expansion": path.touch()
        return result
    client.chat.completions.create.side_effect = completion
    result = retrieval.retrieve(session, req, query, client, "deepseek-test", MODEL, clock=lambda: 0)
    assert result["error_code"] == "semantic_policy_disabled" and result["candidates"] == []
    assert "expand" not in events
    if phase == "before-ann": assert events == []
    if phase == "before-validator": assert events == ["ann"]


@pytest.mark.parametrize("signed_owner", [None, "outsider", "person", "owner"])
def test_http_requires_matching_signed_owner_not_body_claim(monkeypatch, signed_owner):
    _session, req, _query, _candidate, _client, _events = setup_graph()
    monkeypatch.setattr(internal, "secret", lambda: b"synthetic-test-secret")
    headers = {}
    if signed_owner:
        with task_scope(signed_owner, "matching", "synthetic-job"):
            headers = internal.signed_headers()
    graph_config = Mock(side_effect=RuntimeError("synthetic stop after authorized boundary"))
    monkeypatch.setattr(agent_api, "_neo4j_config", graph_config)
    # No lifecycle startup; network/Graph remain offline. Extra client fields
    # cannot alter the operator cohort even when they name an outside owner.
    payload = {**req.model_dump(), "canary": True, "allowed_owner_ids": ["outsider"]}
    response = TestClient(agent_api.app).post("/api/preferences/related-interest-candidates", json=payload, headers=headers)
    if signed_owner == "owner":
        assert response.status_code == 200
        graph_config.assert_called_once()
    else:
        assert response.status_code == 403 and response.json()["detail"] == "invalid_quota_context"
        graph_config.assert_not_called()


def test_forged_cohort_and_requester_cannot_expand(monkeypatch):
    _session, req, _query, _candidate, _client, _events = setup_graph()
    graph_config = Mock()
    monkeypatch.setattr(agent_api, "_neo4j_config", graph_config)
    payload = {**req.model_dump(), "requester_user_id": "outsider", "canary": True,
               "allowed_owner_ids": ["outsider", "person"]}
    response = TestClient(agent_api.app).post("/api/preferences/related-interest-candidates", json=payload)
    assert response.json()["error_code"] == "semantic_policy_disabled"
    graph_config.assert_not_called()
