"""Disposable Graph/Mongo integration. No model/production endpoints or secrets.

Uses existing owner-verified local containers. Replaces ONLY their synthetic
Graph fixture, writes a dedicated Mongo fixture DB, and retains both for review.
Not a synthetic production pilot, R3 rerun or semantic-quality benchmark.
"""
import json
import logging
import os
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT/'social'), str(ROOT/'tests/readiness/preference_bootstrap')]
import run_readiness as harness
import local_mongo
from run_runtime_integration import local_only


def main():
    logging.getLogger('neo4j.notifications').setLevel(logging.ERROR)
    state = harness.read_state(); topology = harness.verify_container(state)
    mongo = local_mongo.verify()
    os.environ.update(AYUE_SKIP_DOTENV='1', DOTENV_DISABLED='1')
    import dotenv
    dotenv.load_dotenv = lambda *_a, **_k: False
    from neo4j import GraphDatabase
    from pymongo import MongoClient
    from matchmaker_agent.concept_identity import canonicalize_concept
    from matchmaker_agent.related_interest_contract import INDEX_NAME, embedding_fingerprint
    from matchmaker_agent import related_interest_retrieval as retrieval
    from matchmaker_agent import related_interest_validator as validator
    from matchmaker_agent.semantic_evidence_readiness import recheck_owner_evidence, pair_preferences_safe
    from services.semantic_user_eligibility import EnabledUserChecks
    from scripts.prepare_related_interest_embeddings import guarded_write, prepare_batch
    from scripts.semantic_embedding_pipeline import provenance, canonical_json, sha
    model = 'models/gemini-embedding-2'; fp = embedding_fingerprint(model); vector = [1.]+[0.]*767
    manifest = provenance(model, {'name': model, 'provider_reported_version': '2', 'supported_methods': ['embedContent']},
        prepare_batch, package_version=lambda p: '0.8.3' if p == 'google-generativeai' else '0.6.10')
    frozen = {'provenance': manifest, 'provenance_fingerprint': sha(canonical_json(manifest)), 'runtime_compatibility_fingerprint': fp}
    concept = canonicalize_concept('Playing Football'); query = canonicalize_concept('Watching Football')
    with local_only({state['bolt'], mongo['port']}), GraphDatabase.driver(f"bolt://127.0.0.1:{state['bolt']}",
            auth=('neo4j', state['password']), max_transaction_retry_time=0) as driver, MongoClient(
            f"mongodb://127.0.0.1:{mongo['port']}/?directConnection=true&replicaSet=bootstrap_test",
            serverSelectionTimeoutMS=3000) as client:
        db = client.semantic_rollout_synthetic_fixture
        with driver.session() as s:
            # Verify ALL existing data is from the already owner-checked fixture.
            labels = {label for r in s.run('MATCH (n) RETURN labels(n) AS labels') for label in r['labels']}
            assert labels <= {'BootstrapHarness', 'User', 'Concept', 'MemoryObservation', 'PreferenceBootstrapOperation'}
            assert not s.run("MATCH (u:User) WHERE NOT (u.id STARTS WITH 'synthetic_' OR u.id STARTS WITH 'pilot-' OR u.id STARTS WITH 'rollout-') RETURN count(u) AS n").single()['n']
            s.run('MATCH (n) DETACH DELETE n').consume()
            for label, prop in (('User', 'id'), ('Concept', 'key')):
                s.run(f'CREATE CONSTRAINT rollout_{label.lower()} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE').consume()
            s.run(f"CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS FOR (c:Concept) ON c.embedding_v2 OPTIONS {{indexConfig: {{`vector.dimensions`:768, `vector.similarity_function`:'cosine'}}}}").consume()
            source = concept.as_dict(); source['embedding'] = [123.]
            record = s.run('CREATE (c:Concept) SET c=$props RETURN elementId(c) AS id,properties(c) AS props', props=source).single()
            nodes = {concept.key: dict(record)}
            for i in range(15):
                owner = f'rollout-{i:02}'
                s.run('CREATE (u:User {id:$owner})', owner=owner).consume()
                db.profiles.replace_one({'user_id': owner}, {'user_id': owner}, upsert=True)
                if i:
                    s.run('MATCH (u:User {id:$owner}),(c:Concept {key:$key}) CREATE (u)-[:PREFERS]->(c)', owner=owner, key=concept.key).consume()
            s.run("CREATE (u:User {id:'rollout-avoid-only'}) WITH u MATCH (c:Concept {key:$key}) CREATE (u)-[:AVOIDS]->(c)", key=concept.key).consume()
            db.profiles.replace_one({'user_id': 'rollout-avoid-only'}, {'user_id': 'rollout-avoid-only'}, upsert=True)
            batch = prepare_batch([source], lambda *_a, **_k: [vector], model)
            with s.begin_transaction(timeout=5) as tx:
                assert guarded_write(tx, batch, nodes, model, frozen, now=time.time()) == {'written': 1, 'reused': 0}
                tx.commit()
            with s.begin_transaction(timeout=5) as tx:
                assert guarded_write(tx, batch, nodes, model, frozen, now=time.time()) == {'written': 0, 'reused': 1}
                tx.commit()
            assert s.run('MATCH(c:Concept {key:$key}) RETURN c.embedding AS v', key=concept.key).single()['v'] == [123.]
            s.run('CALL db.awaitIndexes(30)').consume()
        before = harness.digest_graph(driver)
        def completion(_client, **kwargs):
            pairs = json.loads(kwargs['messages'][1]['content'])['pairs']
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(
                content=json.dumps({'results': [{'id': p['id'], 'relation': 'role_mismatch'} for p in pairs]})))])
        req = SimpleNamespace(requester_user_id='rollout-00', embedding_model=model, embedding_fingerprint=fp,
            query_embedding=vector, request_budget_seconds=27, neighbor_limit=24, concept_limit=8, min_similarity=.82,
            excluded_user_ids=[], per_concept_limit=20, evidence_limit=3, candidate_limit=40)
        with patch.dict(os.environ, {'MATCH_RELATED_INTEREST_ROLLOUT_MODE': 'enabled_accounts',
                'MATCH_RELATED_INTEREST_ENABLED': 'on', 'MATCH_PREFERENCE_SEMANTIC_MODE': 'active',
                'MATCH_RELATED_INTEREST_CANARY_USER_IDS': '[]',
                'MATCH_RELATED_INTEREST_KILL_SWITCH_FILE': str(harness.ARTIFACTS/'rollout-test-kill')}), patch.object(validator, '_completion', completion):
            with driver.session(default_access_mode='READ') as s:
                statements = []
                class Capture:
                    def run(self, statement, **params):
                        if 'UNWIND $concepts' in str(statement): statements.append((str(statement), params))
                        return s.run(statement, **params)
                result = retrieval.retrieve(Capture(), req, query, None, 'deepseek-test-double', model,
                    eligibility_factory=lambda session, **kwargs: EnabledUserChecks(session, profiles=db.profiles,
                        account_lookup=lambda owner, **_kw: owner != 'rollout-14', **kwargs))
                assert result['status'] == 'success', result
                assert len(result['candidates']) == 13 and result['requester_eligibility_verified']
                assert {c['candidate_id'] for c in result['candidates']} == {f'rollout-{i:02}' for i in range(1,14)}
                candidate = result['candidates'][0]
                assert recheck_owner_evidence(s, candidate['candidate_id'], candidate['evidence'], query.key, fp)
                assert pair_preferences_safe(s, 'rollout-00', candidate['candidate_id'], query_key=query.key)
                plans = []
                for statement, params in statements:
                    profile = s.run('PROFILE '+statement, **params).consume().profile
                    operators = [n['operatorType'] for n in harness.plan_nodes(profile)]
                    assert not any('AllNodesScan' in op or 'NodeByLabelScan' in op for op in operators), operators
                    plans.append(operators)
        assert before == harness.digest_graph(driver)
        with driver.session() as s:
            s.run('MATCH(c:Concept {key:$key}) SET c.embedding_v2_source_hash="changed"', key=concept.key).consume()
            assert not recheck_owner_evidence(s, candidate['candidate_id'], candidate['evidence'], query.key, fp)
            s.run('MATCH(c:Concept {key:$key}) SET c.embedding_v2_source_hash=$hash', key=concept.key, hash=concept.semantic_input_hash).consume()
            # Replacing a source after preparation must roll back, including lock writes.
            s.run('MATCH(c:Concept {key:$key}) SET c.semantic_text="changed"', key=concept.key).consume()
            try:
                with s.begin_transaction(timeout=5) as tx: guarded_write(tx, batch, nodes, model, frozen, now=time.time())
            except ValueError: pass
            else: raise AssertionError('stale source was accepted')
            s.run('MATCH(c:Concept {key:$key}) SET c.semantic_text=$text', key=concept.key, text=concept.semantic_text).consume()
        assert before == harness.digest_graph(driver)
    report = {'status': 'PASS', 'synthetic_only': True, 'production_writes': 0, 'real_semantic_quality': 'not_retested',
        'requester_empty_allowed': True, 'eligible_candidates': 13, 'disabled_and_avoids_only_excluded': True,
        'final_stale_source_rejected': True, 'embedding_idempotent_and_legacy_unchanged': True,
        'read_paths_unchanged_graph': True, 'expansion_plans': plans, 'topology': topology}
    harness.json_file(harness.ARTIFACTS/'semantic-rollout-local.json', report)
    print(json.dumps(report))


if __name__ == '__main__': main()
