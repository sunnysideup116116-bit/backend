"""Offline Event readiness, provider/proof transport, accounting and writes."""
from copy import deepcopy
from unittest.mock import MagicMock, Mock

import pytest

from matchmaker_agent.event_v2_contract import query_signal, EventUnavailable
from matchmaker_agent.event_v2_vectors import query_vectors
from matchmaker_agent.preference_embedding_contract import RUNTIME, PROVENANCE
from matchmaker_agent.preference_bootstrap_contract import internal_headers
from services import event_cycle_service as cycle
from services import event_relevance_service as relevance
from services import event_query_vectors as encoder
from services import event_semantic_monitor as monitor
from services import event_v2_proposal as proof
from tests.match_flow_store import Collection
from tests.match_flow_store import matches as matches_query

VECTOR = [1.] + [0.]*767


class Observations(Collection):
    def delete_many(self, query):
        self.rows = [row for row in self.rows if not matches_query(row, query)]


def response(payload):
    result = Mock()
    result.json.return_value = payload
    result.raise_for_status.return_value = None
    return result


def test_readiness_never_queries_legacy_or_waits_for_pending(monkeypatch):
    get = Mock(return_value=response({'status':'degraded','ready':True,'exact_ready':True,
        'semantic_ready':False,'policy':'event_relevance_v2','all_vectors_required':False,'pending_count':99}))
    monkeypatch.setattr(cycle.requests, 'get', get)
    monkeypatch.setattr(cycle.time, 'sleep', Mock(side_effect=AssertionError('no population barrier')))
    result = cycle.wait_for_event_relevance()
    assert result['ready'] and result['exact_ready'] and not result['semantic_ready']
    assert 'pending_count' not in result
    assert get.call_args.args[0].endswith('/api/events/v2/readiness')
    assert get.call_count == 1


def test_durable_weekly_scans_population_with_semantic_pending(monkeypatch):
    from services import event_weekly_service as weekly, event_discovery_service as discovery
    from services import event_lifecycle_service as lifecycle
    stores = {name: Collection() for name in ('RUNS','USERS','matches_coll')}
    stores['profiles_coll'] = Collection([{'user_id':'pending'}, {'user_id':'exact-ready'}])
    for name,store in stores.items(): monkeypatch.setattr(weekly,name,store)
    monkeypatch.setattr(discovery,'discover_and_ingest_events',lambda **_: {'status':'success'})
    monkeypatch.setattr(lifecycle,'run_event_lifecycle_once',lambda: {'status':'success'})
    monkeypatch.setattr(cycle,'wait_for_event_relevance',lambda: {'ready':True,'exact_ready':True,
        'semantic_ready':False,'all_vectors_required':False,'status':'degraded'})
    seen=[]
    def create(owner, **kwargs):
        seen.append(owner)
        return {'status':'created','match_id':'synthetic'} if owner=='exact-ready' else {'status':'unavailable','error_code':'event_positive_embedding_pending'}
    monkeypatch.setattr(weekly,'create_event_opportunity',create)
    result=weekly.run_durable_cycle('synthetic-week',lambda:True,lambda _:None,region='test',window_days=30,categories=[])
    assert set(seen)=={'pending','exact-ready'}
    assert result['invitation_scan']['scanned_count']==2
    assert result['invitation_scan']['created_count']==1
    assert result['invitation_scan']['failed_user_count']==1
    assert result['status']=='partial'


def test_rebuild_is_read_only_and_does_not_enable_old_worker(monkeypatch):
    get=Mock(return_value=response({'policy':'event_relevance_v2','exact_ready':True,'semantic_ready':True}))
    monkeypatch.setattr(relevance.requests,'get',get)
    assert relevance.rebuild_all_event_relevance(100)['embedded_count']==0
    assert get.call_args.args[0].endswith('/api/events/v2/readiness')


@pytest.mark.parametrize('change', ['runtime','provenance','source','dimension','nan','zero','norm'])
def test_query_vector_transport_rejects_mismatch(monkeypatch, change):
    s=query_signal('Synthetic outdoor activity','tag','tag')
    payload={'status':'success','runtime':RUNTIME,'encoder_provenance':PROVENANCE,'vectors':{s.source_hash:VECTOR[:]}}
    if change=='runtime': payload['runtime']='0'*64
    elif change=='provenance': payload['encoder_provenance']='0'*64
    elif change=='source': payload['vectors']={'0'*64:VECTOR[:]}
    elif change=='dimension': payload['vectors'][s.source_hash]=[1.]
    elif change=='nan': payload['vectors'][s.source_hash][0]=float('nan')
    elif change=='zero': payload['vectors'][s.source_hash]=[0.]*768
    elif change=='norm': payload['vectors'][s.source_hash][0]=2.
    http=Mock();http.post.return_value=response(payload)
    monkeypatch.setenv('PREFERENCE_BOOTSTRAP_SIGNING_KEY','synthetic-test-key')
    monkeypatch.setattr('matchmaker_agent.event_v2_vectors.internal_headers', lambda *_a,**_k: {'test':'signed'})
    with pytest.raises(EventUnavailable): query_vectors([s],deadline=100,clock=lambda:0.,http=http)


def test_query_vectors_use_signed_transport_and_source_hash(monkeypatch):
    s=query_signal('Synthetic outdoor activity','tag','tag')
    http=Mock();http.post.return_value=response({'status':'success','runtime':RUNTIME,
        'encoder_provenance':PROVENANCE,'vectors':{s.source_hash:VECTOR[:]}})
    monkeypatch.setattr('matchmaker_agent.event_v2_vectors.internal_headers', lambda *_a,**_k: {'test':'signed'})
    assert query_vectors([s],deadline=100,clock=lambda:0.,http=http)=={s.source_hash:VECTOR}
    assert http.post.call_args.kwargs['headers']=={'test':'signed'}
    assert http.post.call_args.kwargs['allow_redirects'] is False
    assert http.post.call_args.kwargs['json']['signals'][0]['namespace']=='event-signal-v1'


def test_encoder_uses_frozen_pipeline_closes_and_is_idempotent(monkeypatch):
    encoder._cache.clear()
    s=query_signal('Synthetic query','tag','test')
    provider=Mock();provider.embed.return_value=[VECTOR[:]]
    factory=Mock(return_value=provider)
    monkeypatch.setattr('config.GOOGLE_API_KEYS',['synthetic-key'])
    result=encoder.produce([s],deadline=40,clock=lambda:0.,factory=factory)
    assert result[s.source_hash]==VECTOR
    provider.embed.assert_called_once_with([s.text],task_type='semantic_similarity',output_dimensionality=768,request_timeout_seconds=10)
    provider.close.assert_called_once()
    assert factory.call_args.kwargs['keys']==['synthetic-key']
    assert encoder.produce([s],deadline=40,clock=lambda:0.,factory=factory)==result
    assert factory.call_count==1


def test_encoder_failure_closes_and_never_caches_invalid_vector(monkeypatch):
    encoder._cache.clear()
    s=query_signal('Invalid query','tag','test')
    provider=Mock();provider.embed.return_value=[[0.]*768]
    monkeypatch.setattr('config.GOOGLE_API_KEYS',['synthetic-key'])
    with pytest.raises(EventUnavailable,match='event_query_embedding_invalid'):
        encoder.produce([s],deadline=40,clock=lambda:0.,factory=lambda *_a,**_k:provider)
    provider.close.assert_called_once()
    assert not encoder._cache


def test_encoder_does_not_start_uncancellable_metadata_with_insufficient_budget():
    encoder._cache.clear()
    factory=Mock(side_effect=AssertionError('must not start'))
    assert encoder.produce([query_signal('budget','tag','x')],deadline=10,clock=lambda:0.,factory=factory)=={}
    factory.assert_not_called()


def test_same_continuous_unavailable_policy_and_no_raw_telemetry():
    store=Observations(); engage=Mock()
    telemetry={'semantic_triggered':True,'ann_calls':1,'validator_calls':1,'validator':{'error':2},
        'raw_preference':'DO NOT SAVE','provider_output':'DO NOT SAVE','candidate_id':'DO NOT SAVE'}
    for i in range(3):
        stopped=monitor.on_result('unavailable','semantic_validator_unavailable',telemetry,
            collection=store,now=lambda:1000+i,engage=engage)
        assert stopped is (i==2)
    engage.assert_called_once()
    assert 'DO NOT SAVE' not in repr(store.rows)
    assert all('user_id' not in row for row in store.rows)


def test_success_between_failures_resets_consecutive_policy():
    store=Observations();engage=Mock();telemetry={'semantic_triggered':True}
    for i,code in enumerate(['semantic_validator_unavailable','','semantic_validator_unavailable']):
        assert not monitor.on_result('unavailable' if code else 'no_match',code,telemetry,
            collection=store,now=lambda:1000+i,engage=engage)
    engage.assert_not_called()


def test_final_proof_failure_prevents_graph_and_mongo_commit(monkeypatch):
    from services import event_opportunity_service as service
    from tests.test_event_opportunity_service import agent_payload
    payload=agent_payload()
    monkeypatch.setattr(service.requests,'post',lambda *_a,**_k:response(payload))
    matches=MagicMock();matches.count_documents.return_value=0;matches.find_one.return_value=None
    profiles=MagicMock();profiles.find.return_value=[{'user_id':'owner'},{'user_id':'candidate'}]
    monkeypatch.setattr(service,'profiles_coll',profiles);monkeypatch.setattr(service,'matches_coll',matches)
    monkeypatch.setattr(service,'reserve_daily_quota',lambda *_a,**_k:{'status':'reserved'})
    release=Mock();queue=Mock()
    monkeypatch.setattr(service,'release_daily_quota',release)
    monkeypatch.setattr(service,'queue_mediator_event',queue)
    monkeypatch.setattr(service,'event_final_eligible',lambda *_a,**_k:False)
    result=service.create_event_opportunity('owner')
    assert result['status']=='stale'
    matches.insert_one.assert_not_called();queue.assert_not_called();release.assert_called_once()


def test_lease_is_rechecked_after_final_http_proof(monkeypatch):
    from services import event_opportunity_service as service
    from tests.test_event_opportunity_service import agent_payload
    monkeypatch.setattr(service.requests,'post',lambda *_a,**_k:response(agent_payload()))
    matches=MagicMock();matches.count_documents.return_value=0;matches.find_one.return_value=None
    profiles=MagicMock();profiles.find.return_value=[{'user_id':'owner'},{'user_id':'candidate'}]
    monkeypatch.setattr(service,'profiles_coll',profiles);monkeypatch.setattr(service,'matches_coll',matches)
    monkeypatch.setattr(service,'reserve_daily_quota',lambda *_a,**_k:{'status':'reserved'})
    monkeypatch.setattr(service,'release_daily_quota',Mock())
    live=[True]
    def final(*_a,**_k): live[0]=False;return True
    monkeypatch.setattr(service,'event_final_eligible',final)
    with pytest.raises(RuntimeError,match='ownership_lost'):
        service.create_event_opportunity('owner',can_commit=lambda:live[0])
    matches.insert_one.assert_not_called()


def test_unsigned_query_encoding_cannot_call_provider(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app=FastAPI();app.include_router(encoder.router)
    signal=query_signal('Synthetic activity','tag','tag')
    never=Mock(side_effect=AssertionError('unsigned must not call provider'))
    monkeypatch.setattr(encoder,'produce',never)
    result=TestClient(app).post('/api/internal/events/v2/query-vectors',json={'signals':[
        {'text':signal.text,'kind':signal.kind,'namespace':signal.namespace,'source_hash':signal.source_hash}],
        'request_budget_seconds':25.})
    assert result.status_code==403
    never.assert_not_called()


def test_query_endpoint_rechecks_namespace_hash_before_encoding(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app=FastAPI();app.include_router(encoder.router)
    monkeypatch.setattr(encoder,'verify_internal',lambda *_a:None)
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ENABLED','on')
    monkeypatch.setenv('MATCH_RELATED_INTEREST_ROLLOUT_MODE','enabled_accounts')
    monkeypatch.setenv('MATCH_PREFERENCE_SEMANTIC_MODE','active')
    never=Mock(side_effect=AssertionError('wrong hash must not call provider'))
    monkeypatch.setattr(encoder,'produce',never)
    result=TestClient(app).post('/api/internal/events/v2/query-vectors',json={'signals':[
        {'text':'Synthetic activity','kind':'tag','namespace':'event-signal-v1','source_hash':'0'*64}],
        'request_budget_seconds':25.})
    assert result.json()['status']=='unavailable'
    never.assert_not_called()
