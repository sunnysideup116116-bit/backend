"""Lifecycle boundaries, hermetic: no account, provider or database network."""
from contextlib import nullcontext
from copy import deepcopy
from unittest.mock import Mock

import mongomock
import pytest
from pydantic import ValidationError

from matchmaker_agent.concept_identity import canonicalize_concept
from matchmaker_agent import preference_bootstrap_contract as contract
from matchmaker_agent.preference_embedding_contract import frozen_contract, MODEL, RUNTIME, PROVENANCE
from matchmaker_agent.preference_embedding_api import Finish
from matchmaker_agent.semantic_evidence_readiness import verified_vector
from matchmaker_agent.semantic_rollout_inventory import classify_lifecycle
from services import preference_bootstrap_service as bootstrap
from services import preference_embedding_service as worker
from services import preference_action_projection as projection
from services.preference_owner_guard import unique_profile

OWNER='synthetic-lifecycle'
VECTOR=[1.0]+[0.0]*767


@pytest.fixture
def owner(monkeypatch):
    db=mongomock.MongoClient().db
    db.profiles.insert_one({'user_id':OWNER})
    monkeypatch.setattr(bootstrap,'profiles_coll',db.profiles)
    monkeypatch.setattr(bootstrap,'_facts',lambda _owner: [])
    monkeypatch.setattr(contract,'quota_signing_key',lambda: b'synthetic-test-key')
    monkeypatch.setenv('PREFERENCE_BOOTSTRAP_ENABLED','on')
    return db


def test_owner_source_is_read_only_and_never_guesses_empty_avoids(owner,monkeypatch):
    rows=[{'concept':{'key':'legacy_full','label':'Jazz、Mystery Novels'},'relation':'PREFERS'},
          {'concept':{'key':'legacy_avoid','label':'No smoking'},'relation':'AVOIDS'}]
    call=Mock(return_value={'rows':rows,'revision':4,'snapshot_hash':'a'*64})
    monkeypatch.setattr(bootstrap,'graph_call',call)
    before=list(owner.profiles.find())
    result=bootstrap.source(OWNER)
    assert result['prefers']==['Jazz、Mystery Novels'] and result['avoids']==['No smoking']
    assert [item['key'] for item in result['items']]==['legacy_full','legacy_avoid']
    assert result['status']=='confirmation_required' and result['complete']
    assert not result['requester_requires_v2'] and len(result['confirmation_required'])==5
    assert list(owner.profiles.find())==before
    call.assert_called_once_with('source',{'owner':OWNER})
    assert contract.open_source(result['source_token'],OWNER)['snapshot_hash']=='a'*64
    with pytest.raises(contract.BootstrapError):contract.open_preview(result['source_token'],OWNER)
    with pytest.raises(contract.BootstrapError):contract.open_source(result['source_token'],'different-owner')


def test_full_source_exposes_all_server_action_keys_without_cache_limit(owner,monkeypatch):
    rows=[{'concept':canonicalize_concept('Reading collection '+str(i)).as_dict(),
           'relation':'PREFERS' if i%2==0 else 'AVOIDS'} for i in range(64)]
    call=Mock(return_value={'rows':rows,'revision':8,'snapshot_hash':'a'*64})
    monkeypatch.setattr(bootstrap,'graph_call',call)
    result=bootstrap.source(OWNER)
    assert len(result['items'])==64 and len(result['prefers'])==len(result['avoids'])==32
    assert [item['key'] for item in result['items']]==[r['concept']['key'] for r in rows]
    assert all(set(item)=={'key','text','polarity','identity_status'} for item in result['items'])
    call.assert_called_once_with('source',{'owner':OWNER})


def test_source_does_not_invent_missing_legacy_action_key(owner,monkeypatch):
    monkeypatch.setattr(bootstrap,'graph_call',lambda *_a:{'rows':[
        {'concept':{'label':'Reading'},'relation':'PREFERS'}],'revision':0,'snapshot_hash':'a'*64})
    with pytest.raises(contract.BootstrapError,match='preference_identity_missing'):bootstrap.source(OWNER)


def test_changed_full_set_cannot_use_stale_discovery_receipt(owner,monkeypatch):
    token=contract.seal_source(OWNER,'a'*64,bootstrap._facts_hash([]))
    monkeypatch.setattr(bootstrap,'graph_call',Mock(return_value={'snapshot_hash':'b'*64}))
    with pytest.raises(contract.BootstrapError,match='stale_source'):
        bootstrap.preview(OWNER,'complete_set',['Reading'],[],token)


def test_source_receipt_expires_and_empty_has_no_fake_preference(owner,monkeypatch):
    monkeypatch.setattr(contract.time,'time',lambda: 10)
    token=contract.seal_source(OWNER,'a'*64,'b'*64)
    monkeypatch.setattr(contract.time,'time',lambda: 611)
    with pytest.raises(contract.BootstrapError,match='source_receipt_expired'):contract.open_source(token,OWNER)
    monkeypatch.setattr(bootstrap,'graph_call',lambda *_a:{'rows':[],'revision':0,'snapshot_hash':'c'*64})
    result=bootstrap.source(OWNER)
    assert result['items']==result['prefers']==result['avoids']==[]
    assert result['status']=='current'


@pytest.mark.parametrize('state',[{'disabled':True},{'blocked':'false'},{'preference_bootstrap_pending':'op'}])
def test_owner_write_profile_state_fail_closed(owner,state):
    owner.profiles.update_one({'user_id':OWNER},{'$set':state})
    with pytest.raises(contract.BootstrapError):unique_profile(owner.profiles,OWNER)


def test_duplicate_and_missing_profile_never_select_arbitrary_owner(owner):
    owner.profiles.insert_one({'user_id':OWNER})
    with pytest.raises(contract.BootstrapError,match='profile_missing_or_ambiguous'):unique_profile(owner.profiles,OWNER)
    with pytest.raises(contract.BootstrapError):unique_profile(owner.profiles,'missing')


def job():
    identity=canonicalize_concept('Reading long-form science fiction').as_dict()
    return {'key':identity['key'],'source_hash':identity['semantic_input_hash'],'identity':identity,
        'lease':'11111111-1111-4111-8111-111111111111','fingerprint':RUNTIME,'provenance_fingerprint':PROVENANCE}


@pytest.fixture
def embedding_worker(monkeypatch):
    monkeypatch.setenv('PREFERENCE_EMBEDDING_V2_ENABLED','on')
    monkeypatch.setenv('GOOGLE_EMBEDDING_MODEL',MODEL)
    worker._stop.clear()
    # SDK versions are independently verified below; provider is deterministic.
    monkeypatch.setattr(worker,'provenance',lambda *_a: frozen_contract()['provenance'])
    return job()


def test_approved_manifest_source_hashes_are_still_frozen():
    from scripts.semantic_embedding_pipeline import provenance,validate_frozen
    from scripts.prepare_related_interest_embeddings import prepare_batch
    versions={'google-generativeai':'0.8.3','google-ai-generativelanguage':'0.6.10'}
    actual=provenance(MODEL,{'name':MODEL,'provider_reported_version':'2','supported_methods':['embedContent']},
        prepare_batch,package_version=versions.__getitem__)
    assert validate_frozen(frozen_contract(),actual)==PROVENANCE


def test_incremental_worker_full_source_normalized_finish_and_close(embedding_worker):
    calls=[]
    def call(action,body):
        calls.append((action,body))
        return {'job':embedding_worker} if action=='claim' else {'written':1}
    provider=Mock();provider.embed.return_value=[[2.0]+[0.0]*767]
    factory=Mock(return_value=provider)
    assert worker.process_one(call=call,provider_factory=factory)['written']==1
    assert [a for a,_b in calls]==['claim','finish']
    assert calls[-1][1]['vector']==VECTOR
    assert provider.embed.call_args.args[0]==[embedding_worker['identity']['semantic_text']]
    provider.close.assert_called_once()
    assert 'key' not in str(worker.process_one(call=lambda *_a:{'job':None}))


def test_embedding_failure_never_turns_into_candidate_evidence(embedding_worker):
    call=Mock(return_value={'job':embedding_worker})
    provider=Mock();provider.embed.side_effect=RuntimeError('synthetic outage')
    before=deepcopy(embedding_worker['identity'])
    result=worker.process_one(call=call,provider_factory=lambda *_a,**_k:provider)
    assert result['status']=='unavailable' and call.call_args.args[0]=='fail'
    assert not any(c.args[0]=='finish' for c in call.call_args_list)
    assert verified_vector(before,RUNTIME) is None
    assert canonicalize_concept(before['semantic_text']).key==before['key']  # exact identity retained
    assert embedding_worker['identity']==before
    provider.close.assert_called_once()


def test_worker_disabled_or_pipeline_drift_never_claims(monkeypatch):
    call=Mock()
    monkeypatch.setenv('PREFERENCE_EMBEDDING_V2_ENABLED','off')
    assert worker.process_one(call=call)['status']=='disabled'
    monkeypatch.setenv('PREFERENCE_EMBEDDING_V2_ENABLED','on')
    monkeypatch.setenv('GOOGLE_EMBEDDING_MODEL','unexpected')
    assert worker.process_one(call=call)['status']=='unavailable'
    call.assert_not_called()


@pytest.mark.parametrize('vector',[[0.]*768,[1.]*768,[True]+[0.]*767,[float('nan')]+[0.]*767,[1.]*767])
def test_finish_rejects_invalid_vector(vector):
    j=job()
    with pytest.raises(ValidationError):Finish(key=j['key'],lease=j['lease'],source_hash=j['source_hash'],vector=vector)


def classify(rows,vectors=None,code=None):
    return classify_lifecycle({'$id':OWNER,'status':True},[{'_id':'profile','user_id':OWNER}],
        [{'id':OWNER}],{'owner':OWNER,'rows':rows},vectors or {},RUNTIME,preview_check=lambda _s:code)


def test_inventory_never_equates_deterministic_legacy_preview_with_consent():
    legacy={'relation':'PREFERS','concept':{'key':'legacy','label':'Reading'},'properties':{}}
    result=classify([legacy])
    assert result['category']=='BLOCKED_CONFIRMATION' and result['technical_preview_ready']
    assert result['requester_eligible']
    assert classify([legacy],code='bootstrap_item_limit')['category']=='BLOCKED_CAPACITY'
    assert classify([])['category']=='EMPTY'
    v2=canonicalize_concept('Reading').as_dict()
    positive={'relation':'PREFERS','concept':v2,'properties':{}}
    assert classify([positive],{v2['key']:[v2]})['category']=='MIGRATABLE'
    negative=classify([{**positive,'relation':'AVOIDS'}])
    assert negative['category']=='READY' and not negative['semantic_ready_candidate']


def test_action_projection_reconciles_current_graph_only(owner,monkeypatch):
    monkeypatch.setattr(projection,'JOBS',owner.jobs)
    monkeypatch.setattr(projection,'FACTS',owner.facts)
    monkeypatch.setattr(projection,'projection_write',lambda *_a,**_k:nullcontext(None))
    owner.facts.insert_one({'user_id':OWNER,'concept_key':'old','active':True,'stance':'like','evidence_count':9})
    owner.facts.insert_one({'user_id':'other','concept_key':'old','active':True,'stance':'like'})
    monkeypatch.setattr(projection,'graph_call',lambda *_a:{'snapshot_hash':'a','rows':[]})
    ident=projection.stage(OWNER,'old')
    assert projection.process_one(ident)['status']=='idle'  # unknown HTTP outcome waits past command expiry
    assert projection.settle(ident)['status']=='synced'
    assert owner.facts.find_one({'user_id':OWNER})['active'] is False
    assert owner.facts.find_one({'user_id':OWNER})['evidence_count']==9
    assert owner.facts.find_one({'user_id':'other'})['active'] is True
    assert projection.process_one(ident)['status']=='idle'


def test_projection_failure_is_pending_not_graph_replay(owner,monkeypatch):
    monkeypatch.setattr(projection,'JOBS',owner.jobs)
    monkeypatch.setattr(projection,'graph_call',Mock(side_effect=RuntimeError('synthetic outage')))
    ident=projection.stage(OWNER,'old')
    assert projection.settle(ident)=={'status':'pending'}
    assert owner.jobs.find_one({'_id':ident})['state']=='pending'


def test_manual_http_boundary_requires_enabled_owner_and_rejects_forged_user(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers import system,preference_bootstrap as routes
    from services import semantic_user_eligibility as eligibility, memory_service
    app=FastAPI();app.include_router(system.router)
    client=TestClient(app)
    body={'user_id':OWNER,'key':'old','action':'disable'}
    write=Mock(return_value={'status':'success'})
    monkeypatch.setattr(memory_service,'apply_memory_action',write)
    assert client.post('/api/profile/memories/action',json=body).status_code==401
    monkeypatch.setattr(routes,'authenticate_owner',lambda _header:OWNER)
    monkeypatch.setattr(eligibility,'lookup_enabled_account',lambda *_a,**_k:False)
    assert client.post('/api/profile/memories/action',json=body).status_code==403
    monkeypatch.setattr(eligibility,'lookup_enabled_account',lambda *_a,**_k:True)
    assert client.post('/api/profile/memories/action',json={**body,'user_id':'other'}).status_code==403
    write.assert_not_called()
    assert client.post('/api/profile/memories/action',json=body).status_code==200
    write.assert_called_once()


def test_duplicate_graph_owner_fence_rejects_before_mutation():
    from matchmaker_agent.preference_write_fence import lock_preferences,PreferenceFenceError
    from neo4j.exceptions import ResultNotSingleError
    tx=Mock();tx.run.return_value.single.side_effect=ResultNotSingleError(None,'multiple rows')
    with pytest.raises(PreferenceFenceError,match='preference_owner_missing_or_duplicate'):
        lock_preferences(tx,OWNER,require_existing=True)
    tx.run.return_value.single.assert_called_once_with(strict=True)
