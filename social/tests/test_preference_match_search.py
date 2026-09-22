import time
from unittest.mock import Mock

import pytest

from routers import match as router
from services.match_search_context import context_embedding_source_hash
from services.profile_projection import without_expired_recent_context
from matchmaker_agent.concept_identity import canonicalize_concept

K_POP = canonicalize_concept("K-pop").key


def _flow(monkeypatch, *, candidate, target=None):
    target = target or {
        "user_id": "owner",
        "current_context": "最近想游泳",
        "recent_context_expires_at": time.time() + 3600,
        "context_embedding": [0.1],
        "context_embedding_source_hash": context_embedding_source_hash("最近想游泳"),
        "current_context_revision": 1,
    }
    profiles, matches = Mock(), Mock()

    def find_one(query, *_args, **_kwargs):
        return dict(target) if query.get("user_id") == "owner" else dict(candidate)

    profiles.find_one.side_effect = find_one
    profiles.find.return_value = [dict(candidate)]
    profiles.aggregate.return_value = [dict(candidate)]
    matches.find.return_value = []
    matches.find_one.return_value = None
    matches.insert_one.return_value.inserted_id = "proposal"
    monkeypatch.setattr(router, "profiles_coll", profiles)
    monkeypatch.setattr(router, "matches_coll", matches)
    monkeypatch.setattr(router.risk_block_service, "excluded_user_ids", lambda _: set())
    monkeypatch.setattr(router, "agent_candidate_limit", lambda: 3)
    monkeypatch.setattr(router, "build_validated_match_explanation", lambda *_a, **_k: (
        {"graph": 6}, [{"kind": "recommendation_tier", "text": "grounded"}],
        ["對方明確喜歡 K-pop"], "對方明確喜歡 K-pop",
    ))
    monkeypatch.setattr(router, "build_friend_intro_v4", lambda *_a, **_k: {
        "initiator_preview": {"viewer_text": "對方明確喜歡 K-pop；你願意認識對方嗎？"},
        "receiver_invitation": {"viewer_text": "有人想認識你；你願意認識對方嗎？"},
    })
    monkeypatch.setattr(
        router, "_request_matchmaker_selection",
        lambda payload, **_kwargs: [{"matched_user_id": payload["candidates"][0]["user_id"]}],
    )
    return profiles, matches


def _preference_context():
    return {
        "search_intent": "preference",
        "normalized_topic": "K-pop",
        "canonical_preference_key": K_POP,
        "query_text": "幫我找喜歡 K-pop 的人",
    }


def test_preference_search_uses_exact_graph_and_ignores_unrelated_recent_context(monkeypatch):
    candidate = {
        "user_id": "candidate", "current_context": "最近在爬山",
        "recent_context_expires_at": time.time() + 3600,
    }
    profiles, matches = _flow(monkeypatch, candidate=candidate)
    monkeypatch.setattr(router, "get_embedding", Mock(side_effect=AssertionError(
        "preference search must not use recent-context embeddings"
    )))
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": ["candidate"], "canonical_key": K_POP,
        "normalized_topic": "K-pop", "retrieval_source": "graph_exact",
    })
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {K_POP: {"like"}} if user_id == "candidate" else {}
    ))

    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert result["matches"][0]["matched_user_id"] == "candidate"
    profiles.aggregate.assert_not_called()
    profile_filter = profiles.find.call_args.args[0]
    assert profile_filter["test_match_cohort"] == {"$exists": False}
    assert profile_filter["user_id"]["$in"] == ["candidate"]
    assert "owner" in profile_filter["user_id"]["$nin"]
    inserted = matches.insert_one.call_args.args[0]
    assert inserted["match_source_kind"] == "preference"
    assert inserted["search_context"]["canonical_preference_key"] == K_POP
    assert result["diagnostics"]["retrieval_source"] == "graph_exact"
    assert result["diagnostics"]["candidate_count_after_filter"] == 1


def test_preference_search_still_applies_block_and_pair_history_exclusions(monkeypatch):
    candidate = {"user_id": "candidate", "current_context": ""}
    _profiles, _matches = _flow(monkeypatch, candidate=candidate)
    router.matches_coll.find.return_value = [
        {"from_user": "owner", "to_user": "historical", "status": "accepted", "created_at": 1,
         "last_decision": {"from": "pending", "to": "accepted", "action": "accept"}},
    ]
    monkeypatch.setattr(router.risk_block_service, "excluded_user_ids", lambda _: {"blocked"})
    captured = {}

    def lookup(_owner, _topic, *, excluded_user_ids, limit):
        captured.update(excluded=set(excluded_user_ids), limit=limit)
        return {"candidate_ids": ["candidate"], "canonical_key": K_POP,
                "normalized_topic": "K-pop", "retrieval_source": "graph_exact"}

    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lookup)
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {K_POP: {"like"}} if user_id == "candidate" else {}
    ))
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert {"owner", "blocked", "historical"} <= captured["excluded"]
    assert captured["limit"] == router.MATCH_VECTOR_RETRIEVAL_LIMIT


def test_activity_search_remains_vector_driven(monkeypatch):
    candidate = {
        "user_id": "candidate", "current_context": "最近想看展",
        "recent_context_expires_at": time.time() + 3600, "score": .9,
    }
    profiles, _matches = _flow(monkeypatch, candidate=candidate)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", Mock(
        side_effect=AssertionError("activity search must stay on vector retrieval")
    ))
    monkeypatch.setattr(router, "get_embedding", lambda _text: [.2])
    monkeypatch.setattr(router, "_trait_stances", lambda _user_id: {})
    result = router.generate_matches_for_user(
        "owner", source="automatic",
        search_context={"search_intent": "activity", "invitation_topic": "看展",
                        "query_text": "找最近也想看展的人"},
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    profiles.aggregate.assert_called_once()
    assert result["diagnostics"]["retrieval_source"] == "vector"


def test_exact_preference_hit_still_stops_at_a_hard_conflict(monkeypatch):
    candidate = {"user_id": "candidate", "current_context": ""}
    _profiles, matches = _flow(monkeypatch, candidate=candidate)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": ["candidate"], "canonical_key": K_POP,
        "normalized_topic": "K-pop", "retrieval_source": "graph_exact",
        "candidate_count_before_filter": 1, "candidate_count_after_filter": 1,
    })

    def stances(user_id):
        if user_id == "owner":
            return {"smoking": {"avoid"}}
        return {K_POP: {"like"}, "smoking": {"like"}}

    monkeypatch.setattr(router, "_trait_stances", stances)
    select = Mock(side_effect=AssertionError("hard conflicts must not reach Matchmaker"))
    monkeypatch.setattr(router, "_request_matchmaker_selection", select)
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "no_suitable_candidate"
    assert result["diagnostics"]["hard_conflicts"] == ["smoking"]
    assert result["diagnostics"]["qualification_reason_codes"]["hard_conflict"] == 1
    select.assert_not_called()
    matches.insert_one.assert_not_called()


def test_expired_recent_context_cannot_qualify_or_reach_matchmaker(monkeypatch):
    candidate = {
        "user_id": "candidate", "current_context": "最近想看展",
        "context_signals": {"activity": "看展"},
        "recent_context_expires_at": time.time() - 1, "score": .99,
    }
    profiles, matches = _flow(monkeypatch, candidate=candidate)
    select = Mock(return_value=[])
    monkeypatch.setattr(router, "_request_matchmaker_selection", select)
    monkeypatch.setattr(router, "_trait_stances", lambda _user_id: {})
    result = router.generate_matches_for_user(
        "owner", source="automatic", report_progress=lambda _step: True,
        can_commit=lambda: True,
    )
    assert result["status"] == "no_suitable_candidate"
    select.assert_not_called()
    matches.insert_one.assert_not_called()
    match_filter = profiles.aggregate.call_args.args[0][1]["$match"]
    assert any("recent_context_expires_at" in str(clause) for clause in match_filter["$and"])
    assert without_expired_recent_context(candidate)["current_context"] == ""


def test_preference_search_keeps_durable_evidence_but_strips_expired_context(monkeypatch):
    candidate = {
        "user_id": "candidate", "current_context": "最近在爬山",
        "context_signals": {"activity": "爬山"},
        "recent_context_expires_at": time.time() - 1,
    }
    _profiles, matches = _flow(monkeypatch, candidate=candidate)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": ["candidate"], "canonical_key": K_POP,
        "normalized_topic": "K-pop", "retrieval_source": "graph_exact",
        "candidate_count_before_filter": 1, "candidate_count_after_filter": 1,
    })
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {K_POP: {"like"}} if user_id == "candidate" else {}
    ))

    def select(payload, **_kwargs):
        assert payload["candidates"][0].get("current_context", "") == ""
        assert payload["candidates"][0].get("context_signals", {}) == {}
        assert payload["search_context"]["search_intent"] == "preference"
        return [{"matched_user_id": "candidate"}]

    monkeypatch.setattr(router, "_request_matchmaker_selection", select)
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert result["matches"][0]["current_context"] == ""
    saved = matches.insert_one.call_args.args[0]
    assert saved["match_context_snapshot"]["candidate"]["current_context"] == ""


def _activate_semantic(monkeypatch, result):
    monkeypatch.setattr(router, "preference_semantic_mode", lambda: "active")
    monkeypatch.setattr(router, "semantic_embedding_space_confirmed", lambda: True)
    monkeypatch.setattr(router, "qualified_exact_trigger_threshold", lambda: 1)
    lookup = Mock(return_value=result)
    monkeypatch.setattr(router, "retrieve_semantic_preference_candidates", lookup)
    return lookup


def _semantic_result(candidate_id="semantic"):
    return {
        "canonical_key": K_POP,
        "candidate_ids": [candidate_id],
        "evidence_by_candidate": {candidate_id: [{
            "kind": "semantic_related", "concept_key": "korean_pop",
            "similarity": 0.91,
        }]},
        "semantic_concepts_considered": [{
            "concept_key": "korean_pop", "similarity": 0.91,
        }],
        "retrieval_source": "graph_semantic",
    }


def test_hard_conflicting_exact_hits_trigger_semantic_fallback(monkeypatch):
    exact = {"user_id": "exact", "current_context": ""}
    semantic = {"user_id": "semantic", "current_context": ""}
    profiles, matches = _flow(monkeypatch, candidate=semantic)

    def find(query, *_args, **_kwargs):
        ids = set((query.get("user_id") or {}).get("$in") or [])
        return [row for row in (exact, semantic) if row["user_id"] in ids]

    profiles.find.side_effect = find
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": ["exact"], "canonical_key": K_POP,
        "normalized_topic": "K-pop", "query_provenance": "exact_canonical",
        "candidate_count_before_filter": 1, "candidate_count_after_filter": 1,
    })
    semantic_lookup = _activate_semantic(monkeypatch, _semantic_result())

    def stances(user_id):
        return {
            "owner": {"smoking": {"avoid"}},
            "exact": {K_POP: {"like"}, "smoking": {"like"}},
            "semantic": {"korean_pop": {"like"}},
        }.get(user_id, {})

    monkeypatch.setattr(router, "_trait_stances", stances)
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert result["matches"][0]["matched_user_id"] == "semantic"
    assert "preference_retrieval_evidence" not in result["matches"][0]
    assert result["diagnostics"]["qualified_exact_count"] == 0
    assert result["diagnostics"]["semantic_fallback_triggered"] is True
    semantic_lookup.assert_called_once()
    saved = matches.insert_one.call_args.args[0]
    assert saved["preference_retrieval_evidence"] == [{
        "kind": "semantic_related", "concept_key": "korean_pop",
        "similarity": 0.91,
    }]


def test_exact_hits_removed_by_block_history_still_trigger_fallback(monkeypatch):
    semantic = {"user_id": "semantic", "current_context": ""}
    profiles, _matches = _flow(monkeypatch, candidate=semantic)
    profiles.find.return_value = [semantic]
    monkeypatch.setattr(router.risk_block_service, "excluded_user_ids", lambda _uid: {"blocked"})
    router.matches_coll.find.return_value = [{
        "from_user": "owner", "to_user": "historical", "status": "accepted",
        "created_at": 1,
        "last_decision": {"from": "pending", "to": "accepted", "action": "accept"},
    }]
    captured = {}

    def exact_lookup(_owner, _topic, *, excluded_user_ids, limit):
        captured["excluded"] = set(excluded_user_ids)
        return {
            "candidate_ids": [], "canonical_key": K_POP,
            "query_provenance": "exact_canonical",
            "candidate_count_before_filter": 2,
            "candidate_count_after_filter": 0,
        }

    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", exact_lookup)
    semantic_lookup = _activate_semantic(monkeypatch, _semantic_result())
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {"korean_pop": {"like"}} if user_id == "semantic" else {}
    ))
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert {"owner", "blocked", "historical"} <= captured["excluded"]
    assert result["diagnostics"]["qualified_exact_count"] == 0
    semantic_lookup.assert_called_once()


def test_profile_ineligible_exact_hit_triggers_semantic_fallback(monkeypatch):
    semantic = {"user_id": "semantic", "current_context": ""}
    profiles, _matches = _flow(monkeypatch, candidate=semantic)

    def find(query, *_args, **_kwargs):
        ids = set((query.get("user_id") or {}).get("$in") or [])
        return [semantic] if "semantic" in ids else []

    profiles.find.side_effect = find
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": ["profile-ineligible"], "canonical_key": K_POP,
        "query_provenance": "exact_canonical",
        "candidate_count_before_filter": 1, "candidate_count_after_filter": 1,
    })
    semantic_lookup = _activate_semantic(monkeypatch, _semantic_result())
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {"korean_pop": {"like"}} if user_id == "semantic" else {}
    ))
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert result["diagnostics"]["qualified_exact_count"] == 0
    semantic_lookup.assert_called_once()


def test_alias_exact_candidate_keeps_direct_strength_and_skips_semantic(monkeypatch):
    candidate = {"user_id": "candidate", "current_context": ""}
    _profiles, matches = _flow(monkeypatch, candidate=candidate)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": ["candidate"], "canonical_key": K_POP,
        "normalized_topic": "K-pop", "query_provenance": "deterministic_alias",
        "candidate_count_before_filter": 1, "candidate_count_after_filter": 1,
    })
    semantic_lookup = _activate_semantic(
        monkeypatch, _semantic_result("must-not-be-used"),
    )
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {K_POP: {"like"}} if user_id == "candidate" else {}
    ))
    alias_context = _preference_context()
    alias_context["query_text"] = "幫我找喜歡 Kpop 的人"
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=alias_context,
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert result["diagnostics"]["qualified_exact_count"] == 1
    assert result["diagnostics"]["query_provenance"] == "deterministic_alias"
    assert result["diagnostics"]["semantic_fallback_triggered"] is False
    semantic_lookup.assert_not_called()
    assert matches.insert_one.call_args.args[0]["preference_retrieval_evidence"][0][
        "kind"
    ] == "deterministic_alias"


def test_semantic_transient_failure_is_not_reported_as_no_candidates(monkeypatch):
    candidate = {"user_id": "unused", "current_context": ""}
    _profiles, _matches = _flow(monkeypatch, candidate=candidate)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": [], "canonical_key": K_POP,
        "candidate_count_before_filter": 0, "candidate_count_after_filter": 0,
    })
    monkeypatch.setattr(router, "preference_semantic_mode", lambda: "active")
    monkeypatch.setattr(router, "semantic_embedding_space_confirmed", lambda: True)
    monkeypatch.setattr(router, "qualified_exact_trigger_threshold", lambda: 1)
    monkeypatch.setattr(
        router, "retrieve_semantic_preference_candidates",
        Mock(side_effect=router.PreferenceSemanticRetrievalError(
            "semantic_graph_unavailable"
        )),
    )
    with pytest.raises(router.MatchSearchPipelineError) as caught:
        router.generate_matches_for_user(
            "owner", source="automatic", search_context=_preference_context(),
            report_progress=lambda _step: True, can_commit=lambda: True,
        )
    assert caught.value.code == "semantic_graph_unavailable"
    assert caught.value.stage == "preference_semantic_search"


def test_normal_empty_semantic_result_uses_insufficient_semantic_ground(monkeypatch):
    candidate = {"user_id": "unused", "current_context": ""}
    _profiles, _matches = _flow(monkeypatch, candidate=candidate)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": [], "canonical_key": K_POP,
        "candidate_count_before_filter": 0, "candidate_count_after_filter": 0,
    })
    _activate_semantic(monkeypatch, {
        "canonical_key": K_POP, "candidate_ids": [],
        "evidence_by_candidate": {}, "semantic_concepts_considered": [],
    })
    monkeypatch.setattr(router, "_trait_stances", lambda _uid: {})
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "no_suitable_candidate"
    assert result["reason_code"] == "insufficient_semantic_ground"


def test_semantic_selected_candidate_uses_privacy_safe_rationale_context(monkeypatch):
    semantic = {"user_id": "semantic", "current_context": ""}
    _profiles, matches = _flow(monkeypatch, candidate=semantic)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": [], "canonical_key": K_POP,
        "candidate_count_before_filter": 0, "candidate_count_after_filter": 0,
    })
    _activate_semantic(monkeypatch, _semantic_result())
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {"korean_pop": {"like"}} if user_id == "semantic" else {}
    ))
    explain = Mock(return_value=(
        {"graph": 0}, [{"kind": "recommendation_tier", "text": "exploratory"}],
        [], "一般介紹理由",
    ))
    intro = Mock(return_value={
        "initiator_preview": {"viewer_text": "要先認識看看嗎？"},
        "receiver_invitation": {"viewer_text": "有人想認識你。"},
    })
    monkeypatch.setattr(router, "build_validated_match_explanation", explain)
    monkeypatch.setattr(router, "build_friend_intro_v4", intro)
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert all(
        call.kwargs["search_context"] == {"search_intent": "generic"}
        for call in explain.call_args_list
    )
    assert intro.call_args.kwargs["search_context"] == {
        "search_intent": "generic",
    }
    saved = matches.insert_one.call_args.args[0]
    public_copy = " ".join([
        saved["reason"], saved["receiver_reason"],
        str(saved["friend_intro_v4"]),
    ])
    assert "korean_pop" not in public_copy
    assert "共同偏好" not in public_copy


def test_shadow_mode_does_not_run_semantic_on_live_search_path(monkeypatch):
    candidate = {"user_id": "unused", "current_context": ""}
    _profiles, _matches = _flow(monkeypatch, candidate=candidate)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": [], "canonical_key": K_POP,
        "candidate_count_before_filter": 0, "candidate_count_after_filter": 0,
    })
    monkeypatch.setattr(router, "preference_semantic_mode", lambda: "shadow")
    monkeypatch.setattr(router, "qualified_exact_trigger_threshold", lambda: 1)
    semantic_lookup = Mock(side_effect=AssertionError(
        "shadow must use the separate observation path"
    ))
    monkeypatch.setattr(
        router, "retrieve_semantic_preference_candidates", semantic_lookup,
    )
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "no_suitable_candidate"
    assert result["diagnostics"]["semantic_shadow_status"] == (
        "separate_observation_only"
    )
    semantic_lookup.assert_not_called()


def test_active_mode_fails_closed_without_embedding_space_confirmation(monkeypatch):
    candidate = {"user_id": "unused", "current_context": ""}
    _profiles, _matches = _flow(monkeypatch, candidate=candidate)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": [], "canonical_key": K_POP,
        "candidate_count_before_filter": 0, "candidate_count_after_filter": 0,
    })
    monkeypatch.setattr(router, "preference_semantic_mode", lambda: "active")
    monkeypatch.setattr(router, "semantic_embedding_space_confirmed", lambda: False)
    monkeypatch.setattr(router, "qualified_exact_trigger_threshold", lambda: 1)
    with pytest.raises(router.MatchSearchPipelineError) as caught:
        router.generate_matches_for_user(
            "owner", source="automatic", search_context=_preference_context(),
            report_progress=lambda _step: True, can_commit=lambda: True,
        )
    assert caught.value.code == "semantic_readiness_unconfirmed"


def test_usable_exact_candidate_survives_semantic_failure_when_threshold_is_raised(monkeypatch):
    exact = {"user_id": "exact", "current_context": ""}
    _profiles, _matches = _flow(monkeypatch, candidate=exact)
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": ["exact"], "canonical_key": K_POP,
        "query_provenance": "exact_canonical",
        "candidate_count_before_filter": 1, "candidate_count_after_filter": 1,
    })
    monkeypatch.setattr(router, "preference_semantic_mode", lambda: "active")
    monkeypatch.setattr(router, "semantic_embedding_space_confirmed", lambda: True)
    monkeypatch.setattr(router, "qualified_exact_trigger_threshold", lambda: 2)
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {K_POP: {"like"}} if user_id == "exact" else {}
    ))
    semantic_lookup = Mock(side_effect=router.PreferenceSemanticRetrievalError(
        "semantic_graph_unavailable"
    ))
    monkeypatch.setattr(
        router, "retrieve_semantic_preference_candidates", semantic_lookup,
    )
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert result["matches"][0]["matched_user_id"] == "exact"
    assert result["diagnostics"]["semantic_fallback_error"] == (
        "semantic_graph_unavailable"
    )


def test_combined_pool_is_exact_first_then_semantic_score_order(monkeypatch):
    exact = {"user_id": "z-exact", "current_context": ""}
    high = {"user_id": "b-semantic-high", "current_context": ""}
    low = {"user_id": "a-semantic-low", "current_context": ""}
    profiles, _matches = _flow(monkeypatch, candidate=exact)

    def find(query, *_args, **_kwargs):
        ids = set((query.get("user_id") or {}).get("$in") or [])
        return [row for row in (exact, high, low) if row["user_id"] in ids]

    profiles.find.side_effect = find
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": ["z-exact"], "canonical_key": K_POP,
        "query_provenance": "exact_canonical",
        "candidate_count_before_filter": 1, "candidate_count_after_filter": 1,
    })
    monkeypatch.setattr(router, "preference_semantic_mode", lambda: "active")
    monkeypatch.setattr(router, "semantic_embedding_space_confirmed", lambda: True)
    monkeypatch.setattr(router, "qualified_exact_trigger_threshold", lambda: 2)
    monkeypatch.setattr(
        router, "retrieve_semantic_preference_candidates", Mock(return_value={
            "canonical_key": K_POP,
            "candidate_ids": ["b-semantic-high", "a-semantic-low"],
            "evidence_by_candidate": {
                "b-semantic-high": [{
                    "kind": "semantic_related", "concept_key": "korean_pop",
                    "similarity": .93,
                }],
                "a-semantic-low": [{
                    "kind": "semantic_related", "concept_key": "asian_pop",
                    "similarity": .84,
                }],
            },
            "semantic_concepts_considered": [],
        }),
    )
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: {
        "z-exact": {K_POP: {"like"}},
        "b-semantic-high": {"korean_pop": {"like"}},
        "a-semantic-low": {"asian_pop": {"like"}},
    }.get(user_id, {}))
    order = []

    def select(payload, **_kwargs):
        order.extend(candidate["user_id"] for candidate in payload["candidates"])
        return [{"matched_user_id": payload["candidates"][0]["user_id"]}]

    monkeypatch.setattr(router, "_request_matchmaker_selection", select)
    result = router.generate_matches_for_user(
        "owner", source="automatic", search_context=_preference_context(),
        report_progress=lambda _step: True, can_commit=lambda: True,
    )
    assert result["status"] == "success"
    assert order == ["z-exact", "b-semantic-high", "a-semantic-low"]
