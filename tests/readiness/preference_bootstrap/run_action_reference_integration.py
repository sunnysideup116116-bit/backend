"""Opt-in action-reference release gate against verified disposable Graph/Mongo.

No production env, account, services or providers. Reuses the reviewed local-only
harness boundary; leaves synthetic data in stopped disposable volumes for audit.
"""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import logging
import os
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT),str(ROOT/'social'),str(ROOT/'matchmaker_agent'),str(ROOT/'tests/readiness/neo4j')]
import run_readiness as local_graph
import local_mongo
from run_runtime_integration import local_only


def main():
    logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)
    state=local_graph.read_state();local_graph.verify_container(state)
    mongo=local_mongo.verify()
    os.environ.update(AYUE_SKIP_DOTENV='1',DOTENV_DISABLED='1',
        NEO4J_URI=f"bolt://127.0.0.1:{state['bolt']}",NEO4J_USERNAME='neo4j',NEO4J_PASSWORD=state['password'],
        NEO4J_DATABASE='neo4j',MONGO_DB_NAME='action_reference_fixture',
        MONGO_URI=f"mongodb://127.0.0.1:{mongo['port']}/?directConnection=true&replicaSet=bootstrap_test",
        APPWRITE_API_KEY='synthetic-action-reference-signing-only',APPWRITE_PROJECT_ID='synthetic-project',
        LLM_API_KEY='synthetic-unused',LLM_BASE_URL='http://provider.invalid/v1',LLM_MODEL_ID='synthetic-unused',
        PREFERENCE_BOOTSTRAP_ENABLED='off',PREFERENCE_EMBEDDING_V2_ENABLED='off',
        MATCH_PREFERENCE_SEMANTIC_MODE='off',MATCH_RELATED_INTEREST_ENABLED='off',
        MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED='off',DEMO_DESTRUCTIVE_TOOLS_ENABLED='off')
    import dotenv
    dotenv.load_dotenv=lambda *_a,**_k:False
    with local_only({state['bolt'],mongo['port']}):
        import matchmaker
        with patch.object(matchmaker,'MatchmakerAgent',return_value=SimpleNamespace(model='synthetic-unused',client=None)):
            import agent_api
        from neo4j import GraphDatabase
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from database import db
        from routers import system,preference_bootstrap as routes
        from services import memory_service as memory,preference_bootstrap_service as social
        from services import preference_action_projection as projection,semantic_user_eligibility as eligibility
        from matchmaker_agent.preference_bootstrap import BootstrapGraph,snapshot
        from matchmaker_agent.preference_write_fence import lock_preferences,bump_preferences
        from matchmaker_agent.concept_identity import canonicalize_concept
        from matchmaker_agent.preference_action_reference import valid_format
        import requests

        assert db.name=='action_reference_fixture'
        db.client.admin.command('ping')
        run='action_gate_'+uuid.uuid4().hex[:12]
        allowed=set();results=[]
        app=FastAPI();app.include_router(system.router);app.include_router(routes.router)
        client,internal=TestClient(app),TestClient(agent_api.app)
        lost_ack={'next':False}

        def bridge(url,**kwargs):
            assert url.startswith('http://127.0.0.1:9001/api/')
            response=internal.post(url.removeprefix('http://127.0.0.1:9001'),json=kwargs['json'],headers=kwargs.get('headers',{}))
            if url.endswith('/memory/action') and lost_ack['next']:
                lost_ack['next']=False
                assert response.json()['status']=='success'
                raise requests.Timeout('synthetic lost acknowledgement')
            return response

        def bridge_get(url,**kwargs):
            assert url.startswith('http://127.0.0.1:9001/api/memory/')
            return internal.get(url.removeprefix('http://127.0.0.1:9001'),params=kwargs.get('params',{}))

        def owner_auth(header):
            owner=(header or '').removeprefix('Bearer ')
            if owner not in allowed:
                from services.appwrite_identity_service import AppwriteIdentityError
                raise AppwriteIdentityError('owner_auth_required',401)
            return owner

        with GraphDatabase.driver(os.environ['NEO4J_URI'],auth=('neo4j',state['password']),max_transaction_retry_time=0) as driver:
            graph=BootstrapGraph(driver)
            with driver.session() as session:
                for label,field in [('User','id'),('Concept','key')]:
                    session.run(f'CREATE CONSTRAINT action_gate_{label.lower()} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{field} IS UNIQUE').consume()

            def new_owner(name,count=1):
                owner=run+'_'+name;allowed.add(owner)
                db.profiles.insert_one({'user_id':owner,'nickname':'Synthetic only'})
                with driver.session() as s:
                    s.run('CREATE (:User {id:$owner,preference_revision:0})',owner=owner).consume()
                    for i in range(count):
                        identity=canonicalize_concept(f'Action reference reading collection {i}')
                        relation='PREFERS' if i%2==0 else 'AVOIDS'
                        s.run(f'''MATCH (u:User {{id:$owner}})
                            MERGE (c:Concept {{key:$key}}) ON CREATE SET c += $identity
                            CREATE (u)-[:{relation} {{active:true}}]->(c)''',owner=owner,key=identity.key,identity=identity.as_dict()).consume()
                        db.preference_facts.insert_one({'user_id':owner,'concept_key':identity.key,'active':True,
                            'stance':'like' if relation=='PREFERS' else 'avoid','evidence_count':7})
                return owner

            def source(owner):
                r=client.get('/api/profile/preferences/bootstrap/source',headers={'Authorization':'Bearer '+owner})
                assert r.status_code==200,r.json()
                return r.json()

            def submit(owner,key,kind='correct',value='Reading adventure fiction'):
                return client.post('/api/profile/memories/action',headers={'Authorization':'Bearer '+owner},
                    json={'user_id':owner,'key':key,'action':kind,'value':value})

            def all_state(owner):
                with driver.session() as s:
                    users=[dict(r) for r in s.run('MATCH (u:User {id:$owner}) RETURN properties(u) AS p',owner=owner)]
                    edges=[dict(r) for r in s.run('''MATCH (:User {id:$owner})-[r]->(c)
                        RETURN elementId(r) AS id,type(r) AS relation,properties(r) AS p,properties(c) AS c ORDER BY id''',owner=owner)]
                mongo_state={name:list(db[name].find({'$or':[{'user_id':owner},{'owner':owner}]}).sort('_id',1))
                             for name in db.list_collection_names()}
                return deepcopy((users,edges,mongo_state))

            def stale(owner,key,kind='correct'):
                before=all_state(owner)
                r=submit(owner,key,kind)
                assert r.status_code==409 and r.json()['detail']['code']=='stale_source',r.json()
                assert all_state(owner)==before,'stale action left Graph/Mongo effects'

            def mark(name):
                results.append(name);print(json.dumps({'case':name,'status':'PASS'}),flush=True)

            with patch.object(requests,'post',bridge),patch.object(requests,'get',bridge_get),patch.object(routes,'authenticate_owner',owner_auth), \
                 patch.object(eligibility,'lookup_enabled_account',lambda owner,**_k:owner in allowed):
                # Signed private recovery scan cannot be called without service auth.
                assert internal.post('/api/v2/preferences/bootstrap/action-projection-pending',json={'limit':16}).status_code==403
                for kind in ['correct','disable']:
                    owner=new_owner('active_'+kind);ref=source(owner)['items'][0]['key']
                    assert valid_format(ref)
                    response=submit(owner,ref,kind);assert response.status_code==200,response.json()
                    assert set(response.json())=={'status','projection_status'}
                    assert response.json()['projection_status']=='synced'
                    stale(owner,ref,kind)
                mark('active_correct_disable_success_then_consumed_refs_reject')

                owner=new_owner('disabled');ref=source(owner)['items'][0]['key']
                canonical=graph.source(owner)['rows'][0]['concept']['key']
                assert submit(owner,ref,'disable').status_code==200
                for kind in ['correct','disable']:stale(owner,ref,kind)
                restored=asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
                    user_id=owner,key=canonical,action='restore',source_created_at=time.time())))
                assert restored['status']=='success'
                for kind in ['correct','disable']:stale(owner,ref,kind)
                mark('disabled_and_restored_same_identity_old_refs_reject')

                owner=new_owner('polarity');ref=source(owner)['items'][0]['key']
                def change(tx):
                    lock_preferences(tx,owner,source_created_at=time.time(),require_existing=True)
                    tx.run('''MATCH (u:User {id:$owner})-[r:PREFERS]->(c)
                        DELETE r CREATE (u)-[:AVOIDS {active:true}]->(c)''',owner=owner).consume()
                    bump_preferences(tx,owner)
                with driver.session() as s:s.execute_write(change)
                # Facts are intentionally not repaired: stale action must not
                # project or silently fix this test-created inconsistent state.
                for kind in ['correct','disable']:stale(owner,ref,kind)
                mark('polarity_changed_stale_correct_disable_zero_graph_mongo_writes')

                owner=new_owner('revision');ref=source(owner)['items'][0]['key']
                with driver.session() as s:s.execute_write(lambda tx:bump_preferences(tx,owner))
                stale(owner,ref);mark('revision_changed_reference_rejects')

                owner=new_owner('owner');other=new_owner('other');ref=source(owner)['items'][0]['key']
                stale(other,ref);stale(owner,ref[:-1]+('A' if ref[-1]!='A' else 'B'))
                stale(owner,graph.source(owner)['rows'][0]['concept']['key'])
                other_before=all_state(other)
                assert submit(owner,ref).status_code==200
                assert all_state(other)==other_before
                mark('other_owner_tamper_raw_keys_reject_shared_concept_preserved')

                owner=new_owner('concurrent');old=source(owner)['items'][0]['key']
                with driver.session() as s:s.execute_write(lambda tx:bump_preferences(tx,owner))
                current=source(owner)['items'][0]['key'];barrier=threading.Barrier(3)
                def race(ref):
                    barrier.wait();return submit(owner,ref,'disable').status_code
                with ThreadPoolExecutor(max_workers=3) as pool:outcomes=list(pool.map(race,[old,current,current]))
                assert outcomes[0]==409 and sorted(outcomes)==[200,409,409],outcomes
                mark('real_owner_lock_concurrent_stale_current_single_success')

                owner=new_owner('rollback');ref=source(owner)['items'][0]['key'];before=all_state(owner)
                with patch.object(agent_api,'enqueue_keys',side_effect=RuntimeError('synthetic rollback')):
                    r=submit(owner,ref)
                assert r.status_code==503 and all_state(owner)==before
                mark('failure_after_graph_write_rolls_back_no_mongo_projection')

                owner=new_owner('lost_ack');ref=source(owner)['items'][0]['key'];lost_ack['next']=True
                assert submit(owner,ref,'disable').status_code==503
                assert db.preference_action_projection_jobs.count_documents({'owner':owner})==0
                projection.recover_committed()
                assert db.preference_facts.find_one({'user_id':owner})['active'] is False
                with driver.session() as s:
                    assert s.run('MATCH (u:User {id:$owner}) RETURN u.preference_action_projection_keys AS keys',owner=owner).single()['keys']==[]
                stale(owner,ref);mark('lost_ack_graph_marker_recovers_projection_without_mutation_replay')

                owner=new_owner('many',64);full=source(owner)
                assert len(full['items'])==64 and len(set(i['key'] for i in full['items']))==64
                for number,kind in [(13,'correct'),(30,'disable'),(63,'correct')]:
                    fresh=source(owner);item=next(i for i in fresh['items'] if i['text']==f'Action reference reading collection {number}')
                    r=submit(owner,item['key'],kind,value=f'Action reference revised reading collection {number}')
                    assert r.status_code==200,r.json()
                    stale(owner,item['key'],kind)
                assert len(source(owner)['items'])==63
                mark('full_64_source_and_beyond_12_edit_delete_with_refetch')

                # All reference errors above also asserted exact before/after
                # Graph + Mongo equality. No provider or embedding worker ran.
                assert db.profiles.count_documents({'user_id':{'$in':list(allowed)}})==len(allowed)
                assert not list(db.profiles.aggregate([{'$group':{'_id':'$user_id','n':{'$sum':1}}},{'$match':{'n':{'$gt':1}}}]))
                print(json.dumps({'result':'PASS','scenarios':len(results),'production_touched':False,
                    'external_network_allowed':False,'synthetic_db':'action_reference_fixture'}),flush=True)


if __name__=='__main__':main()
