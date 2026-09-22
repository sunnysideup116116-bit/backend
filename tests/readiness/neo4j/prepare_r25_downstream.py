"""Synthetic Graph + source-owned Social/9001 dry-run; no external model or production IO."""
import argparse
import ast,asyncio,hashlib,io,json,logging,os,sys,time
from pathlib import Path
from types import SimpleNamespace
from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from copy import deepcopy
logging.disable(logging.CRITICAL)
HERE=Path(__file__).resolve().parent
sys.path.insert(0,str(HERE))
import run_readiness as h
import mongomock
from neo4j import GraphDatabase,Query
from pydantic import BaseModel,Field,field_validator
from fastapi import HTTPException

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-synthetic-reset',action='store_true',required=True)
    parser.add_argument('--calibration-report',type=Path,default=HERE/'artifacts/r25-report.json')
    args=parser.parse_args()
    state=h.read_state()
    h.verify_container(state)
    assert all(os.environ.get(f,'off')=='off' for f in h.FLAGS)
    fixture=json.loads((HERE/'fixtures.json').read_text())
    calibration=json.loads(args.calibration_report.read_text())
    assert calibration['status']=='COMPLETED'
    assert calibration['dataset_sha256']==hashlib.sha256((HERE/'fixtures.json').read_bytes()).hexdigest()
    h.MODEL=calibration['embedding_contract']['resolved_model']
    identity=h.canonical_module()
    cases=['adjacent-negative-03','adjacent-negative-05','adjacent-negative-04','borderline-04','hard-negative-03',*[f'positive-{i:02d}' for i in range(1,6)]]
    records=[]
    source=h.ROOT/'matchmaker_agent/matchmaker.py'
    tree=ast.parse(source.read_text())
    cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='MatchmakerAgent')
    init=next(n for n in cls.body if isinstance(n,ast.FunctionDef) and n.name=='__init__')
    system_prompt=next(ast.literal_eval(n.value) for n in init.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Attribute) and t.attr=='system_prompt' for t in n.targets))
    with h.local_network_only(state['bolt']):
     with GraphDatabase.driver(f"bolt://127.0.0.1:{state['bolt']}",auth=('neo4j',state['password']),connection_timeout=3,max_transaction_retry_time=0) as driver:
      with driver.session(database='neo4j') as session:
       assert session.run("MATCH(n) WHERE coalesce(n.readiness_fixture,'')<>$m RETURN count(n) AS n",m=h.MARKER).single()['n']==0
       nodes=session.run('MATCH(c:Concept) RETURN c.key AS key,c.label AS label,c.embedding AS embedding').data()
       # Only disposable synthetic users are replaced; concept identity/vectors retained.
       session.run('MATCH(u:User {readiness_fixture:$m}) DETACH DELETE u',m=h.MARKER).consume()
      rt=h.runtime(state,fixture,nodes)
      mm=h.load_source('matchmaker_agent/matchmaker.py',['MatchmakerAgent','provider_search_context','MatchEvaluationError'],{**rt.common,'asyncio':asyncio,'json':json})
      agent=mm['MatchmakerAgent'].__new__(mm['MatchmakerAgent'])
      agent.system_prompt=system_prompt
      agent.model='not_called_prepare_only'
      llm_payloads=[]
      async def capture(target,candidates,graph_memory,global_heuristics,target_deep_profile,*,search_context):
       messages=agent._match_messages(target,candidates,graph_memory,global_heuristics,target_deep_profile,search_context=search_context)
       llm_payloads.append({'messages':messages,'candidate_count':len(candidates),'target_deep_profile':target_deep_profile})
       return json.dumps({'outcome':'no_suitable_candidate','matches':[]})
      agent.match_async=capture
      localenv={'NEO4J_URI':f"bolt://127.0.0.1:{state['bolt']}",'NEO4J_USERNAME':'neo4j','NEO4J_PASSWORD':state['password'],'NEO4J_DATABASE':'neo4j'}
      pool=ThreadPoolExecutor(max_workers=4)
      api=h.load_source('matchmaker_agent/agent_api.py',['get_user_graph_memory','get_global_rules','MatchRequest'],
         {**rt.common,'json':json,'os':SimpleNamespace(getenv=lambda k,default=None:localenv.get(k,default)),
          'GraphDatabase':GraphDatabase,'Query':Query,'asyncio':asyncio,'partial':partial,'_MATCH_GRAPH_POOL':pool,
          'agent':agent,'HTTPException':HTTPException,'field_validator':field_validator,
          'MatchEvaluationError':mm['MatchEvaluationError'],'safe_search_context':mm['safe_search_context'],
          'provider_search_context':mm['provider_search_context'],'GLOBAL_RULE_LIMIT':2,'GLOBAL_RULE_CHAR_LIMIT':30})
      endpoint_ast=next(n for n in ast.parse((h.ROOT/'matchmaker_agent/agent_api.py').read_text()).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_evaluate_match_request')
      endpoint_ast.decorator_list=[]
      exec(compile(ast.fix_missing_locations(ast.Module(body=[endpoint_ast],type_ignores=[])),'source_owned_evaluate_match_request','exec'),api)
      api['MatchRequest'].model_rebuild()
      for pairid in cases:
       p=next(p for p in fixture['semantic_pairs'] if p['id']==pairid)
       left,right=identity.canonicalize_concept(p['left']),identity.canonicalize_concept(p['right'])
       for variant in (('baseline',) if pairid.startswith('positive-') else ('baseline','explicit_saved_avoid','missing_profile')):
        with driver.session(database='neo4j') as session:
         session.run('MATCH(u:User {readiness_fixture:$m}) DETACH DELETE u',m=h.MARKER).consume()
         session.run('CREATE(o:User {id:"r25-owner",readiness_fixture:$m}),(u:User {id:"r25-candidate",readiness_fixture:$m}) WITH o,u MATCH(a:Concept {key:$left}),(b:Concept {key:$right}) CREATE(o)-[:PREFERS]->(a) CREATE(u)-[:PREFERS]->(b)',m=h.MARKER,left=left.key,right=right.key).consume()
         if variant=='explicit_saved_avoid':
          session.run('MATCH(o:User {id:"r25-owner"}),(b:Concept {key:$key}) CREATE(o)-[:AVOIDS]->(b)',key=right.key).consume()
        db=mongomock.MongoClient()['r25']
        owner={'user_id':'r25-owner','test_match_cohort':'r25','current_context':'','deep_profile':{}}
        candidate={'user_id':'r25-candidate','test_match_cohort':'r25','current_context':'','deep_profile':{}}
        db.profiles.insert_one(deepcopy(owner))
        if variant!='missing_profile':db.profiles.insert_one(deepcopy(candidate))
        before=list(db.profiles.find())
        def stances(user):
         with driver.session(database='neo4j') as s:
          rows=s.run('MATCH(:User {id:$id})-[r:PREFERS|AVOIDS]->(c:Concept) RETURN type(r) AS relation,c.key AS key LIMIT 20',id=user)
          result={}
          for r in rows:result.setdefault(r['key'],set()).add('like' if r['relation']=='PREFERS' else 'avoid')
          return result
        base={**rt.common,**{n:rt.context[n] for n in ('safe_search_context','search_context_for_turn','provider_search_context','context_embedding_source_hash')},
              **{n:rt.projection[n] for n in ('recent_context_is_active','without_expired_recent_context','safe_recent_context')},
              'profiles_coll':db.profiles,'matches_coll':db.matches,'MatchRequest':lambda **kw:SimpleNamespace(**kw),
              'semantic_config':rt.semantic['semantic_config'],'preference_semantic_mode':lambda:'active',
              'semantic_embedding_space_confirmed':lambda:True,'qualified_exact_trigger_threshold':rt.semantic['qualified_exact_trigger_threshold'],
              'retrieve_preference_candidate_ids':rt.exact['retrieve_preference_candidate_ids'],
              'retrieve_semantic_preference_candidates':rt.semantic['retrieve_semantic_preference_candidates'],
              'PreferenceCandidateLookupError':rt.exact['PreferenceCandidateLookupError'],'PreferenceSemanticRetrievalError':rt.semantic['PreferenceSemanticRetrievalError'],
              'MatchSearchPipelineError':RuntimeError,'RiskBlockServiceUnavailable':RuntimeError,'INVITE_ON_MATCH':'invite_on_match',
              'participant_pair_key':lambda a,b:'|'.join(sorted([a,b])),'has_verified_acceptance':lambda row:True,
              '_trait_stances':stances,'risk_block_service':SimpleNamespace(excluded_user_ids=lambda user:set())}
        router=h.load_source('social/routers/match.py',['generate_matches_for_user','candidate_qualification'],base)
        captured=[]
        def selection(payload,*,timeout):
         captured.append(deepcopy(payload))
         result=asyncio.run(api['_evaluate_match_request'](api['MatchRequest'](**payload)))
         assert result['outcome']=='no_suitable_candidate'
         return []  # Capture boundary only; NEVER report this stub as an LLM rejection.
        router['_request_matchmaker_selection']=selection
        progress=[]
        def stop_before_write(step):
         progress.append(step)
         return step!='proposal_write'
        offset=len(llm_payloads)
        context={'search_intent':'preference','normalized_topic':left.label,'canonical_preference_key':left.key,'query_text':f'幫我找喜歡{p["left"]}的人'}
        with redirect_stdout(io.StringIO()):
         result=router['generate_matches_for_user']('r25-owner',source='automatic',search_context=context,report_progress=stop_before_write,can_commit=lambda:False)
        assert list(db.profiles.find())==before and db.matches.count_documents({})==0
        diag=result['diagnostics']
        assert diag['semantic_fallback_triggered'] and diag['qualified_exact_count']==0
        record={'fixture_id':pairid,'variant':variant,'left':p['left'],'right':p['right'],'fixture_label':p['label'],
                'retrieval_source':diag['retrieval_source'],'candidate_pool_count':diag.get('candidate_pool_count'),
                'qualified_count':diag.get('candidate_count_after_filter'),'qualification_reason_codes':diag['qualification_reason_codes'],
                'hard_conflict_keys':diag['hard_conflicts'],'reached_matchmaker':len(llm_payloads)>offset,
                'matchmaker_outcome':'NOT_EVALUATED_CAPTURE_ONLY','write_count':0,'progress':progress}
        if len(llm_payloads)>offset:
         record['social_payload']=captured[0]
         record['llm_input']=llm_payloads[offset]
        if variant=='baseline':assert record['reached_matchmaker']
        if variant=='explicit_saved_avoid':assert not record['reached_matchmaker'] and right.key in diag['hard_conflicts']
        if variant=='missing_profile':assert not record['reached_matchmaker'] and diag['candidate_pool_count']==0
        records.append(record)
      pool.shutdown(wait=True)
    report={'status':'PREPARED','live_matchmaker_executed':False,'data_classification':'synthetic_non_user_data',
            'source_sha256':h.SOURCE_HASHES,'fixture_sha256':hashlib.sha256((HERE/'fixtures.json').read_bytes()).hexdigest(),
            'cases':records,'scope':'Source-owned graph retrieval, Social filtering/qualification/payload and 9001 graph enrichment/prompt; in-memory Mongo and empty Risk block fixture; provider capture only; no proposal/quota writes',
            'runtime_flags':{f:'off' for f in h.FLAGS}}
    h.json_file(HERE/'artifacts/r25-downstream-replay.json',report)
    print(json.dumps({'cases':len(records),'baseline_reaches_matchmaker':sum(r['variant']=='baseline' and r['reached_matchmaker'] for r in records),
     'hard_conflict_rejected':sum(r['variant']=='explicit_saved_avoid' and not r['reached_matchmaker'] for r in records),
     'profile_missing_rejected':sum(r['variant']=='missing_profile' and not r['reached_matchmaker'] for r in records),'live_llm':'NOT_RUN'},ensure_ascii=False))


if __name__ == '__main__':
    main()
