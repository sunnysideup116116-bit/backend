import copy
import json
import time
from types import SimpleNamespace

import mongomock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services import conversation_compaction_service as c
from services import conversation_summary_operations as ops
from services.message_use_service import metadata_for_use
from routers import conversation_summaries as routes
ORIGINAL_SCOPE = ops._scope


@pytest.fixture
def store(monkeypatch):
    db = mongomock.MongoClient()['summary_ops']
    monkeypatch.setattr(ops, 'JOBS', db.jobs)
    monkeypatch.setattr(ops, 'ROLLOUTS', db.rollouts)
    monkeypatch.setattr(c, 'messages_coll', db.messages)
    monkeypatch.setattr(c, 'CONVERSATION_COMPACTIONS', db.summaries)
    monkeypatch.setattr(c, 'CONVERSATION_COMPACTION_RUNS', db.runs)
    monkeypatch.setattr(ops, 'algorithm_fingerprint', lambda: 'fingerprint')
    monkeypatch.setattr(ops, '_resolve_chat_model', lambda: 'test-model')
    monkeypatch.setattr(ops, 'selected_provider', lambda: 'ollama')
    monkeypatch.setattr(ops, '_scope', lambda owner, room: owner == 'owner' and room == 'ai_assistant_owner')
    monkeypatch.setenv('AYUE_CONVERSATION_CONTEXT_MODE', 'on')
    monkeypatch.setenv('AYUE_CONVERSATION_COMPACTION_MODE', 'shadow')
    monkeypatch.setenv('AYUE_CONVERSATION_CONTEXT_USER_ALLOWLIST', '*')
    monkeypatch.setattr(c, '_rollout_readiness_cache', (0., False))
    monkeypatch.setattr(c, 'queue_profile_coverage', lambda *a, **k: {'status':'disabled','requeued_count':0})
    return db


def report():
    return {'version':'summary-benchmark-v1','policy':c.COMPACTION_POLICY_VERSION,
            'synthetic':True,'production_writes':False,'model':'test-model','provider':'ollama',
            'algorithm_sha256':'fingerprint','finished_at':time.time(),
            'cases':[{'name':f'case{i}','family':f'family{i%10}','passed':True,'content_pass':True,
                      'evaluation':{'status':'pass'}} for i in range(50)]}


def messages(db, n=45, text='虛構角色名是白鷺'):
    db.messages.insert_many([{'room_id':'ai_assistant_owner','sender_id':'owner','content':text,
        'timestamp':float(i),'metadata':{'message_use':metadata_for_use('ordinary')}} for i in range(n)])


def good_models(monkeypatch):
    monkeypatch.setattr(c, '_generate_summary', lambda *a, **k: c.ConversationSummaryV1(known_continuity=['虛構角色白鷺']))
    monkeypatch.setattr(c, '_evaluate_summary', lambda *a, **k: c.ConversationCompactionEvaluationDecisionV1(
        retention=c.ContinuityRetentionV1(**{f:True for f in c.SUMMARY_FIELDS}),
        unsupported_content=False,role_confusion=False,canonical_state_leak=False,confidence=.95))


def test_approval_is_separate_from_stale_metrics_and_reads_do_not_write(store, monkeypatch):
    ops.approve_benchmark(report(), 'digest')
    monkeypatch.setattr(c, 'conversation_compaction_rollout_readiness', lambda: {
        'status':'not_ready','reason_codes':['stale_metrics']})
    before = copy.deepcopy(list(store.rollouts.find()))
    assert ops.rollout_status()['health'] == 'stale'
    assert c.conversation_context_enabled_for_user('arbitrary_new_owner')
    assert list(store.rollouts.find()) == before
    assert ops.monitor_rollout()['approval_valid']
    assert ops.rollout_decision()


def test_sufficient_bad_health_pauses_without_automatic_reapproval(store, monkeypatch):
    ops.approve_benchmark(report(), 'digest')
    monkeypatch.setattr(c, 'conversation_compaction_rollout_readiness', lambda: {
        'status':'not_ready','reason_codes':['pass_rate_below_target']})
    ops.monitor_rollout()
    assert ops.rollout_decision() is False
    monkeypatch.setattr(c, 'conversation_compaction_rollout_readiness', lambda: {'status':'ready','reason_codes':[]})
    assert ops.rollout_decision() is False


def test_few_samples_are_observable_not_auto_pause(store, monkeypatch):
    ops.approve_benchmark(report(), 'digest')
    monkeypatch.setattr(c, 'conversation_compaction_rollout_readiness', lambda: {
        'status':'not_ready','reason_codes':['insufficient_samples','unavailable_rate_above_target']})
    assert ops.monitor_rollout()['health']=='insufficient_data'
    assert ops.rollout_decision()


@pytest.mark.parametrize('change', ['model','algorithm','policy','samples','families','duplicate','old','quality','leak','false_pass'])
def test_invalid_evidence_cannot_approve(store, change):
    data=report()
    if change=='model': data['model']='other'
    if change=='algorithm': data['algorithm_sha256']='other'
    if change=='policy': data['policy']='old'
    if change=='samples': data['cases']=data['cases'][:49]
    if change=='families':
        for r in data['cases']: r['family']='one'
    if change=='duplicate': data['cases'][-1]['name']='case0'
    if change=='old': data['finished_at']=1
    if change=='quality':
        for r in data['cases'][:3]: r.update(passed=False,evaluation=None)
    if change=='leak': data['cases'][0]['leaked']=['not_allowed']
    if change=='false_pass': data['cases'][0]['content_pass']=False
    with pytest.raises(ValueError): ops.approve_benchmark(data,'digest')
    assert not list(store.rollouts.find())


def test_dry_run_does_not_approve_and_model_change_invalidates(store, monkeypatch):
    assert ops.approve_benchmark(report(),'digest',apply=False)['validated']
    assert ops.rollout_decision() is None
    ops.approve_benchmark(report(),'digest')
    monkeypatch.setattr(ops,'_resolve_chat_model',lambda:'new-model')
    assert ops.rollout_decision() is False


def test_manual_pause_overrides_legacy_ready(store, monkeypatch):
    ops.pause_rollout()
    monkeypatch.setattr(c,'conversation_compaction_rollout_readiness',lambda:{'status':'ready'})
    assert not c.conversation_context_enabled_for_user('owner')


def test_read_status_does_not_generate_or_queue(store):
    messages(store, n=4)
    assert ops.summary_status('owner','ai_assistant_owner')['state']=='not_needed'
    assert not list(store.jobs.find()) and not list(store.summaries.find())
    with pytest.raises(PermissionError): ops.summary_status('other','ai_assistant_owner')


def test_worker_catches_up_idempotently_and_status_has_no_content(store, monkeypatch):
    messages(store)
    good_models(monkeypatch)
    ops.approve_benchmark(report(),'digest')
    assert ops.enqueue_rebuild('owner','ai_assistant_owner')['queued']
    assert ops.enqueue_rebuild('owner','ai_assistant_owner')['queued']
    assert store.jobs.count_documents({})==1
    for _ in range(5):
        store.jobs.update_many({}, {'$set':{'next_attempt_at':0}})
        result=ops.run_rebuild_once()
        if result['status']=='complete': break
    assert result['status']=='complete'
    status=ops.summary_status('owner','ai_assistant_owner')
    assert status['summary_available'] and status['injection_enabled']
    assert status['pending_message_count']<=30
    assert '白鷺' not in json.dumps(status,ensure_ascii=False)
    assert '白鷺' not in json.dumps(list(store.jobs.find()),ensure_ascii=False,default=str)


def test_expired_lease_is_recovered(store, monkeypatch):
    messages(store)
    good_models(monkeypatch)
    ops.enqueue_rebuild('owner','ai_assistant_owner')
    store.jobs.update_one({}, {'$set':{'state':'running','token':'old','lease_until':0}})
    assert ops.run_rebuild_once()['status']=='queued'
    assert store.summaries.count_documents({})==1


def test_transient_failures_stop_after_three_attempts(store, monkeypatch):
    messages(store)
    ops.enqueue_rebuild('owner','ai_assistant_owner')
    monkeypatch.setattr(c,'queue_conversation_compaction_shadow',lambda *a,**k:{'status':'storage_unavailable'})
    for i in range(3):
        store.jobs.update_one({}, {'$set':{'next_attempt_at':0}})
        outcome=ops.run_rebuild_once()
    assert outcome['status']=='failed'
    assert not ops.enqueue_rebuild('owner','ai_assistant_owner')['queued']
    assert ops.enqueue_rebuild('owner','ai_assistant_owner',retry=True)['queued']


def test_oversized_source_is_blocked_and_unknown_owner_cannot_queue(store):
    messages(store,n=31,text='甲'*9001)
    assert ops.enqueue_rebuild('owner','ai_assistant_owner')['status']=='blocked'
    assert ops.summary_status('owner','ai_assistant_owner')['state']=='blocked'
    with pytest.raises(PermissionError): ops.enqueue_rebuild('other','ai_assistant_owner')


def test_http_binds_owner_and_never_accepts_user_id_override(store, monkeypatch):
    app=FastAPI()
    app.include_router(routes.router)
    monkeypatch.setattr(routes,'authenticate_owner',lambda token:'owner')
    messages(store,n=4)
    with TestClient(app) as client:
        assert client.get('/conversation/summary-status',params={'room_id':'ai_assistant_owner'}).status_code==200
        assert client.get('/conversation/summary-status',params={'room_id':'ai_assistant_other'}).status_code==404
        assert client.post('/conversation/summary-rebuild',json={'room_id':'ai_assistant_owner','user_id':'other'}).status_code==422
    assert not list(store.jobs.find())


def test_http_requires_authentication(store, monkeypatch):
    from services.appwrite_identity_service import AppwriteIdentityError
    monkeypatch.setattr(routes,'authenticate_owner',lambda token: (_ for _ in ()).throw(AppwriteIdentityError('missing',401)))
    app=FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        assert client.get('/conversation/summary-status',params={'room_id':'ai_assistant_owner'}).status_code==401


def test_hub_status_never_calls_the_provisioning_reader(store, monkeypatch):
    monkeypatch.setattr(ops,'get_room',lambda *a: pytest.fail('hub read could provision state'))
    assert not ORIGINAL_SCOPE('owner','ai_room::owner::match_hub')
    assert not ORIGINAL_SCOPE('owner','ai_room::owner::proposal::example')


def test_scope_storage_errors_are_not_misreported_as_missing_room(store, monkeypatch):
    monkeypatch.setattr(ops,'get_room',lambda *a: (_ for _ in ()).throw(RuntimeError('storage')))
    with pytest.raises(RuntimeError): ORIGINAL_SCOPE('owner','ai_room::owner::example')
