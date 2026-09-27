"""Extra scenarios called only inside the verified local-only harness guard."""
import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch


def exercise(driver, api, db, graph, reset, client, headers, path, consent, mark):
    from matchmaker_agent import preference_embedding_jobs as jobs
    from matchmaker_agent.preference_embedding_contract import MODEL, RUNTIME, frozen_contract
    from matchmaker_agent.preference_bootstrap_contract import BootstrapError
    from matchmaker_agent.semantic_evidence_readiness import verified_vector
    from matchmaker_agent.concept_identity import canonicalize_fresh_concept
    from services import preference_bootstrap_service as social
    from services import preference_action_projection as projection
    from matchmaker_agent.preference_action_reference import reference, acknowledge_projection
    frozen=frozen_contract();vector=[1.0]+[0.0]*767

    def write(owner,text,stance='like'):
        identity=canonicalize_fresh_concept(text)
        result=asyncio.run(api.apply_memory(api.MemoryApplyRequest(user_id=owner,
            source_created_at=time.time(),message_id='synthetic-'+owner+identity.key,
            memories=[{**identity.as_dict(),'stance':stance,'confidence':1.0}])))
        assert result['status']=='success',result
        return identity

    def action(owner,key,kind,value=None):
        supplied = key
        if kind != 'restore':
            source = graph.source(owner)
            row = next(r for r in source['rows'] if r['concept']['key']==key)
            supplied = reference(owner, source['revision'], row)
        result=asyncio.run(api.memory_action(api.MemoryActionRequest(user_id=owner,key=supplied,action=kind,
            value=value,source_created_at=time.time(),expires_at=time.time()+30)))
        assert result['status']=='success',result
        return result

    def props(key):
        with driver.session() as session:
            return dict(session.run('MATCH (c:Concept {key:$key}) RETURN properties(c) AS p',key=key).single()['p'])

    def tx_call(fn,*args,**kwargs):
        with driver.session() as session:
            return session.execute_write(lambda tx:fn(tx,*args,**kwargs))

    def projection_graph(action, body):
        if action == 'source':return graph.source(body['owner'])
        assert action == 'action-projection-ack'
        return graph.write(acknowledge_projection, body['owner'], body['key'], body['revision'])

    reset()
    # A new EMPTY owner can acquire its first normal preference, no bootstrap.
    db.profiles.insert_one({'user_id':'synthetic_new'})
    identity=write('synthetic_new','Reading historical novels')
    original=props(identity.key)
    assert original['canonicalization_version']=='v2' and original['semantic_text']==identity.semantic_text
    assert original['preference_embedding_state']=='pending' and not verified_vector(original,RUNTIME)
    assert not any(k.startswith('embedding') for k in original)
    negative=write('synthetic_new','Loud venues','avoid')
    assert 'preference_embedding_state' not in props(negative.key)
    with driver.session() as session:
        assert session.run("MATCH (:User {id:'synthetic_new'})-[:PREFERS|AVOIDS]->(c) WHERE c.canonicalization_version<>'v2' RETURN count(c) AS n").single()['n']==0
        assert session.run("MATCH (:User {id:'synthetic_new'})-[:PREFERS]->(c:Concept {key:$key}) RETURN count(c) AS n",key=identity.key).single()['n']==1
    mark('new_empty_owner_first_write_v2_exact_present_semantic_not_ready_avoids_not_queued')

    # Only one transaction can claim a shared Concept, even across workers.
    def concurrent_claim(_n):
        from neo4j.exceptions import Neo4jError
        try:return tx_call(jobs.claim,MODEL,frozen)
        except Neo4jError as exc:
            # Explicit rollback under lock contention is a transient 503 at the
            # private API, not a second lease or an ambiguous commit replay.
            assert exc.code=='Neo.TransientError.Transaction.DeadlockDetected'
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        claimed=list(pool.map(concurrent_claim,range(2)))
    assert sum(item is not None for item in claimed)==1
    first=next(item for item in claimed if item)
    tx_call(jobs.fail,identity.key,first['lease'])
    assert props(identity.key)['preference_embedding_state']=='retry'
    assert not verified_vector(props(identity.key),RUNTIME)
    assert tx_call(jobs.claim,MODEL,frozen) is None
    second=tx_call(jobs.claim,MODEL,frozen,now=time.time()+70)
    packet={'key':identity.key,'lease':second['lease'],'source_hash':identity.semantic_input_hash,'vector':vector}
    assert tx_call(jobs.finish,packet,MODEL,frozen,now=time.time()+71)['written']==1
    assert tx_call(jobs.finish,packet,MODEL,frozen,now=time.time()+72)=={'written':0,'reused':1}
    assert verified_vector(props(identity.key),RUNTIME)
    assert props(identity.key)['embedding_v2_provenance_fingerprint']==frozen['provenance_fingerprint']
    mark('concurrent_claim_retry_missing_only_frozen_embedding_finish_idempotency')

    write('synthetic_b',identity.semantic_text)
    before=props(identity.key)
    db.preference_facts.insert_one({'user_id':'synthetic_new','concept_key':identity.key,'active':True,'stance':'like','evidence_count':7})
    intent=projection.stage('synthetic_new',identity.key)
    action('synthetic_new',identity.key,'disable')
    with patch.object(projection,'graph_call',projection_graph):
        assert projection.settle(intent)['status']=='synced'
    assert db.preference_facts.find_one({'user_id':'synthetic_new'})['active'] is False
    assert props(identity.key)==before
    action('synthetic_new',identity.key,'restore')
    with patch.object(projection,'graph_call',projection_graph):
        assert projection.settle(projection.stage('synthetic_new',identity.key))['status']=='synced'
    assert db.preference_facts.find_one({'user_id':'synthetic_new'})['active'] is True
    edited=action('synthetic_new',identity.key,'correct','Reading contemporary fiction')
    assert edited['key']!=identity.key and props(identity.key)==before
    assert props(edited['key'])['preference_embedding_state']=='pending'
    with patch.object(projection,'graph_call',projection_graph):
        assert projection.settle(projection.stage('synthetic_new',identity.key))['status']=='synced'
    assert db.preference_facts.find_one({'user_id':'synthetic_new'})['active'] is False
    with driver.session() as session:
        assert session.run("MATCH (:User {id:'synthetic_b'})-[:PREFERS]->(:Concept {key:$key}) RETURN count(*) AS n",key=identity.key).single()['n']==1
        assert session.run("MATCH (:User {id:'synthetic_new'})-[:PREFERENCE_SUPERSEDED]->(:Concept {key:$key}) RETURN count(*) AS n",key=identity.key).single()['n']==1
    mark('owner_edit_delete_restore_mongo_projection_shared_concept_preserved')

    pending=tx_call(jobs.claim,MODEL,frozen)
    action('synthetic_new',edited['key'],'disable')
    receipt=tx_call(jobs.finish,{'key':pending['key'],'lease':pending['lease'],'source_hash':pending['source_hash'],'vector':vector},MODEL,frozen)
    assert receipt['cancelled'] and 'embedding_v2' not in props(edited['key'])
    action('synthetic_new',edited['key'],'restore')
    pending=tx_call(jobs.claim,MODEL,frozen)
    bad={'key':pending['key'],'lease':pending['lease'],'source_hash':'0'*64,'vector':vector}
    try:tx_call(jobs.finish,bad,MODEL,frozen)
    except BootstrapError as exc:assert exc.code=='embedding_source_changed'
    else:raise AssertionError('stale source committed')
    assert 'embedding_v2' not in props(edited['key'])
    mark('removed_owner_and_stale_source_cannot_finish_embedding')

    reset()
    with patch.object(social,'graph_call',lambda operation,body:graph.source(body['owner']) if operation=='source' else
            graph.preview(body['owner'],body['mode'],body['prefers'],body['avoids'],body['mongo_snapshot_hash'])):
        source=client.get(path+'/source',headers=headers)
        assert source.status_code==200,source.json()
        source=source.json()
        assert source['status']=='confirmation_required' and source['avoids']==['舊避免條件']
        assert tx_call(jobs.claim,MODEL,frozen) is None
        assert db.preference_bootstrap_operations.count_documents({})==0
        preview=client.post(path+'/preview',headers=headers,json={'mode':'complete_set','prefers':source['prefers'],
            'avoids':source['avoids'],'source_token':source['source_token']})
        assert preview.status_code==200,preview.json()
    # Existing real signed HTTP bridge is restored here by the harness caller.
    packet=preview.json()
    assert all(i['canonicalization_version']=='v2' for i in packet['items'])
    assert db.preference_bootstrap_operations.count_documents({})==0
    mark('first_use_full_owner_preview_never_silently_migrates')

    # Registration's distinct writer must also enqueue only verified PREFERS.
    reset()
    from matchmaker_agent.registration_graph import seed_registration,registration_message_id
    registered=canonicalize_fresh_concept('Playing board games')
    tx_call(seed_registration,'synthetic_registration',registration_message_id('synthetic_registration'),
        [{**registered.as_dict(),'stance':'like','confidence':1.0,'category':'activity','evidence_span':'Playing board games'}])
    assert props(registered.key)['preference_embedding_state']=='pending'
    mark('registration_first_preference_uses_same_v2_queue')
