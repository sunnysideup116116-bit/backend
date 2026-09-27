"""Partial validation does not bypass Social qualification, proof or monitoring."""
from unittest.mock import Mock

import mongomock
import pytest

from routers import match as router
from services import preference_semantic_service as semantic
from services import related_interest_pilot_monitor as monitor
from services.related_interest_telemetry import summarize
from matchmaker_agent.related_interest_contract import POLICY
from .test_preference_match_search import _flow, _preference_context, K_POP, KOREAN_POP
from .test_related_interest_pilot import evidence


@pytest.fixture(autouse=True)
def runtime(monkeypatch, tmp_path):
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ROLLOUT_MODE', 'enabled_accounts')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED', 'on')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE', 'active')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED', 'on')
    path = tmp_path/'kill'
    monkeypatch.setenv('MATCH_RELATED_INTEREST_KILL_SWITCH_FILE', str(path))
    return path


def payload(*, unavailable=False, candidates=()):
    return {'status': 'error' if unavailable else 'success', 'canonical_key': K_POP,
        **({'error_code': 'semantic_validator_unavailable'} if unavailable else {}),
        'candidates': list(candidates), 'semantic_concepts_considered': [],
        'validator_counts': {'accepted': 0, 'rejected': 6, 'unrelated': 6, 'error': 2,
            'attempts': 5, 'retries': 1, 'attempt_errors': 2},
        'requester_eligibility_verified': True}


def bind_retrieval(monkeypatch, response):
    post = Mock(return_value=response)
    monkeypatch.setattr(semantic, '_post_semantic', post)
    monkeypatch.setattr(router, 'retrieve_semantic_preference_candidates', semantic.retrieve_semantic_preference_candidates)
    monkeypatch.setattr(router, 'retrieve_preference_candidate_ids', lambda *_a, **_kw: {'canonical_key': K_POP, 'candidate_ids': []})
    monkeypatch.setattr(router, '_trait_stances', lambda uid: {KOREAN_POP: {'like'}} if uid != 'owner' else {})
    return post


def run_search():
    return router.generate_matches_for_user('owner', source='automatic', search_context=_preference_context(),
        report_progress=lambda _: True, can_commit=lambda: True)


def test_six_reject_two_error_reaches_normal_no_match_without_proposal(monkeypatch):
    _, matches = _flow(monkeypatch, candidate={'user_id': 'candidate'})
    post = bind_retrieval(monkeypatch, payload())
    select = Mock(side_effect=AssertionError('no accepted evidence may reach selection'))
    proof = Mock(side_effect=AssertionError('no proposal may be rechecked'))
    monkeypatch.setattr(router, '_request_matchmaker_selection', select)
    monkeypatch.setattr(router, 'semantic_proposal_eligible', proof)
    result = run_search()
    assert result['status'] == 'no_suitable_candidate'
    assert result['reason_code'] == 'insufficient_semantic_ground' and result['matches'] == []
    assert result['diagnostics']['semantic_fallback_triggered'] is True
    assert result['diagnostics']['related_interest_validator']['error'] == 2
    post.assert_called_once(); select.assert_not_called(); proof.assert_not_called(); matches.insert_one.assert_not_called()


def test_job_wide_unavailability_still_propagates_as_typed_failure(monkeypatch):
    _, matches = _flow(monkeypatch, candidate={'user_id': 'candidate'})
    response = payload(unavailable=True)
    response['validator_counts']['job_unavailable'] = 1
    bind_retrieval(monkeypatch, response)
    with pytest.raises(router.MatchSearchPipelineError, match='semantic_validator_unavailable'):
        run_search()
    matches.insert_one.assert_not_called()


@pytest.mark.parametrize('final_ready', [True, False])
def test_error_evidence_never_enters_proposal_and_final_recheck_still_required(monkeypatch, final_ready):
    _, matches = _flow(monkeypatch, candidate={'user_id': 'candidate'})
    accepted = evidence(q='K-pop', c='Korean Pop', relation='equivalent')
    error = {**evidence(q='K-pop', c='Singing pop music', relation='sibling_related'), 'validator_status': 'ERROR'}
    response = payload(candidates=[{'candidate_id': 'candidate', 'evidence': [accepted, error]},
        {'candidate_id': 'error-only-owner', 'evidence': [error]}])
    response['validator_counts'] = {'accepted': 1, 'error': 2}
    bind_retrieval(monkeypatch, response)
    proof = Mock(return_value=final_ready)
    monkeypatch.setattr(router, 'semantic_proposal_eligible', proof)
    if final_ready:
        assert run_search()['status'] == 'success'
        inserted = matches.insert_one.call_args.args[0]
        assert inserted['to_user'] == 'candidate' and inserted['preference_retrieval_evidence'] == [accepted]
        assert inserted['related_interest_pilot']['final_proof_verified'] is True
    else:
        with pytest.raises(router.MatchSearchPipelineError, match='semantic_final_readiness_failed'):run_search()
        matches.insert_one.assert_not_called()
    proof.assert_called_once()
    assert proof.call_args.args[2] == [accepted]


def test_exact_hit_never_calls_partial_validator_path(monkeypatch):
    _, matches = _flow(monkeypatch, candidate={'user_id': 'candidate'})
    post = bind_retrieval(monkeypatch, payload(unavailable=True))
    monkeypatch.setattr(router, 'retrieve_preference_candidate_ids', lambda *_a, **_kw: {'canonical_key': K_POP, 'candidate_ids': ['candidate']})
    monkeypatch.setattr(router, '_trait_stances', lambda uid: {K_POP: {'like'}} if uid != 'owner' else {})
    result = run_search()
    assert result['status'] == 'success' and result['diagnostics']['qualified_exact_count'] == 1
    assert result['diagnostics']['semantic_fallback_triggered'] is False
    post.assert_not_called()
    assert 'related_interest_pilot' not in matches.insert_one.call_args.args[0]


def test_partial_reject_is_not_unavailable_but_three_typed_failures_still_kill(monkeypatch, runtime):
    import database
    db = mongomock.MongoClient().db
    monkeypatch.setattr(database, 'db', db)
    monkeypatch.setattr(monitor.time, 'time', lambda: 1000)
    meta = {'policy_version': POLICY, 'telemetry_version': 3, 'triggered': True, 'rollout_mode': 'enabled_accounts'}
    partial = {'user_id': 'owner', 'completed_at': 999, 'status': 'insufficient_common_ground',
        'error_code': 'insufficient_semantic_ground', 'related_interest_pilot': {**meta, 'validator_counts': payload()['validator_counts']}}
    assert not monitor.on_job_finished('owner', partial['status'], partial['error_code'])
    assert not runtime.exists()
    db.match_search_jobs.insert_one(partial)
    for i in range(3):
        db.match_search_jobs.insert_one({'user_id': 'owner', 'completed_at': 1000+i/10, 'status': 'failed',
            'error_code': 'semantic_validator_unavailable', 'related_interest_pilot': meta})
    monkeypatch.setattr(monitor.time, 'time', lambda: 1001)
    assert monitor.on_job_finished('owner', 'failed', 'semantic_validator_unavailable')
    assert runtime.exists()
    summary = summarize([partial], [])
    assert summary['validator']['error'] == 2 and summary['validator_unavailable_jobs'] == 0
