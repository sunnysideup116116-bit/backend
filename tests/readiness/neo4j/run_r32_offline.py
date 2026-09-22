"""R3.2 independent generation/refinement axes on frozen synthetic development labels."""
from __future__ import annotations
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor,as_completed
import hashlib,json,logging,math,os,random,statistics,threading,time,warnings
from pathlib import Path
from run_r31_reliability import FROZEN,frozen_cases,safe_failure_text
from run_r3_offline import FLAGS,selected_config,write_report,metrics
from r31_diagnostics import classify_response,classify_provider_error
from r32_primary import classify_primary_response

HERE=Path(__file__).resolve().parent
OUT=HERE/'artifacts/r32'

def schedule_r32(cases,phase):
    if phase not in {'generation','development'}:raise ValueError('invalid_phase')
    seeds=(7401,7402) if phase=='generation' else (8401,8402,8403)
    controls=('baseline','none') if phase=='generation' else ('none',)
    jobs=[]
    for repeat,seed in enumerate(seeds,1):
        ordered=list(cases);random.Random(seed).shuffle(ordered)
        for control in controls:
            for size in (2,4):
                for offset in range(0,len(ordered),size):
                    jobs.append({'job_id':f'{phase}-r{repeat}-{control}-s{size}-b{offset//size+1:03}',
                                 'repeat':repeat,'seed':seed,'control':control,'batch_size':size,
                                 'cases':ordered[offset:offset+size]})
    random.Random(93201 if phase=='generation' else 93202).shuffle(jobs)
    return jobs

def check_probe(report):
    if (report.get('status')!='COMPLETED' or report.get('requested_model')!='deepseek-v4.1-flash:cloud'
        or report.get('frozen_hashes')!=FROZEN
        or report.get('control_support',{}).get('status')!='OBSERVED_REASONING_OUTPUT_SUPPRESSION'):
        raise ValueError('reduced_control_not_verified')
    none=[j for j in report['jobs'] if j['control']=='none']
    if len(none)!=4 or any(not j['valid'] or j['reasoning_char_count']!=0 for j in none):
        raise ValueError('probe_evidence_inconsistent')

def experiment_manifest(phase,cases,jobs):
    prompt_name='r3_prompt.txt' if phase=='generation' else 'r32_prompt_v2.txt'
    return {'phase':phase,'dataset_use':'development_not_final_holdout','frozen_v1_hashes':FROZEN,
            'prompt_file':prompt_name,'prompt_sha256':hashlib.sha256((HERE/prompt_name).read_bytes()).hexdigest(),
            'v2_contract_sha256':hashlib.sha256((HERE/'R32_CONTRACT.md').read_bytes()).hexdigest(),
            'human_decisions_confirmed_before_prompt':True,'labels_changed':False,'case_count':len(cases),
            'scheduled_requests':len(jobs),'hard_attempt_budget':2*len(jobs),'workers':3,
            'temperature':0,'max_tokens':4096,'client_timeout_seconds':60,'api_auto_retries':0,
            'effort_transport':'SDK extra_body merges reasoning_effort into top-level wire JSON',
            'retry':'one identical retry on malformed/empty/truncated or transient API error; never valid NO/ABSTAIN',
            'classifier':'frozen_v1' if phase=='generation' else 'v2_primary_authoritative',
            'runtime_flags':dict.fromkeys(FLAGS,'off'),'production_ready':False}

def run(args):
    import httpx
    from openai import OpenAI
    phase=args.action;cases=frozen_cases();jobs=schedule_r32(cases,phase)
    check_probe(json.loads((OUT/'control_probe_wire.json').read_text()))
    manifest=experiment_manifest(phase,cases,jobs)
    path=OUT/(phase+'.json')
    if path.exists():raise ValueError('refusing_to_overwrite_experiment')
    write_report(OUT/(phase+'-manifest.json'),manifest)
    settings=selected_config(args.matchmaker_config,{'LLM_API_KEY','LLM_BASE_URL'})
    model=selected_config(args.model_config,{'LLM_MODEL_ID'}).get('LLM_MODEL_ID')
    key=settings.get('LLM_API_KEY');base=settings.get('LLM_BASE_URL','');del settings
    if not key or base.rstrip('/')!='https://ollama.com/v1' or model!='deepseek-v4.1-flash:cloud':raise ValueError('provider_unconfirmed')
    prompt=(HERE/manifest['prompt_file']).read_text()
    classifier=classify_response if phase=='generation' else classify_primary_response
    report={'status':'RUNNING','manifest':manifest,'model':model,'http_attempts':0,'jobs':[],'graph_access':False}
    write_report(path,report,[key]);lock=threading.Lock();local=threading.local();clients=[]
    def request_gate(request):
        if (request.method!='POST' or request.url.scheme!='https' or request.url.host!='ollama.com'
            or request.url.path!='/v1/chat/completions' or request.url.port not in (None,443)
            or request.url.query or request.url.userinfo):raise ValueError('unexpected_endpoint')
        with lock:
            if report['http_attempts']>=manifest['hard_attempt_budget']:raise ValueError('attempt_budget_exhausted')
            report['http_attempts']+=1
    def client():
        if not hasattr(local,'client'):
            local.client=OpenAI(api_key=key,base_url=base,max_retries=0,timeout=60,
                http_client=httpx.Client(trust_env=False,follow_redirects=False,event_hooks={'request':[request_gate]}))
            with lock:clients.append(local.client)
        return local.client
    def evaluate(job):
        result={k:v for k,v in job.items() if k!='cases'}
        result['ids']=[c['id'] for c in job['cases']];result['attempts']=[]
        payload=[{k:c[k] for k in ('id','Q','C')} for c in job['cases']]
        kwargs={'model':model,'temperature':0,'max_tokens':4096,
                'messages':[{'role':'system','content':prompt},{'role':'user','content':json.dumps({'pairs':payload},ensure_ascii=False)}]}
        if job['control']=='none':kwargs['extra_body']={'reasoning_effort':'none'}
        begin=time.perf_counter()
        for number in (1,2):
            content=None;retryable=False;start=time.perf_counter();record={'attempt':number,'usage':None}
            try:
                response=client().chat.completions.create(**kwargs)
                if response.usage:
                    record['usage']={name:int(getattr(response.usage,name,0) or 0) for name in ('prompt_tokens','completion_tokens','total_tokens')}
                    value=getattr(getattr(response.usage,'completion_tokens_details',None),'reasoning_tokens',None)
                    record['usage']['reasoning_tokens']=value if type(value)is int else None
                try:
                    choice=response.choices[0];content=choice.message.content
                    record.update(classifier(content,choice.finish_reason,set(result['ids'])))
                    reasoning=getattr(choice.message,'reasoning_content',None)
                    if not isinstance(reasoning,str):reasoning=getattr(choice.message,'reasoning',None)
                    record['metadata']['reasoning_char_count']=len(reasoning) if isinstance(reasoning,str) else 0
                    record['metadata']['reasoning_present']=bool(reasoning) if isinstance(reasoning,str) else False
                except Exception:
                    record.update(valid=False,errors=['parser_internal_failure'],decisions={},metadata={})
                retryable=not record['valid'] and 'parser_internal_failure' not in record['errors']
            except Exception as exc:
                info=classify_provider_error(exc)
                record.update(valid=False,errors=['provider_api_failure'],decisions={},metadata=info)
                retryable=info['retryable']
            record['latency_seconds']=time.perf_counter()-start
            if not record['valid']:
                text,retention=safe_failure_text(content,key);record['failure_retention']=retention
                if text is not None:
                    failure_path=OUT/'failed_outputs'/f'{job["job_id"]}-attempt{number}.json'
                    write_report(failure_path,{'synthetic_only':True,'job_id':job['job_id'],'errors':record['errors'],
                                 'metadata':record['metadata'],'model_output':text},[key])
            result['attempts'].append(record)
            if record['valid'] or not retryable or number==2:break
            time.sleep(.5)
        result.update(elapsed_seconds=time.perf_counter()-begin,first_pass_valid=result['attempts'][0]['valid'],eventual_valid=result['attempts'][-1]['valid'])
        return result
    try:
        with ThreadPoolExecutor(max_workers=3) as executor:
            for future in as_completed([executor.submit(evaluate,j) for j in jobs]):
                item=future.result();report['jobs'].append(item);write_report(path,report,[key])
                if len(report['jobs'])%12==0 or not item['first_pass_valid']:
                    print(json.dumps({'phase':phase,'completed':len(report['jobs']),'total':len(jobs),
                                      'condition':f'{item["control"]}-s{item["batch_size"]}','first_valid':item['first_pass_valid'],
                                      'eventual_valid':item['eventual_valid'],'errors':[a['errors'] for a in item['attempts']]}),flush=True)
        report['status']='COMPLETED'
    finally:
        for c in clients:c.close()
        write_report(path,report,[key])

def sample_stats(values):
    if not values:return None
    ordered=sorted(values)
    return {'count':len(values),'median':statistics.median(values),'p95_nearest_rank':ordered[math.ceil(.95*len(ordered))-1],
            'mean':statistics.mean(values),'min':min(values),'max':max(values)}

def safe_metrics(cases,decisions):
    result=metrics(cases,decisions)
    result['non_admitted_non_yes_including_errors']=result['TN']
    result['TN']=sum(c['expected']!='YES' and decisions.get(c['id'],{}).get('decision') in {'NO','ABSTAIN'} for c in cases)
    result['ERROR']=sum(decisions.get(c['id'],{}).get('decision') not in {'YES','NO','ABSTAIN'} for c in cases)
    # Positive ERROR remains a recall miss/FN; it is not a fabricated NO decision.
    return result

def condition_summary(jobs,cases,phase):
    by_id={c['id']:c for c in cases};repeats=sorted({j['repeat'] for j in jobs})
    attempts=[a for j in jobs for a in j['attempts']]
    first={r:{} for r in repeats};final={r:{} for r in repeats}
    for j in jobs:
        if j['first_pass_valid']:first[j['repeat']].update(j['attempts'][0]['decisions'])
        if j['eventual_valid']:final[j['repeat']].update(j['attempts'][-1]['decisions'])
    valid_first=[d for ds in first.values() for d in ds.values()]
    valid_final=[d for ds in final.values() for d in ds.values()]
    first_stats=[safe_metrics(cases,first[r]) for r in repeats];final_stats=[safe_metrics(cases,final[r]) for r in repeats]
    complete=set.intersection(*(set(ds) for ds in final.values()))
    primary_consistent=[i for i in complete if len({final[r][i]['decision'] for r in repeats})==1]
    secondary_consistent=[i for i in complete if len({final[r][i]['relation'] for r in repeats})==1]
    totals={k:sum(m[k] for m in final_stats) for k in ('TP','FP','FN','TN','ERROR')}
    precision=totals['TP']/(totals['TP']+totals['FP']) if totals['TP']+totals['FP'] else None
    recall=totals['TP']/(totals['TP']+totals['FN']) if totals['TP']+totals['FN'] else None
    role_false_yes=sum(d['decision']=='YES' and by_id[d['id']]['relation']=='role_mismatch' for d in valid_final)
    constraint_false_yes=sum(d['decision']=='YES' and by_id[d['id']]['relation']=='constraint_conflict' for d in valid_final)
    result={'first_requests':len(jobs),'total_attempts':len(attempts),'first_valid':sum(j['first_pass_valid'] for j in jobs),
            'eventual_valid':sum(j['eventual_valid'] for j in jobs),'retry_count':len(attempts)-len(jobs),
            'first_pass_rate':sum(j['first_pass_valid'] for j in jobs)/len(jobs),
            'eventual_rate':sum(j['eventual_valid'] for j in jobs)/len(jobs),
            'first_case_coverage':len(valid_first),'final_case_coverage':len(valid_final),'expected_case_outputs':len(cases)*len(repeats),
            'primary_accuracy_valid_first':sum(d['decision']==by_id[d['id']]['expected'] for d in valid_first)/len(valid_first) if valid_first else None,
            'primary_accuracy_valid_final':sum(d['decision']==by_id[d['id']]['expected'] for d in valid_final)/len(valid_final) if valid_final else None,
            'first_per_run':first_stats,'final_per_run':final_stats,'final_pooled_yes':{**totals,'precision':precision,'recall':recall},
            'role_false_yes':role_false_yes,'constraint_false_yes':constraint_false_yes,
            'equivalent_false_no':sum(d['decision']=='NO' and by_id[d['id']]['relation']=='equivalent' for d in valid_final),
            'broad_narrow_errors':sum(d['decision']!=by_id[d['id']]['expected'] and by_id[d['id']]['category']=='broad_narrow' for d in valid_final),
            'unknown_handling':dict(Counter(d['decision'] for d in valid_final if by_id[d['id']]['expected']=='ABSTAIN')),
            'all_repeat_valid_cases':len(complete),'primary_consistent_cases':len(primary_consistent),
            'primary_consistency_all_cases':len(primary_consistent)/len(cases),'secondary_consistent_cases':len(secondary_consistent),
            'unstable_or_error_ids':[c['id'] for c in cases if c['id'] not in primary_consistent],
            'first_latency_seconds':sample_stats([j['attempts'][0]['latency_seconds'] for j in jobs]),
            'eventual_latency_seconds':sample_stats([j['elapsed_seconds'] for j in jobs]),
            'completion_tokens':sample_stats([a['usage']['completion_tokens'] for a in attempts if a['usage']]),
            'total_completion_tokens':sum(a['usage']['completion_tokens'] for a in attempts if a['usage']),
            'finish_reasons':dict(Counter(a['metadata'].get('finish_reason','unknown') for a in attempts)),
            'first_failure_taxonomy':dict(Counter(e for j in jobs for e in j['attempts'][0]['errors'])),
            'first_truncation_rate':sum('truncated_response' in j['attempts'][0]['errors'] for j in jobs)/len(jobs),
            'reasoning_present_count':sum(a['metadata'].get('reasoning_present',False) for a in attempts),
            'secondary_primary_disagreement_count':sum(a['metadata'].get('secondary_primary_disagreement',False) for a in attempts),
            'special_cases':{i:[final[r].get(i,{'decision':'ERROR','relation':'ERROR'}) for r in repeats] for i in ('h02-f','h02-r','h12-f','h12-r')}}
    gates={'first_pass_ge_99':result['first_pass_rate']>=.99,'eventual_ge_999_or_sample_100':result['eventual_rate']>=.999,
           'role_constraint_zero_false_yes':role_false_yes==constraint_false_yes==0,
           'primary_consistency_ge_99':result['primary_consistency_all_cases']>=.99,
           'yes_precision_ge_99':precision is not None and precision>=.99}
    result['proposed_engineering_gates']=gates
    result['all_gates_pass']=all(gates.values())
    result['scope']='development only; errors not NO; all-case consistency counts missing/error as not consistent; pooled repeats not independent'
    return result

def summarize():
    cases=frozen_cases();result={'production_stop':True,'runtime_flags':dict.fromkeys(FLAGS,'off'),'phases':{}}
    for phase in ('generation','development'):
        path=OUT/(phase+'.json')
        if not path.exists():continue
        data=json.loads(path.read_text())
        if data['status']!='COMPLETED':raise ValueError('incomplete_phase')
        current_hash=hashlib.sha256((HERE/data['manifest']['prompt_file']).read_bytes()).hexdigest()
        if current_hash!=data['manifest']['prompt_sha256']:raise ValueError('prompt_changed_after_scoring')
        conditions={}
        for control,size in sorted({(j['control'],j['batch_size']) for j in data['jobs']}):
            conditions[f'{control}-s{size}']=condition_summary([j for j in data['jobs'] if j['control']==control and j['batch_size']==size],cases,phase)
        result['phases'][phase]={'manifest':data['manifest'],'conditions':conditions,'http_attempts':data['http_attempts']}
    write_report(OUT/'summary.json',result)
    print(json.dumps({p:{c:{k:v[k] for k in ('first_pass_rate','eventual_rate','primary_consistency_all_cases','final_pooled_yes','all_gates_pass')} for c,v in d['conditions'].items()} for p,d in result['phases'].items()}))

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['generation','development','summarize'])
    parser.add_argument('--model-config',type=Path);parser.add_argument('--matchmaker-config',type=Path)
    parser.add_argument('--confirm-synthetic-provider-use',action='store_true')
    args=parser.parse_args();warnings.filterwarnings('ignore');logging.disable(logging.CRITICAL)
    if any(os.environ.get(f,'off')!='off' for f in FLAGS):parser.error('semantic flags must stay OFF')
    if args.action=='summarize':summarize();return
    if not args.confirm_synthetic_provider_use or not args.model_config or not args.matchmaker_config:parser.error('explicit provider permission/config required')
    try:run(args)
    except Exception:
        print('R3.2 stopped; inspect secret-safe artifacts. No production changes.',flush=True)
        raise SystemExit(1) from None

if __name__=='__main__':main()
