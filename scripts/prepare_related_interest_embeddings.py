#!/usr/bin/env python3
"""Explicit-key, dry-run-first preparation of a separate Gemini vector space.

Never rekeys/merges Concepts or changes preference edges. No dotenv is loaded
by this CLI; connection settings must be supplied by the operator environment.
Not an activation command. --apply requires independent deployment approval.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from matchmaker_agent.concept_identity import stored_concept_identity
from matchmaker_agent.related_interest_contract import INDEX_NAME, embedding_fingerprint, embedding_manifest, unit_vector
from matchmaker_agent.semantic_evidence_readiness import verified_vector


def prepare_batch(records, embed, model):
    if len(records) > 20:
        raise ValueError("embedding_batch_limit")
    valid = []
    for record in records:
        identity = stored_concept_identity(record)
        if identity and identity.canonicalization_version == "v2":
            valid.append(identity)
    if not valid:
        return []
    vectors = embed([i.semantic_text for i in valid], task_type="semantic_similarity",
        output_dimensionality=768, request_timeout_seconds=10.0)
    if not isinstance(vectors, list) or len(vectors) != len(valid):
        raise ValueError("embedding_batch_invalid")
    normalized = [unit_vector(v) for v in vectors]
    if any(v is None for v in normalized):
        raise ValueError("embedding_vector_invalid")
    return [{"key": i.key, "source_hash": i.semantic_input_hash, "semantic_text": i.semantic_text,
        "fingerprint": embedding_fingerprint(model), "vector": v}
        for i, v in zip(valid, normalized)]


def existing_vector_state(record, model, frozen):
    """Missing only if NO versioned metadata exists; conflicts never overwrite."""
    identity = stored_concept_identity(record)
    if not identity or identity.canonicalization_version != 'v2':
        raise ValueError('unverified_identity_v2_source')
    present = [k for k, v in record.items() if k.startswith('embedding_v2') and v is not None]
    if not present:
        return 'missing'
    if (not verified_vector(record, embedding_fingerprint(model))
            or record.get('embedding_v2_provenance_fingerprint') != frozen['provenance_fingerprint']
            or record.get('embedding_v2_manifest') != json.dumps(embedding_manifest(model), sort_keys=True)):
        raise ValueError('existing_versioned_embedding_conflict')
    from scripts.semantic_embedding_pipeline import canonical_json
    if record.get('embedding_v2_provenance_manifest') != canonical_json(frozen['provenance']):
        raise ValueError('existing_versioned_provenance_conflict')
    return 'compatible'


def guarded_write(tx, batch, expected_nodes, model, frozen, *, now):
    """All-or-nothing bounded CAS under Concept locks; no automatic tx retry."""
    from scripts.semantic_embedding_pipeline import canonical_json
    if not 1 <= len(batch) <= 100 or len({r['key'] for r in batch}) != len(batch):
        raise ValueError('invalid_generated_batch')
    for item in batch:
        original = expected_nodes[item['key']]['props']
        if not verified_vector({**original, 'vector': item['vector'], 'fingerprint': item['fingerprint'],
                'source_hash': item['source_hash'], 'provenance': frozen['provenance_fingerprint']}, embedding_fingerprint(model)):
            raise ValueError('generated_vector_invalid')
    pending = []
    protected = {}
    for item in sorted(batch, key=lambda r: r['key']):
        rows = list(tx.run('''MATCH (c:Concept {key:$key})
            SET c.embedding_v2_fingerprint=c.embedding_v2_fingerprint
            RETURN elementId(c) AS id,properties(c) AS props''', key=item['key']))
        if len(rows) != 1 or rows[0]['id'] != expected_nodes[item['key']]['id']:
            raise ValueError('concept_changed_before_write')
        props = dict(rows[0]['props'])
        identity = stored_concept_identity(props)
        if (not identity or identity.semantic_text != item['semantic_text']
                or identity.semantic_input_hash != item['source_hash']):
            raise ValueError('semantic_source_changed_before_write')
        if existing_vector_state(props, model, frozen) == 'missing':
            pending.append(item)
        protected[item['key']] = {k: v for k, v in props.items() if not k.startswith('embedding_v2')}
    if not pending:
        return {'written': 0, 'reused': len(batch)}
    row = tx.run('''UNWIND $batch AS item MATCH (c:Concept {key:item.key})
        WHERE c.canonicalization_version='v2' AND c.fidelity_status='complete'
          AND c.semantic_text=item.semantic_text AND c.semantic_input_hash=item.source_hash
          AND c.embedding_v2 IS NULL AND c.embedding_v2_fingerprint IS NULL
        SET c.embedding_v2=item.vector,c.embedding_v2_source_hash=item.source_hash,
            c.embedding_v2_fingerprint=item.fingerprint,c.embedding_v2_manifest=$manifest,
            c.embedding_v2_provenance_fingerprint=$provenance_fp,
            c.embedding_v2_provenance_manifest=$provenance,c.embedding_v2_created_at=$now
        RETURN count(c) AS written''', batch=pending, now=now,
        manifest=json.dumps(embedding_manifest(model), sort_keys=True),
        provenance_fp=frozen['provenance_fingerprint'], provenance=canonical_json(frozen['provenance'])).single()
    if not row or row['written'] != len(pending):
        raise ValueError('embedding_write_count_mismatch')
    after = list(tx.run('''UNWIND $keys AS key MATCH (c:Concept {key:key})
        RETURN c.key AS key,properties(c) AS props''', keys=sorted(protected)))
    if len(after) != len(protected):
        raise ValueError('embedding_postwrite_scope_mismatch')
    for record in after:
        props = dict(record['props'])
        if (existing_vector_state(props, model, frozen) != 'compatible'
                or {k: v for k, v in props.items() if not k.startswith('embedding_v2')} != protected[record['key']]):
            raise ValueError('embedding_postwrite_integrity')
    return {'written': len(pending), 'reused': len(batch)-len(pending)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-file", type=Path, required=True, help="JSON array of explicit Concept keys; max 100")
    parser.add_argument("--provenance-file", type=Path, required=True, help="Approved frozen provenance/runtime manifest; not credentials")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ack-versioned-embedding-write", action="store_true")
    parser.add_argument("--create-index", action="store_true", help="Deprecated: index/schema writes require a separate approved operation")
    args = parser.parse_args()
    if args.apply and not args.ack_versioned_embedding_write:
        parser.error("apply needs explicit versioned-write acknowledgement")
    if args.create_index:
        parser.error("rollout preparation reuses the ONLINE v2 index; schema writes are separate")
    model = os.getenv("GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2")
    provider = None
    written = 0
    commit_attempted = False
    try:
        from scripts.semantic_embedding_pipeline import provenance, validate_frozen, SafeGeminiEmbedding
        frozen = json.loads(args.provenance_file.read_text())
        expected = frozen['provenance']
        # Local SDK/source/config drift must fail before any provider call.
        current = provenance(model, {'name': model, 'provider_reported_version': expected['provider_reported_model_version'],
            'supported_methods': ['embedContent']}, prepare_batch)
        validate_frozen(frozen, current)
        keys = json.loads(args.keys_file.read_text())
        if not isinstance(keys, list) or not 1 <= len(keys) <= 100 or any(
                not isinstance(k, str) or not k.startswith("v2_") or len(k) != 51 for k in keys):
            raise ValueError("invalid_explicit_key_batch")
        from neo4j import GraphDatabase, Query
        from matchmaker_agent.related_interest_retrieval import index_metadata
        uri = os.environ["NEO4J_URI"]
        auth = (os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
        fingerprint = embedding_fingerprint(model)
        with GraphDatabase.driver(uri, auth=auth, connection_timeout=3,
                connection_acquisition_timeout=3, max_transaction_retry_time=0) as driver:
            with driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j"),
                    default_access_mode="WRITE" if args.apply else "READ") as session:
                if not index_metadata(session)['ready']:
                    raise ValueError('existing_dedicated_index_not_ready')
                rows = list(session.run(Query("""
                    UNWIND $keys AS key MATCH (c:Concept {key:key})
                    RETURN c.key AS key,elementId(c) AS id,properties(c) AS props
                """, timeout=3), keys=sorted(set(keys))))
                if len(rows) != len(set(keys)) or len({r['key'] for r in rows}) != len(rows):
                    raise ValueError('explicit_key_missing_or_ambiguous')
                nodes = {r['key']: {'id': r['id'], 'props': dict(r['props'])} for r in rows}
                states = {k: existing_vector_state(r['props'], model, frozen) for k, r in nodes.items()}
                missing = [nodes[k]['props'] for k in sorted(nodes) if states[k] == 'missing']
                report = {"status": "dry_run", "requested": len(set(keys)), "found": len(rows),
                    "eligible_complete_v2": len(nodes), "missing": len(missing), "compatible_reused": len(nodes)-len(missing),
                    "manifest": embedding_manifest(model), "fingerprint": fingerprint,
                    "provenance_fingerprint": frozen['provenance_fingerprint'],
                    "source_hashes": {k: r['props']['semantic_input_hash'] for k, r in nodes.items()},
                    "written": 0, "feature_enabled": False, "index_created": False}
                if args.apply and missing:
                    # Generate and validate the ENTIRE explicit batch first.
                    # Every vector and key remains in memory, never in reports.
                    provider = SafeGeminiEmbedding(model, frozen, prepare_batch)
                    generated = []
                    for offset in range(0, len(missing), 20):
                        generated.extend(prepare_batch(missing[offset:offset+20], provider.embed, model))
                    if len(generated) != len(missing):
                        raise ValueError('generated_batch_incomplete')
                    with session.begin_transaction(timeout=15) as tx:
                        receipt = guarded_write(tx, generated, nodes, model, frozen, now=time.time())
                        commit_attempted = True
                        tx.commit()
                    written = receipt['written']
                    report.update(written=written, compatible_reused=len(nodes)-written,
                        provider_failures=provider.failures, api_attempts=provider.attempts, api_retries=provider.retries)
                if args.apply:
                    report['status'] = 'applied' if written else 'idempotent_noop'
                print(json.dumps(report, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({"status": "error", "error_code": "versioned_embedding_preparation_failed",
            "feature_enabled": False, "written_acknowledged": written,
            "commit_outcome_unknown": commit_attempted and not written}))
        return 1
    finally:
        if provider: provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
