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
