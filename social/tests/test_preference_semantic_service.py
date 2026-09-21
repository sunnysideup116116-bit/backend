from unittest.mock import Mock

import pytest
import requests

from services import preference_semantic_service as service


def _response(payload):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = payload
    return response


def test_fallback_embeds_only_canonical_label_and_dedupes_candidates(monkeypatch):
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY", "0.82")
    post = Mock(side_effect=[
        _response({
            "status": "query_embedding_required",
            "canonical_key": "k_pop",
            "normalized_topic": "K-pop",
            "candidates": [],
        }),
        _response({
            "status": "success",
            "canonical_key": "k_pop",
            "normalized_topic": "K-pop",
            "embedding_source": "request",
            "semantic_concepts_considered": [
                {"concept_key": "korean_pop", "similarity": 0.91},
                {"concept_key": "asian_pop", "similarity": 0.86},
            ],
            "candidates": [
                {"candidate_id": "candidate-b", "evidence": [
                    {"concept_key": "asian_pop", "similarity": 0.86},
                ]},
                {"candidate_id": "candidate-a", "evidence": [
                    {"concept_key": "korean_pop", "similarity": 0.91},
                    {"concept_key": "asian_pop", "similarity": 0.85},
                ]},
                {"candidate_id": "candidate-a", "evidence": [
                    {"concept_key": "korean_pop", "similarity": 0.90},
                ]},
            ],
        }),
    ])
    embed = Mock(return_value=[[3.0] + [4.0] + [0.0] * 766])
    monkeypatch.setattr(service.requests, "post", post)
    monkeypatch.setattr(service, "get_embeddings", embed)

    result = service.retrieve_semantic_preference_candidates(
        "owner", "Kpop", excluded_user_ids=set(),
    )

    embed.assert_called_once_with(
        ["K-pop"], task_type="semantic_similarity",
        output_dimensionality=768, request_timeout_seconds=3.0,
    )
    assert result["candidate_ids"] == ["candidate-a", "candidate-b"]
    assert list(result["evidence_by_candidate"]) == ["candidate-a", "candidate-b"]
    assert result["evidence_by_candidate"]["candidate-a"] == [
        {"kind": "semantic_related", "concept_key": "korean_pop",
         "similarity": 0.91},
        {"kind": "semantic_related", "concept_key": "asian_pop",
         "similarity": 0.85},
    ]
    sent = post.call_args_list[1].kwargs["json"]
    assert len(sent["query_embedding"]) == 768
    assert sent["topic"] == "K-pop"
    assert "raw_message" not in sent


def test_below_threshold_evidence_is_insufficient_ground(monkeypatch):
    monkeypatch.setattr(service.requests, "post", Mock(return_value=_response({
        "status": "success", "canonical_key": "k_pop",
        "candidates": [{"candidate_id": "candidate", "evidence": [
            {"concept_key": "j_pop", "similarity": 0.81},
        ]}],
    })))
    result = service.retrieve_semantic_preference_candidates(
        "owner", "K-pop", excluded_user_ids=set(),
    )
    assert result["candidate_ids"] == []
    assert result["semantic_concepts_considered"] == []


def test_equal_semantic_scores_use_candidate_id_tie_break(monkeypatch):
    monkeypatch.setattr(service.requests, "post", Mock(return_value=_response({
        "status": "success", "canonical_key": "k_pop",
        "candidates": [
            {"candidate_id": "candidate-b", "evidence": [
                {"concept_key": "korean_pop", "similarity": 0.9},
            ]},
            {"candidate_id": "candidate-a", "evidence": [
                {"concept_key": "asian_pop", "similarity": 0.9},
            ]},
        ],
    })))
    result = service.retrieve_semantic_preference_candidates(
        "owner", "K-pop", excluded_user_ids=set(),
    )
    assert result["candidate_ids"] == ["candidate-a", "candidate-b"]


def test_graph_timeout_is_a_typed_transient_failure(monkeypatch):
    monkeypatch.setattr(
        service.requests, "post", Mock(side_effect=requests.Timeout("slow")),
    )
    with pytest.raises(service.PreferenceSemanticRetrievalError) as caught:
        service.retrieve_semantic_preference_candidates(
            "owner", "K-pop", excluded_user_ids=set(),
        )
    assert caught.value.code == "semantic_graph_unavailable"


def test_embedding_failure_is_a_typed_transient_failure(monkeypatch):
    monkeypatch.setattr(service.requests, "post", Mock(return_value=_response({
        "status": "query_embedding_required", "canonical_key": "k_pop",
        "candidates": [],
    })))
    monkeypatch.setattr(
        service, "get_embeddings", Mock(side_effect=TimeoutError("slow")),
    )
    with pytest.raises(service.PreferenceSemanticRetrievalError) as caught:
        service.retrieve_semantic_preference_candidates(
            "owner", "K-pop", excluded_user_ids=set(),
        )
    assert caught.value.code == "semantic_query_embedding_unavailable"


def test_total_semantic_budget_exhaustion_is_typed(monkeypatch):
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_TOTAL_TIMEOUT_SECONDS", "6")
    monkeypatch.setattr(service.time, "monotonic", Mock(side_effect=[0.0, 7.0]))
    post = Mock(side_effect=AssertionError("expired budget must not call Graph"))
    monkeypatch.setattr(service.requests, "post", post)
    with pytest.raises(service.PreferenceSemanticRetrievalError) as caught:
        service.retrieve_semantic_preference_candidates(
            "owner", "K-pop", excluded_user_ids=set(),
        )
    assert caught.value.code == "semantic_retrieval_timeout"
    post.assert_not_called()


def test_feature_defaults_and_hard_bounds(monkeypatch):
    monkeypatch.delenv("MATCH_PREFERENCE_SEMANTIC_MODE", raising=False)
    monkeypatch.delenv(
        "MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED", raising=False,
    )
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_NEIGHBOR_LIMIT", "999")
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_CONCEPT_LIMIT", "999")
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_CANDIDATE_LIMIT", "999")
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MIN_SIMILARITY", "0")
    monkeypatch.setenv(
        "MATCH_PREFERENCE_SEMANTIC_QUALIFIED_EXACT_THRESHOLD", "999",
    )
    config = service.semantic_config()
    assert config["mode"] == "off"
    assert config["embedding_space_confirmed"] is False
    assert config["neighbor_limit"] == 32
    assert config["concept_limit"] == 12
    assert config["candidate_limit"] == 50
    assert config["min_similarity"] == 0.75
    assert config["qualified_exact_threshold"] == 5


@pytest.mark.parametrize("confirmed", ["off", "on"])
def test_readiness_keeps_active_disabled_when_fingerprint_is_unknown(monkeypatch, confirmed):
    monkeypatch.setenv(
        "MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED", confirmed,
    )
    monkeypatch.setattr(service.requests, "get", Mock(return_value=_response({
        "status": "success", "retrieval_ready": True,
        "historical_embedding_fingerprint": "unknown",
        "index": {"state": "ONLINE", "dimension": 768},
    })))
    result = service.preference_semantic_readiness()
    assert result["active_enable_ready"] is False
    assert result["historical_embedding_fingerprint"] == "unknown"
    assert result["configured_task"] == "semantic_similarity"


def test_shadow_summary_never_returns_candidate_ids(monkeypatch):
    monkeypatch.setattr(service, "retrieve_semantic_preference_candidates", Mock(
        return_value={
            "canonical_key": "k_pop", "candidate_ids": ["private-id"],
            "candidate_count": 1, "retrieval_source": "graph_semantic",
            "embedding_source": "concept",
            "semantic_concepts_considered": [
                {"concept_key": "korean_pop", "similarity": 0.91},
            ],
        },
    ))
    result = service.semantic_observation_summary("owner", "K-pop")
    assert result["candidate_count"] == 1
    assert "candidate_ids" not in result


@pytest.mark.parametrize("score", [0.81999, float("nan"), float("inf"), 1.01])
def test_invalid_or_rounded_up_score_cannot_create_evidence(monkeypatch, score):
    monkeypatch.setattr(service.requests, "post", Mock(return_value=_response({
        "status": "success", "canonical_key": "k_pop",
        "candidates": [{"candidate_id": "candidate", "evidence": [{"concept_key": "korean_pop", "similarity": score}]}],
    })))
    result = service.retrieve_semantic_preference_candidates("owner", "K-pop", excluded_user_ids=set())
    assert result["candidate_ids"] == []


@pytest.mark.parametrize("payload", [
    {"status": "success", "canonical_key": "k_pop"},
    {"status": "success", "canonical_key": "k_pop", "candidates": {}},
    {"status": "query_embedding_required", "canonical_key": "wrong"},
    {"status": "error", "canonical_key": "k_pop", "error_code": "raw message and private candidate ID"},
])
def test_malformed_semantic_response_is_typed_and_not_empty_ground(monkeypatch, payload):
    monkeypatch.setattr(service.requests, "post", Mock(return_value=_response(payload)))
    with pytest.raises(service.PreferenceSemanticRetrievalError) as caught:
        service.retrieve_semantic_preference_candidates("owner", "K-pop", excluded_user_ids=set())
    assert caught.value.code == "semantic_graph_invalid_response"


def test_semantic_diagnostics_drop_nested_private_fields():
    from services.match_search_job_service import _bounded_diagnostics
    diagnostic = _bounded_diagnostics({
        "candidate_ids": ["private-id"], "raw_message": "raw-owner-message",
        "raw_graph": {"label": "private-label"},
        "semantic_fallback_error": "raw-owner-message", "semantic_mode": "private-id",
        "semantic_concepts_considered": [
            {"concept_key": "korean_pop", "similarity": .91,
             "label": "private-label", "candidate_ids": ["private-id"]},
            {"concept_key": "raw owner message", "similarity": .9},
            {"concept_key": "private_id", "similarity": float("nan")},
        ],
    })
    assert diagnostic["semantic_concepts_considered"] == [{"concept_key": "korean_pop", "similarity": .91}]
    for private in ("raw-owner-message", "private-label", "private-id", "private_id"):
        assert private not in str(diagnostic)


def test_public_basis_and_state_history_opening_drop_internal_evidence(monkeypatch):
    from bson import ObjectId
    from routers import match as router
    from services import match_card_projection, proactive_delivery_service, pair_opening_service
    match_id = ObjectId()
    internal = [{"kind": "semantic_related", "concept_key": "private_concept",
                 "similarity": .91, "label": "private-label"}]
    match = {
        "_id": match_id, "from_user": "owner", "to_user": "counterparty",
        "status": "draft", "proposal_revision": 0,
        "preference_retrieval_evidence": internal,
        "match_basis": {"level": "adjacent", "need_evidence": ["本次搜尋"],
                        "counterparty_evidence": [], "concrete_overlap": [],
                        "cannot_infer": ["語意相關不等於共同偏好"],
                        "internal_evidence": internal},
        "reason": "願意認識看看嗎？", "search_context": {},
    }
    collection = Mock()
    collection.find_one.return_value = match
    collection.find.return_value = [match]
    monkeypatch.setattr(router, "matches_coll", collection)
    monkeypatch.setattr(router, "_proposal_source_projection", lambda *_: {})
    monkeypatch.setattr(router, "match_hub_v1_enabled", lambda: False)
    monkeypatch.setattr(router, "build_active_proposal_card", lambda doc, uid: {
        "match_basis": router._public_match_basis(doc), "viewer_reason": doc["reason"],
    })
    monkeypatch.setattr(router, "proposal_counterparty_nickname", lambda *_: "好友")
    surfaces = [
        router.get_single_match_state("owner", str(match_id)),
        router.build_status_proposal_card(match, "owner"),
        match_card_projection.project_match_card_history([
            {"metadata": {"event_type": "match_proposal", "match_id": str(match_id)}},
        ], "owner", collection=collection),
        proactive_delivery_service._public_match_basis(match, "owner"),
        pair_opening_service.public_opening_evidence(match),
    ]
    for surface in surfaces:
        assert "private-label" not in str(surface)
        assert "private_concept" not in str(surface)
        assert "internal_evidence" not in str(surface)
