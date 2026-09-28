"""Owner-fenced restore: durable edges only target verified Identity-v2.

Call within the SAME transaction as the owner fence. No provider, Mongo, text
splitting, bulk migration, or rewrite of a shared Concept's identity.
"""
import re

from .concept_identity import (
    canonicalize_concept, normalize_fresh_preference_text, stored_concept_identity,
    is_v2_preference_key, has_mixed_preference_polarity, PreferenceTextError,
)
from .preference_bootstrap_contract import PROTECTED, is_compound_item
from .preference_embedding_jobs import enqueue_keys
from .preference_write_fence import PreferenceFenceError
from .preference_action_reference import MAX_PENDING


def require(ok, code):
    if not ok:
        raise PreferenceFenceError(code)


def restore_identity(concept):
    identity = stored_concept_identity(concept)
    if identity:
        return identity, False
    require(concept.get('canonicalization_version') != 'v2'
            and not is_v2_preference_key(concept.get('key')), 'preference_identity_unverified')
    raw = concept.get('semantic_text') or concept.get('label')
    require(isinstance(raw, str) and bool(raw), 'legacy_restore_source_missing')
    try:
        text = normalize_fresh_preference_text(raw)
        require(not has_mixed_preference_polarity(text), 'legacy_restore_polarity_ambiguous')
        identity = canonicalize_concept(text)
        require(identity is not None and not re.search(PROTECTED, identity.semantic_text, re.I),
                'legacy_restore_not_admissible')
        # Same lossless legacy-compound rule; never split or use display text.
        require(not is_compound_item(text) or identity.semantic_text == raw,
                'legacy_restore_text_not_lossless')
    except PreferenceTextError as exc:
        raise PreferenceFenceError(exc.code) from None
    return identity, True


def restore_memory(tx, owner, key, now):
    rows = list(tx.run('''/* restore_source */
        MATCH (u:User {id:$owner})-[d:MEMORY_DISABLED]->(c:Concept {key:$key})
        SET c.key=c.key
        RETURN elementId(d) AS id,properties(d) AS disabled,
          c{.key,.label,.semantic_text,.display_label,.canonicalization_version,
            .semantic_input_hash,.fidelity_status} AS concept LIMIT 2
    ''', owner=owner, key=key))
    if not rows:
        return None
    require(len(rows) == 1, 'preference_restore_ambiguous')
    pending = tx.run('''MATCH (u:User {id:$owner})
        RETURN coalesce(u.preference_action_projection_keys,[]) AS keys
    ''', owner=owner).single(strict=True)['keys']
    require(key in pending or len(pending) < MAX_PENDING, 'preference_projection_pending')
    source = rows[0]
    relation = source['disabled'].get('original_relation')
    expires = source['disabled'].get('original_expires_at')
    require(not source['disabled'].get('restored_v2_key'), 'legacy_restore_already_retired')
    require(relation in {'PREFERS', 'AVOIDS', 'CURRENTLY_WANTS'}, 'preference_restore_polarity_ambiguous')
    if relation == 'CURRENTLY_WANTS':
        # Preserve existing non-preference restore and expiration semantics.
        tx.run('''/* restore_context */
            MATCH (u:User {id:$owner})-[d:MEMORY_DISABLED]->(c:Concept {key:$key})
            WHERE elementId(d)=$association
            DELETE d
            FOREACH (_ IN CASE WHEN coalesce($expires,0)>$now THEN [1] ELSE [] END |
                MERGE (u)-[r:CURRENTLY_WANTS]->(c) SET r.expires_at=$expires)
        ''', owner=owner, key=key, association=source['id'], expires=expires, now=now).consume()
        return {'original_relation': relation, 'original_expires_at': expires, 'projection_key': key}

    identity, legacy = restore_identity(dict(source['concept']))
    targets = list(tx.run('''/* restore_target_identity */
        MATCH (c:Concept {key:$key}) RETURN properties(c) AS concept LIMIT 2
    ''', key=identity.key))
    require(len(targets) <= 1, 'duplicate_concept_key')
    if targets:
        existing = stored_concept_identity(dict(targets[0]['concept']))
        require(existing and existing.as_dict() == identity.as_dict(), 'preference_identity_conflict')
    associations = list(tx.run('''/* restore_target_associations */
        MATCH (u:User {id:$owner})-[r:PREFERS|AVOIDS|CURRENTLY_WANTS|MEMORY_DISABLED]->(c:Concept {key:$key})
        RETURN elementId(r) AS id,type(r) AS relation,r.active AS active LIMIT 3
    ''', owner=owner, key=identity.key))
    others = [r for r in associations if r['id'] != source['id']]
    require(len(others) <= 1 and all(r['relation'] == relation and
        (r['active'] is None or r['active'] is True) for r in others), 'preference_restore_conflict')
    props = {**identity.as_dict(), 'kind': 'preference'}
    # Lock the shared target before final validation, including concurrent
    # creation by another owner. An exception rolls the whole tx back.
    target = tx.run('''/* restore_create_or_reuse_v2 */
        MERGE (c:Concept {key:$key}) ON CREATE SET c += $props
        ON MATCH SET c.key=c.key
        RETURN properties(c) AS concept
    ''', key=identity.key, props=props).single(strict=True)
    current = stored_concept_identity(dict(target['concept'])) if target else None
    require(current and current.as_dict() == identity.as_dict(), 'preference_identity_conflict')
    retirement = ('SET d.restored_v2_key=$target,d.restored_at=$now' if legacy else 'DELETE d')
    tx.run(f'''/* restore_active_v2 */
        MATCH (u:User {{id:$owner}})-[d:MEMORY_DISABLED]->(old:Concept {{key:$source}}),
              (c:Concept {{key:$target}})
        WHERE elementId(d)=$association
        MERGE (u)-[:{relation}]->(c)
        {retirement}
    ''', owner=owner, source=key, target=identity.key, association=source['id'], now=now).consume()
    if relation == 'PREFERS':
        enqueue_keys(tx, owner, [identity.key])
    return {'original_relation': relation, 'original_expires_at': expires,
            'projection_key': key, 'key': identity.key}
