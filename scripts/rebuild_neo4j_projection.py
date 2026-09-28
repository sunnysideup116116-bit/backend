"""Owner-scoped projection maintenance, NOT preference migration authority.

Durable facts only reference already-active authoritative Graph v2 edges.
Never derive a preference identity from Mongo labels, revive retired edges,
rewrite shared Concepts, or reset/delete another owner's Graph/history.
Dry-run is default; --apply requires an explicit canonical --owner.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
import time
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from neo4j import GraphDatabase
from pymongo import MongoClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from matchmaker_agent.concept_identity import is_v2_preference_key, stored_concept_identity
from matchmaker_agent.semantic_rollout_policy import valid_owner_id
from matchmaker_agent.preference_write_fence import lock_preferences, bump_preferences
from matchmaker_agent.preference_embedding_jobs import enqueue_keys


def require(ok, code):
    if not ok:
        raise ValueError(code)


def concept_key(label: str, existing: str = "") -> str:
    """Non-preference context only; never borrow the reserved v2 key space."""
    key = str(existing or "").strip().lower()
    if not is_v2_preference_key(key) and re.fullmatch(r"[a-z][a-z0-9_]{1,50}", key):
        return key
    normalized = re.sub(r"\s+", "", str(label or "").strip().lower())
    return "concept_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def context_concepts(profile: dict) -> list[dict]:
    fields = ((profile.get("recent_context_state") or {}).get("fields") or {})
    concepts, seen = [], set()
    for field_name in ("activity", "destination"):
        label = re.sub(r"\s+", " ", str((fields.get(field_name) or {}).get("value") or "").strip())[:40]
        normalized = re.sub(r"\s+", "", label.lower())
        if label and normalized not in seen:
            seen.add(normalized)
            concepts.append({"key": concept_key(label), "label": label})
    return concepts


def preference_references(facts):
    result = []
    for fact in facts:
        polarity = {'like': 'PREFERS', 'require': 'PREFERS', 'avoid': 'AVOIDS', 'dislike': 'AVOIDS'}.get(fact.get('stance'))
        require(polarity is not None, 'projection_polarity_unknown')
        require(is_v2_preference_key(fact.get('concept_key')), 'legacy_preference_rebuild_forbidden')
        require(valid_owner_id(fact.get('user_id')), 'projection_owner_invalid')
        result.append({'user_id': fact['user_id'], 'key': fact['concept_key'], 'relation': polarity})
    return result


def rebuild_projection_transaction(tx, profiles: list[dict], facts: list[dict], intents: list[dict]):
    owners = [p.get('user_id') for p in profiles]
    require(0 < len(owners) <= 1000 and all(valid_owner_id(o) for o in owners)
            and len(set(owners)) == len(owners), 'projection_owner_missing_or_ambiguous')
    require(len(facts) <= 10000, 'projection_fact_limit')
    claims = set()
    for fact in facts:
        require(fact.get('user_id') in owners, 'projection_owner_mismatch')
        require(fact.get('relation') in {'PREFERS', 'AVOIDS'}, 'projection_polarity_unknown')
        require(is_v2_preference_key(fact.get('key')), 'legacy_preference_rebuild_forbidden')
        pair = (fact['user_id'], fact['key'])
        require(pair not in claims, 'projection_preference_ambiguous')
        claims.add(pair)
    intent_rows = []
    for intent in intents:
        require(intent.get('user_id') in owners, 'projection_owner_mismatch')
        for concept in intent['concepts']:
            require(isinstance(concept.get('key'), str) and bool(concept['key'])
                    and not is_v2_preference_key(concept['key']), 'context_cannot_overwrite_v2')
            intent_rows.append({**concept, 'user_id': intent['user_id'], 'expires_at': float(intent['expires_at'])})
    for owner in sorted(owners):
        lock_preferences(tx, owner, source_created_at=time.time(), require_existing=True)
    rows = list(tx.run('''/* projection_v2_authority */
        MATCH (u:User)-[r:PREFERS|AVOIDS]->(c:Concept)
        WHERE u.id IN $owners AND coalesce(r.active,true)=true
        RETURN u.id AS owner,type(r) AS relation,properties(c) AS concept LIMIT 10001
    ''', owners=owners))
    require(len(rows) <= 10000, 'projection_fact_limit')
    authoritative = {}
    for row in rows:
        identity = stored_concept_identity(dict(row['concept']))
        require(identity is not None, 'projection_v2_authority_missing')
        pair = (row['owner'], identity.key)
        require(pair not in authoritative, 'projection_preference_ambiguous')
        authoritative[pair] = row['relation']
    for fact in facts:
        require(authoritative.get((fact['user_id'],fact['key'])) == fact['relation'], 'projection_v2_authority_missing')
    for item in intent_rows:
        current = tx.run('''/* projection_context_lock */
            MERGE (c:Concept {key:$key}) ON CREATE SET c.kind='activity'
            ON MATCH SET c.key=c.key
            RETURN c.canonicalization_version AS version
        ''', key=item['key']).single(strict=True)
        require(current is not None and current['version'] != 'v2', 'context_cannot_overwrite_v2')
    # Durable edges are authoritative and already projected. Do NOT delete or
    # recreate them from stale/incomplete Mongo caches, including v2 preferences
    # that legitimately have no Mongo preference_fact.
    tx.run('''MATCH (u:User)-[r:CURRENTLY_WANTS]->()
        WHERE u.id IN $owners DELETE r''', owners=owners).consume()
    tx.run('''UNWIND $intents AS item
        MATCH (u:User {id:item.user_id})
        MERGE (c:Concept {key:item.key})
        ON CREATE SET c.kind='activity'
        WITH u,c,item WHERE coalesce(c.canonicalization_version,'') <> 'v2'
        SET c.label=item.label
        MERGE (u)-[r:CURRENTLY_WANTS]->(c) SET r.expires_at=item.expires_at
    ''', intents=intent_rows).consume()
    for owner in sorted(owners):
        enqueue_keys(tx, owner, [f['key'] for f in facts if f['user_id'] == owner and f['relation'] == 'PREFERS'])
        bump_preferences(tx, owner)
    record = tx.run('''MATCH (user:User) WHERE user.id IN $owners
        OPTIONAL MATCH (user)-[relation:PREFERS|AVOIDS|CURRENTLY_WANTS]->(:Concept)
        RETURN count(DISTINCT user) AS users,count(relation) AS relations''', owners=owners).single()
    require(record and record['users'] == len(owners), 'projection_verification_failed')
    return {**dict(record), 'verified_v2_preferences': len(facts), 'legacy_preferences_written': 0}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--owner', help='Exact canonical stable user ID; never nickname/email')
    args = parser.parse_args()
    if args.apply and not args.owner:
        parser.error('--apply requires an explicit --owner')
    if args.owner and not valid_owner_id(args.owner):
        parser.error('invalid canonical owner')
    load_dotenv(ROOT / 'social' / '.env', override=False)
    with MongoClient(os.environ['MONGO_URI'], serverSelectionTimeoutMS=8000) as mongo:
        db = mongo[os.getenv('MONGO_DB_NAME', 'profiling_db')]
        scope = {'user_id': args.owner} if args.owner else {}
        profiles = list(db.profiles.find(scope, {'user_id': 1, 'recent_context_state': 1, 'recent_context_expires_at': 1}).limit(1001))
        require(0 < len(profiles) <= 1000 and len({p.get('user_id') for p in profiles}) == len(profiles), 'projection_owner_missing_or_ambiguous')
        raw = list(db.preference_facts.find({**scope, 'active': True}, {'user_id': 1, 'concept_key': 1, 'stance': 1}).limit(10001))
        require(len(raw) <= 10000, 'projection_fact_limit')
        facts = preference_references(raw)  # No label parsing or polarity default.
        now = time.time()
        intents = [{'user_id': p['user_id'], 'expires_at': float(p['recent_context_expires_at']),
                    'concepts': context_concepts(p)} for p in profiles
                   if float(p.get('recent_context_expires_at') or 0) > now and context_concepts(p)]
        print({'mode': 'apply' if args.apply else 'dry-run', 'owners': len(profiles),
               'v2_references': len(facts), 'authority_verified': False})
        if not args.apply:
            return
        config = dotenv_values(ROOT / 'matchmaker_agent' / '.env')
        with GraphDatabase.driver(config.get('NEO4J_URI'), auth=(config.get('NEO4J_USERNAME'), config.get('NEO4J_PASSWORD'))) as driver:
            with driver.session(database=config.get('NEO4J_DATABASE', 'neo4j')) as session:
                print(session.execute_write(rebuild_projection_transaction, profiles, facts, intents))


if __name__ == '__main__':
    main()
