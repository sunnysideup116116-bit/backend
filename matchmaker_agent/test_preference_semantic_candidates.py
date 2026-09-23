from unittest.mock import MagicMock, patch
import pytest

import agent_api
from concept_identity import canonicalize_concept


class _Result(list):
    def __init__(self, rows=(), single=None):
        super().__init__(rows)
        self._single = single

    def single(self):
        return self._single


def _graph(run):
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    session.run.side_effect = run
    return driver, session


def _online_index():
    return {
        "name": "concept_embedding_index",
        "state": "ONLINE",
        "populationPercent": 100.0,
        "labelsOrTypes": ["Concept"], "properties": ["embedding"],
        "options": {"indexConfig": {
            "vector.dimensions": 768,
            "vector.similarity_function": "cosine",
        }},
    }


def test_semantic_lookup_is_bounded_prefers_only_and_read_only():
    vector = [1.0] + [0.0] * 767
    seen = {}
    query_identity = canonicalize_concept("K-pop")
    korean = canonicalize_concept("Korean Pop")
    asian = canonicalize_concept("Asian Pop")

    def run(query, **params):
        text = str(query)
        if "SHOW VECTOR INDEXES" in text:
            return _Result(single=_online_index())
        if "RETURN concept.embedding AS embedding" in text:
            return _Result(single={**query_identity.as_dict(), "embedding": vector,
                "model": "test-space", "task": "semantic_similarity",
                "embedding_source_hash": query_identity.semantic_input_hash})
        if "db.index.vector.queryNodes" in text:
            seen.update(ann=text, ann_params=params)
            return _Result(rows=[
                {**korean.as_dict(), "concept_key": korean.key, "similarity": .91,
                 "model": "test-space", "task": "semantic_similarity", "embedding_source_hash": korean.semantic_input_hash},
                {**asian.as_dict(), "concept_key": asian.key, "similarity": .87,
                 "model": "test-space", "task": "semantic_similarity", "embedding_source_hash": asian.semantic_input_hash},
            ])
        if "UNWIND $concepts" in text:
            seen.update(query=text, params=params)
            return _Result(rows=[{
                "candidate_id": "candidate-a",
                "best_score": 0.91,
                "evidence": [
                    {"concept_key": korean.key, "similarity": 0.91},
                    {"concept_key": asian.key, "similarity": 0.87},
                ],
            }])
        raise AssertionError(text)

    driver, _session = _graph(run)
    request = agent_api.PreferenceSemanticCandidateRequest(
        requester_user_id="owner", topic="K-pop", min_similarity=0.82, embedding_model="test-space",
    )
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_semantic_candidates(request)

    assert result["status"] == "success"
    assert result["embedding_source"] == "concept"
    assert result["candidates"] == [{
        "candidate_id": "candidate-a",
        "best_similarity": 0.91,
        "evidence": [
            {"concept_key": korean.key, "similarity": 0.91,
             "kind": "semantic_related"},
            {"concept_key": asian.key, "similarity": 0.87,
             "kind": "semantic_related"},
        ],
    }]
    query = seen["query"]
    assert "db.index.vector.queryNodes" in seen["ann"]
    assert seen["ann_params"]["index_name"] == "concept_embedding_index"
    assert "MATCH (concept:Concept)" not in query
    assert "[:PREFERS]" in query
    assert "AVOIDS" not in query
    assert "LIMIT $concept_limit" in seen["ann"]
    assert "LIMIT $per_concept_limit" in query
    assert "LIMIT $candidate_limit" in query
    # Expansion has a streaming limit before exclusion/sort/group operations.
    assert query.index("LIMIT $per_concept_limit") < query.index("WHERE candidate.id")
    assert query.index("LIMIT $per_concept_limit") < query.index("ORDER BY")
    assert "Concept {key:hit.concept_key}" in query
    assert not any(token in query for token in ("MERGE ", " SET ", "DELETE ", "CREATE "))
    assert seen["ann_params"]["neighbor_limit"] == 24
    assert seen["ann_params"]["concept_limit"] == 8
    assert seen["params"]["candidate_limit"] == 40


def test_missing_query_concept_embedding_requests_ephemeral_query_vector():
    def run(query, **_params):
        text = str(query)
        if "SHOW VECTOR INDEXES" in text:
            return _Result(single=_online_index())
        if "RETURN concept.embedding AS embedding" in text:
            return _Result(single=None)
        raise AssertionError(text)

    driver, _session = _graph(run)
    request = agent_api.PreferenceSemanticCandidateRequest(
        requester_user_id="owner", topic="韓流音樂",
    )
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_semantic_candidates(request)
    assert result["status"] == "query_embedding_required"
    assert result["candidates"] == []


def test_readiness_reports_unknown_historical_fingerprint_without_writing():
    seen = []

    def run(query, **_params):
        text = str(query)
        seen.append(text)
        if "SHOW VECTOR INDEXES" in text:
            return _Result(single=_online_index())
        if "count(concept) AS total" in text:
            return _Result(single={"total": 10, "embedded": 8})
        raise AssertionError(text)

    driver, _session = _graph(run)
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_semantic_readiness()
    assert result["retrieval_ready"] is True
    assert result["active_enable_ready"] is False
    assert result["historical_embedding_fingerprint"] == "unknown"
    assert result["embedding_coverage"]["coverage_ratio"] == 0.8
    assert not any(
        token in query for query in seen
        for token in ("MERGE ", " SET ", "DELETE ", "CREATE ")
    )


@pytest.mark.parametrize("fault", ["missing", "populating", "dimension", "metric", "property"])
def test_offline_or_wrong_dimension_index_fails_closed(fault):
    bad_index = _online_index()
    if fault == "missing":
        bad_index = None
    elif fault == "populating":
        bad_index["state"] = "POPULATING"
    elif fault == "dimension":
        bad_index["options"]["indexConfig"]["vector.dimensions"] = 1536
    elif fault == "metric":
        bad_index["options"]["indexConfig"]["vector.similarity_function"] = "euclidean"
    else:
        bad_index["properties"] = ["wrong_embedding"]

    def run(query, **_params):
        if "SHOW VECTOR INDEXES" in str(query):
            return _Result(single=bad_index)
        raise AssertionError(str(query))

    driver, _session = _graph(run)
    request = agent_api.PreferenceSemanticCandidateRequest(
        requester_user_id="owner", topic="K-pop",
    )
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_semantic_candidates(request)
    assert result["status"] == "error"
    assert result["error_code"] == "semantic_index_unavailable"
    assert result["candidates"] == []


@pytest.mark.parametrize("model,task", [(None, None), ("other-model", "semantic_similarity"), ("test-space", "retrieval_document")])
def test_unknown_or_mismatched_space_stops_before_user_expansion(model, task):
    def run(query, **_params):
        if "SHOW VECTOR INDEXES" in str(query):
            return _Result(single=_online_index())
        if "db.index.vector.queryNodes" in str(query):
            return _Result(rows=[{"concept_key": "korean_pop", "similarity": .93, "model": model, "task": task}])
        raise AssertionError("must not expand users with unverifiable vector space")
    driver, session = _graph(run)
    request = agent_api.PreferenceSemanticCandidateRequest(
        requester_user_id="owner", topic="K-pop", query_embedding=[1.] + [0.] * 767,
        embedding_model="test-space",
    )
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_semantic_candidates(request)
    assert result["error_code"] == "semantic_readiness_unconfirmed"
    assert result["candidates"] == []
    assert session.run.call_count == 2
