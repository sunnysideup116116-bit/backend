"""Transactional per-Concept derived queue, independent of preference authority.

No provider, legacy migration, vector-index creation or matching decisions.
Queue metadata deliberately does not use the reserved embedding_v2* prefix.
"""
import time
import uuid

from .concept_identity import stored_concept_identity, is_v2_preference_key
from .related_interest_contract import embedding_fingerprint
from .semantic_evidence_readiness import verified_vector
from .preference_bootstrap_contract import require

LEASE_SECONDS = 300
MAX_ATTEMPTS = 8
QUEUE_FIELDS = ('preference_embedding_state', 'preference_embedding_source_hash',
    'preference_embedding_attempts', 'preference_embedding_next_at', 'preference_embedding_lease',
    'preference_embedding_lease_until', 'preference_embedding_error')

# Called inside the SAME transaction as a new/changed active PREFERS edge.
# Existing compatible vectors are never regenerated: the worker reuses them.
ENQUEUE = '''
    FOREACH (_ IN CASE WHEN c.preference_embedding_source_hash IS NULL
                         OR c.preference_embedding_source_hash <> c.semantic_input_hash
                         OR c.preference_embedding_state='cancelled' THEN [1] ELSE [] END |
        SET c.preference_embedding_state='pending',
            c.preference_embedding_source_hash=c.semantic_input_hash,
            c.preference_embedding_attempts=0,c.preference_embedding_next_at=0)
'''


def enqueue_keys(tx, owner, keys):
    """Restore/correction hook; only current verified source ownership is queued."""
    rows = list(tx.run('''UNWIND $keys AS key MATCH (u:User {id:$owner})-[r:PREFERS]->(c:Concept {key:key})
        WHERE coalesce(r.active,true)=true RETURN properties(c) AS props''', owner=owner, keys=keys))
    verified = []
    for row in rows:
        props = dict(row['props']);identity = stored_concept_identity(props)
        require(identity or not (is_v2_preference_key(props.get('key')) or props.get('canonicalization_version')=='v2'),
                'preference_identity_unverified', 422)
        if identity:verified.append(identity.key)
    if verified:
        tx.run('''UNWIND $keys AS key MATCH (u:User {id:$owner})-[r:PREFERS]->(c:Concept {key:key})
            WHERE coalesce(r.active,true)=true
            WITH DISTINCT c
        '''+ENQUEUE, owner=owner, keys=verified).consume()


def claim(tx, model, frozen, *, now=None):
    """One bounded lease. Expired lease recovery rechecks source and PREFERS."""
    from scripts.prepare_related_interest_embeddings import existing_vector_state
    now = time.time() if now is None else now
    rows = list(tx.run('''MATCH (c:Concept)
        WHERE (c.preference_embedding_state IN ['pending','retry'] AND coalesce(c.preference_embedding_next_at,0)<=$now)
           OR (c.preference_embedding_state='leased' AND c.preference_embedding_lease_until<=$now)
        RETURN c.key AS key ORDER BY c.preference_embedding_next_at,c.key LIMIT 5''', now=now))
    for candidate in rows:
        key = candidate['key']
        # Property-dependent write takes the Concept lock before rechecking lease.
        tx.run('''MATCH (c:Concept {key:$key})
            SET c.preference_embedding_attempts=coalesce(c.preference_embedding_attempts,0)''',key=key).consume()
        current = list(tx.run('''MATCH (c:Concept {key:$key})
            RETURN elementId(c) AS id,properties(c) AS props,
              EXISTS { MATCH (:User)-[r:PREFERS]->(c) WHERE coalesce(r.active,true)=true } AS any_positive,
              EXISTS { MATCH (u:User)-[r:PREFERS]->(c) WHERE coalesce(r.active,true)=true
                AND u.preference_projection_pending IS NULL } AS positive''', key=key))
        require(len(current)==1, 'embedding_concept_ambiguous')
        row = current[0];props = dict(row['props'])
        state = props.get('preference_embedding_state')
        due = state in {'pending','retry'} and props.get('preference_embedding_next_at',0)<=now
        due |= state == 'leased' and props.get('preference_embedding_lease_until',0)<=now
        if not due:continue
        identity = stored_concept_identity(props)
        if not identity or props.get('preference_embedding_source_hash') != identity.semantic_input_hash:
            _state(tx,key,'blocked','embedding_source_invalid');continue
        if not row['positive']:
            # Pending bootstrap projection can become eligible after its ack.
            _state(tx,key,'retry' if row['any_positive'] else 'cancelled','positive_owner_not_ready', next_at=now+30);continue
        try:existing = existing_vector_state(props, model, frozen)
        except ValueError:
            _state(tx,key,'blocked','existing_versioned_embedding_conflict');continue
        if existing == 'compatible':
            _state(tx,key,'complete','');continue
        if props.get('preference_embedding_attempts',0)>=MAX_ATTEMPTS:
            _state(tx,key,'failed','embedding_attempt_limit');continue
        lease = str(uuid.uuid4())
        tx.run('''MATCH (c:Concept {key:$key}) SET c.preference_embedding_state='leased',
            c.preference_embedding_lease=$lease,c.preference_embedding_lease_until=$until,
            c.preference_embedding_attempts=c.preference_embedding_attempts+1''', key=key,lease=lease,until=now+LEASE_SECONDS).consume()
        return {'key':key,'lease':lease,'source_hash':identity.semantic_input_hash,
            'identity':identity.as_dict(),'fingerprint':embedding_fingerprint(model),
            'provenance_fingerprint':frozen['provenance_fingerprint']}
    return None


def _state(tx, key, state, error, *, next_at=0):
    tx.run('''MATCH (c:Concept {key:$key}) SET c.preference_embedding_state=$state,
        c.preference_embedding_error=$error,c.preference_embedding_next_at=$next_at
        REMOVE c.preference_embedding_lease,c.preference_embedding_lease_until''',
        key=key,state=state,error=error,next_at=next_at).consume()


def finish(tx, item, model, frozen, *, now=None):
    """Lease/source CAS plus approved all-or-nothing missing-vector writer."""
    from scripts.prepare_related_interest_embeddings import guarded_write, existing_vector_state
    now = time.time() if now is None else now
    tx.run('''MATCH (c:Concept {key:$key})
        SET c.preference_embedding_attempts=coalesce(c.preference_embedding_attempts,0)''',key=item['key']).consume()
    rows = list(tx.run('''MATCH (c:Concept {key:$key})
        RETURN elementId(c) AS id,properties(c) AS props,
            EXISTS { MATCH (u:User)-[r:PREFERS]->(c) WHERE coalesce(r.active,true)=true
              AND u.preference_projection_pending IS NULL } AS positive''',key=item['key']))
    require(len(rows)==1,'embedding_concept_ambiguous')
    row=rows[0];props=dict(row['props'])
    # Lost response after a successful commit is an idempotent read, not a rewrite.
    if (props.get('preference_embedding_state')=='complete' and existing_vector_state(props,model,frozen)=='compatible'
            and props.get('embedding_v2_source_hash')==item['source_hash']):
        return {'written':0,'reused':1}
    require(props.get('preference_embedding_state')=='leased' and props.get('preference_embedding_lease')==item['lease']
        and props.get('preference_embedding_lease_until',0)>now,'embedding_lease_lost')
    identity=stored_concept_identity(props)
    require(identity and identity.semantic_input_hash==item['source_hash'],'embedding_source_changed')
    if not row['positive']:
        _state(tx,item['key'],'cancelled','no_active_positive_owner')
        return {'written':0,'cancelled':True}
    batch=[{'key':item['key'],'source_hash':item['source_hash'],'semantic_text':identity.semantic_text,
        'fingerprint':embedding_fingerprint(model),'vector':item['vector']}]
    receipt=guarded_write(tx,batch,{item['key']:{'id':row['id'],'props':props}},model,frozen,now=now)
    _state(tx,item['key'],'complete','')
    return receipt


def fail(tx, key, lease, *, now=None):
    now=time.time() if now is None else now
    row=tx.run('''MATCH (c:Concept {key:$key}) SET c.preference_embedding_attempts=coalesce(c.preference_embedding_attempts,0)
        RETURN c.preference_embedding_state AS state,c.preference_embedding_lease AS lease,
               c.preference_embedding_attempts AS attempts''',key=key).single(strict=True)
    require(row and row['state']=='leased' and row['lease']==lease,'embedding_lease_lost')
    attempts=row['attempts']
    _state(tx,key,'failed' if attempts>=MAX_ATTEMPTS else 'retry','embedding_provider_unavailable',
        next_at=now+min(3600,30*2**min(attempts,7)))
    return {'status':'retry' if attempts<MAX_ATTEMPTS else 'failed'}
