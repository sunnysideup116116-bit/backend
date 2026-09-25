"""Hermetic contract tests. No provider, credentials, Graph, or service startup."""
import copy
import importlib.util
import json
from pathlib import Path
import pytest

HERE=Path(__file__).resolve().parent
spec=importlib.util.spec_from_file_location('r3_offline_test_target',HERE/'run_r3_offline.py')
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

def fixture():
    return json.loads((HERE/'r3_holdout.json').read_text())

def test_fixed_directional_holdout_counts_and_reversal():
    cases=module.expand_cases(fixture())
    assert len(cases)==96
    from collections import Counter
    assert Counter(c['language'] for c in cases)=={'en-en':24,'zh-zh':24,'en-zh':24,'zh-en':24}
    assert Counter(c['expected'] for c in cases)=={'YES':28,'NO':60,'ABSTAIN':8}
    by_id={c['id']:c for c in cases}
    for g in fixture()['groups']:
        a,b=by_id[g['id']+'-f'],by_id[g['id']+'-r']
        assert (a['Q'],a['C'])==(b['C'],b['Q'])
    assert by_id['h03-f']['expected']=='YES'
    assert by_id['h03-r']['expected']=='NO'

@pytest.mark.parametrize('fault',['count','duplicate','oversize','classification','label'])
def test_rejects_unbounded_or_invalid_input(fault):
    data=fixture()
    if fault=='count':data['groups']=data['groups'][:1]
    if fault=='duplicate':data['groups'][1]['id']=data['groups'][0]['id']
    if fault=='oversize':data['groups'][0]['a']='x'*121
    if fault=='classification':data['data_classification']='user_memory'
    if fault=='label':data['groups'][0]['f']=['YES','constraint_conflict']
    with pytest.raises(ValueError):module.expand_cases(data)

def test_valid_output_is_explicit_and_bounded():
    obj={'results':[{'id':'one','decision':'NO','relation':'role_mismatch'},{'id':'two','decision':'ABSTAIN','relation':'unknown'}]}
    parsed=module.parse_decisions(json.dumps(obj),{'one','two'})
    assert parsed['one']['decision']=='NO' and parsed['two']['decision']=='ABSTAIN'

@pytest.mark.parametrize('fault',['wrong_id','duplicate','missing','extra_field','raw_memory','unknown_enum','decision_relation','invalid_json'])
def test_bad_model_output_is_error_not_repaired_to_yes_or_no(fault):
    row={'id':'one','decision':'YES','relation':'equivalent'}
    obj={'results':[row]}
    if fault=='wrong_id':row['id']='invented'
    if fault=='duplicate':obj['results'].append(copy.deepcopy(row))
    if fault=='missing':obj['results']=[]
    if fault=='extra_field':obj['confidence']=1
    if fault=='raw_memory':row['raw_memory']='must not be accepted'
    if fault=='unknown_enum':row['decision']='MAYBE'
    if fault=='decision_relation':row['relation']='role_mismatch'
    text='invalid' if fault=='invalid_json' else json.dumps(obj)
    with pytest.raises(ValueError):module.parse_decisions(text,{'one'})

def test_abstain_and_failure_do_not_disappear_from_recall_or_confusion():
    cases=[{'id':'a','expected':'YES','relation':'equivalent'},
           {'id':'b','expected':'YES','relation':'equivalent'},
           {'id':'c','expected':'ABSTAIN','relation':'unknown'}]
    m=module.metrics(cases,{'a':{'decision':'ABSTAIN','relation':'unknown'},'c':{'decision':'YES','relation':'equivalent'}})
    assert m['TP']==0 and m['FN']==2 and m['FP']==1 and m['recall']==0
    assert m['primary_confusion']['YES']['ABSTAIN']==1
    assert m['primary_confusion']['YES']['ERROR']==1

def test_only_allowlisted_secret_assignments_are_parsed(tmp_path):
    path=tmp_path/'synthetic.env'
    path.write_text('GOOGLE_API_KEYS1=synthetic-test-token\nNEO4J_URI=never-read\nAPPWRITE_API_KEY=never-load\n')
    assert module.selected_config(path,{'GOOGLE_API_KEYS1'})=={'GOOGLE_API_KEYS1':'synthetic-test-token'}

def test_report_secret_guard_blocks_output(tmp_path):
    path=tmp_path/'report.json'
    with pytest.raises(ValueError):module.write_report(path,{'bad':'synthetic-test-token'},['synthetic-test-token'])
    assert not path.exists()

def test_raw_cosine_scale_distinct_from_ann():
    assert module.cosine([1.,0.],[0.,1.])==0
    assert (1+module.cosine([1.,0.],[0.,1.]))/2==.5
