#!/usr/bin/env python3
"""Read-only paginated rollout inventory + aggregates, no fixed cohort/corpus.

Explicit environment only; never load dotenv, create accounts/indexes, bootstrap,
embed, clear a kill file or activate runtime. IDs require --include-user-ids and
must remain in a private operator report, never Git/public telemetry.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from collections import Counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'social')]


def collect_integrity(db, driver, database, *, limit=20000):
    """Bounded metadata-only integrity observations, never a repair operation."""
    from neo4j import Query
    from pymongo import timeout
    with driver.session(database=database, default_access_mode='READ') as session:
        users = [dict(r) for r in session.run(Query('''MATCH (u:User)
            RETURN u.id AS id,u.preference_projection_pending AS pending LIMIT $limit''', timeout=5), limit=limit+1)]
        edges = [dict(r) for r in session.run(Query('''MATCH (u:User)-[r:PREFERS|AVOIDS]->(c:Concept)
            WHERE coalesce(r.active,true)=true
            RETURN u.id AS owner,c.key AS key,type(r) AS polarity LIMIT $limit''', timeout=5), limit=limit+1)]
    with timeout(5):
        facts = list(db.preference_facts.find({'active': True}, {'_id': 0, 'user_id': 1, 'concept_key': 1, 'stance': 1})
            .limit(limit+1).max_time_ms(3000))
        profiles = list(db.profiles.find({}, {'_id': 0, 'user_id': 1, 'preference_bootstrap_pending': 1})
            .limit(limit+1).max_time_ms(3000))
        pending_ops = db.preference_bootstrap_operations.count_documents({'status': {'$ne': 'complete'}}, maxTimeMS=3000)
    if any(len(rows) > limit for rows in (users, edges, facts, profiles)):
        raise ValueError('integrity_inventory_truncated')
    mapped = {(r['owner'], r['key'], r['polarity']) for r in edges}
    unknown = unmapped = 0
    for fact in facts:
        stance = fact.get('stance')
        polarity = 'PREFERS' if stance in {'like', 'require'} else 'AVOIDS' if stance in {'avoid', 'dislike'} else None
        unknown += polarity is None
        unmapped += (fact.get('user_id'), fact.get('concept_key'), polarity) not in mapped
    return {'truncated': False, 'scope': 'bounded Graph/Mongo metadata, not a full historical content audit',
        'duplicate_graph_user_groups': sum(n > 1 for n in Counter(r.get('id') for r in users).values()),
        'duplicate_profile_groups': sum(n > 1 for n in Counter(r.get('user_id') for r in profiles).values()),
        'duplicate_or_mixed_owner_associations': sum(n > 1 for n in Counter((r['owner'], r['key']) for r in edges).values()),
        'pending_graph_projections': sum(bool(r.get('pending')) for r in users),
        'pending_mongo_projections': sum(bool(r.get('preference_bootstrap_pending')) for r in profiles),
        'pending_bootstrap_operations': pending_ops,
        'active_fact_count': len(facts), 'unmapped_active_facts': unmapped, 'unknown_fact_polarity': unknown}


def inventory(accounts, profiles, driver, database, model, *, max_concepts=10000, lifecycle=False):
    from neo4j import Query
    from matchmaker_agent.concept_identity import stored_concept_identity
    from matchmaker_agent.related_interest_contract import embedding_fingerprint
    from matchmaker_agent.semantic_evidence_readiness import verified_vector
    from matchmaker_agent.semantic_rollout_inventory import classify_owner, summarize_readiness
    if lifecycle:
        from matchmaker_agent.semantic_rollout_inventory import classify_lifecycle as classify_owner, summarize_lifecycle as summarize_readiness
    from matchmaker_agent.preference_bootstrap import snapshot, preview_plan
    from matchmaker_agent.preference_bootstrap_contract import BootstrapError
    fp = embedding_fingerprint(model)
    with driver.session(database=database, default_access_mode='READ') as session:
        records = [dict(r) for r in session.run(Query('''MATCH (c:Concept)
            RETURN c.key AS key,c.semantic_text AS semantic_text,c.canonicalization_version AS canonicalization_version,
              c.semantic_input_hash AS semantic_input_hash,c.fidelity_status AS fidelity_status,
              c.embedding_v2 AS vector,c.embedding_v2_fingerprint AS fingerprint,
              c.embedding_v2_source_hash AS source_hash,c.embedding_v2_provenance_fingerprint AS provenance
              ,c.preference_embedding_state AS embedding_job_state
            LIMIT $limit''', timeout=10), limit=max_concepts+1)]
        if len(records) > max_concepts:
            raise ValueError('concept_inventory_truncated')
        vectors = {}
        for record in records:
            vectors.setdefault(record.get('key'), []).append(record)
        rows = []
        for account in accounts:
            if account.get('status') is not True:
                continue
            owner = account.get('$id', '')
            p = list(profiles.find({'user_id': owner}, {'user_id': 1, 'enabled': 1, 'active': 1,
                'disabled': 1, 'blocked': 1, 'is_active': 1, 'is_disabled': 1, 'is_blocked': 1,
                'deleted_at': 1, 'status': 1, 'preference_bootstrap_pending': 1}).limit(2).max_time_ms(3000))
            users = [dict(r) for r in session.run(Query('''MATCH (u:User {id:$owner})
                RETURN u.id AS id,u.enabled AS enabled,u.active AS active,u.is_active AS is_active,u.disabled AS disabled,
                  u.blocked AS blocked,u.is_disabled AS is_disabled,u.is_blocked AS is_blocked,
                  u.deleted_at AS deleted_at,u.status AS status,
                  u.preference_projection_pending AS preference_projection_pending LIMIT 2''', timeout=3), owner=owner)]
            snap = None
            snapshot_error = None
            if len(users) == 1:
                try:
                    snap, _ = session.execute_read(snapshot, owner)
                except BootstrapError as exc:
                    snapshot_error = exc.code
            def preview_check(before):
                active = [r for r in before['rows'] if r['relation'] in {'PREFERS', 'AVOIDS'} and r['properties'].get('active') is not False]
                source = lambda r: r['concept'].get('semantic_text') or r['concept'].get('label')
                if any(not isinstance(source(r), str) or not source(r) for r in active):
                    return 'missing_source_text'
                try:
                    plan, current = session.execute_read(preview_plan, owner, 'complete_set',
                        [source(r) for r in active if r['relation'] == 'PREFERS'],
                        [source(r) for r in active if r['relation'] == 'AVOIDS'])
                    if current != before:
                        return 'snapshot_changed'
                    if {r['id'] for r in plan['retire_edges']} != {r['id'] for r in active if not stored_concept_identity(r['concept'])}:
                        return 'retirement_scope_mismatch'
                except BootstrapError as exc:
                    return exc.code
                return None
            result=classify_owner(account, p, users, snap, vectors, fp, preview_check=preview_check)
            if lifecycle and result['requester_eligible'] and snapshot_error in {'bootstrap_inventory_overflow','bootstrap_inventory_too_large'}:
                result.update(category='BLOCKED_CAPACITY',reason_codes=[snapshot_error])
            rows.append(result)
        from matchmaker_agent.related_interest_retrieval import index_metadata
        index = index_metadata(session)
    return {'status': 'success', 'sampled_at': time.time(), 'truncated': False,
        'consistency': 'read-only per-owner snapshots; not distributed ACID',
        **summarize_readiness(rows), 'accounts': rows, 'index': index,
        'concept_count': len(records),
        'verified_v2_concept_count': sum(bool(stored_concept_identity(r)) for r in records),
        'embedding_v2_present_count': sum(r.get('vector') is not None for r in records),
        'compatible_embedding_v2_count': sum(bool(verified_vector(r, fp)) for r in records),
        'incremental_embedding_jobs': dict(Counter(r['embedding_job_state']
            if r.get('embedding_job_state') in {'pending','retry','leased','complete','failed','blocked','cancelled'}
            else 'unknown' for r in records if r.get('embedding_job_state') is not None)),
        'duplicate_concept_key_groups': sum(len(group) > 1 for group in vectors.values()),
        'invalid_claimed_v2_concepts': sum(r.get('canonicalization_version') == 'v2' and not stored_concept_identity(r) for r in records),
        'runtime_fingerprint': fp, 'historical_embedding_fingerprint': 'unknown',
        'production_writes': 0, 'activation_approved': False}


def enabled_accounts(settings, api_key, *, max_accounts, http):
    from matchmaker_agent.semantic_rollout_policy import valid_owner_id
    accounts = []; offset = 0; expected_total = None
    while True:
        response = http.get(settings.endpoint + '/users', headers={'X-Appwrite-Project': settings.project_id,
            'X-Appwrite-Key': api_key}, params={'queries[]': [json.dumps({'method': 'limit', 'values': [100]}),
            json.dumps({'method': 'offset', 'values': [offset]})]}, timeout=(2, 8),
            verify=settings.verify_tls, allow_redirects=False)
        if response.status_code != 200:
            raise ValueError('account_inventory_unavailable')
        page = response.json()
        if not isinstance(page, dict) or not isinstance(page.get('users'), list) or type(page.get('total')) is not int:
            raise ValueError('account_inventory_invalid')
        if (page['total'] < 0 or page['total'] > max_accounts or len(page['users']) > 100
                or any(not isinstance(a, dict) or not valid_owner_id(a.get('$id'))
                    or type(a.get('status')) is not bool for a in page['users'])):
            raise ValueError('account_inventory_invalid')
        if expected_total is not None and expected_total != page['total']:
            raise ValueError('account_inventory_changed')
        expected_total = page['total']
        accounts.extend(page['users'])
        if len(accounts) > max_accounts:
            raise ValueError('account_inventory_truncated')
        if len(accounts) > page['total']:
            raise ValueError('account_inventory_changed')
        if len(accounts) == page['total']:
            break
        if not page['users']:
            raise ValueError('account_inventory_incomplete')
        offset += len(page['users'])
    if len({a.get('$id') for a in accounts}) != len(accounts):
        raise ValueError('account_inventory_ambiguous')
    return accounts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--include-user-ids', action='store_true')
    parser.add_argument('--lifecycle', action='store_true', help='Six owner-confirmation-aware lifecycle categories')
    parser.add_argument('--max-accounts', type=int, default=10000)
    parser.add_argument('--since', type=float)
    parser.add_argument('--until', type=float, help='Only jobs/proposals initiated before this exclusive cutoff')
    args = parser.parse_args()
    if not 1 <= args.max_accounts <= 10000:
        parser.error('invalid inventory resource bound')
    os.environ.update(AYUE_SKIP_DOTENV='1', DOTENV_DISABLED='1')
    client = driver = None
    try:
        import requests
        from neo4j import GraphDatabase
        from pymongo import MongoClient
        from services.appwrite_identity_service import AppwriteIdentitySettings
        settings = AppwriteIdentitySettings.from_env()
        key = os.environ['APPWRITE_API_KEY']
        if not key.strip():
            raise ValueError('account_inventory_unconfigured')
        accounts = enabled_accounts(settings, key, max_accounts=args.max_accounts, http=requests)
        client = MongoClient(os.environ['MONGO_URI'], serverSelectionTimeoutMS=5000, socketTimeoutMS=5000)
        db = client[os.environ['MONGO_DB_NAME']]
        driver = GraphDatabase.driver(os.environ['NEO4J_URI'], auth=(os.environ['NEO4J_USERNAME'], os.environ['NEO4J_PASSWORD']),
            connection_timeout=5, connection_acquisition_timeout=5, max_transaction_retry_time=0)
        report = inventory(accounts, db.profiles, driver, os.getenv('NEO4J_DATABASE', 'neo4j'),
            os.getenv('GOOGLE_EMBEDDING_MODEL', 'models/gemini-embedding-2'), lifecycle=args.lifecycle)
        duplicate_groups = list(db.profiles.aggregate([{'$group': {'_id': '$user_id', 'n': {'$sum': 1}}},
            {'$match': {'n': {'$gt': 1}}}, {'$count': 'groups'}], maxTimeMS=5000))
        report['duplicate_profile_groups'] = duplicate_groups[0]['groups'] if duplicate_groups else 0
        report['enabled_account_count'] = sum(a.get('status') is True for a in accounts)
        report['integrity'] = collect_integrity(db, driver, os.getenv('NEO4J_DATABASE', 'neo4j'))
        if args.since is not None:
            from scripts.report_related_interest_pilot import collect_report
            report['telemetry'] = collect_report(db, None, since=args.since, until=args.until, limit=5000)
        if not args.include_user_ids:
            report.pop('accounts')
        print(json.dumps(report, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({'status': 'unavailable', 'error_code': 'rollout_inventory_unavailable', 'activation_approved': False}))
        return 1
    finally:
        if driver: driver.close()
        if client: client.close()


if __name__ == '__main__':
    raise SystemExit(main())
