import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest

from concept_identity import canonicalize_concept
from related_interest_contract import ACCEPTED, RELATIONS, INDEX_NAME, embedding_fingerprint, validated_evidence
from related_interest_validator import parse_relations, validate_concepts
from related_interest_retrieval import retrieve
from agent_api import RelatedInterestCandidateRequest

MODEL = "models/gemini-embedding-2"
VECTOR = [1.0] + [0.0]*767


@pytest.fixture(autouse=True)
def synthetic_completion_boundary(monkeypatch):
    import related_interest_validator as validator
    def complete(client, *, timeout, **request):
        return client.with_options(timeout=timeout, max_retries=0).chat.completions.create(**request)
    monkeypatch.setattr(validator, "_completion", complete)


def response(relations, finish="stop"):
    rows = [{"id": str(i), "relation": r} for i, r in enumerate(relations)]
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content=json.dumps({"results": rows})))])


@pytest.mark.parametrize("relation", sorted(RELATIONS))
def test_frozen_pilot_mapping_and_no_retry_for_valid_rejection(relation):
    client = Mock(); client.with_options.return_value = client
    client.chat.completions.create.return_value = response([relation])
    accepted, counts = validate_concepts("Watching Football", [{"semantic_text": "Playing Football"}],
        client, "deepseek-v4.1-flash:cloud", deadline=100, clock=lambda: 0)
    assert bool(accepted) == (relation in ACCEPTED)
    assert counts[relation] == 1
    assert counts["attempts"] == 1
    client.with_options.assert_called_once_with(timeout=6.0, max_retries=0)
    sent = client.chat.completions.create.call_args.kwargs
    assert "reasoning_effort" not in sent and sent["temperature"] == 0
    assert set(json.loads(sent["messages"][1]["content"])["pairs"][0]) == {"id", "Q", "C"}


def test_error_retries_once_then_fails_closed_without_saving_output():
    client = Mock(); client.with_options.return_value = client
    client.chat.completions.create.side_effect = [response(["equivalent"], "length"), RuntimeError("private-body")]
    accepted, counts = validate_concepts("Q", [{"semantic_text": "C"}], client, "deepseek-test",
        deadline=100, clock=lambda: 0)
    assert not accepted and counts["error"] == 1 and counts["attempts"] == 2
    assert "private-body" not in str(counts)


def test_one_retry_can_recover_and_deadline_never_starts_an_attempt():
    client = Mock(); client.with_options.return_value = client
    client.chat.completions.create.side_effect = [TimeoutError(), response(["role_mismatch"])]
    accepted, counts = validate_concepts("Q", [{"semantic_text": "C"}], client, "deepseek-test",
        deadline=100, clock=lambda: 0)
    assert len(accepted) == 1 and counts["attempts"] == 2
    client.reset_mock()
    accepted, counts = validate_concepts("Q", [{"semantic_text": "C"}], client, "deepseek-test",
        deadline=0, clock=lambda: 0)
    assert not accepted and counts["error"] == 1 and counts["attempts"] == 0
    client.with_options.assert_not_called()


def test_operator_stop_between_attempts_does_not_retry():
    client = Mock(); client.with_options.return_value = client
    client.chat.completions.create.side_effect = TimeoutError()
    accepted, counts = validate_concepts("Q", [{"semantic_text": "C"}], client, "deepseek-test",
        deadline=100, clock=lambda: 0, is_enabled=Mock(side_effect=[True, False]))
    assert not accepted and counts["error"] == 1 and counts["attempts"] == 1


@pytest.mark.parametrize("text", [
    '{}', '{"results":[],"extra":1}', '{"results":[{"id":"0","relation":"YES"}]}',
    '{"results":[{"id":"0","relation":"equivalent","accept":true}]}',
    '{"results":[{"id":"0","relation":"equivalent"},{"id":"0","relation":"equivalent"}]}',
    '{"results":[{"id":"0","relation":"equivalent","relation":"unrelated"}]}',
])
def test_strict_schema_fails_closed(text):
    with pytest.raises(ValueError):
        parse_relations(text, "stop", {"0"})


class Result(list):
    def single(self):
        return self[0] if self else None


def setup_graph(relation="role_mismatch", *, fingerprint=None):
    query = canonicalize_concept("Watching Football")
    candidate = canonicalize_concept("Playing Football")
    fp = embedding_fingerprint(MODEL)
    events = []
    class Session:
        def run(self, statement, **params):
            text = str(statement)
            assert not any(word in text for word in ("MERGE ", " SET ", "DELETE ", "CREATE "))
            if "SHOW VECTOR INDEXES" in text:
                return Result([{"state": "ONLINE", "labelsOrTypes": ["Concept"], "properties": ["embedding_v2"],
                    "options": {"indexConfig": {"vector.dimensions": 768, "vector.similarity_function": "cosine"}}}])
            if "SHOW INDEXES" in text:
                return Result([{"count": 1}])
            if "db.index.vector.queryNodes" in text:
                events.append("ann")
                assert params["index_name"] == INDEX_NAME
                assert params["neighbor_limit"] <= 32 and params["concept_limit"] <= 12
                return Result([{**candidate.as_dict(), "vector": VECTOR, "fingerprint": fingerprint or fp,
                    "source_hash": candidate.semantic_input_hash, "similarity": .91}])
            if "UNWIND $concepts" in text:
                assert events == ["ann", "validate"]
                events.append("expand")
                assert params["concepts"][0]["relation"] in ACCEPTED
                assert "[:PREFERS]" in text and "AVOIDS" not in text
                assert text.index("LIMIT $per_concept_limit") < text.index("WHERE candidate.id")
                assert params["per_concept_limit"] <= 20 and params["candidate_limit"] <= 50
                return Result([{"candidate_id": "person", "evidence": [{"concept_key": candidate.key}]}]*2)
            raise AssertionError("unexpected query")
    client = Mock(); client.with_options.return_value = client
    def create(**kwargs):
        events.append("validate")
        assert json.loads(kwargs["messages"][1]["content"])["pairs"][0]["C"] == candidate.semantic_text
        return response([relation])
    client.chat.completions.create.side_effect = create
    req = RelatedInterestCandidateRequest(requester_user_id="owner", topic=query.semantic_text,
        embedding_model=MODEL, embedding_fingerprint=fp, query_embedding=VECTOR)
    return Session(), req, query, candidate, client, events


def test_validation_precedes_user_expansion_and_candidate_dedupes(monkeypatch):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    session, req, query, candidate, client, events = setup_graph()
    result = retrieve(session, req, query, client, "deepseek-test", MODEL, clock=lambda: 0)
    assert events == ["ann", "validate", "expand"]
    assert result["status"] == "success" and len(result["candidates"]) == 1
    packet = result["candidates"][0]["evidence"][0]
    assert validated_evidence(packet, query_key=query.key) == packet
    assert packet["candidate_preference"] == candidate.semantic_text


@pytest.mark.parametrize("relation", sorted(RELATIONS-ACCEPTED))
def test_rejected_relation_never_expands_owners(monkeypatch, relation):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    session, req, query, _, client, events = setup_graph(relation)
    result = retrieve(session, req, query, client, "deepseek-test", MODEL, clock=lambda: 0)
    assert events == ["ann", "validate"]
    assert result["candidates"] == [] and result["validator_counts"]["rejected"] == 1


def test_wrong_vector_provenance_is_not_used_and_off_does_not_read_graph(monkeypatch):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    session, req, query, _, client, events = setup_graph(fingerprint="wrong")
    result = retrieve(session, req, query, client, "deepseek-test", MODEL)
    assert events == ["ann"] and result["candidates"] == []
    client.with_options.assert_not_called()
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "off")
    result = retrieve(Mock(side_effect=AssertionError()), req, query, client, "deepseek-test", MODEL)
    assert result["error_code"] == "semantic_policy_disabled"


def test_missing_key_index_stops_before_ann_or_validator(monkeypatch):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    session, req, query, _, client, events = setup_graph()
    original = session.run
    session.run = lambda statement, **params: Result([{"count": 0}]) if "SHOW INDEXES" in str(statement) else original(statement, **params)
    result = retrieve(session, req, query, client, "deepseek-test", MODEL)
    assert result["error_code"] == "semantic_index_unavailable" and events == []
    client.with_options.assert_not_called()


def test_wrong_provider_model_fails_closed():
    client = Mock(); client.with_options.return_value = client
    wrong = response(["equivalent"]); wrong.model = "unexpected-model"
    client.chat.completions.create.return_value = wrong
    accepted, counts = validate_concepts("Q", [{"semantic_text": "C"}], client, "deepseek-test",
        deadline=100, clock=lambda: 0)
    assert not accepted and counts["attempts"] == 2 and counts["error"] == 1
