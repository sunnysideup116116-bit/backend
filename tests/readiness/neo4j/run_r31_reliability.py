"""R3.1 development-set reliability experiments. No Graph or production runtime IO."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import logging
import os
from pathlib import Path
import random
import re
import statistics
import threading
import time
import warnings

from run_r3_offline import ALLOWED, FLAGS, expand_cases, selected_config, write_report
from r31_diagnostics import classify_response, classify_provider_error

HERE=Path(__file__).resolve().parent
OUT=HERE/'artifacts/r31'
FROZEN={
    'r3_holdout.json':'0943c22f0f3aa4e9091644f811161ca8342fa00838359f2ded9239cf303b70e7',
    'r3_prompt.txt':'97e12b04f4295399e9ea555c2cf4de81833ca13fccb51f9f46882d9ad47fce9d',
    'R3_DIRECTIONAL_CONTRACT.md':'2764daabe42f3a373e68b7c3ddac348f3a5c39ea8c083354ff3dfa21d9511bfd',
}

def frozen_cases():
    for filename,sha in FROZEN.items():
        if hashlib.sha256((HERE/filename).read_bytes()).hexdigest()!=sha:raise ValueError('frozen_input_changed')
    cases=expand_cases(json.loads((HERE/'r3_holdout.json').read_text()))
    # Match the original R3 canonicalization without loading any service/config.
    import run_readiness as h
    identity=h.canonical_module()
    for c in cases:
        c['Q']=identity.canonicalize_concept(c['Q']).label
        c['C']=identity.canonicalize_concept(c['C']).label
    return cases

def schedule(cases,action):
    by_id={c['id']:c for c in cases}
    jobs=[]
    if action=='replay':
        old=json.loads((HERE/'artifacts/r3-validator.json').read_text())
        failures=[(run['repeat'],i+1,b['ids']) for run in old['runs'] for i,b in enumerate(run['batches']) if b['error']]
        if len(failures)!=2:raise ValueError('unexpected_original_failure_count')
        for repeat in (1,2):
            for old_repeat,old_batch,ids in failures:
                jobs.append({'job_id':f'replay-r{repeat}-old{old_repeat}b{old_batch}','repeat':repeat,'batch_size':8,
                             'original_repeat':old_repeat,'original_batch':old_batch,'cases':[by_id[i] for i in ids]})
    else:
        for repeat,seed in enumerate((7303,7304),1):
            ordered=list(cases);random.Random(seed).shuffle(ordered)
            for size in (1,2,4,8):
                for offset in range(0,len(cases),size):
                    jobs.append({'job_id':f'matrix-r{repeat}-s{size}-b{offset//size+1:03}',
                                 'repeat':repeat,'shuffle_seed':seed,'batch_size':size,'cases':ordered[offset:offset+size]})
        # Interleave conditions to reduce time/load confounding. Requests remain stateless.
        random.Random(93131).shuffle(jobs)
    return jobs

def safe_failure_text(content,key):
    if not isinstance(content,str):return None,'non_text'
    fragments=[key]+([key[:6],key[-4:]] if len(key)>10 else [])
    if any(part and part in content for part in fragments):return None,'secret_material_blocked'
    if re.search(r'AIza[\w-]{20,}|(?:api[_-]?key|authorization|bearer)[\s\"\x27:=]+[\w./-]{12,}',content,re.I):
        return None,'credential_like_material_blocked'
    if len(content)>65536:return None,'oversize_output_not_retained'
    return content,'synthetic_only_secret_checked'

def run(args):
    import httpx
    from openai import OpenAI
    cases=frozen_cases();jobs=schedule(cases,args.action)
    settings=selected_config(args.matchmaker_config,{'LLM_API_KEY','LLM_BASE_URL'})
    model=selected_config(args.model_config,{'LLM_MODEL_ID'}).get('LLM_MODEL_ID')
    key=settings.get('LLM_API_KEY');base=settings.get('LLM_BASE_URL','');del settings
    if not key or base.rstrip('/')!='https://ollama.com/v1' or model!='deepseek-v4.1-flash:cloud':raise ValueError('provider_config_unconfirmed')
    prompt=(HERE/'r3_prompt.txt').read_text()
    path=OUT/(args.action+'.json')
    if path.exists():raise ValueError('refusing_to_overwrite_experiment')
    report={'status':'RUNNING','frozen_commit':'60d3142b063793039cc7dc88fc6fc1736caba982','frozen_hashes':FROZEN,
            'dataset_use':'development_not_final_validation','model':model,'provider':'Ollama Cloud',
            'temperature':0,'max_tokens':4096,'timeout_seconds':60,'workers':3,
            'retry_policy':'at most one identical retry for schema/transient API failures; NEVER valid NO/ABSTAIN',
            'structured_output':'UNSUPPORTED_BY_OLLAMA_CLOUD_NOT_RUN','response_format':'not_supplied_plain_prompt',
            'scheduled_requests':len(jobs),'hard_request_budget':2*len(jobs),'http_attempts':0,'jobs':[],
            'runtime_flags':dict.fromkeys(FLAGS,'off'),'graph_access':False,'production_ready':False}
    write_report(path,report,[key])
    lock=threading.Lock();clients=[];local=threading.local()
    def request_gate(request):
        if request.url.scheme!='https' or request.url.host!='ollama.com' or request.url.path!='/v1/chat/completions':raise ValueError('unexpected_endpoint')
        with lock:
            if report['http_attempts']>=report['hard_request_budget']:raise ValueError('request_budget_exhausted')
            report['http_attempts']+=1
    def get_client():
        if not hasattr(local,'client'):
            local.client=OpenAI(api_key=key,base_url=base,max_retries=0,timeout=60,
                  http_client=httpx.Client(trust_env=False,follow_redirects=False,event_hooks={'request':[request_gate]}))
            with lock:clients.append(local.client)
        return local.client
    def evaluate(job):
        payload=[{k:c[k] for k in ('id','Q','C')} for c in job['cases']]
        result={k:v for k,v in job.items() if k!='cases'}
        result['ids']=[c['id'] for c in job['cases']]
        result['attempts']=[]
        started=time.perf_counter()
        for attempt in (1,2):
            content=None;retryable=False;begin=time.perf_counter()
            record={'attempt':attempt,'usage':None}
            try:
                response=get_client().chat.completions.create(model=model,temperature=0,max_tokens=4096,
                    messages=[{'role':'system','content':prompt},{'role':'user','content':json.dumps({'pairs':payload},ensure_ascii=False)}])
                if response.usage:
                    record['usage']={name:int(getattr(response.usage,name,0) or 0) for name in ('prompt_tokens','completion_tokens','total_tokens')}
                    details=getattr(response.usage,'completion_tokens_details',None)
                    reasoning_tokens=getattr(details,'reasoning_tokens',None)
                    record['usage']['reasoning_tokens']=reasoning_tokens if type(reasoning_tokens) is int else None
                try:
                    choice=response.choices[0];content=choice.message.content
                    record.update(classify_response(content,choice.finish_reason,set(result['ids'])))
                    reasoning=getattr(choice.message,'reasoning_content',None)
                    if not isinstance(reasoning,str):reasoning=getattr(choice.message,'reasoning',None)
                    # Count only; never retain or display reasoning text.
                    record['metadata']['reasoning_text_present']=isinstance(reasoning,str) and bool(reasoning)
                    record['metadata']['reasoning_char_count']=len(reasoning) if isinstance(reasoning,str) else None
                except Exception:
                    record.update(valid=False,errors=['parser_internal_failure'],decisions={},metadata={})
                retryable=not record['valid'] and 'parser_internal_failure' not in record['errors']
            except Exception as exc:
                info=classify_provider_error(exc)
                record.update(valid=False,errors=['provider_api_failure'],decisions={},metadata=info)
                retryable=info['retryable']
            record['latency_seconds']=time.perf_counter()-begin
            if not record['valid']:
                safe,retention=safe_failure_text(content,key)
                record['failure_output_retention']=retention
                if safe is not None:
                    target=OUT/'failed_outputs'/f"{job['job_id']}-attempt{attempt}.json"
                    write_report(target,{'data_classification':'synthetic_r3_model_output','job_id':job['job_id'],
                                 'ids':result['ids'],'attempt':attempt,'errors':record['errors'],
                                 'metadata':record['metadata'],'model_output':safe},[key])
                    record['failure_output_file']=str(target.relative_to(OUT))
            result['attempts'].append(record)
            if record['valid'] or not retryable or attempt==2:break
            time.sleep(.5)
        result['elapsed_seconds']=time.perf_counter()-started
        result['first_pass_valid']=result['attempts'][0]['valid']
        result['eventual_valid']=result['attempts'][-1]['valid']
        return result
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures=[executor.submit(evaluate,j) for j in jobs]
            for future in as_completed(futures):
                result=future.result();report['jobs'].append(result)
                write_report(path,report,[key])
                if args.action=='replay' or len(report['jobs'])%12==0 or not result['first_pass_valid']:
                    print(json.dumps({'completed':len(report['jobs']),'total':len(jobs),'job_id':result['job_id'],
                         'first_pass_valid':result['first_pass_valid'],'eventual_valid':result['eventual_valid'],
                         'errors':[a['errors'] for a in result['attempts']]}),flush=True)
        report['status']='COMPLETED'
    finally:
        for client in clients:client.close()
        write_report(path,report,[key])

def distribution(values):
    return {'count':len(values),'min':min(values),'median':statistics.median(values),'mean':statistics.mean(values),'max':max(values)} if values else None

def summarize():
    cases=frozen_cases();by_id={c['id']:c for c in cases}
    raw=json.loads((OUT/'matrix.json').read_text())
    if raw['status']!='COMPLETED':raise ValueError('matrix_incomplete')
    result={'dataset_use':'development','matrix':{},'consistency':{},'failure_taxonomy':{},
            'structured_output':'UNSUPPORTED_NOT_RUN','root_cause_limit':'Original R3 failure bodies were not retained; reproduction is evidence of failure mechanisms, not proof of the exact historical response.',
            'prompt_contract_labels_unchanged':True}
    first_errors=Counter();all_errors=Counter()
    for size in (1,2,4,8):
        jobs=[j for j in raw['jobs'] if j['batch_size']==size]
        first_valid=[j for j in jobs if j['first_pass_valid']]
        eventual=[j for j in jobs if j['eventual_valid']]
        first_decisions=[d for j in first_valid for d in j['attempts'][0]['decisions'].values()]
        final_decisions=[d for j in eventual for d in j['attempts'][-1]['decisions'].values()]
        actual=len(jobs);attempts=[a for j in jobs for a in j['attempts']]
        for j in jobs:first_errors.update(j['attempts'][0]['errors'])
        for a in attempts:all_errors.update(a['errors'])
        result['matrix'][str(size)]={
            'first_requests':actual,'total_requests':len(attempts),'first_valid':len(first_valid),
            'eventual_valid':len(eventual),'first_pass_rate':len(first_valid)/actual,'eventual_rate':len(eventual)/actual,
            'retry_count':sum(len(j['attempts'])==2 for j in jobs),
            'recovered':sum(not j['first_pass_valid'] and j['eventual_valid'] for j in jobs),
            'valid_first_case_outputs':len(first_decisions),'valid_final_case_outputs':len(final_decisions),
            'primary_accuracy_valid_first':sum(d['decision']==by_id[d['id']]['expected'] for d in first_decisions)/len(first_decisions) if first_decisions else None,
            'secondary_accuracy_valid_first':sum(d['relation']==by_id[d['id']]['relation'] for d in first_decisions)/len(first_decisions) if first_decisions else None,
            'primary_accuracy_valid_final':sum(d['decision']==by_id[d['id']]['expected'] for d in final_decisions)/len(final_decisions) if final_decisions else None,
            'latency_first_seconds':distribution([j['attempts'][0]['latency_seconds'] for j in jobs]),
            'latency_eventual_seconds':distribution([j['elapsed_seconds'] for j in jobs]),
            'output_tokens':distribution([a['usage']['completion_tokens'] for a in attempts if a['usage']]),
            'total_output_tokens':sum(a['usage']['completion_tokens'] for a in attempts if a['usage']),
            'total_input_tokens':sum(a['usage']['prompt_tokens'] for a in attempts if a['usage'])}
        per_run={n:{} for n in (1,2)}
        for j in jobs:
            if j['first_pass_valid']:per_run[j['repeat']].update(j['attempts'][0]['decisions'])
        common=set(per_run[1])&set(per_run[2])
        bycat={}
        for category in sorted({c['category'] for c in cases}):
            group=[i for i in common if by_id[i]['category']==category]
            bycat[category]={'both_valid':len(group),'primary_same':sum(per_run[1][i]['decision']==per_run[2][i]['decision'] for i in group),
                             'secondary_same':sum(per_run[1][i]['relation']==per_run[2][i]['relation'] for i in group)}
        result['consistency'][str(size)]={'both_runs_valid':len(common),'primary_same':sum(per_run[1][i]['decision']==per_run[2][i]['decision'] for i in common),
                 'secondary_same':sum(per_run[1][i]['relation']==per_run[2][i]['relation'] for i in common),'categories':bycat,
                 'special_cases':{i:[per_run[n].get(i,{'decision':'ERROR','relation':'ERROR'}) for n in (1,2)] for i in ('h02-f','h02-r','h12-f','h12-r')}}
    result['failure_taxonomy']={'first_attempts':dict(first_errors),'all_attempts':dict(all_errors)}
    result['http_attempts']=raw['http_attempts']
    write_report(OUT/'summary.json',result)
    print(json.dumps({'matrix':result['matrix'],'failure_taxonomy':result['failure_taxonomy']},ensure_ascii=False))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['replay','matrix','summarize'])
    parser.add_argument('--model-config',type=Path)
    parser.add_argument('--matchmaker-config',type=Path)
    parser.add_argument('--confirm-synthetic-provider-use',action='store_true')
    args=parser.parse_args()
    if any(os.environ.get(f,'off')!='off' for f in FLAGS):parser.error('semantic flags must remain OFF')
    warnings.filterwarnings('ignore');logging.disable(logging.CRITICAL)
    if args.action=='summarize':summarize();return
    if not args.model_config or not args.matchmaker_config or not args.confirm_synthetic_provider_use:parser.error('explicit provider config and permission required')
    try:run(args)
    except Exception:
        print('R3.1 stopped; inspect secret-safe classified artifacts. No production changes.',flush=True)
        raise SystemExit(1) from None

if __name__=='__main__':main()
