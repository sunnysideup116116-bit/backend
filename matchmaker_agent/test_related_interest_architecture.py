"""Architecture regressions only; no provider, database, or semantic experiment."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
import agent_api
import matchmaker
import related_interest_validator as validator
import related_interest_retrieval as retrieval
from concept_identity import canonicalize_concept
from related_interest_contract import POLICY
from test_related_interest import setup_graph, response, MODEL


def packet():
    q = "Watching football matches with friends in a quiet alcohol-free accessible venue"
    c = "Playing football matches with friends in a quiet alcohol-free accessible venue"
    return {"kind": "semantic_related", "basis_type": "related_interest", "policy_version": POLICY,
        "query_preference": q, "candidate_preference": c, "concept_key": canonicalize_concept(c).key,
        "relation": "role_mismatch", "semantic_score": .93, "similarity": .93, "validator_status": "accepted"}


def test_real_async_validator_transport_is_cancelled_closed_and_has_no_sdk_retry(monkeypatch):
    cancelled, clients, attempts = [], [], []
    async def handler(request):
        attempts.append(request)
        try:
            await asyncio.sleep(10)
        finally:
            cancelled.append(True)
    original = validator.AsyncOpenAI
    def factory(**kwargs):
        assert kwargs["max_retries"] == 0
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler)); clients.append(client)
        return original(**kwargs, http_client=client)
    monkeypatch.setattr(validator, "AsyncOpenAI", factory)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        validator._completion(SimpleNamespace(api_key="synthetic", base_url="http://provider.invalid/v1"),
            timeout=.05, model="deepseek-test", messages=[])
    assert time.monotonic()-started < .5
    assert len(attempts) == 1 and cancelled == [True] and all(c.is_closed for c in clients)


def test_validator_retry_spends_original_deadline_and_stops_later_batches(monkeypatch):
    now, timeouts = [0.0], []
    def complete(_client, *, timeout, **_request):
        timeouts.append(timeout); now[0] += timeout
        return response(["equivalent", "equivalent"], finish="length")
    monkeypatch.setattr(validator, "_completion", complete)
    accepted, counts = validator.validate_concepts("Q", [{"semantic_text": "C"}]*4,
        Mock(), "deepseek-test", deadline=7.0, clock=lambda: now[0])
    assert timeouts == [6.0, 1.0]
    assert not accepted and counts["attempts"] == 2 and counts["error"] == 4


def test_graph_time_is_subtracted_from_validator_budget(monkeypatch):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    session, req, query, _, client, events = setup_graph()
    now = [0.0]; original = session.run
    def run(statement, **params):
        assert statement.timeout <= 1.0
        result = original(statement, **params); now[0] += .2
        return result
    session.run = run
    def validate(*args, deadline, **kwargs):
        assert deadline == 1.0 and now[0] == pytest.approx(.6)
        now[0] = 1.0
        return [], {"error": 1}
    monkeypatch.setattr(retrieval, "validate_concepts", validate)
    result = retrieval.retrieve(session, req, query, client, "deepseek-test", MODEL,
        deadline=1.0, clock=lambda: now[0])
    assert events == ["ann"] and result["error_code"] == "semantic_validator_unavailable"


def test_expired_server_budget_stops_before_graph_and_provider(monkeypatch):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    session, req, query, _, client, events = setup_graph()
    with pytest.raises(TimeoutError):
        retrieval.retrieve(session, req, query, client, "deepseek-test", MODEL, deadline=0, clock=lambda: 0)
    assert not events and not client.with_options.called


def test_matchmaker_endpoint_honors_remaining_caller_budget(monkeypatch):
    cancelled = []
    async def slow(_req):
        try: await asyncio.sleep(10)
        finally: cancelled.append(True)
    monkeypatch.setattr(agent_api, "_evaluate_match_request", slow)
    req = agent_api.MatchRequest(target_user={}, candidates=[], request_budget_seconds=.02)
    started = time.monotonic()
    with pytest.raises(agent_api.HTTPException) as error:
        asyncio.run(agent_api.match_endpoint(req))
    assert error.value.detail["code"] == "matchmaker_timeout"
    assert cancelled == [True] and time.monotonic()-started < .5
    assert agent_api.MatchRequest(target_user={}, candidates=[]).request_budget_seconds is None


def test_complete_evidence_survives_dto_graph_enrichment_and_model_serializer(monkeypatch):
    p = packet(); q = canonicalize_concept(p["query_preference"])
    req = agent_api.MatchRequest(target_user={"user_id": "owner"},
        candidates=[{"user_id": "candidate", "preference_retrieval_evidence": [p]}],
        search_context={"search_intent": "preference", "normalized_topic": q.semantic_text,
            "canonical_preference_key": q.key, **q.as_dict()})
    monkeypatch.setattr(agent_api, "get_user_graph_memory", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(agent_api, "get_global_rules", lambda **_kwargs: "")
    async def evaluate(target, candidates, graph_memory, global_heuristics, deep, *, search_context):
        messages = agent_api.agent._match_messages(target, candidates, graph_memory, global_heuristics, deep, search_context)
        sent = json.loads(messages[1]["content"])
        assert sent["candidates"][0]["preference_retrieval_evidence"] == [p]
        assert "accessible venue" in sent["candidates"][0]["preference_retrieval_evidence"][0]["candidate_preference"]
        assert "不等同於" in messages[0]["content"] and "query_preference 只是搜尋條件" in messages[0]["content"]
        return '{"outcome":"selected","matches":[{"matched_user_id":"candidate"}]}'
    monkeypatch.setattr(agent_api.agent, "match_async", evaluate)
    result = asyncio.run(agent_api.match_endpoint(req))
    assert result["matches"] == [{"matched_user_id": "candidate"}]


def test_related_matchmaker_retry_records_each_attempt_without_remote_replay(monkeypatch):
    from agent_quota import service
    deferred = Mock(); immediate = Mock(side_effect=AssertionError("no remote accounting in related model deadline"))
    monkeypatch.setattr(service, "record_usage_deferred", deferred)
    monkeypatch.setattr(service, "record_usage", immediate)
    attempts = []
    async def handler(request):
        attempts.append(request)
        return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 1,
            "model": "deepseek-test", "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
            "choices": [{"index": 0, "message": {"role": "assistant", "content":
                '{"outcome":"no_suitable_candidate","matches":[]}'},
                "finish_reason": "length" if len(attempts) == 1 else "stop"}]})
    original = matchmaker.AsyncOpenAI
    monkeypatch.setattr(matchmaker, "AsyncOpenAI", lambda **kwargs: original(**kwargs,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))))
    agent = matchmaker.MatchmakerAgent.__new__(matchmaker.MatchmakerAgent)
    agent.client = SimpleNamespace(api_key="synthetic", base_url="http://provider.invalid/v1")
    agent.model = "deepseek-test"; agent.system_prompt = ""
    asyncio.run(agent.match_async({}, [{"user_id": "candidate", "preference_retrieval_evidence": [packet()]}]))
    assert len(attempts) == 2 and deferred.call_count == 2
    assert deferred.call_args_list[0].args[0] != deferred.call_args_list[1].args[0]
    immediate.assert_not_called()
