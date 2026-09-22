"""Approved synthetic-only, in-memory credential/vector calibration. Not runtime code."""
import argparse
import os
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
os.environ['GRPC_VERBOSITY'] = 'NONE'
import warnings
warnings.filterwarnings('ignore')
import logging
logging.disable(logging.CRITICAL)
import ast
import hashlib
import importlib.util
import io
import json
import math
import re
import statistics
import sys
import time
from collections import Counter
from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
sys.path.insert(0, str(HERE))
import run_readiness as harness
REPORT = HERE / 'artifacts/r25-replay-report.json'
FLAGS = harness.FLAGS
report = {'status':'RUNNING', 'production_ready':False,
          'historical_production_fingerprint':'unknown',
          'threshold':0.82, 'runtime_flags':{k:'off' for k in FLAGS},
          'api_failures':{'auth_failure':0,'quota_exhausted':0,'provider_failure':0},
          'api_attempts':0,'api_successes':0,'api_retries':0,
          'started_at':datetime.now(timezone.utc).isoformat()}

def require(value, code):
    if not value:
        raise RuntimeError(code)

def cosine(a,b):
    return math.fsum(x*y for x,y in zip(a,b)) / math.sqrt(math.fsum(x*x for x in a)*math.fsum(x*x for x in b))

def unit(a):
    require(len(a)==768 and all(math.isfinite(x) for x in a),'embedding_contract_invalid')
    norm=math.sqrt(math.fsum(x*x for x in a))
    require(norm>0,'embedding_contract_invalid')
    return [x/norm for x in a],norm

def run():
    require(all(os.environ.get(k,'off')=='off' for k in FLAGS),'runtime_flags_not_off')
    fixture_bytes=(HERE/'fixtures.json').read_bytes()
    fixture=json.loads(fixture_bytes)
    require(fixture['data_classification']=='synthetic_non_user_data','not_synthetic')
    pairs=fixture['semantic_pairs']
    require(len(pairs)==100,'unexpected_fixture_count')
    require(hashlib.sha256(fixture_bytes).hexdigest()=='428214c4cd645b3a5037022e419e480b59e9b0910cabf076d8c6003309b6eeb0','fixture_labels_changed_stop')
    identity=harness.canonical_module()
    texts=sorted({p[side] for p in pairs for side in ('left','right')})
    concepts={text:identity.canonicalize_concept(text) for text in texts}
    labels={text:' '.join(concepts[text].label.split())[:60] for text in texts}
    report.update(dataset_sha256=hashlib.sha256(fixture_bytes).hexdigest(),dataset_version=fixture['fixture_version'],
                  source_commit=harness.command(['git','rev-parse','HEAD']).stdout.strip(),
                  aliases_excluded=True, original_unique_text_count=len(texts))
    # Parse only explicitly approved key assignments, never load_dotenv or service imports.
    from dotenv import dotenv_values
    selected=[]
    model_lines=[]
    with CREDENTIAL_FILE.open() as handle:
        for line in handle:
            name=line.split('=',1)[0].strip().removeprefix('export ').strip()
            if re.fullmatch(r'GOOGLE_API_KEYS[1-6]',name): selected.append(line)
            if name=='GOOGLE_EMBEDDING_MODEL': model_lines.append(line)
    secrets=dotenv_values(stream=io.StringIO(''.join(selected)),interpolate=False)
    keys=list(dict.fromkeys(secrets.get(f'GOOGLE_API_KEYS{i}','').strip() for i in range(1,7) if secrets.get(f'GOOGLE_API_KEYS{i}','').strip()))
    del secrets,selected,line
    require(bool(keys),'credential_pool_empty')
    model=os.environ.get('GOOGLE_EMBEDDING_MODEL') or dotenv_values(stream=io.StringIO(''.join(model_lines)),interpolate=False).get('GOOGLE_EMBEDDING_MODEL')
    require(model=='models/gemini-embedding-2','model_contract_changed_stop')
    prefix='task: sentence similarity | query: '
    inputs=sorted(set(prefix+label[:500] for label in labels.values()))
    report['embedding_contract']={'provider':'Google Gemini Developer API','sdk':'google-generativeai',
        'sdk_version':version('google-generativeai'),'comparison_sdk':'google-genai',
        'comparison_sdk_version':version('google-genai'),'requested_model':model,
        'resolved_model_revision':'unknown','logical_task':'semantic_similarity',
        'api_task_type':'omitted','prefix':prefix,'dimension':768,
        'preprocessing':'deterministic canonical label; collapse whitespace; max60 label/max500 provider chars; prefix',
        'normalization':'finite/nonzero validation then L2 unit normalization in Python float64',
        'transport':'legacy SDK REST; explicit credential, automatic retries disabled',
        'input_records':[{'original':t,'canonical_key':concepts[t].key,'canonical_label':labels[t],
                         'exact_input':prefix+labels[t], 'sha256':hashlib.sha256((prefix+labels[t]).encode()).hexdigest()} for t in texts]}
    import google.generativeai as legacy
    from google.ai import generativelanguage as glm
    from google.api_core.client_options import ClientOptions
    from google import genai
    from google.genai import types
    rejected=set()
    cursor=0
    def api(operation):
        nonlocal cursor
        for attempt in range(len(keys)):
            index=(cursor+attempt)%len(keys)
            if index in rejected: continue
            report['api_attempts']+=1
            started=time.perf_counter()
            try:
                # No SDK log, warning, raw response or exception reaches a file/terminal.
                with open(os.devnull,'w') as sink, redirect_stdout(sink),redirect_stderr(sink):
                    result=operation(keys[index])
                report.setdefault('api_latency_ms',[]).append((time.perf_counter()-started)*1000)
                report['api_successes']+=1
                cursor=index
                return result
            except Exception as exc:
                code=getattr(exc,'code',None)
                if callable(code): code=code()
                try: code=int(code)
                except (ValueError,TypeError): code=0
                category='auth_failure' if code in (401,403) else 'quota_exhausted' if code==429 else 'provider_failure'
                report['api_failures'][category]+=1
                if code in (400,404,422):
                    raise RuntimeError('provider_model_contract_rejected_stop') from None
                rejected.add(index)
                if len(rejected)<len(keys): report['api_retries']+=1
        raise RuntimeError('provider_pool_unavailable') from None
    def metadata(key):
        client=glm.ModelServiceClient(transport='rest',client_options=ClientOptions(api_key=key))
        try:
            result=client.get_model(name=model,timeout=15,retry=None)
            return {'name':result.name,'version':result.version,'supported_methods':list(result.supported_generation_methods)}
        finally: client.transport.close()
    meta=api(metadata)
    require(meta['name']==model and 'embedContent' in meta['supported_methods'],'provider_model_contract_changed_stop')
    report['embedding_contract'].update(resolved_model=meta['name'],resolved_model_revision=meta['version'] or 'unknown')
    def embed(batch,key):
        client=glm.GenerativeServiceClient(transport='rest',client_options=ClientOptions(api_key=key))
        try:
            result=legacy.embed_content(model=model,content=batch,output_dimensionality=768,client=client,
                                        request_options={'timeout':20,'retry':None})
            vectors=result['embedding']
            require(len(vectors)==len(batch),'legacy_batch_contract_changed_stop')
            return vectors
        finally: client.transport.close()
    cache={}
    # Check actual new/old SDK parity with two explicit Content objects, not a flat string list.
    sample=inputs[:2]
    initial=api(lambda key:embed(sample,key))
    cache.update(zip(sample,initial))
    def modern(key):
        with genai.Client(api_key=key,vertexai=False,http_options=types.HttpOptions(timeout=20000,retry_options=types.HttpRetryOptions(attempts=1))) as client:
            response=client.models.embed_content(model=model,
                contents=[types.Content(parts=[types.Part(text=t)]) for t in sample],
                config=types.EmbedContentConfig(output_dimensionality=768))
            return [list(e.values) for e in response.embeddings]
    comparison=api(modern)
    require(len(comparison)==len(sample),'modern_batch_contract_changed_stop')
    parity=[{'cosine':cosine(a,b),'max_abs_delta':max(abs(x-y) for x,y in zip(a,b))} for a,b in zip(initial,comparison)]
    require(all(len(v)==768 for v in comparison) and all(p['cosine']>0.99999 and p['max_abs_delta']<1e-5 for p in parity),'sdk_embedding_contract_mismatch_stop')
    report['sdk_parity']={'passed':True,'vectors':parity,'duplicate_inputs_for_contract_check':len(sample),
        'modern_contents':'one explicit Content per text; flat string list is not the equivalent embedding-2 batch contract'}
    for start in range(2,len(inputs),20):
        batch=inputs[start:start+20]
        cache.update(zip(batch,api(lambda key:embed(batch,key))))
    keys.clear()
    vectors={}
    norms=[]
    for text in texts:
        vectors[text],norm=unit(cache[prefix+labels[text]])
        norms.append(norm)
    report.update(unique_embedded_concept_count=len(inputs),observed_dimensions=[768],provider_vector_norm_range=[min(norms),max(norms)])
    rows=[]
    for p in pairs:
        rows.append({**p,'raw_cosine':cosine(cache[prefix+labels[p['left']]],cache[prefix+labels[p['right']]]),
                     'neo4j_ann_score':None,'ann_rank':None})
    report['pairs']=rows
    report['calibration_split']={'original_r2_ids':[p['id'] for p in pairs if int(p['id'].rsplit('-',1)[1])<=5], 'new_r25_count':75}
    report['label_policy']='Frozen before provider calls; original 25 labels unchanged; 20 borderline excluded from binary metrics'
    # No generated vector is serialized. Verify owned topology before any Graph connection.
    state=harness.read_state()
    report['local_topology']=harness.verify_container(state)
    from neo4j import GraphDatabase
    with harness.local_network_only(state['bolt']):
        with GraphDatabase.driver(f"bolt://127.0.0.1:{state['bolt']}",auth=('neo4j',state['password']),connection_timeout=3,max_transaction_retry_time=0) as driver:
            with driver.session(database='neo4j') as session:
                foreign=session.run("MATCH(n) WHERE coalesce(n.readiness_fixture,'')<>$marker RETURN count(n) AS n",marker=harness.MARKER).single()['n']
                require(foreign==0,'non_fixture_graph_stop')
                # Remove only previous disposable readiness fixtures, never production data.
                session.run('MATCH(n {readiness_fixture:$marker}) DETACH DELETE n',marker=harness.MARKER).consume()
                for ddl in (HERE/'schema.cypher').read_text().split(';'):
                    if ddl.strip(): session.run(ddl).consume()
                nodes=[{'key':concepts[t].key,'label':labels[t],'embedding':vectors[t]} for t in texts]
                require(len({n['key'] for n in nodes})==len(nodes),'fixture_identity_collision_stop')
                session.run('UNWIND $rows AS row CREATE (c:Concept) SET c=row,c.readiness_fixture=$marker,c.embedding_model=$model,c.embedding_task="semantic_similarity"',rows=nodes,marker=harness.MARKER,model=model).consume()
                session.run('UNWIND $keys AS key MATCH(c:Concept {key:key}) UNWIND range(1,21) AS i CREATE(u:User {id:"r2-"+key+"-"+toString(i),readiness_fixture:$marker}) CREATE(u)-[:PREFERS]->(c)',keys=[n['key'] for n in nodes],marker=harness.MARKER).consume()
                session.run('CREATE(u:User {id:"r2-multi",readiness_fixture:$marker}) WITH u MATCH(c:Concept) CREATE(u)-[:PREFERS]->(c)',marker=harness.MARKER).consume()
                session.run('CREATE(u:User {id:"r2-avoid-only",readiness_fixture:$marker}) WITH u MATCH(c:Concept) CREATE(u)-[:AVOIDS]->(c)',marker=harness.MARKER).consume()
                session.run('CALL db.awaitIndexes(30)').consume()
                index=session.run("SHOW VECTOR INDEXES YIELD * WHERE name='concept_embedding_index' RETURN name,state,options,indexProvider,populationPercent").single().data()
                require(index['state']=='ONLINE','index_not_online')
                report['index']=index
                rankings={}
                query='CALL db.index.vector.queryNodes($index,$k,$vector) YIELD node,score RETURN node.key AS key,score ORDER BY score DESC,key ASC'
                timings=[]
                for text in sorted({p['left'] for p in pairs}):
                    started=time.perf_counter()
                    ranking=session.run(query,index='concept_embedding_index',k=32,vector=vectors[text]).data()
                    timings.append((time.perf_counter()-started)*1000)
                    require(len(ranking)<=32,'ann_topk_unbounded')
                    rankings[text]=ranking
                for row in rows:
                    for rank,entry in enumerate(rankings[row['left']],1):
                        if entry['key']==concepts[row['right']].key:
                            row.update(neo4j_ann_score=entry['score'],ann_rank=rank)
                            break
                # Preserve corpus ranking separately. Pairwise threshold measurement must not
                # conflate a missing top-32 neighbor with an unknown or zero similarity.
                for row in rows:
                    row['corpus_ann_score']=row.pop('neo4j_ann_score')
                    row['corpus_ann_rank']=row.pop('ann_rank')
                session.run("CREATE VECTOR INDEX r25_pair_embedding_index IF NOT EXISTS FOR (c:R25Pair) ON (c.embedding) OPTIONS {indexConfig:{`vector.dimensions`:768,`vector.similarity_function`:'cosine'}}").consume()
                session.run('CALL db.awaitIndexes(30)').consume()
                pair_index=session.run("SHOW VECTOR INDEXES YIELD name,state,options,indexProvider WHERE name='r25_pair_embedding_index' RETURN name,state,options,indexProvider").single().data()
                require(pair_index['state']=='ONLINE','pair_index_not_online')
                report['pair_measurement_index']=pair_index
                for row in rows:
                    wanted={concepts[row['left']].key,concepts[row['right']].key}
                    session.run('MATCH(c:R25Pair) REMOVE c:R25Pair').consume()
                    session.run('UNWIND $keys AS key MATCH(c:Concept {key:key}) SET c:R25Pair',keys=sorted(wanted)).consume()
                    for attempt in range(20):
                        pair_hits=session.run(query,index='r25_pair_embedding_index',k=2,vector=vectors[row['left']]).data()
                        if {hit['key'] for hit in pair_hits}==wanted: break
                        time.sleep(.1)
                    require({hit['key'] for hit in pair_hits}==wanted,'pair_index_visibility_failed')
                    row['neo4j_ann_score']=next(hit['score'] for hit in pair_hits if hit['key']==concepts[row['right']].key)
                    row['ann_score_source']='bounded_two_concept_pair_index'
                session.run('MATCH(c:R25Pair) REMOVE c:R25Pair').consume()
                report['ann_rankings']=rankings
                report['ann_latency_ms']=timings
            # Execute source-owned bounded queries without any service import or provider call.
            rt=harness.runtime(state,fixture,nodes)
            checks=[]
            for limits in [(24,8,10,40),(32,12,20,50)]:
                req=rt.graph['PreferenceSemanticCandidateRequest'](requester_user_id='r2-searcher',topic='Board Games',
                     query_embedding=vectors['Board Games'],embedding_model=model,neighbor_limit=limits[0],
                     concept_limit=limits[1],per_concept_limit=limits[2],candidate_limit=limits[3],min_similarity=.82)
                result=rt.graph['preference_semantic_candidates'](req)
                require(result['status']=='success','bounded_source_query_failed')
                candidates=result['candidates']
                require(len(candidates)<=limits[3],'candidate_unbounded')
                checks.append({'limits':limits,'candidate_count':len(candidates),'status':result['status']})
            report['bounded_expansion']=checks
            report['captured_query_kinds']=[r['kind'] for r in rt.capture]
            report['source_sha256']=harness.SOURCE_HASHES
    for row in rows:
        row['accepted_at_0_82']=row['neo4j_ann_score'] is not None and row['neo4j_ann_score']>=.82
    report['distributions']={}
    for cat in sorted({r['label'] for r in rows}):
        group=[r for r in rows if r['label']==cat]
        entry={}
        for field in ('raw_cosine','neo4j_ann_score'):
            vals=[r[field] for r in group if r[field] is not None]
            entry[field]={'count':len(vals),'min':min(vals) if vals else None,'median':statistics.median(vals) if vals else None,
                          'mean':statistics.mean(vals) if vals else None,'max':max(vals) if vals else None}
        entry['accepted_at_0_82']=[r['id'] for r in group if r['accepted_at_0_82']]
        report['distributions'][cat]=entry
    decisive=[r for r in rows if r['label']!='borderline_related']
    def matrix(threshold):
        counts=Counter({'TP':0,'FN':0,'FP':0,'TN':0})
        for r in decisive:
            positive=r['label'] in {'clear_semantic_positive','cross_language_positive'}
            accepted=r['neo4j_ann_score'] is not None and r['neo4j_ann_score']>=threshold
            counts[('TP' if accepted else 'FN') if positive else ('FP' if accepted else 'TN')]+=1
        d=dict(counts)
        d['precision']=d['TP']/(d['TP']+d['FP']) if d['TP']+d['FP'] else None
        d['recall']=d['TP']/(d['TP']+d['FN'])
        return d
    report['confusion_matrix']={'threshold':.82,'borderline_excluded':True,**matrix(.82)}
    report['threshold_observations']=[{'threshold':t,**matrix(t), 'borderline_accepted':sum(r['neo4j_ann_score']>=t for r in rows if r['label']=='borderline_related')} for t in [.82,.90,.91,.92,.93,.94,.95,.96,.97,.98]]
    measured=[r for r in rows if r['neo4j_ann_score'] is not None]
    report['score_consistency']={'measured_pairs':len(measured),'max_abs_ann_minus_shifted_cosine':max(abs(r['neo4j_ann_score']-(1+r['raw_cosine'])/2) for r in measured)}
    report['corpus_top32_coverage']={'pair_count':len(rows),'target_retrieved':sum(r['corpus_ann_score'] is not None for r in rows),'positive_retrieved':sum(r['corpus_ann_score'] is not None for r in rows if r['label'] in {'clear_semantic_positive','cross_language_positive'})}
    report['language_distributions']={lang:{'count':len([r for r in rows if r['language']==lang]),'min':min(r['neo4j_ann_score'] for r in rows if r['language']==lang),'median':statistics.median(r['neo4j_ann_score'] for r in rows if r['language']==lang),'mean':statistics.mean(r['neo4j_ann_score'] for r in rows if r['language']==lang),'max':max(r['neo4j_ann_score'] for r in rows if r['language']==lang)} for lang in sorted({r['language'] for r in rows})}
    report['status']='COMPLETED'
    report['production_ready']=False


def main():
    global CREDENTIAL_FILE
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--credential-file',type=Path,required=True)
    parser.add_argument('--confirm-synthetic-provider-use',action='store_true',required=True)
    args=parser.parse_args()
    CREDENTIAL_FILE=args.credential_file.resolve(strict=True)
    # Credentials are only parsed by run(), never at import time.
    try:
        with open(os.devnull,'w') as sink, redirect_stdout(sink),redirect_stderr(sink):
            run()
    except Exception as exc:
        # Never serialize provider exceptions, request objects, credentials, or vectors.
        report['status']='STOPPED'
        safe={'credential_pool_empty','model_contract_changed_stop','provider_model_contract_rejected_stop',
              'provider_pool_unavailable','provider_model_contract_changed_stop','legacy_batch_contract_changed_stop',
              'modern_batch_contract_changed_stop','sdk_embedding_contract_mismatch_stop','embedding_contract_invalid'}
        report['error_code']=str(exc) if type(exc) is RuntimeError and str(exc) in safe else 'local_calibration_check_failed'
        report['error_type']=type(exc).__name__
    finally:
        # Report fields are allowlisted metadata/statistics; no raw provider object.
        harness.json_file(REPORT,report)
        print(json.dumps({k:report[k] for k in ['status','api_attempts','api_successes','api_retries','api_failures']},ensure_ascii=False))
        if 'error_code' in report: print(report['error_code'])
        print('Report: tests/readiness/neo4j/artifacts/r25-replay-report.json')
    if report['status'] != 'COMPLETED':
        raise SystemExit(1)

if __name__ == '__main__':
    main()
