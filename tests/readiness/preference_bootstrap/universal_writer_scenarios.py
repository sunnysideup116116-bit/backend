"""Extra real synthetic Graph checks; called only inside the local DB harness."""
import asyncio
import time
from copy import deepcopy

from matchmaker_agent.concept_identity import canonicalize_fresh_concept, stored_concept_identity
from matchmaker_agent.preference_bootstrap import snapshot
from matchmaker_agent.preference_action_reference import reference
from scripts.rebuild_neo4j_projection import rebuild_projection_transaction


def exercise(driver, agent_api, graph, reset, mark):
    def query(cypher, **params):
        with driver.session() as session:
            return [dict(r) for r in session.run(cypher, **params)]

    for polarity in ('PREFERS', 'AVOIDS'):
        reset()
        text = 'Synthetic restored preference '+polarity
        identity = canonicalize_fresh_concept(text)
        query('''MATCH (u:User {id:'synthetic_a'})
            CREATE (c:Concept {key:'disabled_legacy',label:$text,embedding:[0.25]})
            CREATE (u)-[:MEMORY_DISABLED {original_relation:$polarity}]->(c)''', text=text, polarity=polarity)
        result = asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
            user_id='synthetic_a',key='disabled_legacy',action='restore',source_created_at=time.time())))
        assert result['status']=='success', result
        rows=query('''MATCH (:User {id:'synthetic_a'})-[r]->(c:Concept {key:$key})
            RETURN type(r) AS relation,properties(c) AS concept''', key=identity.key)
        assert len(rows)==1 and rows[0]['relation']==polarity and stored_concept_identity(rows[0]['concept'])
        assert (rows[0]['concept'].get('preference_embedding_state')=='pending')==(polarity=='PREFERS')
        source=query('''MATCH (:User {id:'synthetic_a'})-[d:MEMORY_DISABLED]->(c:Concept {key:'disabled_legacy'})
            RETURN d.restored_v2_key AS target,properties(c) AS concept''')[0]
        assert source['target']==identity.key and source['concept']=={'key':'disabled_legacy','label':text,'embedding':[0.25]}
        before=graph.read(snapshot,'synthetic_a')[0]
        result=asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
            user_id='synthetic_a',key='disabled_legacy',action='restore',source_created_at=time.time())))
        assert result['error_code']=='legacy_restore_already_retired' and graph.read(snapshot,'synthetic_a')[0]==before
        mark('real_legacy_'+polarity.lower()+'_restore_v2_only_queue_polarity_replay_rejected')

    reset()
    query("MATCH (u:User {id:'synthetic_a'}) CREATE (c:Concept {key:'disabled_bad',label:$text}) CREATE (u)-[:MEMORY_DISABLED {original_relation:'PREFERS'}]->(c)",text='x'*501)
    before=graph.read(snapshot,'synthetic_a')[0]
    result=asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
        user_id='synthetic_a',key='disabled_bad',action='restore',source_created_at=time.time())))
    assert result['status']=='error' and graph.read(snapshot,'synthetic_a')[0]==before
    mark('real_invalid_restore_no_graph_write')

    # A fresh empty synthetic owner, not migration of the reset legacy owners.
    identity=canonicalize_fresh_concept('Projection maintenance synthetic')
    query('CREATE (:User {id:"synthetic_rebuild"}),(:Concept $props)',props=identity.as_dict())
    query('MATCH (u:User {id:"synthetic_rebuild"}),(c:Concept {key:$key}) CREATE (u)-[:PREFERS]->(c)',key=identity.key)
    profiles=[{'user_id':'synthetic_rebuild'}]
    facts=[{'user_id':'synthetic_rebuild','key':identity.key,'relation':'PREFERS'}]
    with driver.session() as session:
        result=session.execute_write(rebuild_projection_transaction,profiles,facts,[])
    assert result['legacy_preferences_written']==0 and result['verified_v2_preferences']==1
    before=graph.read(snapshot,'synthetic_rebuild')[0]
    bad=deepcopy(facts);bad[0]['key']='legacy_payload'
    with driver.session() as session:
        try:session.execute_write(rebuild_projection_transaction,profiles,bad,[])
        except ValueError as exc:assert str(exc)=='legacy_preference_rebuild_forbidden'
        else:raise AssertionError('legacy payload accepted')
    assert graph.read(snapshot,'synthetic_rebuild')[0]==before
    assert query('MATCH (c:Concept {key:"legacy_payload"}) RETURN count(c) AS n')[0]['n']==0
    mark('real_projection_v2_preserved_legacy_payload_no_write')
