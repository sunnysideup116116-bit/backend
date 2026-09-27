import json
import time
from unittest.mock import Mock
import mongomock
import pytest
from services import semantic_proposal_eligibility as final
from services import related_interest_pilot_monitor as monitor
from services.related_interest_telemetry import summarize
from matchmaker_agent.related_interest_contract import POLICY


@pytest.fixture
def enabled(monkeypatch, tmp_path):
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ROLLOUT_MODE', 'enabled_accounts')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED', 'on')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE', 'active')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_KILL_SWITCH_FILE', str(tmp_path/'kill'))
    monkeypatch.delenv('MATCH_RELATED_INTEREST_CANARY_USER_IDS', raising=False)
    return tmp_path/'kill'


@pytest.mark.parametrize('case', ['ready', 'block', 'pending_history', 'accepted_history', 'declined_history',
    'duplicate_requester', 'missing_candidate', 'qualification_changed', 'proof_changed', 'deadline', 'kill'])
def test_proposal_final_recheck_is_fresh_and_fail_closed(enabled, monkeypatch, case):
    db = mongomock.MongoClient().db
    db.profiles.insert_many([{'user_id': 'owner'}, {'user_id': 'person'}])
    block = Mock(return_value=case == 'block')
    monkeypatch.setattr(final.risk_block_service, 'is_pair_blocked', block)
    proof = Mock(return_value=case != 'proof_changed')
    monkeypatch.setattr(final, 'recheck_semantic_proposal', proof)
    if case == 'duplicate_requester': db.profiles.insert_one({'user_id': 'owner'})
    if case == 'missing_candidate': db.profiles.delete_one({'user_id': 'person'})
    if case.endswith('_history'):
        status = case.split('_')[0]
        db.matches.insert_one({'from_user': 'owner', 'to_user': 'person', 'status': status,
            'created_at': time.time(), 'last_decision': {'from': 'pending', 'to': 'accepted', 'action': 'accept'}})
    if case == 'kill': enabled.touch()
    result = final.proposal_eligible('owner', 'person', [{'synthetic': True}], {'canonical_preference_key': 'query'},
        profiles=db.profiles, matches=db.matches, profile_filter=lambda *_a, **_k: {'user_id': 'person'},
        qualify=lambda *_a, **_k: {'eligible': case != 'qualification_changed', 'semantic_related_preference_matched': True},
        target_stances={}, candidate_stances={}, deadline=time.monotonic() + (-1 if case == 'deadline' else 20))
    assert result is (case == 'ready')
    if case not in {'ready', 'proof_changed'}: proof.assert_not_called()
    assert db.matches.count_documents({}) == int(case.endswith('_history'))


def test_typed_unavailable_protection_uses_rollout_population_without_allowlist(enabled, monkeypatch):
    import database
    now = time.time(); db = mongomock.MongoClient().db
    for i in range(3):
        db.match_search_jobs.insert_one({'user_id': f'owner-{10000+i}', 'completed_at': now-i,
            'status': 'failed', 'error_code': 'semantic_validator_unavailable',
            'related_interest_pilot': {'policy_version': POLICY, 'telemetry_version': 3,
                'triggered': True, 'rollout_mode': 'enabled_accounts'}})
    monkeypatch.setattr(database, 'db', db)
    assert monitor.on_job_finished('owner-10000', 'failed', 'semantic_validator_unavailable')
    assert enabled.exists()


def test_creation_latency_denominators_and_aggregate_privacy():
    meta = {'policy_version': POLICY, 'telemetry_version': 3, 'triggered': True}
    rows = [{'user_id': 'private-owner', 'created_at': 10, 'completed_at': 30, 'related_interest_pilot': meta},
        {'created_at': 10, 'completed_at': 9, 'related_interest_pilot': meta},
        {'started_at': 20, 'completed_at': 30, 'related_interest_pilot': meta}]
    result = summarize(rows, [])
    assert result['latency_seconds'] == {'samples': 2, 'p50': 15, 'p95': 20}
    assert result['latency_source_counts'] == {'job_creation_to_completion': 1, 'legacy_processing_only': 1, 'missing_or_invalid': 1}
    assert 'private-owner' not in json.dumps(result)


def test_window_excludes_new_jobs_but_observes_lifecycle_now():
    from scripts.report_related_interest_pilot import collect_report
    db = mongomock.MongoClient().db
    meta = {'policy_version': POLICY, 'telemetry_version': 3, 'triggered': True, 'rollout_mode': 'enabled_accounts',
        'requester_eligibility_verified': True}
    for created in (9, 10, 19, 20):
        db.match_search_jobs.insert_one({'created_at': created, 'completed_at': created+10, 'user_id': 'private',
            'related_interest_pilot': meta})
    report = collect_report(db, None, since=10, until=20, limit=10)
    assert report['preference_search_jobs'] == 2 and report['observed_enabled_rollout_requesters'] == 1
    assert report['lifecycle_observed_at'] >= time.time()-1 and not report['truncated']
    assert 'private' not in json.dumps(report)


def test_enabled_accounts_exact_hit_never_checks_rollout_readiness_or_ann(enabled, monkeypatch):
    from routers import match as router
    from .test_preference_match_search import _flow, _preference_context, K_POP
    _, matches = _flow(monkeypatch, candidate={'user_id': 'legacy-exact-owner'})
    monkeypatch.setattr(router, 'retrieve_preference_candidate_ids', lambda *_a, **_k: {
        'canonical_key': K_POP, 'candidate_ids': ['legacy-exact-owner']})
    monkeypatch.setattr(router, '_trait_stances', lambda uid: {K_POP: {'like'}} if uid != 'owner' else {})
    ann = Mock(side_effect=AssertionError('exact must not call ANN'))
    check = Mock(side_effect=AssertionError('exact must not require semantic readiness'))
    monkeypatch.setattr(router, 'retrieve_semantic_preference_candidates', ann)
    monkeypatch.setattr(router, 'semantic_proposal_eligible', check)
    result = router.generate_matches_for_user('owner', source='automatic', search_context=_preference_context(),
        report_progress=lambda _: True, can_commit=lambda: True)
    assert result['status'] == 'success' and result['diagnostics']['qualified_exact_count'] == 1
    ann.assert_not_called(); check.assert_not_called()
    assert 'related_interest_pilot' not in matches.insert_one.call_args.args[0]


@pytest.mark.parametrize('eligible', [True, False])
def test_rollout_router_requires_final_proof_before_inserting_and_releases_quota(enabled, monkeypatch, eligible):
    from routers import match as router
    from .test_preference_match_search import _flow, _preference_context, _activate_semantic, _semantic_result, K_POP, KOREAN_POP
    _, matches = _flow(monkeypatch, candidate={'user_id': 'semantic'})
    _activate_semantic(monkeypatch, _semantic_result())
    monkeypatch.setattr(router, 'retrieve_preference_candidate_ids', lambda *_a, **_k: {'canonical_key': K_POP, 'candidate_ids': []})
    monkeypatch.setattr(router, '_trait_stances', lambda uid: {KOREAN_POP: {'like'}} if uid != 'owner' else {})
    final_proof = Mock(return_value=eligible)
    monkeypatch.setattr(router, 'semantic_proposal_eligible', final_proof)
    reserve, release = Mock(return_value={'status': 'reserved'}), Mock()
    monkeypatch.setattr(router, 'reserve_daily_quota', reserve); monkeypatch.setattr(router, 'release_daily_quota', release)
    def run():
        return router.generate_matches_for_user('owner', source='manual', search_context=_preference_context(),
            report_progress=lambda _: True, can_commit=lambda: True, search_job_id='synthetic-rollout')
    if eligible:
        assert run()['status'] == 'success'
        proof = matches.insert_one.call_args.args[0]['related_interest_pilot']
        assert proof['final_proof_verified'] is True and proof['eligibility_policy'] == 'enabled-account-v1'
    else:
        with pytest.raises(router.MatchSearchPipelineError, match='semantic_final_readiness_failed'): run()
        release.assert_called_once(); matches.insert_one.assert_not_called()
    final_proof.assert_called_once(); reserve.assert_called_once()
