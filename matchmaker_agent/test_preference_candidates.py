from unittest.mock import MagicMock, patch

import agent_api


def test_exact_preference_lookup_is_canonical_bounded_and_owner_safe():
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    query_rows = [
        {"candidate_id": "candidate-a"}, {"candidate_id": "blocked"},
        {"candidate_id": "candidate-b"},
    ]
    session.run.return_value = query_rows
    request = agent_api.PreferenceCandidateRequest(
        requester_user_id="owner", topic="Kpop",
        excluded_user_ids=["blocked"], limit=20,
    )
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_candidates(request)
    assert result == {
        "status": "success", "canonical_key": "k_pop",
        "normalized_topic": "K-pop",
        "candidate_ids": ["candidate-a", "candidate-b"],
        "candidate_count_before_filter": 3,
        "candidate_count_after_filter": 2,
    }
    query = session.run.call_args
    assert "Concept {key:$key}" in query.args[0]
    assert "<-[:PREFERS]-" in query.args[0]
    assert "AVOIDS" not in query.args[0]
    assert "ORDER BY" not in query.args[0]
    assert query.args[0].index("LIMIT $limit") < query.args[0].index(
        "RETURN candidate.id AS candidate_id",
    )
    assert query.kwargs["key"] == "k_pop"
    assert query.kwargs["requester_user_id"] == "owner"
    assert query.kwargs["limit"] == 20


def test_preference_lookup_failure_returns_no_private_graph_payload():
    driver = MagicMock()
    driver.__enter__.side_effect = RuntimeError("private graph detail")
    request = agent_api.PreferenceCandidateRequest(
        requester_user_id="owner", topic="K-pop", limit=20,
    )
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_candidates(request)
    assert result == {
        "status": "error", "error_code": "preference_graph_unavailable",
        "canonical_key": "k_pop", "candidate_ids": [],
    }
