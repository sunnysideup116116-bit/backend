"""R3 synthetic directional relation evaluation. Offline tooling, never a runtime service."""
from __future__ import annotations
import argparse
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import random
import re
import statistics
import time
import warnings

HERE = Path(__file__).resolve().parent
ALLOWED = {
    'YES': {'equivalent', 'candidate_specific_satisfies_broader_query'},
    'NO': {'candidate_broader_insufficient', 'sibling_related', 'role_mismatch',
           'constraint_conflict', 'lexical_ambiguity', 'unrelated'},
    'ABSTAIN': {'unknown'},
}
FLAGS = ('MATCH_PREFERENCE_SEMANTIC_MODE', 'MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED')

def expand_cases(fixture):
    if fixture.get('data_classification') != 'synthetic_non_user_data':
        raise ValueError('fixture_not_synthetic')
    groups = fixture['groups']
    if not 40 <= len(groups) <= 60:
        raise ValueError('unbounded_holdout')
    result = []
    for g in groups:
        for direction, q, c, languages in (
            ('f', g['a'], g['b'], g['languages']),
            ('r', g['b'], g['a'], list(reversed(g['languages']))),
        ):
            if not all(isinstance(v,str) and 0 < len(v) <= 120 for v in (q,c)):
                raise ValueError('invalid_concept_bound')
            expected, relation = g[direction]
            if relation not in ALLOWED.get(expected,set()):
                raise ValueError('invalid_human_label')
            result.append({'id':g['id']+'-'+direction, 'group':g['id'], 'Q':q, 'C':c,
                           'language':'-'.join(languages), 'category':g['category'],
                           'expected':expected, 'relation':relation})
    if len({r['id'] for r in result}) != len(result):
        raise ValueError('duplicate_case_id')
    return result

def parse_decisions(text, ids):
    obj=json.loads(text)
    if not isinstance(obj,dict) or set(obj)!={'results'} or not isinstance(obj['results'],list):
        raise ValueError('invalid_output_schema')
    rows=obj['results']
    if len(rows)!=len(ids):raise ValueError('missing_or_extra_result')
    found={}
    for row in rows:
        if not isinstance(row,dict) or set(row)!={'id','decision','relation'}:
            raise ValueError('invalid_output_fields')
        if row['id'] not in ids or row['id'] in found:
            raise ValueError('invalid_output_id')
        if row['relation'] not in ALLOWED.get(row['decision'],set()):
            raise ValueError('invalid_output_relation')
        found[row['id']]=row
    return found

def selected_config(path,names):
    from dotenv import dotenv_values
    selected=[]
    with Path(path).open() as handle:
        for line in handle:
            name=line.split('=',1)[0].strip().removeprefix('export ').strip()
            if name in names:selected.append(line)
    return dotenv_values(stream=io.StringIO(''.join(selected)),interpolate=False)

def cosine(a,b):
    return math.fsum(x*y for x,y in zip(a,b))/math.sqrt(math.fsum(x*x for x in a)*math.fsum(x*x for x in b))

def unit(a):
    if len(a)!=768 or not all(math.isfinite(x) for x in a):raise ValueError('embedding_contract_invalid')
    norm=math.sqrt(math.fsum(x*x for x in a))
    if not norm:raise ValueError('embedding_contract_invalid')
    return [x/norm for x in a]

def write_report(path,data,secrets=()):
    text=json.dumps(data,ensure_ascii=False,indent=2)+'\n'
    if any(secret and secret in text for secret in secrets):raise ValueError('secret_output_blocked')
    path.parent.mkdir(parents=True,exist_ok=True)
    with os.fdopen(os.open(path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600),'w') as handle:handle.write(text)

def metrics(cases, decisions):
    counts=Counter({'TP':0,'FP':0,'FN':0,'TN':0})
    confusion={e:{p:0 for p in ('YES','NO','ABSTAIN','ERROR')} for e in ALLOWED}
    for c in cases:
        decision=decisions.get(c['id'],{}).get('decision','ERROR')
        confusion[c['expected']][decision]+=1
        actual_yes=decision=='YES'; expected_yes=c['expected']=='YES'
        counts[('TP' if actual_yes else 'FN') if expected_yes else ('FP' if actual_yes else 'TN')]+=1
    return {**dict(counts), 'precision':counts['TP']/(counts['TP']+counts['FP']) if counts['TP']+counts['FP'] else None,
            'recall':counts['TP']/(counts['TP']+counts['FN']) if counts['TP']+counts['FN'] else None,
            'primary_confusion':confusion,
            'exact_primary_accuracy':sum(decisions.get(c['id'],{}).get('decision')==c['expected'] for c in cases)/len(cases),
            'secondary_accuracy':sum(decisions.get(c['id'],{}).get('relation')==c['relation'] for c in cases)/len(cases)}

def prepare():
    import run_readiness as h
    fixture=json.loads((HERE/'r3_holdout.json').read_text())
    cases=expand_cases(fixture)
    identity=h.canonical_module()
    old=json.loads((HERE/'fixtures.json').read_text())['semantic_pairs']
    old_pairs={(identity.canonicalize_concept(p['left']).key,identity.canonicalize_concept(p['right']).key) for p in old}
    old_pairs|={(b,a) for a,b in old_pairs}
    for case in cases:
        q=identity.canonicalize_concept(case['Q']);c=identity.canonicalize_concept(case['C'])
        if (q.key,c.key) in old_pairs:raise ValueError('holdout_overlaps_r25_pair')
        if q.key==c.key:raise ValueError('exact_identity_not_semantic_holdout')
        case.update(query_key=q.key,candidate_key=c.key,query_label=q.label,candidate_label=c.label)
    hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in [HERE/'r3_holdout.json',HERE/'r3_prompt.txt',HERE/'R3_DIRECTIONAL_CONTRACT.md']}
    manifest={'status':'FROZEN_BEFORE_SCORING','hashes':hashes,'case_count':len(cases),
              'expected_counts':dict(Counter(c['expected'] for c in cases)),
              'language_counts':dict(Counter(c['language'] for c in cases)),
              'category_counts':dict(Counter(c['category'] for c in cases)),
              'r25_pair_overlap':0,'repeat_count':3,'batch_size':8,'shuffle_seeds':[7301,7302,7303],
              'source_commit':h.command(['git','rev-parse','HEAD']).stdout.strip()}
    path=HERE/'artifacts/r3-preflight.json'
    if path.exists() and json.loads(path.read_text())!=manifest:raise ValueError('frozen_manifest_changed')
    write_report(path,manifest)
    return manifest,cases

def run_embedding(args,manifest,cases):
    import run_readiness as h
    from neo4j import GraphDatabase
    import google.generativeai as legacy
    from google.ai import generativelanguage as glm
    from google.api_core.client_options import ClientOptions
    from importlib.metadata import version
    state=h.read_state();h.verify_container(state)
    selected=selected_config(args.google_config,{*(f'GOOGLE_API_KEYS{i}' for i in range(1,7)),'GOOGLE_EMBEDDING_MODEL'})
    keys=list(dict.fromkeys(selected.get(f'GOOGLE_API_KEYS{i}','').strip() for i in range(1,7) if selected.get(f'GOOGLE_API_KEYS{i}','').strip()))
    model=selected.get('GOOGLE_EMBEDDING_MODEL');del selected
    if not keys or model!='models/gemini-embedding-2':raise ValueError('embedding_config_unconfirmed')
    report={'status':'RUNNING','manifest':manifest,'provider':'Google Gemini Developer API','sdk':version('google-generativeai'),
            'model':model,'task':'semantic_similarity','dimension':768,'prefix':'task: sentence similarity | query: ',
            'normalization':'L2 unit vectors','api_attempts':0,'failures':dict.fromkeys(['auth_failure','quota_exhausted','provider_failure'],0),
            'production_fingerprint':'unknown','runtime_flags':dict.fromkeys(FLAGS,'off')}
    labels=sorted({c[k] for c in cases for k in ('query_label','candidate_label')})
    cache={};rejected=set();cursor=0
    try:
        for start in range(0,len(labels),20):
            batch=labels[start:start+20];ok=False
            for offset in range(len(keys)):
                idx=(cursor+offset)%len(keys)
                if idx in rejected:continue
                if report['api_attempts']>=16:raise ValueError('embedding_request_budget_exhausted')
                report['api_attempts']+=1
                client=glm.GenerativeServiceClient(transport='rest',client_options=ClientOptions(api_key=keys[idx]))
                try:
                    with open(os.devnull,'w') as sink,redirect_stdout(sink),redirect_stderr(sink):
                        result=legacy.embed_content(model=model,content=[report['prefix']+t[:500] for t in batch],
                              output_dimensionality=768,client=client,request_options={'timeout':20,'retry':None})
                    vectors=result['embedding']
                    if len(vectors)!=len(batch):raise ValueError('embedding_batch_mismatch')
                    cache.update({label:unit(v) for label,v in zip(batch,vectors)})
                    cursor=idx;ok=True;break
                except Exception as exc:
                    code=getattr(exc,'code',0)
                    if callable(code):code=code()
                    try:code=int(code)
                    except (ValueError,TypeError):code=0
                    kind='auth_failure' if code in (401,403) else 'quota_exhausted' if code==429 else 'provider_failure'
                    report['failures'][kind]+=1;rejected.add(idx)
                    if code in (400,404,422):raise ValueError('embedding_contract_changed') from None
                finally:client.transport.close()
            if not ok:raise ValueError('embedding_pool_unavailable')
        report['unique_embedded_labels']=len(cache)
        report['inputs']=[{'label':t,'exact_input':report['prefix']+t[:500]} for t in labels]
        with h.local_network_only(state['bolt']):
            with GraphDatabase.driver(f"bolt://127.0.0.1:{state['bolt']}",auth=('neo4j',state['password']),connection_timeout=3,max_transaction_retry_time=0) as driver:
                with driver.session(database='neo4j') as s:
                    if s.run("MATCH(n) WHERE coalesce(n.readiness_fixture,'')<>$m RETURN count(n) AS n",m=h.MARKER).single()['n']:raise ValueError('not_disposable_data')
                    # Dedicated synthetic labels/index; existing Concept identities are untouched.
                    s.run('MATCH(n:R3SyntheticConcept {readiness_fixture:$m}) DETACH DELETE n',m=h.MARKER).consume()
                    s.run("CREATE VECTOR INDEX r3_pair_embedding_index IF NOT EXISTS FOR(c:R3Pair) ON(c.embedding) OPTIONS {indexConfig:{`vector.dimensions`:768,`vector.similarity_function`:'cosine'}}").consume()
                    s.run('UNWIND $rows AS row CREATE(c:R3SyntheticConcept) SET c=row,c.readiness_fixture=$m',rows=[{'label':t,'embedding':cache[t]} for t in labels],m=h.MARKER).consume()
                    s.run('CALL db.awaitIndexes(30)').consume()
                    report['index']=s.run("SHOW VECTOR INDEXES YIELD name,state,options,indexProvider WHERE name='r3_pair_embedding_index' RETURN name,state,options,indexProvider").single().data()
                    if report['index']['state']!='ONLINE':raise ValueError('index_not_online')
                    measured={}
                    for case in cases:
                        q,c=case['query_label'],case['candidate_label']
                        # Reverse cases are separately queried; no assumed score symmetry.
                        s.run('MATCH(n:R3Pair) REMOVE n:R3Pair').consume()
                        s.run('MATCH(n:R3SyntheticConcept {readiness_fixture:$m}) WHERE n.label IN $labels SET n:R3Pair',m=h.MARKER,labels=[q,c]).consume()
                        for _ in range(20):
                            hits=s.run("CALL db.index.vector.queryNodes('r3_pair_embedding_index',2,$v) YIELD node,score RETURN node.label AS label,score",v=cache[q]).data()
                            if {x['label'] for x in hits}=={q,c}:break
                            time.sleep(.1)
                        if {x['label'] for x in hits}!={q,c}:raise ValueError('index_visibility_failed')
                        measured[case['id']]={'raw_cosine':cosine(cache[q],cache[c]),'ann_score':next(x['score'] for x in hits if x['label']==c)}
                    s.run('MATCH(n:R3Pair) REMOVE n:R3Pair').consume()
                    report['scores']=measured
        report['status']='COMPLETED'
    except Exception:
        report['status']='FAILED';report['error']='embedding_or_local_validation_failed'
        raise
    finally:write_report(HERE/'artifacts/r3-embedding.json',report,keys)

def run_validator(args,manifest,cases):
    import httpx
    from openai import OpenAI
    settings=selected_config(args.matchmaker_config,{'LLM_API_KEY','LLM_BASE_URL'})
    model=selected_config(args.google_config,{'LLM_MODEL_ID'}).get('LLM_MODEL_ID')
    key=settings.get('LLM_API_KEY');base=settings.get('LLM_BASE_URL','');del settings
    if not key or base.rstrip('/')!='https://ollama.com/v1' or model!='deepseek-v4.1-flash:cloud':raise ValueError('validator_provider_unconfirmed')
    prompt=(HERE/'r3_prompt.txt').read_text()
    report={'status':'RUNNING','manifest':manifest,'provider':'Ollama Cloud','model':model,'temperature':0,
            'api_attempts':0,'failures':dict.fromkeys(['auth_failure','quota_exhausted','provider_failure','invalid_output'],0),
            'runs':[],'runtime_flags':dict.fromkeys(FLAGS,'off'),'no_production_flow':True}
    def request_gate(request):
        if request.url.scheme!='https' or request.url.host!='ollama.com' or request.url.path!='/v1/chat/completions':raise ValueError('unexpected_endpoint')
        if report['api_attempts']>=40:raise ValueError('validator_request_budget_exhausted')
        report['api_attempts']+=1
    try:
        with OpenAI(api_key=key,base_url=base,max_retries=0,timeout=60,
                    http_client=httpx.Client(trust_env=False,follow_redirects=False,event_hooks={'request':[request_gate]})) as client:
            for repeat,seed in enumerate(manifest['shuffle_seeds'],1):
                ordered=list(cases);random.Random(seed).shuffle(ordered)
                decisions={};batches=[]
                for start in range(0,len(ordered),8):
                    batch=ordered[start:start+8]
                    # IDs do not reveal labels/categories; only the two concept descriptions are sent.
                    payload=[{'id':c['id'],'Q':c['query_label'],'C':c['candidate_label']} for c in batch]
                    started=time.perf_counter();error=None
                    try:
                        with open(os.devnull,'w') as sink,redirect_stdout(sink),redirect_stderr(sink):
                            response=client.chat.completions.create(model=model,temperature=0,max_tokens=4096,
                                messages=[{'role':'system','content':prompt},{'role':'user','content':json.dumps({'pairs':payload},ensure_ascii=False)}])
                        if response.choices[0].finish_reason!='stop':raise ValueError('invalid_output')
                        try:parsed=parse_decisions(response.choices[0].message.content,{c['id'] for c in batch})
                        except Exception:raise ValueError('invalid_output') from None
                        decisions.update(parsed)
                    except Exception as exc:
                        code=getattr(exc,'status_code',0)
                        error='invalid_output' if isinstance(exc,ValueError) else 'auth_failure' if code in (401,403) else 'quota_exhausted' if code==429 else 'provider_failure'
                        report['failures'][error]+=1
                    batches.append({'ids':[c['id'] for c in batch],'error':error,'seconds':time.perf_counter()-started})
                    print(json.dumps({'repeat':repeat,'batch':start//8+1,'validated':len(decisions),'error':error}),flush=True)
                    report['current_progress']={'repeat':repeat,'decisions':decisions,'batches':batches}
                    write_report(HERE/'artifacts/r3-validator.json',report,[key])
                report['runs'].append({'repeat':repeat,'decisions':decisions,'batches':batches,'metrics':metrics(cases,decisions)})
        report.pop('current_progress',None)
        report['status']='COMPLETED'
    except Exception:
        report['status']='FAILED';report['error']='validator_experiment_failed'
        raise
    finally:write_report(HERE/'artifacts/r3-validator.json',report,[key])

def summarize(manifest,cases):
    embeddings=json.loads((HERE/'artifacts/r3-embedding.json').read_text())
    validator=json.loads((HERE/'artifacts/r3-validator.json').read_text())
    if embeddings['manifest']!=manifest or validator['manifest']!=manifest:raise ValueError('manifest_mismatch')
    if embeddings['status']!='COMPLETED' or validator['status']!='COMPLETED':raise ValueError('incomplete_experiment')
    runs=validator['runs'];scores=embeddings['scores']
    stable_primary=[];stable_relation=[];critical=[];equivalent_no=[]
    for case in cases:
        outputs=[run['decisions'].get(case['id'],{'decision':'ERROR','relation':'ERROR'}) for run in runs]
        if len({o['decision'] for o in outputs})==1 and outputs[0]['decision']!='ERROR':stable_primary.append(case['id'])
        if len({(o['decision'],o['relation']) for o in outputs})==1 and outputs[0]['decision']!='ERROR':stable_relation.append(case['id'])
        if case['relation'] in {'role_mismatch','constraint_conflict'} and any(o['decision']=='YES' for o in outputs):critical.append(case['id'])
        if case['relation']=='equivalent' and any(o['decision']=='NO' for o in outputs):equivalent_no.append(case['id'])
    combined={}
    acceptance_fields=('TP','FP','FN','TN','precision','recall')
    for threshold in [.92,.93,.94,.95]:
        result=[]
        for run in runs:
            accepted={k:v for k,v in run['decisions'].items() if scores[k]['ann_score']>=threshold}
            for c in cases:
                if scores[c['id']]['ann_score']<threshold:accepted[c['id']]={'decision':'NO','relation':'ann_filtered'}
            measured=metrics(cases,accepted)
            result.append({k:measured[k] for k in acceptance_fields})
        ann_only=metrics(cases,{c['id']:{'decision':'YES' if scores[c['id']]['ann_score']>=threshold else 'NO'} for c in cases})
        combined[str(threshold)]={'per_run':result,'ann_only':{k:ann_only[k] for k in acceptance_fields},
                                 'scope':'acceptance-only gate metrics; ANN filtered is NOT a validator NO decision'}
    strata={}
    for field in ('language','category','relation'):
        strata[field]={value:[metrics([c for c in cases if c[field]==value],run['decisions']) for run in runs] for value in sorted({c[field] for c in cases})}
    summary={'status':'COMPLETED','manifest':manifest,'case_count':len(cases),'validator_per_run':[r['metrics'] for r in runs],
             'stable_primary_count':len(stable_primary),'stable_primary_relation_count':len(stable_relation),
             'unstable_primary_ids':[c['id'] for c in cases if c['id'] not in stable_primary],
             'unstable_relation_ids':[c['id'] for c in cases if c['id'] not in stable_relation],
             'role_or_constraint_false_yes_ids':critical,'equivalent_false_no_ids':equivalent_no,
             'strata':strata,'combined':combined,'cases':cases,'scores':scores,
             'max_ann_mapping_error':max(abs(x['ann_score']-(1+x['raw_cosine'])/2) for x in scores.values()),
             'production_implementation':'STOP' if critical or len(stable_primary)<len(cases) or equivalent_no or any(validator['failures'].values()) else 'REQUIRES_INDEPENDENT_REVIEW_NOT_ACTIVATION',
             'production_fingerprint':'unknown','production_neo4j_version':'unknown','production_index_coverage_integrity':'unknown','runtime_flags':dict.fromkeys(FLAGS,'off')}
    unanimous={c['id']:{'decision':'YES' if all(r['decisions'].get(c['id'],{}).get('decision')=='YES' for r in runs) else 'ABSTAIN'} for c in cases}
    summary['unanimous_yes_metrics']=metrics(cases,unanimous)
    complete=[c for c in cases if all(c['id'] in r['decisions'] for r in runs)]
    summary['complete_three_run_case_count']=len(complete)
    summary['valid_output_count']=sum(len(r['decisions']) for r in runs)
    summary['errors_count']=len(cases)*len(runs)-summary['valid_output_count']
    summary['pooled_yes']={k:sum(r['metrics'][k] for r in runs) for k in ('TP','FP','FN','TN')}
    totals=summary['pooled_yes']
    totals['precision']=totals['TP']/(totals['TP']+totals['FP']) if totals['TP']+totals['FP'] else None
    totals['recall']=totals['TP']/(totals['TP']+totals['FN'])
    summary['gate_detail']={}
    for name,selected in [('role_mismatch',[c for c in cases if c['relation']=='role_mismatch']),
                          ('constraint_conflict',[c for c in cases if c['relation']=='constraint_conflict']),
                          ('equivalent',[c for c in cases if c['relation']=='equivalent']),
                          ('cross_language_equivalent',[c for c in cases if c['relation']=='equivalent' and c['language'] in {'en-zh','zh-en'}]),
                          ('broad_narrow',[c for c in cases if c['category']=='broad_narrow']),
                          ('unknown',[c for c in cases if c['expected']=='ABSTAIN'])]:
        outcomes=Counter(r['decisions'].get(c['id'],{}).get('decision','ERROR') for c in selected for r in runs)
        summary['gate_detail'][name]={'case_count':len(selected),'outcomes':dict(outcomes)}
    write_report(HERE/'artifacts/r3-summary.json',summary)
    print(json.dumps({k:summary[k] for k in ('case_count','stable_primary_count','role_or_constraint_false_yes_ids','equivalent_false_no_ids','production_implementation')}))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','embed','validate','summarize'])
    parser.add_argument('--google-config',type=Path)
    parser.add_argument('--matchmaker-config',type=Path)
    parser.add_argument('--confirm-synthetic-provider-use',action='store_true')
    args=parser.parse_args()
    if any(os.environ.get(f,'off')!='off' for f in FLAGS):parser.error('semantic flags must remain OFF')
    warnings.filterwarnings('ignore');logging.disable(logging.CRITICAL)
    manifest,cases=prepare()
    if args.action=='prepare':print(json.dumps(manifest));return
    if args.action in {'embed','validate'} and (not args.confirm_synthetic_provider_use or not args.google_config):parser.error('explicit provider permission/config required')
    if args.action=='validate' and not args.matchmaker_config:parser.error('explicit Matchmaker provider config required')
    try:
        if args.action=='embed':run_embedding(args,manifest,cases)
        elif args.action=='validate':run_validator(args,manifest,cases)
        else:summarize(manifest,cases)
    except Exception:
        print('R3 stopped: inspect classified, secret-safe report; no production changes.',flush=True)
        raise SystemExit(1) from None

if __name__=='__main__':main()
