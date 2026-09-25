"""Trusted-short P0 differential oracle and explicit legacy-unknown exclusions.

All rows are synthetic. The frozen P0 router/context function bodies are loaded
verbatim; no graph, provider, quota service, or external persistence is called.
"""

import pytest

from routers import match as router
from matchmaker_agent.concept_identity import canonicalize_concept, canonicalize_concept_v1
from tests.test_preference_off_parity import (
    SHORT_PREFERENCE_LABELS, baseline_symbols, frozen_context_contract,
    identity_equivalence_projection,
)


@pytest.mark.parametrize("topic", SHORT_PREFERENCE_LABELS)
@pytest.mark.parametrize("owner_stance,candidate_stance", [
    ("like", "like"), ("avoid", "like"), ("like", "avoid"),
])
def test_trusted_short_graph_evidence_matches_frozen_p0_qualification(
    monkeypatch, topic, owner_stance, candidate_stance,
):
    results = []
    for old in (True, False):
        with monkeypatch.context() as patch:
            identity = (canonicalize_concept_v1 if old else canonicalize_concept)(topic)
            # P0 rows here are independently known complete synthetic owner
            # assertions, NOT a claim that arbitrary production v1 data is safe.
            row = {"key": identity.key, "label": identity.label} if old else identity.as_dict()
            patch.setattr(router, "get_user_graph_memories", lambda uid, _limit: [{
                **row, "stance": owner_stance if uid == "owner" else candidate_stance,
            }])
            if old:
                env = baseline_symbols("social/routers/match.py", [
                    "candidate_qualification", "_trait_stances",
                ], router.__dict__)
                env["safe_search_context"] = frozen_context_contract()["safe_search_context"]
            else:
                env = router.__dict__
            result = env["candidate_qualification"](
                {"user_id": "owner"}, {"user_id": "candidate"},
                search_context={"search_intent": "preference", "normalized_topic": topic,
                                "canonical_preference_key": identity.key},
            )
            if not old:
                assert "legacy_identity_status" not in result
                assert result.pop("semantic_related_preference_matched") is False
                assert result.pop("preference_retrieval_evidence") == []
            results.append(identity_equivalence_projection(result))
    assert results[1] == results[0]
    assert results[1]["eligible"] is (owner_stance == candidate_stance == "like")


@pytest.mark.parametrize("topic", SHORT_PREFERENCE_LABELS)
def test_unverified_legacy_short_preference_is_not_exact_evidence(monkeypatch, topic):
    old = canonicalize_concept_v1(topic)
    new = canonicalize_concept(topic)
    monkeypatch.setattr(router, "get_user_graph_memories", lambda uid, _limit: [
        {"key": old.key, "label": old.label, "stance": "like"},
    ] if uid == "candidate" else [])
    result = router.candidate_qualification(
        {"user_id": "owner"}, {"user_id": "candidate"},
        search_context={"search_intent": "preference", "normalized_topic": topic,
                        "canonical_preference_key": new.key},
    )
    assert not result["eligible"]
    assert not result["requested_preference_matched"]
    assert result["shared_preference_keys"] == []


@pytest.mark.parametrize("legacy_side", ["owner", "candidate"])
@pytest.mark.parametrize("legacy_stance,v2_stance", [("avoid", "like"), ("like", "avoid")])
def test_mixed_version_avoid_cannot_silently_become_no_conflict(monkeypatch, legacy_side, legacy_stance, v2_stance):
    old = canonicalize_concept_v1("Coffee Shop")
    current = canonicalize_concept("Coffee Shop")
    monkeypatch.setattr(router, "get_user_graph_memories", lambda uid, _limit: [
        {"key": old.key, "label": old.label, "stance": legacy_stance}
        if uid == legacy_side else {**current.as_dict(), "stance": v2_stance},
    ])
    result = router.candidate_qualification(
        {"user_id": "owner", "context_signals": {"activity": "看展"}},
        {"user_id": "candidate", "context_signals": {"activity": "看展"}},
        search_context={"search_intent": "preference", "normalized_topic": "Coffee Shop"},
    )
    assert not result["eligible"]
    assert result["legacy_identity_status"] == "indeterminate_legacy_conflict"
    # There is otherwise sufficient positive ground: rejection must come from
    # indeterminate cross-version conflict, not simply absence of match evidence.
    assert "shared_activity" in result["strong_reason_codes"]


def test_owner_assertion_proof_allows_read_only_legacy_comparison(monkeypatch):
    old = canonicalize_concept_v1("K-pop")
    current = canonicalize_concept("K-pop")
    row = {"key": old.key, "label": old.label, "stance": "like",
           "legacy_evidence_scope": "owner_assertion", "legacy_fidelity_status": "complete",
           "legacy_semantic_text": "K-pop", "legacy_semantic_input_hash": current.semantic_input_hash}
    original = dict(row)
    monkeypatch.setattr(router, "get_user_graph_memories", lambda uid, _limit: [row] if uid == "candidate" else [])
    result = router.candidate_qualification(
        {"user_id": "owner"}, {"user_id": "candidate"},
        search_context={"search_intent": "preference", "normalized_topic": "K-pop"},
    )
    assert result["eligible"]
    assert result["requested_preference_matched"]
    assert row == original
    assert row["key"] == old.key


def test_display_label_cannot_supply_a_missing_v2_semantic_source(monkeypatch):
    identity = canonicalize_concept("K-pop")
    row = {**identity.as_dict(), "stance": "like", "semantic_text": ""}
    monkeypatch.setattr(router, "get_user_graph_memories", lambda uid, _limit: [row] if uid == "candidate" else [])
    result = router.candidate_qualification(
        {"user_id": "owner"}, {"user_id": "candidate"},
        search_context={"search_intent": "preference", "normalized_topic": "K-pop"},
    )
    assert not result["eligible"]
    assert not result["requested_preference_matched"]


def test_unverified_legacy_projection_never_claims_confirmed_shared_preference(monkeypatch):
    # A concrete activity can legitimately qualify this pair, but that must
    # not turn an unverified same-prefix legacy Concept into shared evidence.
    monkeypatch.setattr(router, "get_user_graph_memories", lambda _uid, _limit: [
        {"key": "legacy_prefix", "label": "Unverified preference", "stance": "like"},
    ])
    _, reasons, _, reason_text = router.build_validated_match_explanation(
        {"user_id": "owner", "context_signals": {"activity": "看展"}},
        {"user_id": "candidate", "context_signals": {"activity": "看展"}},
        0.8,
    )
    assert "Unverified preference" not in reason_text
    assert not any(item.get("kind") == "shared_graph" for item in reasons)


def test_forged_v2_identity_cannot_create_requested_preference_public_claim(monkeypatch):
    identity = canonicalize_concept("K-pop")
    monkeypatch.setattr(router, "get_user_graph_memories", lambda uid, _limit: [
        {**identity.as_dict(), "semantic_text": "Whisky", "stance": "like"},
    ] if uid == "candidate" else [])
    _, reasons, _, reason_text = router.build_validated_match_explanation(
        {"user_id": "owner"}, {"user_id": "candidate"}, 0,
        search_context={"search_intent": "preference", "normalized_topic": "K-pop"},
    )
    assert not any(item.get("kind") == "requested_preference" for item in reasons)
    assert "明確提過喜歡" not in reason_text
    intros = router.build_friend_intro_v4(
        {"user_id": "owner"}, {"user_id": "candidate"}, 0,
        search_context={"search_intent": "preference", "normalized_topic": "K-pop"},
    )
    assert all("明確提過喜歡" not in item["viewer_text"] for item in intros.values())


@pytest.mark.parametrize("topic", SHORT_PREFERENCE_LABELS)
def test_trusted_short_public_rationale_matches_frozen_p0(monkeypatch, topic):
    results = []
    for old in (True, False):
        with monkeypatch.context() as patch:
            identity = (canonicalize_concept_v1 if old else canonicalize_concept)(topic)
            row = {"key": identity.key, "label": identity.label} if old else identity.as_dict()
            patch.setattr(router, "get_user_graph_memories", lambda _uid, _limit: [{**row, "stance": "like"}])
            if old:
                env = baseline_symbols("social/routers/match.py", ["build_validated_match_explanation"], router.__dict__)
                env["safe_search_context"] = frozen_context_contract()["safe_search_context"]
            else:
                env = router.__dict__
            result = env["build_validated_match_explanation"](
                {"user_id": "owner"}, {"user_id": "candidate"}, 0,
                search_context={"search_intent": "preference", "normalized_topic": topic},
            )
            results.append(identity_equivalence_projection(result))
    assert results[1] == results[0]
    assert any(item["kind"] == "shared_graph" for item in results[1][1])
    assert any(item["kind"] == "requested_preference" for item in results[1][1])
