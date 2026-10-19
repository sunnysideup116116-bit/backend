from unittest.mock import MagicMock, patch

import agent_api
from concept_identity import canonicalize_concept


def _query_text(query):
    return getattr(query, "text", query)


def test_exact_preference_lookup_is_canonical_bounded_and_owner_safe():
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    query_rows = [
        {"candidate_id": "candidate-a"}, {"candidate_id": "blocked"},
        {"candidate_id": "candidate-b"},
    ]
    identity = canonicalize_concept("K-pop")
    metadata = MagicMock()
    metadata.single.return_value = identity.as_dict()
    session.run.side_effect = [metadata, query_rows, []]
    request = agent_api.PreferenceCandidateRequest(
        requester_user_id="owner", topic="Kpop",
        excluded_user_ids=["blocked"], limit=20,
    )
    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_candidates(request)
    assert result == {
        "status": "success", "canonical_key": identity.key,
        "normalized_topic": "K-pop",
        "candidate_ids": ["candidate-a", "candidate-b"],
        "candidate_count_before_filter": 3,
        "candidate_count_after_filter": 2,
    }
    query = session.run.call_args_list[1]
    assert "Concept {key:$key}" in query.args[0]
    assert "<-[:PREFERS]-" in query.args[0]
    assert "AVOIDS" not in query.args[0]
    assert "ORDER BY" not in query.args[0]
    assert query.args[0].index("LIMIT $limit") < query.args[0].index(
        "RETURN candidate.id AS candidate_id",
    )
    assert query.kwargs["key"] == identity.key
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
        "canonical_key": canonicalize_concept("K-pop").key, "candidate_ids": [],
    }


def test_activity_v2_only_lookup_batches_bounded_phrase_keys_and_requires_active_prefers():
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    badminton = canonicalize_concept("羽球")
    play_badminton = canonicalize_concept("打羽球")
    doubles = canonicalize_concept("羽球雙打")
    metadata = MagicMock()
    metadata.single.return_value = badminton.as_dict()
    session.run.side_effect = [
        metadata,
        [{"candidate_id": "candidate-exact"}],
        [
            {"canonical_key": play_badminton.key, "candidate_id": "candidate-play"},
            {"canonical_key": doubles.key, "candidate_id": "candidate-doubles"},
        ],
    ]
    request = agent_api.PreferenceCandidateRequest(
        requester_user_id="owner", topic="羽球", excluded_user_ids=["blocked"],
        limit=10, v2_only=True, additional_topics=["打羽球", "羽球雙打"],
    )

    driver_factory = MagicMock(return_value=driver)
    with patch.object(agent_api.GraphDatabase, "driver", driver_factory):
        result = agent_api.preference_candidates(request)

    assert result["v2_only"] is True
    assert result["candidate_ids"] == ["candidate-exact"]
    assert result["additional_candidates"] == [
        {"canonical_key": play_badminton.key, "candidate_ids": ["candidate-play"]},
        {"canonical_key": doubles.key, "candidate_ids": ["candidate-doubles"]},
    ]
    assert len(session.run.call_args_list) == 3  # key lookup, exact owners, one batched variant query
    for call in session.run.call_args_list:
        assert 0 < call.args[0].timeout <= agent_api.PREFERENCE_CANDIDATE_GRAPH_TIMEOUT_SECONDS
    assert driver_factory.call_args.kwargs["connection_timeout"] == 1.0
    assert driver_factory.call_args.kwargs["connection_acquisition_timeout"] == 1.0
    exact_query = session.run.call_args_list[1]
    for required in (
        "coalesce(preference.active,true)=true",
        "candidate.preference_projection_pending IS NULL",
        "NOT EXISTS",
        "concept.semantic_text = $semantic_text",
        "coalesce(concept.fidelity_status,'complete') = 'complete'",
        "concept.canonical_key IS NULL",
        "concept.concept_key IS NULL",
        "AVOIDS",
        "MEMORY_DISABLED",
        "candidate.id NOT IN $excluded_user_ids",
    ):
        assert required in _query_text(exact_query.args[0])
    assert "<-[:PREFERS]-" not in _query_text(exact_query.args[0])
    variant_query = session.run.call_args_list[2]
    assert "UNWIND $proofs AS proof" in _query_text(variant_query.args[0])
    assert "Concept {key:proof.key}" in _query_text(variant_query.args[0])
    assert "LIMIT $limit" in _query_text(variant_query.args[0])
    assert variant_query.kwargs["limit"] == 9
    assert "legacy_key" not in " ".join(
        _query_text(call.args[0]) for call in session.run.call_args_list
    )


def test_activity_v2_only_phrase_lookup_runs_when_main_topic_concept_is_absent():
    driver, session = MagicMock(), MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    badminton = canonicalize_concept("羽球")
    play_badminton = canonicalize_concept("打羽球")
    metadata = MagicMock()
    metadata.single.return_value = None
    session.run.side_effect = [
        metadata,
        [{"canonical_key": play_badminton.key, "candidate_id": "candidate-play"}],
    ]
    request = agent_api.PreferenceCandidateRequest(
        requester_user_id="owner", topic="羽球", limit=10, v2_only=True,
        additional_topics=["打羽球"],
    )

    with patch.object(agent_api.GraphDatabase, "driver", return_value=driver):
        result = agent_api.preference_candidates(request)

    assert result["canonical_key"] == badminton.key
    assert result["candidate_ids"] == []
    assert result["additional_candidates"] == [
        {"canonical_key": play_badminton.key, "candidate_ids": ["candidate-play"]},
    ]
    assert len(session.run.call_args_list) == 2
    assert all(
        0 < call.args[0].timeout <= agent_api.PREFERENCE_CANDIDATE_GRAPH_TIMEOUT_SECONDS
        for call in session.run.call_args_list
    )
    assert "UNWIND $proofs AS proof" in _query_text(session.run.call_args_list[1].args[0])
    assert session.run.call_args_list[1].kwargs["limit"] == 10
