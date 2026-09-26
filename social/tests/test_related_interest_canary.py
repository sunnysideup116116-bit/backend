from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from routers import match as router
from services import preference_semantic_service as service
from .test_preference_match_search import (
    _flow, _preference_context, _activate_semantic, _semantic_result, K_POP, KOREAN_POP,
)


@pytest.fixture(params=[2, 5, 10])
def flow(monkeypatch, tmp_path, request):
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(tmp_path / "kill"))
    profiles, matches = _flow(monkeypatch, candidate={"user_id": "semantic"})
    lookup = _activate_semantic(monkeypatch, _semantic_result())
    cohort = ["owner", "semantic", *[f"synthetic-{i}" for i in range(request.param-2)]]
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", json.dumps(cohort))
    monkeypatch.setattr(router, "retrieve_preference_candidate_ids", Mock(return_value={
        "canonical_key": K_POP, "candidate_ids": []}))
    monkeypatch.setattr(router, "_trait_stances", lambda uid: {KOREAN_POP: {"like"}} if uid != "owner" else {})
    return profiles, matches, lookup


def run(**kwargs):
    return router.generate_matches_for_user("owner", source="automatic", search_context=_preference_context(),
        report_progress=kwargs.pop("report_progress", lambda _: True), can_commit=lambda: True, **kwargs)


def test_canary_pair_gets_related_proposal_without_shared_preference(flow):
    _profiles, matches, lookup = flow
    result = run()
    assert result["status"] == "success" and lookup.call_count == 1
    proposal = matches.insert_one.call_args.args[0]
    assert proposal["from_user"] == "owner" and proposal["to_user"] == "semantic"
    assert proposal["preference_retrieval_evidence"] == _semantic_result()["evidence_by_candidate"]["semantic"]
    assert "你們都喜歡" not in proposal["reason"]
    assert "你這次想找" in proposal["reason"] and "Korean Pop" in proposal["reason"]


@pytest.mark.parametrize("cohort", ['', 'not-json', '["other","semantic"]'])
def test_noncanary_or_bad_config_exact_only_before_semantic(flow, monkeypatch, cohort):
    _profiles, matches, lookup = flow
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", cohort)
    result = run()
    assert result["diagnostics"]["semantic_mode"] == "off"
    assert not result["diagnostics"]["semantic_fallback_triggered"]
    lookup.assert_not_called(); matches.insert_one.assert_not_called()


def test_outside_semantic_owner_dropped_before_hydration_or_qualification(flow, monkeypatch):
    profiles, matches, lookup = flow
    lookup.return_value = _semantic_result("outsider")
    qualified = Mock(side_effect=AssertionError("outside owner cannot qualify"))
    monkeypatch.setattr(router, "candidate_qualification", qualified)
    assert run()["status"] == "no_suitable_candidate"
    assert profiles.find.call_args.args[0]["user_id"]["$in"] == []
    qualified.assert_not_called(); matches.insert_one.assert_not_called()


def test_cohort_rechecked_before_qualification(flow, monkeypatch):
    _profiles, matches, _lookup = flow
    qualified = Mock(side_effect=AssertionError("removed owner cannot qualify"))
    monkeypatch.setattr(router, "candidate_qualification", qualified)
    def progress(stage):
        if stage == "candidate_qualification":
            monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", '["owner","other"]')
        return True
    assert run(report_progress=progress)["status"] == "no_suitable_candidate"
    qualified.assert_not_called(); matches.insert_one.assert_not_called()


@pytest.mark.parametrize("stop", ["cohort", "kill"])
def test_final_proposal_guard_after_reason_releases_reserved_quota(flow, monkeypatch, tmp_path, stop):
    from services import related_interest_reason_service as reasons
    _profiles, matches, _lookup = flow
    original = reasons.related_friend_intro
    kill = tmp_path / "late-kill"
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(kill))
    def reason(*args, **kwargs):
        value = original(*args, **kwargs)
        if stop == "cohort":
            monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", '["owner","other"]')
        else:
            kill.touch()
        return value
    monkeypatch.setattr(reasons, "related_friend_intro", reason)
    reserve, release = Mock(return_value={"status": "reserved"}), Mock()
    monkeypatch.setattr(router, "reserve_daily_quota", reserve)
    monkeypatch.setattr(router, "release_daily_quota", release)
    with pytest.raises(router.MatchSearchPipelineError, match="semantic_policy_disabled"):
        router.generate_matches_for_user("owner", source="manual", search_context=_preference_context(),
            report_progress=lambda _: True, can_commit=lambda: True, search_job_id="synthetic-job")
    reserve.assert_called_once(); release.assert_called_once()
    matches.insert_one.assert_not_called()


def test_kill_before_search_keeps_exact_only(flow, tmp_path, monkeypatch):
    _profiles, matches, lookup = flow
    path = tmp_path / "stop"; path.touch()
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(path))
    assert run()["diagnostics"]["semantic_mode"] == "off"
    lookup.assert_not_called(); matches.insert_one.assert_not_called()


@pytest.mark.parametrize("canary_requester", [True, False])
def test_exact_outside_candidate_and_off_mode_payload_proposal_parity(monkeypatch, tmp_path, canary_requester):
    snapshots = []
    for mode in ("off", "active"):
        monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
        monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", mode)
        monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS",
            '["owner","semantic"]' if canary_requester else '["other","semantic"]')
        monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(tmp_path / "kill"))
        _profiles, matches = _flow(monkeypatch, candidate={"user_id": "outside-exact"})
        monkeypatch.setattr(router, "retrieve_preference_candidate_ids", lambda *_a, **_k: {
            "canonical_key": K_POP, "candidate_ids": ["outside-exact"]})
        monkeypatch.setattr(router, "_trait_stances", lambda uid: {K_POP: {"like"}} if uid != "owner" else {})
        semantic = Mock(side_effect=AssertionError("exact hit must not use ANN"))
        monkeypatch.setattr(router, "retrieve_semantic_preference_candidates", semantic)
        payloads = []
        def select(payload, **kwargs):
            payloads.append(deepcopy(payload)); return [{"matched_user_id": "outside-exact"}]
        monkeypatch.setattr(router, "_request_matchmaker_selection", select)
        assert run()["status"] == "success"
        semantic.assert_not_called()
        saved = deepcopy(matches.insert_one.call_args.args[0]); saved.pop("created_at"); saved.pop("state_history")
        snapshots.append((payloads, saved))
    assert snapshots[0] == snapshots[1]


def test_outsider_cannot_bypass_service_guard_with_related_flag(monkeypatch):
    monkeypatch.setenv("MATCH_PREFERENCE_SEMANTIC_MODE", "active")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_ENABLED", "on")
    monkeypatch.setenv("MATCH_RELATED_INTEREST_CANARY_USER_IDS", '["owner","semantic"]')
    http, embed = Mock(), Mock()
    monkeypatch.setattr(service.requests, "post", http); monkeypatch.setattr(service, "get_embeddings", embed)
    with pytest.raises(service.PreferenceSemanticRetrievalError, match="semantic_policy_disabled"):
        service.retrieve_semantic_preference_candidates("outsider", "K-pop", excluded_user_ids=set(), related_policy_required=True)
    http.assert_not_called(); embed.assert_not_called()
