"""Focused coverage for evidence-first activity matching (Phase 1).

Covers the owner-grounded activity-evidence lookup, its qualification and
user-visible reason contract, the deterministic evidence-over-vector selection
policy, the exploratory consent record, and the confirmation boundary that
creates the consent flag.
"""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from routers.match import (
    _activity_evidence_candidates,
    activity_selection_scope,
    build_validated_match_explanation,
    candidate_qualification,
)
from services import match_search_job_service as jobs
from services.match_search_context import safe_search_context
from tests.match_flow_store import Collection


class _Rows:
    """Minimal cursor for a mocked ``profiles_coll.find``."""

    def __init__(self, rows):
        self._rows = list(rows)

    def limit(self, _count):
        return list(self._rows)


TOPIC_CONTEXT = {"search_intent": "activity", "invitation_topic": "羽球"}
PREFERS_EVIDENCE = [{"kind": "candidate_prefers", "concept_key": "v2_" + "a" * 48, "label": "羽球"}]
RECENT_EVIDENCE = [{"kind": "candidate_recent_activity", "activity": "打羽球", "label": "羽球"}]


# --------------------------------------------------------------------------
# Evidence lookup
# --------------------------------------------------------------------------

def test_activity_evidence_candidates_combines_exact_preference_and_typed_activity(monkeypatch):
    import routers.match as match_router

    recent_row = {"user_id": "cand-recent", "context_signals": {"activity": "打羽球"}}
    prefers_row = {"user_id": "cand-pref", "context_signals": {}}
    profiles = MagicMock()
    profiles.find.side_effect = [_Rows([recent_row]), _Rows([prefers_row])]
    monkeypatch.setattr(match_router, "profiles_coll", profiles)
    monkeypatch.setattr(match_router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": ["cand-pref", "owner", "blocked-user"],
        "canonical_key": "v2_" + "a" * 48,
    })

    evidence, docs = match_router._activity_evidence_candidates(
        {"user_id": "owner"}, TOPIC_CONTEXT, {"owner", "blocked-user"},
    )

    assert set(evidence) == {"cand-pref", "cand-recent"}
    assert evidence["cand-pref"][0]["kind"] == "candidate_prefers"
    assert evidence["cand-recent"][0]["kind"] == "candidate_recent_activity"
    # Deterministic priority: durable preference evidence before typed activity.
    assert [doc["user_id"] for doc in docs] == ["cand-pref", "cand-recent"]


def test_activity_evidence_lookup_outage_degrades_to_typed_activity(monkeypatch):
    import routers.match as match_router

    profiles = MagicMock()
    profiles.find.side_effect = [_Rows([{"user_id": "cand-recent",
                                         "context_signals": {"activity": "打羽球"}}])]
    monkeypatch.setattr(match_router, "profiles_coll", profiles)

    def _unavailable(*_args, **_kwargs):
        raise match_router.PreferenceCandidateLookupError("preference_graph_unavailable")

    monkeypatch.setattr(match_router, "retrieve_preference_candidate_ids", _unavailable)

    evidence, docs = match_router._activity_evidence_candidates(
        {"user_id": "owner"}, TOPIC_CONTEXT, {"owner"},
    )

    assert set(evidence) == {"cand-recent"}
    assert [doc["user_id"] for doc in docs] == ["cand-recent"]


def test_activity_evidence_requires_a_bound_topic(monkeypatch):
    import routers.match as match_router

    profiles = MagicMock()
    monkeypatch.setattr(match_router, "profiles_coll", profiles)
    lookup = MagicMock()
    monkeypatch.setattr(match_router, "retrieve_preference_candidate_ids", lookup)

    evidence, docs = match_router._activity_evidence_candidates(
        {"user_id": "owner"}, {"search_intent": "activity"}, {"owner"},
    )

    assert evidence == {} and docs == []
    lookup.assert_not_called()
    profiles.find.assert_not_called()


# --------------------------------------------------------------------------
# Deterministic selection policy
# --------------------------------------------------------------------------

def test_activity_selection_scope_prefers_evidence_then_requires_consent():
    evidence = {"cand-a": PREFERS_EVIDENCE}
    assert activity_selection_scope(["cand-a", "cand-b"], evidence,
                                    allow_exploratory=False) == "evidence"
    assert activity_selection_scope(["cand-b"], evidence,
                                    allow_exploratory=False) == "consent_required"
    assert activity_selection_scope(["cand-b"], evidence,
                                    allow_exploratory=True) == "exploratory"
    assert activity_selection_scope([], {}, allow_exploratory=False) == "consent_required"


# --------------------------------------------------------------------------
# Qualification contract
# --------------------------------------------------------------------------

@patch("routers.match.get_user_graph_memories", return_value=[])
def test_candidate_activity_evidence_is_direct_and_never_claims_willingness(_graph):
    qualification = candidate_qualification(
        {"user_id": "owner"}, {"user_id": "candidate"},
        search_context=TOPIC_CONTEXT, activity_evidence=PREFERS_EVIDENCE,
    )
    assert qualification["eligible"] is True
    assert "candidate_activity_evidence" in qualification["strong_reason_codes"]
    assert qualification["match_basis"]["level"] == "direct"
    assert "對方明確提過喜歡羽球" in qualification["match_basis"]["counterparty_evidence"]
    cannot_infer = " ".join(qualification["match_basis"]["cannot_infer"])
    assert "不代表擅長、願意或已同意" in cannot_infer


@patch("routers.match.get_user_graph_memories", return_value=[])
def test_recent_activity_evidence_qualifies_with_activity_text(_graph):
    qualification = candidate_qualification(
        {"user_id": "owner"}, {"user_id": "candidate"},
        search_context=TOPIC_CONTEXT, activity_evidence=RECENT_EVIDENCE,
    )
    assert qualification["eligible"] is True
    assert qualification["match_basis"]["level"] == "direct"
    assert "對方近期活動:打羽球" in qualification["match_basis"]["counterparty_evidence"]


@patch("routers.match.get_user_graph_memories")
def test_activity_evidence_never_bypasses_hard_conflict(graph_memories):
    graph_memories.side_effect = lambda user_id, _limit: (
        [{"key": "smoking", "stance": "avoid"}]
        if user_id == "owner" else [{"key": "smoking", "stance": "like"}]
    )
    qualification = candidate_qualification(
        {"user_id": "owner"}, {"user_id": "candidate"},
        search_context=TOPIC_CONTEXT, activity_evidence=PREFERS_EVIDENCE,
    )
    assert qualification["eligible"] is False
    assert qualification["hard_conflict_keys"] == ["smoking"]


@patch("routers.match.get_user_graph_memories", return_value=[])
def test_activity_evidence_without_topic_is_not_direct(_graph):
    qualification = candidate_qualification(
        {"user_id": "owner"}, {"user_id": "candidate"},
        search_context={"search_intent": "activity"},
        activity_evidence=PREFERS_EVIDENCE,
    )
    assert qualification["eligible"] is False


# --------------------------------------------------------------------------
# User-visible reasons
# --------------------------------------------------------------------------

@patch("routers.match.get_user_graph_memories", return_value=[])
def test_activity_explanation_is_grounded_on_candidate_evidence_only(_graph):
    score, items, top_reasons, reason = build_validated_match_explanation(
        {"user_id": "owner"}, {"user_id": "candidate"}, 0.0,
        search_context=TOPIC_CONTEXT, activity_evidence=PREFERS_EVIDENCE,
    )
    kinds = [item["kind"] for item in items]
    assert "candidate_activity_interest" in kinds
    assert "activity_willingness_unconfirmed" in kinds
    assert "exploratory_notice" not in kinds
    assert items[-1]["kind"] == "recommendation_tier" and items[-1]["text"] == "grounded"
    assert "仍待本人確認" in reason
    assert top_reasons == ["對方明確提過喜歡羽球"]
    interest = next(item for item in items if item["kind"] == "candidate_activity_interest")
    assert interest["candidate_evidence_ids"] == [f"graph:candidate:{'v2_' + 'a' * 48}"]


@patch("routers.match.get_user_graph_memories", return_value=[])
def test_activity_explanation_without_evidence_stays_exploratory(_graph):
    _, items, _, reason = build_validated_match_explanation(
        {"user_id": "owner"}, {"user_id": "candidate"}, 0.82,
        search_context=TOPIC_CONTEXT,
    )
    kinds = [item["kind"] for item in items]
    assert "exploratory_notice" in kinds
    assert items[-1]["text"] == "exploratory"
    assert "仍待本人確認" not in reason


# --------------------------------------------------------------------------
# Exploratory consent record and search-context flag
# --------------------------------------------------------------------------

def test_exploratory_consent_recorded_requires_same_topic_recent_terminal_job(monkeypatch):
    import time as _time
    now = _time.time()
    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", Collection([
        {"user_id": "owner", "status": "no_candidates",
         "error_code": "exploratory_consent_required",
         "search_context": {"invitation_topic": "羽球"}, "updated_at": now - 60},
        {"user_id": "owner", "status": "completed", "error_code": "",
         "search_context": {"invitation_topic": "羽球"}, "updated_at": now - 5},
    ]))
    assert jobs.exploratory_consent_recorded("owner", "羽球") is True
    assert jobs.exploratory_consent_recorded("owner", "滑雪") is False
    assert jobs.exploratory_consent_recorded("other", "羽球") is False

    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", Collection([
        {"user_id": "owner", "status": "no_candidates",
         "error_code": "exploratory_consent_required",
         "search_context": {"invitation_topic": "羽球"},
         "updated_at": now - jobs.EXPLORATORY_CONSENT_WINDOW_SECONDS - 10},
    ]))
    assert jobs.exploratory_consent_recorded("owner", "羽球") is False


def test_exploratory_flag_is_kept_only_for_bound_activity_context():
    kept = safe_search_context({
        "search_intent": "activity", "invitation_topic": "羽球",
        "query_text": "想找人一起打羽球", "allow_exploratory": True,
    })
    assert kept["allow_exploratory"] == "1"

    no_topic = safe_search_context({
        "search_intent": "activity", "allow_exploratory": "1",
    })
    assert "allow_exploratory" not in no_topic

    preference = safe_search_context({
        "search_intent": "preference", "normalized_topic": "羽球",
        "canonical_preference_key": "k", "allow_exploratory": "1",
    })
    assert "allow_exploratory" not in preference

    garbage = safe_search_context({
        "search_intent": "activity", "invitation_topic": "羽球",
        "allow_exploratory": "maybe",
    })
    assert "allow_exploratory" not in garbage


def test_prepare_confirmation_marks_exploratory_only_after_server_consent(monkeypatch):
    from services.ayue_agent.shared import write_executors
    from tests.test_pi_feedback_regressions import make_turn

    monkeypatch.setattr(write_executors, "assess_match_opportunity",
                        lambda *_a, **_k: SimpleNamespace(state="ready"))
    _ctx, turn = make_turn("今天不想碰專題，想找人一起打羽球換個心情")
    ctx = turn._raw_ctx
    args = {"search_request": {"kind": "activity", "topic": "羽球", "exploratory": True}}

    monkeypatch.setattr(jobs, "exploratory_consent_recorded", lambda *_a, **_k: True)
    payload, preview = write_executors.prepare_write_confirmation(
        "match.start_search", args, ctx, turn,
    )
    assert payload["data"]["search_context"]["allow_exploratory"] == "1"
    assert "探索性" in preview

    monkeypatch.setattr(jobs, "exploratory_consent_recorded", lambda *_a, **_k: False)
    payload, preview = write_executors.prepare_write_confirmation(
        "match.start_search", args, ctx, turn,
    )
    assert "allow_exploratory" not in payload["data"]["search_context"]
    assert "探索性" not in preview


def test_prepare_confirmation_never_marks_preference_exploratory(monkeypatch):
    from services.ayue_agent.shared import write_executors
    from tests.test_pi_feedback_regressions import make_turn

    monkeypatch.setattr(write_executors, "assess_match_opportunity",
                        lambda *_a, **_k: SimpleNamespace(state="ready"))
    _ctx, turn = make_turn("幫我找喜歡 Kpop 的人")
    ctx = turn._raw_ctx
    args = {"search_request": {"kind": "preference", "topic": "K-pop", "exploratory": True}}
    monkeypatch.setattr(jobs, "exploratory_consent_recorded", lambda *_a, **_k: True)

    payload, _preview = write_executors.prepare_write_confirmation(
        "match.start_search", args, ctx, turn,
    )
    assert "allow_exploratory" not in payload["data"]["search_context"]


# --------------------------------------------------------------------------
# Evidence correctness: typed signal only, active context, traceable provenance
# --------------------------------------------------------------------------

def test_activity_evidence_query_uses_only_typed_signal_and_active_context(monkeypatch):
    import json

    import routers.match as match_router

    profiles = MagicMock()
    profiles.find.return_value = _Rows([])
    monkeypatch.setattr(match_router, "profiles_coll", profiles)
    monkeypatch.setattr(match_router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
        "candidate_ids": [], "canonical_key": "", "normalized_topic": "",
        "retrieval_source": "graph_exact",
    })

    match_router._activity_evidence_candidates(
        {"user_id": "owner"}, TOPIC_CONTEXT, {"owner"},
    )

    query = profiles.find.call_args_list[0].args[0]
    assert "context_signals.activity" in query
    # Free text (negation/third-person risk) is never a retrieval source.
    assert "current_context" not in json.dumps(query, default=str)
    # Expired situations cannot qualify: the activity query is expiry-scoped.
    assert "recent_context_expires_at" in json.dumps(query, default=str)


@patch("routers.match.get_user_graph_memories", return_value=[])
def test_recent_activity_explanation_provenance_and_no_overclaim(_graph):
    _, items, _, reason = build_validated_match_explanation(
        {"user_id": "owner"}, {"user_id": "candidate"}, 0.0,
        search_context=TOPIC_CONTEXT, activity_evidence=RECENT_EVIDENCE,
    )
    recent = next(item for item in items if item["kind"] == "candidate_recent_activity")
    assert recent["candidate_evidence_ids"] == [
        "profile:candidate:context_signals:activity",
    ]
    assert "仍待本人確認" in reason
    assert "對方是否擅長、願意或已同意同行" in reason  # explicit disclaimer
    for overclaim in ("對方會打羽球", "擅長打羽球", "對方已同意", "對方已經同意", "可望出席"):
        assert overclaim not in reason


# --------------------------------------------------------------------------
# Isolated consent chain: ask -> explicit consent -> confirmation -> proposal
# --------------------------------------------------------------------------

def test_job_without_evidence_asks_for_consent_and_writes_nothing(monkeypatch):
    job = {
        "_id": "job-row", "job_id": "job-1", "user_id": "owner",
        "source": "agent_pi", "lease_id": "lease-1", "quota_billable": False,
        "search_context": {"search_intent": "activity", "invitation_topic": "羽球",
                           "query_text": "想找人一起打羽球"},
    }
    pipeline = MagicMock(return_value={
        "status": "no_suitable_candidate", "matches": [],
        "reason_code": "exploratory_consent_required", "diagnostics": {},
    })
    monkeypatch.setattr(jobs, "_claim_next_job", lambda _now: job)
    monkeypatch.setattr(jobs, "_pipeline", pipeline)
    monkeypatch.setattr(jobs, "_report_progress", lambda *_a, **_k: True)
    monkeypatch.setattr(jobs, "_job_has_ownership", lambda _job: True)
    monkeypatch.setattr(jobs, "profiles_coll", MagicMock())
    finish = MagicMock(return_value=True)
    monkeypatch.setattr(jobs, "_finish_job", finish)
    queue = MagicMock()
    monkeypatch.setattr(jobs, "queue_mediator_event", queue)

    assert jobs.run_one_match_search_job() is True
    assert finish.call_args.args[0] is job
    assert finish.call_args.args[1] == "no_candidates"
    assert finish.call_args.kwargs["error_code"] == "exploratory_consent_required"
    assert "探索性" in queue.call_args.args[1]
    assert queue.call_args.args[2] == "match_search_empty"


def test_confirmed_consent_queues_exactly_one_exploratory_search(monkeypatch):
    from services.ayue_agent.shared import write_executors
    from tests.test_pi_feedback_regressions import make_turn

    monkeypatch.setattr(write_executors, "assess_match_opportunity",
                        lambda *_a, **_k: SimpleNamespace(state="ready"))
    _ctx, turn = make_turn("今天不想碰專題，想找人一起打羽球換個心情")
    ctx = turn._raw_ctx
    monkeypatch.setattr(jobs, "exploratory_consent_recorded", lambda *_a, **_k: True)
    payload, preview = write_executors.prepare_write_confirmation(
        "match.start_search",
        {"search_request": {"kind": "activity", "topic": "羽球", "exploratory": True}},
        ctx, turn,
    )
    assert "探索性" in preview

    started = MagicMock(return_value={"status": "queued"})
    monkeypatch.setattr(write_executors, "start_match_search", started)
    monkeypatch.setattr(write_executors.TOOL_CALLS, "find_one_and_update", lambda *_a, **_k: None)
    monkeypatch.setattr(write_executors.TOOL_CALLS, "update_one", MagicMock())

    ok, _reply, _code = write_executors.execute_write(
        "match.start_search", {}, ctx, turn, "run1", 0,
        confirmation_id="conf1", payload=payload["data"],
    )
    assert ok
    started.assert_called_once()
    assert started.call_args.kwargs["search_context"]["allow_exploratory"] == "1"
    assert started.call_args.kwargs["idempotency_key"] == "confirmation:conf1"

    # A repeated confirmation of the same card must never queue a second search.
    monkeypatch.setattr(write_executors.TOOL_CALLS, "find_one_and_update",
                        lambda *_a, **_k: {"idempotency_key": "already-claimed"})
    ok, reply, _code = write_executors.execute_write(
        "match.start_search", {}, ctx, turn, "run1", 0,
        confirmation_id="conf1", payload=payload["data"],
    )
    assert ok and "已經處理過" in reply
    started.assert_called_once()


def test_consent_record_is_scoped_to_one_owner_and_topic(monkeypatch):
    import time as _time
    now = _time.time()
    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", Collection([
        {"user_id": "owner", "status": "no_candidates",
         "error_code": "exploratory_consent_required",
         "search_context": {"invitation_topic": "羽球"}, "updated_at": now - 30},
    ]))
    assert jobs.exploratory_consent_recorded("owner", "羽球") is True
    assert jobs.exploratory_consent_recorded("owner", "爬山") is False  # other activity
    assert jobs.exploratory_consent_recorded("someone-else", "羽球") is False  # other owner
    assert jobs.exploratory_consent_recorded("owner", "羽球", now=now + 90000) is False  # expired


# --------------------------------------------------------------------------
# User-visible invitation copy: evidence reaches the initiator, role-safe
# --------------------------------------------------------------------------

@patch("routers.match.get_user_graph_memories", return_value=[])
def test_initiator_copy_mentions_prefers_evidence_without_consent_claim(_graph):
    from routers.match import build_friend_intro_v4

    intro = build_friend_intro_v4(
        {"user_id": "owner", "current_context": "最近想找人打羽球"},
        {"user_id": "candidate"},
        0.0, refine=False, search_context=TOPIC_CONTEXT,
        activity_evidence=PREFERS_EVIDENCE,
    )
    initiator = intro["initiator_preview"]["viewer_text"]
    receiver = intro["receiver_invitation"]["viewer_text"]
    assert "對方明確提過喜歡羽球" in initiator
    assert "是否願意同行仍待確認" in initiator
    for overclaim in ("對方已同意", "對方已經同意", "對方會打羽球", "擅長"):
        assert overclaim not in initiator
    # Role isolation: the receiver's card never claims their own evidence.
    assert "對方明確提過喜歡羽球" not in receiver
    assert "有位朋友" in receiver and "你願意先認識對方嗎" in receiver


@patch("routers.match.get_user_graph_memories", return_value=[])
def test_initiator_copy_mentions_recent_activity_evidence(_graph):
    from routers.match import build_friend_intro_v4

    intro = build_friend_intro_v4(
        {"user_id": "owner", "current_context": "最近想找人打羽球"},
        {"user_id": "candidate"},
        0.0, refine=False, search_context=TOPIC_CONTEXT,
        activity_evidence=RECENT_EVIDENCE,
    )
    initiator = intro["initiator_preview"]["viewer_text"]
    assert "近期活動" in initiator and "打羽球" in initiator
    assert "是否願意同行仍待確認" in initiator
    assert "有位朋友" not in initiator


@patch("routers.match.get_user_graph_memories")
def test_shared_reason_still_surfaces_activity_evidence(graph_memories):
    from matchmaker_agent.concept_identity import canonicalize_concept

    identity = canonicalize_concept("戶外")
    row = {**identity.as_dict(), "display_label": identity.display_label,
           "stance": "like", "label": identity.display_label}
    graph_memories.return_value = [row]
    _, items, top_reasons, reason = build_validated_match_explanation(
        {"user_id": "owner"}, {"user_id": "candidate"}, 0.0,
        search_context=TOPIC_CONTEXT, activity_evidence=PREFERS_EVIDENCE,
    )
    kinds = [item["kind"] for item in items]
    assert "shared_graph" in kinds
    assert "candidate_activity_interest" in kinds
    assert "對方明確提過喜歡羽球" in reason
    assert "是否願意同行仍待確認" in reason
    assert any("對方明確提過喜歡羽球" == text for text in top_reasons)


def test_activity_phrase_and_alias_expansion_is_small_and_exact():
    from routers.match import _activity_preference_lookup_topics

    assert [label for label, _ in _activity_preference_lookup_topics("羽球")] == [
        "羽球", "打羽球", "羽球雙打",
    ]
    expected_titles = {"Street Fighter", "街頭霸王", "快打旋風", "玩街頭霸王", "玩快打旋風"}
    token = lambda value: "".join(value.casefold().split())
    expected_tokens = {token(label) for label in expected_titles}
    for topic in ("Street Fighter", "street fighter", "街頭霸王", "快打旋風"):
        variants = _activity_preference_lookup_topics(topic)
        assert len(variants) == 5
        assert variants[0][0] == topic
        assert {token(label) for label, _ in variants} == expected_tokens
    for unrelated in ("格鬥遊戲", "布蘭卡", "收藏羽球拍", "不想打羽球"):
        assert _activity_preference_lookup_topics(unrelated) == [(unrelated, "exact_canonical")]


def test_bounded_phrase_lookup_keeps_candidate_owned_v2_provenance_and_exclusions(monkeypatch):
    import routers.match as match_router
    from matchmaker_agent.concept_identity import canonicalize_concept

    play = canonicalize_concept("打羽球")
    doubles = canonicalize_concept("羽球雙打")
    profiles = MagicMock()
    profiles.find.side_effect = [
        _Rows([]),
        _Rows([
            {"user_id": "candidate-play", "context_signals": {}},
            {"user_id": "candidate-doubles", "context_signals": {}},
        ]),
    ]
    monkeypatch.setattr(match_router, "profiles_coll", profiles)
    lookup = MagicMock(return_value={
        "v2_only": True,
        "canonical_key": canonicalize_concept("羽球").key,
        "normalized_topic": "羽球",
        "candidate_ids": [],
        "additional_candidates": [
            {"canonical_key": play.key, "candidate_ids": ["candidate-play", "blocked"]},
            {"canonical_key": doubles.key, "candidate_ids": ["candidate-doubles"]},
        ],
    })
    monkeypatch.setattr(match_router, "retrieve_preference_candidate_ids", lookup)

    evidence, docs = match_router._activity_evidence_candidates(
        {"user_id": "owner"}, TOPIC_CONTEXT, {"owner", "blocked"},
    )

    lookup.assert_called_once_with(
        "owner", "羽球", excluded_user_ids={"owner", "blocked"},
        limit=match_router.MATCH_VECTOR_RETRIEVAL_LIMIT, v2_only=True,
        additional_topics=["打羽球", "羽球雙打"],
    )
    assert set(evidence) == {"candidate-play", "candidate-doubles"}
    assert {item["concept_key"] for item in evidence["candidate-play"]} == {play.key}
    assert evidence["candidate-play"][0]["label"] == "打羽球"
    assert evidence["candidate-play"][0]["activity_evidence_match"] == "bounded_phrase"
    assert {doc["user_id"] for doc in docs} == {"candidate-play", "candidate-doubles"}


@patch("routers.match.get_user_graph_memories", return_value=[])
def test_street_fighter_alias_phrase_reaches_initiator_reason_with_graph_provenance(_graph):
    from matchmaker_agent.concept_identity import canonicalize_concept
    from routers.match import build_friend_intro_v4

    identity = canonicalize_concept("玩街頭霸王")
    activity_evidence = [{
        "kind": "candidate_prefers", "concept_key": identity.key,
        "label": "玩街頭霸王", "activity_evidence_match": "deterministic_alias_phrase",
    }]
    context = {"search_intent": "activity", "invitation_topic": "Street Fighter"}
    _, items, _top_reasons, _reason = build_validated_match_explanation(
        {"user_id": "owner"}, {"user_id": "candidate"}, 0.0,
        search_context=context, activity_evidence=activity_evidence,
    )
    item = next(item for item in items if item["kind"] == "candidate_activity_interest")
    assert item["candidate_evidence_ids"] == [f"graph:candidate:{identity.key}"]
    assert item["text"] == "對方明確提過喜歡玩街頭霸王"

    intro = build_friend_intro_v4(
        {"user_id": "owner", "current_context": "想找人一起玩 Street Fighter"},
        {"user_id": "candidate"}, 0.0, refine=False,
        search_context=context, activity_evidence=activity_evidence,
    )
    initiator = intro["initiator_preview"]["viewer_text"]
    assert "對方明確提過喜歡玩街頭霸王" in initiator
    assert "是否願意同行仍待確認" in initiator
    for unsupported_claim in ("對方擅長", "對方願意同行", "對方已同意同行"):
        assert unsupported_claim not in initiator
    assert "對方明確提過喜歡玩街頭霸王" not in intro["receiver_invitation"]["viewer_text"]


def _run_activity_generation_fixture(
    monkeypatch, *, candidates, context, lookup_result, source="automatic",
    existing_matches=(), lookup_callable=None,
):
    from routers import match as match_router
    from tests.test_preference_match_search import _flow

    real_explanation = match_router.build_validated_match_explanation
    real_friend_intro = match_router.build_friend_intro_v4
    profiles, matches = _flow(monkeypatch, candidate=candidates[0])
    candidate_rows = [dict(candidate) for candidate in candidates]
    profiles.aggregate.return_value = candidate_rows
    profiles.find.side_effect = lambda *_args, **_kwargs: _Rows(candidate_rows)
    matches.find.return_value = list(existing_matches)
    monkeypatch.setattr(match_router, "get_embedding", lambda _text: [.2])
    monkeypatch.setattr(match_router, "get_user_graph_memories", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(match_router, "_trait_stances", lambda _user_id: {})
    monkeypatch.setattr(match_router, "build_validated_match_explanation", real_explanation)

    def build_friend_intro(*args, **kwargs):
        kwargs["refine"] = False
        return real_friend_intro(*args, **kwargs)

    monkeypatch.setattr(match_router, "build_friend_intro_v4", build_friend_intro)
    lookup = (MagicMock(side_effect=lookup_callable) if lookup_callable
              else MagicMock(return_value=lookup_result))
    monkeypatch.setattr(match_router, "retrieve_preference_candidate_ids", lookup)
    selection_payloads = []

    def select(payload, **_kwargs):
        selection_payloads.append(payload)
        return [{"matched_user_id": payload["candidates"][0]["user_id"]}]

    selection = MagicMock(side_effect=select)
    monkeypatch.setattr(match_router, "_request_matchmaker_selection", selection)
    reserve = MagicMock(return_value={"status": "reserved"})
    monkeypatch.setattr(match_router, "reserve_daily_quota", reserve)
    consent_lookup = MagicMock(return_value=False)
    monkeypatch.setattr(match_router, "exploratory_consent_recorded", consent_lookup)

    result = match_router.generate_matches_for_user(
        "owner", source=source, search_context=context,
        report_progress=lambda _step, **_kwargs: True,
        can_commit=lambda: True,
    )
    return {
        "router": match_router, "profiles": profiles, "matches": matches,
        "lookup": lookup, "selection": selection,
        "selection_payloads": selection_payloads, "reserve": reserve,
        "consent_lookup": consent_lookup, "result": result,
    }


def _wire_mock_preference_api(monkeypatch, candidate_ids_by_key):
    """Connect Social's real adapter to the Matchmaker endpoint with mock graph I/O."""
    import importlib.util
    import sys
    from pathlib import Path

    matchmaker_dir = Path(__file__).resolve().parents[2] / "matchmaker_agent"
    monkeypatch.syspath_prepend(str(matchmaker_dir))
    import matchmaker

    model_agent = MagicMock()
    agent_factory = MagicMock(return_value=model_agent)
    monkeypatch.setattr(matchmaker, "MatchmakerAgent", agent_factory)
    api_spec = importlib.util.spec_from_file_location(
        "_activity_evidence_matchmaker_api_fixture", matchmaker_dir / "agent_api.py",
    )
    assert api_spec and api_spec.loader
    matchmaker_api = importlib.util.module_from_spec(api_spec)
    monkeypatch.setitem(sys.modules, api_spec.name, matchmaker_api)
    api_spec.loader.exec_module(matchmaker_api)
    agent_factory.assert_called_once_with()
    from services import preference_candidate_service

    session = MagicMock()

    def run_graph_query(query, **parameters):
        cypher = getattr(query, "text", query)
        assert 0 < query.timeout <= matchmaker_api.PREFERENCE_CANDIDATE_GRAPH_TIMEOUT_SECONDS
        if "RETURN concept.key AS key" in cypher:
            return SimpleNamespace(single=lambda: None)
        if "UNWIND $proofs AS proof" in cypher:
            rows = []
            for proof in parameters["proofs"]:
                for candidate_id in candidate_ids_by_key.get(proof["key"], ()):
                    rows.append({"canonical_key": proof["key"], "candidate_id": candidate_id})
            return rows
        raise AssertionError(f"unexpected graph query: {cypher[:100]}")

    session.run.side_effect = run_graph_query
    driver = MagicMock()
    driver.__enter__.return_value = driver
    driver.session.return_value.__enter__.return_value = session
    driver_factory = MagicMock(return_value=driver)
    monkeypatch.setattr(matchmaker_api.GraphDatabase, "driver", driver_factory)

    def local_http_post(url, *, json, timeout):
        assert url.endswith("/api/preferences/candidates")
        assert timeout == (1, 5)
        request = matchmaker_api.PreferenceCandidateRequest.model_validate(json)
        payload = matchmaker_api.preference_candidates(request)
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = payload
        return response

    monkeypatch.setattr(preference_candidate_service.requests, "post", local_http_post)
    return preference_candidate_service.retrieve_preference_candidate_ids, session


def test_street_fighter_alias_runs_through_qualification_selection_and_primary_copy(monkeypatch):
    from matchmaker_agent.concept_identity import canonicalize_concept

    candidate = {
        "user_id": "candidate-sf", "current_context": "",
        "context_signals": {}, "recent_context_expires_at": __import__("time").time() + 3600,
        "score": .94,
    }
    title = canonicalize_concept("Street Fighter")
    saved_phrase = canonicalize_concept("玩街頭霸王")
    lookup_callable, graph_session = _wire_mock_preference_api(
        monkeypatch, {saved_phrase.key: ["candidate-sf"]},
    )
    flow = _run_activity_generation_fixture(
        monkeypatch, candidates=[candidate],
        context={
            "search_intent": "activity", "invitation_topic": "Street Fighter",
            "query_text": "想找人一起玩 Street Fighter",
        },
        lookup_callable=lookup_callable,
        lookup_result={
            "status": "success", "v2_only": True, "canonical_key": title.key,
            "normalized_topic": "Street Fighter", "candidate_ids": [],
            "additional_candidates": [],
        },
    )

    assert flow["result"]["status"] == "success"
    assert flow["lookup"].call_args.kwargs["v2_only"] is True
    assert flow["lookup"].call_args.kwargs["additional_topics"] == [
        "街頭霸王", "快打旋風", "玩街頭霸王", "玩快打旋風",
    ]
    assert len(flow["selection_payloads"]) == 1
    assert [row["user_id"] for row in flow["selection_payloads"][0]["candidates"]] == [
        "candidate-sf",
    ]
    match_doc = flow["matches"].insert_one.call_args.args[0]
    initiator = match_doc["friend_intro_v4"]["initiator_preview"]["viewer_text"]
    assert "對方明確提過喜歡玩街頭霸王" in initiator
    assert "是否願意同行仍待確認" in initiator
    for unsupported_claim in ("對方擅長", "對方願意同行", "對方已同意同行"):
        assert unsupported_claim not in initiator
    evidence_item = next(
        item for item in match_doc["reason_items"]
        if item["kind"] == "candidate_activity_interest"
    )
    assert evidence_item["candidate_evidence_ids"] == [
        f"graph:candidate-sf:{saved_phrase.key}",
    ]
    assert "對方明確提過喜歡玩街頭霸王" in match_doc["match_basis"]["counterparty_evidence"]
    assert len(graph_session.run.call_args_list) == 2


def test_badminton_phrase_evidence_for_both_saved_forms_reaches_selection(monkeypatch):
    from matchmaker_agent.concept_identity import canonicalize_concept

    candidates = [
        {"user_id": "candidate-play", "current_context": "", "context_signals": {},
         "recent_context_expires_at": __import__("time").time() + 3600, "score": .92},
        {"user_id": "candidate-doubles", "current_context": "", "context_signals": {},
         "recent_context_expires_at": __import__("time").time() + 3600, "score": .91},
    ]
    topic = canonicalize_concept("羽球")
    play = canonicalize_concept("打羽球")
    doubles = canonicalize_concept("羽球雙打")
    lookup_callable, graph_session = _wire_mock_preference_api(
        monkeypatch, {
            play.key: ["candidate-play"],
            doubles.key: ["candidate-doubles"],
        },
    )
    flow = _run_activity_generation_fixture(
        monkeypatch, candidates=candidates,
        context={
            "search_intent": "activity", "invitation_topic": "羽球",
            "query_text": "想找人一起打羽球",
        },
        lookup_callable=lookup_callable,
        lookup_result={
            "status": "success", "v2_only": True, "canonical_key": topic.key,
            "normalized_topic": "羽球", "candidate_ids": [],
            "additional_candidates": [],
        },
    )

    assert flow["result"]["status"] == "success"
    assert flow["lookup"].call_args.kwargs["additional_topics"] == ["打羽球", "羽球雙打"]
    assert {row["user_id"] for row in flow["selection_payloads"][0]["candidates"]} == {
        "candidate-play", "candidate-doubles",
    }
    match_doc = flow["matches"].insert_one.call_args.args[0]
    assert "對方明確提過喜歡" in match_doc["reason"]
    assert any(
        item.get("kind") == "candidate_activity_interest"
        and item.get("candidate_evidence_ids") == [f"graph:candidate-play:{play.key}"]
        for item in match_doc["reason_items"]
    ) or any(
        item.get("kind") == "candidate_activity_interest"
        and item.get("candidate_evidence_ids") == [f"graph:candidate-doubles:{doubles.key}"]
        for item in match_doc["reason_items"]
    )
    assert len(graph_session.run.call_args_list) == 2


def test_unexpected_phrase_cannot_become_evidence_and_requires_consent_before_quota(monkeypatch):
    from matchmaker_agent.concept_identity import canonicalize_concept
    import time

    candidate = {
        "user_id": "collector", "current_context": "我只收藏羽球拍",
        "context_signals": {}, "recent_context_expires_at": time.time() + 3600,
        "score": .94,
    }
    topic = canonicalize_concept("羽球")
    collector = canonicalize_concept("收藏羽球拍")
    lookup_callable, graph_session = _wire_mock_preference_api(monkeypatch, {})
    flow = _run_activity_generation_fixture(
        monkeypatch, candidates=[candidate], source="agent_pi",
        context={
            "search_intent": "activity", "invitation_topic": "羽球",
            "query_text": "想找人一起打羽球", "allow_exploratory": "1",
        },
        lookup_callable=lookup_callable,
        lookup_result={
            "status": "success", "v2_only": True, "canonical_key": topic.key,
            "normalized_topic": "羽球", "candidate_ids": [],
            "additional_candidates": [],
        },
    )

    assert flow["result"]["status"] == "no_suitable_candidate"
    assert flow["result"]["reason_code"] == "exploratory_consent_required"
    assert flow["selection"].call_count == 0
    flow["matches"].insert_one.assert_not_called()
    flow["reserve"].assert_not_called()
    flow["consent_lookup"].assert_called_once_with("owner", "羽球")
    queried_keys = {proof["key"] for proof in graph_session.run.call_args_list[1].kwargs["proofs"]}
    assert collector.key not in queried_keys
    assert {proof["semantic_text"] for proof in graph_session.run.call_args_list[1].kwargs["proofs"]} == {
        "打羽球", "羽球雙打",
    }


def test_pair_excluded_candidate_cannot_reenter_through_alias(monkeypatch):
    from matchmaker_agent.concept_identity import canonicalize_concept
    import time

    candidate = {
        "user_id": "candidate-sf", "current_context": "",
        "context_signals": {}, "recent_context_expires_at": time.time() + 3600,
        "score": .94,
    }
    title = canonicalize_concept("Street Fighter")
    saved_phrase = canonicalize_concept("玩街頭霸王")
    lookup_callable, _graph_session = _wire_mock_preference_api(
        monkeypatch, {saved_phrase.key: ["candidate-sf"]},
    )
    flow = _run_activity_generation_fixture(
        monkeypatch, candidates=[candidate],
        existing_matches=[{
            "from_user": "owner", "to_user": "candidate-sf",
            "status": "declined", "created_at": time.time() - 60,
            "context_revision": 1,
        }],
        context={
            "search_intent": "activity", "invitation_topic": "Street Fighter",
            "query_text": "想找人一起玩 Street Fighter",
        },
        lookup_callable=lookup_callable,
        lookup_result={
            "status": "success", "v2_only": True, "canonical_key": title.key,
            "normalized_topic": "Street Fighter", "candidate_ids": [],
            "additional_candidates": [{
                "canonical_key": saved_phrase.key, "candidate_ids": ["candidate-sf"],
            }],
        },
    )

    assert flow["selection"].call_count == 0
    flow["matches"].insert_one.assert_not_called()
    assert flow["result"]["status"] != "success"
