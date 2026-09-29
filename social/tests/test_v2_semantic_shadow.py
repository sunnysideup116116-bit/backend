"""Shadow may observe; only the unchanged exact pool may create visible results."""
import json
import time
from unittest.mock import Mock
import mongomock
import pytest
from routers import match as router
from services import preference_semantic_service as semantic
from services import related_interest_telemetry as telemetry
from services import related_interest_pilot_monitor as monitor
from matchmaker_agent.related_interest_contract import POLICY
from matchmaker_agent.semantic_rollout_policy import pair_route_allowed
from .test_preference_match_search import _flow, _preference_context, K_POP, KOREAN_POP
from .test_related_interest_pilot import evidence


class Cursor(list):
    def limit(self, size):
        return Cursor(self[:size])


@pytest.fixture
def shadow(monkeypatch, tmp_path):
    import database
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE', 'shadow')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED', 'off')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ROLLOUT_MODE', 'enabled_accounts')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED', 'on')
    kill = tmp_path/'kill'
    monkeypatch.setenv('MATCH_RELATED_INTEREST_KILL_SWITCH_FILE', str(kill))
    db = mongomock.MongoClient().db
    db.match_search_jobs.insert_one({'job_id': 'job', 'user_id': 'owner', 'created_at': time.time(), 'status': 'running'})
    monkeypatch.setattr(database, 'db', db)
    profiles, matches = _flow(monkeypatch, candidate={'user_id': 'candidate'})
    profiles.find.return_value = Cursor([{'user_id': 'candidate'}])
    monkeypatch.setattr(router, '_trait_stances', lambda _: {})
    monkeypatch.setattr(router, 'retrieve_preference_candidate_ids', lambda *_a, **_k: {'canonical_key': K_POP, 'candidate_ids': []})
    return db, kill, profiles, matches


def run():
    return router.generate_matches_for_user('owner', source='automatic', search_context=_preference_context(),
        search_job_id='job', report_progress=lambda _: True, can_commit=lambda: True)


def accepted_payload():
    packet = evidence(q='K-pop', c='Korean Pop', relation='equivalent')
    return {'candidate_ids': ['candidate'], 'evidence_by_candidate': {'candidate': [packet]},
        'validator_counts': {'accepted': 1, 'equivalent': 1, 'attempts': 1}, 'ann_observations': []}


def test_exact_hit_bypasses_shadow_and_keeps_exact_proposal(shadow, monkeypatch):
    db, _, _, matches = shadow
    monkeypatch.setattr(router, 'retrieve_preference_candidate_ids', lambda *_a, **_k: {'canonical_key': K_POP, 'candidate_ids': ['candidate']})
    monkeypatch.setattr(router, '_trait_stances', lambda uid: {K_POP: {'like'}} if uid=='candidate' else {})
    retrieve = Mock(side_effect=AssertionError('qualified exact must bypass ANN/validator'))
    monkeypatch.setattr(router, 'retrieve_semantic_preference_candidates', retrieve)
    result = run()
    assert result['status']=='success' and result['diagnostics']['qualified_exact_count']==1
    retrieve.assert_not_called()
    assert 'semantic_shadow' not in db.match_search_jobs.find_one({'job_id':'job'})
    assert 'related_interest_pilot' not in matches.insert_one.call_args.args[0]


@pytest.mark.parametrize('outcome', ['related','unrelated','ERROR','exception'])
def test_shadow_result_cannot_reach_visible_pool_or_write(shadow, monkeypatch, outcome):
    db, _, profiles, matches = shadow
    payload = accepted_payload()
    if outcome=='unrelated':payload={'candidate_ids': [], 'validator_counts': {'rejected':1,'unrelated':1}}
    if outcome=='ERROR':
        payload['evidence_by_candidate']['candidate'][0]['validator_status']='ERROR'
        payload['validator_counts']={'error':1}
    retrieve=Mock(return_value=payload)
    if outcome=='exception':retrieve.side_effect=semantic.PreferenceSemanticRetrievalError('semantic_validator_unavailable',{'error':1})
    monkeypatch.setattr(router, 'retrieve_semantic_preference_candidates', retrieve)
    visible = run()
    retrieve.assert_called_once()
    assert retrieve.call_args.kwargs['shadow_only'] is True
    assert visible['status']=='no_suitable_candidate' and visible['matches']==[]
    assert visible.get('reason_code') is None  # Preserve the existing empty-exact response.
    assert visible['diagnostics']['semantic_fallback_triggered'] is False
    assert visible['diagnostics']['semantic_concepts_considered']==[]
    matches.insert_one.assert_not_called();profiles.update_one.assert_not_called()
    meta=db.match_search_jobs.find_one({'job_id':'job'})['semantic_shadow']
    assert meta['qualified_count']==int(outcome=='related')
    assert meta['observation_only'] is True
    assert '"candidate"' not in json.dumps(meta) and 'Korean Pop' not in json.dumps(meta)
    assert db.match_search_jobs.find_one({'job_id':'job'})['status']=='running'
    assert pair_route_allowed('owner','candidate') is False
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE','off')
    baseline=run()
    assert {k:v for k,v in visible.items() if k!='diagnostics'}=={k:v for k,v in baseline.items() if k!='diagnostics'}


def test_kill_or_unavailable_telemetry_prevents_shadow(shadow, monkeypatch):
    db, kill, _, _ = shadow
    retrieve=Mock(side_effect=AssertionError('must not compute'))
    monkeypatch.setattr(router,'retrieve_semantic_preference_candidates',retrieve)
    kill.touch();assert run()['matches']==[];retrieve.assert_not_called()
    assert 'semantic_shadow' not in db.match_search_jobs.find_one({'job_id':'job'})
    kill.unlink()  # isolated test fixture only
    monkeypatch.setattr(telemetry,'record_shadow',lambda *_a,**_k:False)
    assert run()['matches']==[];retrieve.assert_not_called()


def test_shadow_uses_signed_v2_endpoint_and_strict_evidence(shadow, monkeypatch):
    from agent_quota import internal
    packet=accepted_payload()['evidence_by_candidate']['candidate'][0]
    bad={**packet,'validator_status':'ERROR'}
    response=Mock();response.json.return_value={'status':'success','canonical_key':K_POP,
        'candidates':[{'candidate_id':'candidate','evidence':[packet,bad]}],'validator_counts':{'accepted':1,'error':1}}
    post=Mock(return_value=response)
    monkeypatch.setattr(semantic.requests,'post',post)
    monkeypatch.setattr(internal,'signed_headers',lambda:{'signed':'test'})
    result=semantic.retrieve_semantic_preference_candidates('owner','K-pop',excluded_user_ids=set(),shadow_only=True)
    assert post.call_args.args==(semantic.AGENT_RELATED_URL,)
    assert post.call_args.kwargs['json']['shadow_only'] is True
    assert post.call_args.kwargs['json']['embedding_fingerprint']
    assert result['evidence_by_candidate']=={'candidate':[packet]}
    assert semantic.semantic_config()['total_timeout_seconds']==38


def test_three_shadow_unavailable_engage_kill_without_visible_status_change(shadow, monkeypatch):
    db, kill, _, _=shadow
    monkeypatch.setattr(monitor.time,'time',lambda:1000)
    monkeypatch.setattr(telemetry.time,'time',lambda:1000)
    for i in range(3):
        db.match_search_jobs.insert_one({'job_id':str(i),'user_id':'owner','status':'no_suitable_candidate'})
        assert telemetry.record_shadow(str(i),'owner',status='failed',error_code='semantic_validator_unavailable',counts={'error':2})
        assert monitor.on_shadow_finished('owner','semantic_validator_unavailable') is (i==2)
    assert kill.exists()
    assert db.match_search_jobs.count_documents({'status':'no_suitable_candidate'})==3
    summary=telemetry.summarize(list(db.match_search_jobs.find()),[])
    assert summary['shadow']['typed_unavailable']==3
    assert summary['validator_unavailable_jobs']==0


def test_shadow_block_history_and_duplicate_profile_are_not_qualified(shadow, monkeypatch):
    db, _, profiles, _=shadow
    profiles.find.return_value=Cursor([{'user_id':'candidate'},{'user_id':'candidate'}])
    monkeypatch.setattr(router,'retrieve_semantic_preference_candidates',Mock(return_value=accepted_payload()))
    assert run()['matches']==[]
    assert db.match_search_jobs.find_one({'job_id':'job'})['semantic_shadow']['qualified_count']==0


def test_raw_exact_hit_without_qualified_evidence_still_observes(shadow,monkeypatch):
    db,_,_,matches=shadow
    monkeypatch.setattr(router,'retrieve_preference_candidate_ids',lambda *_a,**_k:{'canonical_key':K_POP,'candidate_ids':['candidate']})
    retrieve=Mock(return_value=accepted_payload());monkeypatch.setattr(router,'retrieve_semantic_preference_candidates',retrieve)
    result=run()
    assert result['diagnostics']['qualified_exact_count']==0
    retrieve.assert_called_once();matches.insert_one.assert_not_called()


def test_shadow_success_breaks_consecutive_failures_and_report_is_separate(shadow,monkeypatch):
    from scripts.report_related_interest_pilot import collect_report
    db,kill,_,_=shadow
    now=time.time()
    for i,code in enumerate(['semantic_validator_unavailable',None,'semantic_validator_unavailable']):
        db.match_search_jobs.insert_one({'job_id':str(i),'user_id':'owner','created_at':now,'status':'no_suitable_candidate'})
        assert telemetry.record_shadow(str(i),'owner',status='failed' if code else 'success',error_code=code)
        assert not monitor.on_shadow_finished('owner',code)
    assert not kill.exists()
    report=collect_report(db,None,since=now-1,limit=20)
    assert report['shadow']['jobs']==3 and report['shadow']['typed_unavailable']==2
    assert report['semantic_proposals']==report['fallback_trigger_jobs']==0
    assert 'owner' not in json.dumps(report)
