import time
from unittest.mock import Mock

from routers import match as router
from services.match_search_context import context_embedding_source_hash
from services.profile_projection import without_expired_recent_context


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
        "canonical_preference_key": "k_pop",
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
        "candidate_ids": ["candidate"], "canonical_key": "k_pop",
        "normalized_topic": "K-pop", "retrieval_source": "graph_exact",
    })
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {"k_pop": {"like"}} if user_id == "candidate" else {}
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
    assert inserted["search_context"]["canonical_preference_key"] == "k_pop"
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
        return {"candidate_ids": ["candidate"], "canonical_key": "k_pop",
                "normalized_topic": "K-pop", "retrieval_source": "graph_exact"}

    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lookup)
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {"k_pop": {"like"}} if user_id == "candidate" else {}
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
        "candidate_ids": ["candidate"], "canonical_key": "k_pop",
        "normalized_topic": "K-pop", "retrieval_source": "graph_exact",
        "candidate_count_before_filter": 1, "candidate_count_after_filter": 1,
    })

    def stances(user_id):
        if user_id == "owner":
            return {"smoking": {"avoid"}}
        return {"k_pop": {"like"}, "smoking": {"like"}}

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
        "candidate_ids": ["candidate"], "canonical_key": "k_pop",
        "normalized_topic": "K-pop", "retrieval_source": "graph_exact",
        "candidate_count_before_filter": 1, "candidate_count_after_filter": 1,
    })
    monkeypatch.setattr(router, "_trait_stances", lambda user_id: (
        {"k_pop": {"like"}} if user_id == "candidate" else {}
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
