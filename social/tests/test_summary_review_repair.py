import json
from types import SimpleNamespace

import mongomock
import pytest

from services import conversation_compaction_service as c
from services.message_use_service import metadata_for_use


def test_report_cannot_hide_unavailable_evaluations_inside_objects(monkeypatch):
    import time
    from services import conversation_summary_operations as ops
    monkeypatch.setattr(ops, 'algorithm_fingerprint', lambda:'test')
    monkeypatch.setattr(ops, '_resolve_chat_model', lambda:'test')
    monkeypatch.setattr(ops, 'selected_provider', lambda:'test')
    report={'version':'summary-benchmark-v1','synthetic':True,'production_writes':False,
            'policy':c.COMPACTION_POLICY_VERSION,'model':'test','provider':'test','algorithm_sha256':'test',
            'finished_at':time.time(),'cases':[{'name':str(i),'family':str(i%10),'passed':True,
                'content_pass':True,'evaluation':{'status':'pass'}} for i in range(100)]}
    for row in report['cases'][:3]:row.update(passed=False,content_pass=False,evaluation={'status':'unavailable'})
    with pytest.raises(ValueError,match='benchmark_quality_not_ready'):
        ops.approve_benchmark(report,'digest',apply=False)


def summary(**fields):
    return c.ConversationSummaryV1(**fields)


def decision(passed=True):
    retention = {field: True for field in c.SUMMARY_FIELDS}
    retention['known_continuity'] = passed
    return c.ConversationCompactionEvaluationDecisionV1(retention=c.ContinuityRetentionV1(**retention),
        unsupported_content=False, role_confusion=False, canonical_state_leak=False, confidence=.95)


@pytest.fixture
def store(monkeypatch):
    db = mongomock.MongoClient().review_repair
    monkeypatch.setattr(c, 'messages_coll', db.messages)
    monkeypatch.setattr(c, 'CONVERSATION_COMPACTIONS', db.summaries)
    monkeypatch.setattr(c, 'CONVERSATION_COMPACTION_RUNS', db.runs)
    monkeypatch.setenv('AYUE_CONVERSATION_COMPACTION_MODE', 'shadow')
    doc = {'room_id':'ai_assistant_owner','sender_id':'owner','content':'虛構旅客使用 Reed，最高 Diamond，目前 Platinum。',
           'timestamp':1.,'metadata':{'message_use':metadata_for_use('ordinary')}}
    doc['_id'] = db.messages.insert_one(doc).inserted_id
    return db, doc


@pytest.mark.parametrize('bad', ['overflow','too_long','non_string','unsafe','missing_field'])
def test_generated_output_is_rejected_before_silent_truncation(bad):
    data = summary().model_dump()
    if bad == 'overflow': data['known_continuity'] = [f'Fact{i}' for i in range(7)]
    if bad == 'too_long': data['known_continuity'] = ['a' * 121]
    if bad == 'non_string': data['known_continuity'] = [{'fact':'x'}]
    if bad == 'unsafe': data['known_continuity'] = ['seed_user_01 的資料']
    if bad == 'missing_field': data.pop('owner_goals')
    with pytest.raises(c.GeneratedSummaryContractError): c._validate_generated_summary(data)


def test_provider_overflow_gets_contract_retry_without_losing_seventh_fact(monkeypatch):
    bad = summary().model_dump();bad['known_continuity'] = [f'Fact{i}' for i in range(7)]
    good = summary(known_continuity=['Fact0 Fact1','Fact2','Fact3','Fact4','Fact5','Fact6']).model_dump()
    payloads = iter([bad, good]);prompts = []
    def provider(prompt, **kwargs):
        prompts.append(prompt)
        return SimpleNamespace(content=json.dumps(next(payloads)))
    monkeypatch.setattr(c, 'generate_chat_completion', provider)
    value, attempts, code = c._run_typed_step_with_retry(lambda:c._generate_summary({},[], 'owner'),
        lambda:c._generate_summary({},[], 'owner',contract_repair=True))
    assert attempts==2 and code=='success_after_retry'
    assert 'Fact6' in str(value.model_dump())
    assert 'known_continuity": 6' in prompts[0]


def test_review_repairs_with_feedback_and_revalidates_original_sources(store, monkeypatch):
    db, doc = store;seen = []
    def generate(prior, messages, owner, **kwargs):
        seen.append(kwargs)
        return summary(known_continuity=['Reed Diamond Platinum'] if kwargs.get('review_feedback') else ['Reed'])
    monkeypatch.setattr(c, '_generate_summary', generate)
    evaluations = iter([decision(False), decision(True)])
    monkeypatch.setattr(c, '_evaluate_summary', lambda *a,**k:next(evaluations))
    result=c.run_conversation_compaction_shadow('owner',doc['room_id'],[str(doc['_id'])],0,None)
    assert result['status']=='stored' and len(seen)==2
    assert seen[1]['review_feedback']['issue_codes']==['omitted_known_continuity']
    saved=db.summaries.find_one({});obs=saved['observability']
    assert obs['semantic_repair_attempted'] and obs['repair_result_code']=='pass'
    assert obs['initial_issue_codes']==['omitted_known_continuity']
    assert obs['generation_attempt_count']==2 and obs['evaluation_attempt_count']==2
    assert 'Diamond' in str(saved['summary'])


def test_persistent_review_is_not_forced_through_and_repair_is_bounded(store,monkeypatch):
    db,doc=store;calls=[]
    monkeypatch.setattr(c,'_generate_summary',lambda *a,**k:(calls.append(k) or summary(active_topics=['fiction'])))
    monkeypatch.setattr(c,'_evaluate_summary',lambda *a,**k:decision(False))
    assert c.run_conversation_compaction_shadow('owner',doc['room_id'],[str(doc['_id'])],0,None)['status']=='review'
    assert len(calls)==2 and db.summaries.count_documents({})==0
    assert db.runs.find_one({})['observability']['repair_result_code']=='review'


def test_repair_failure_keeps_previous_valid_summary_and_watermark(store,monkeypatch):
    db,doc=store
    monkeypatch.setattr(c,'_generate_summary',lambda *a,**k:summary(known_continuity=['original']))
    monkeypatch.setattr(c,'_evaluate_summary',lambda *a,**k:decision(True))
    assert c.run_conversation_compaction_shadow('owner',doc['room_id'],[str(doc['_id'])],0,None)['status']=='stored'
    previous=db.summaries.find_one({})
    second=dict(doc);second.pop('_id');second['timestamp']=2.;second_id=db.messages.insert_one(second).inserted_id
    def generate(*a,**kwargs):
        if kwargs.get('review_feedback'): raise c.GeneratedSummaryContractError('bad')
        return summary(active_topics=['missing original'])
    monkeypatch.setattr(c,'_generate_summary',generate)
    monkeypatch.setattr(c,'_evaluate_summary',lambda *a,**k:decision(False))
    result=c.run_conversation_compaction_shadow('owner',doc['room_id'],[str(second_id)],previous['revision'],previous['source_hash'])
    assert result['status']=='review' and db.summaries.find_one({})==previous
    run=db.runs.find_one({'source_hash':{'$ne':previous['source_hash']}})
    assert run['observability']['generation_attempt_count']==3
    assert run['observability']['repair_result_code']=='invalid_schema'


def test_contract_retry_receives_exact_safe_overflow_feedback(monkeypatch):
    bad=summary().model_dump();bad['known_continuity']=[f'Fact{i}' for i in range(7)]
    good=summary(known_continuity=['Fact0 Fact1','Fact2','Fact3','Fact4','Fact5','Fact6']).model_dump()
    values=iter([bad,good]);prompts=[]
    def provider(prompt,**kwargs):
        prompts.append(prompt)
        return SimpleNamespace(content=json.dumps(next(values)))
    monkeypatch.setattr(c,'generate_chat_completion',provider)
    result,attempts,code=c._generate_with_contract_retry({},[],'owner')
    assert attempts==2 and code=='success_after_retry'
    assert 'summary_contract_item_limit:known_continuity:actual=7,limit=6' in prompts[1]
    assert 'Fact6' in str(result.model_dump())


def test_maximum_retry_cost_is_counted_and_bounded(store,monkeypatch):
    db,doc=store
    generated=iter([c.GeneratedSummaryContractError('bad'),summary(active_topics=['partial']),
                    c.GeneratedSummaryContractError('bad'),summary(known_continuity=['Reed Diamond Platinum'])])
    evaluated=iter([TimeoutError(),decision(False),TimeoutError(),decision(True)])
    def generate(*args,**kwargs):
        value=next(generated)
        if isinstance(value,Exception):raise value
        return value
    def evaluate(*args,**kwargs):
        value=next(evaluated)
        if isinstance(value,Exception):raise value
        return value
    monkeypatch.setattr(c,'_generate_summary',generate)
    monkeypatch.setattr(c,'_evaluate_summary',evaluate)
    assert c.run_conversation_compaction_shadow('owner',doc['room_id'],[str(doc['_id'])],0,None)['status']=='stored'
    obs=db.summaries.find_one({})['observability']
    assert obs['generation_attempt_count']==4 and obs['evaluation_attempt_count']==4
    assert obs['repair_result_code']=='pass'
