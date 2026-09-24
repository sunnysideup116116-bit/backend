#!/usr/bin/env python3
"""Explicit-key, dry-run-first preparation of a separate Gemini vector space.

Never rekeys/merges Concepts or changes preference edges. No dotenv is loaded
by this CLI; connection settings must be supplied by the operator environment.
Not an activation command. --apply requires independent deployment approval.
"""
import argparse
from contextlib import redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from matchmaker_agent.concept_identity import stored_concept_identity
from matchmaker_agent.related_interest_contract import INDEX_NAME, embedding_fingerprint, embedding_manifest, unit_vector


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keys-file", type=Path, required=True, help="JSON array of explicit Concept keys; max 100")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--ack-versioned-embedding-write", action="store_true")
    parser.add_argument("--create-index", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.ack_versioned_embedding_write:
        parser.error("apply needs explicit versioned-write acknowledgement")
    if args.create_index and not args.apply:
        parser.error("index creation is a write and requires --apply")
    model = os.getenv("GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2")
    try:
        keys = json.loads(args.keys_file.read_text())
        if not isinstance(keys, list) or not 1 <= len(keys) <= 100 or any(
                not isinstance(k, str) or not k.startswith("v2_") or len(k) != 51 for k in keys):
            raise ValueError("invalid_explicit_key_batch")
        from neo4j import GraphDatabase, Query
        uri = os.environ["NEO4J_URI"]
        auth = (os.environ["NEO4J_USERNAME"], os.environ["NEO4J_PASSWORD"])
        fingerprint = embedding_fingerprint(model)
        with GraphDatabase.driver(uri, auth=auth, connection_timeout=3,
                connection_acquisition_timeout=3, max_transaction_retry_time=0) as driver:
            with driver.session(database=os.getenv("NEO4J_DATABASE", "neo4j"),
                    default_access_mode="WRITE" if args.apply else "READ") as session:
                rows = list(session.run(Query("""
                    UNWIND $keys AS key MATCH (c:Concept {key:key})
                    RETURN c.key AS key, c.semantic_text AS semantic_text,
                      c.canonicalization_version AS canonicalization_version,
                      c.semantic_input_hash AS semantic_input_hash, c.fidelity_status AS fidelity_status
                """, timeout=3), keys=sorted(set(keys))))
                records = [dict(row) for row in rows]
                complete = [r for r in records if (i := stored_concept_identity(r)) and i.canonicalization_version == "v2"]
                report = {"status": "dry_run", "requested": len(set(keys)), "found": len(records),
                    "eligible_complete_v2": len(complete), "skipped_unverified": len(records)-len(complete),
                    "manifest": embedding_manifest(model), "fingerprint": fingerprint,
                    "written": 0, "feature_enabled": False}
                if args.apply:
                    if args.create_index:
                        session.run(Query(f"""CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS
                            FOR (c:Concept) ON c.embedding_v2
                            OPTIONS {{indexConfig: {{`vector.dimensions`: 768, `vector.similarity_function`: 'cosine'}}}}
                        """, timeout=10)).consume()
                    # Uses the same Gemini request semantics as query embedding.
                    # Legacy key-pool console output is discarded in this single-
                    # process CLI; neither raw exception nor vector is reported.
                    os.environ["AYUE_SKIP_DOTENV"] = "1"
                    sys.path.insert(0, str(ROOT / "social"))
                    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                        from services.ai_service import get_embeddings
                        from config import GOOGLE_EMBEDDING_MODEL
                        if embedding_fingerprint(GOOGLE_EMBEDDING_MODEL) != fingerprint:
                            raise ValueError("embedding_config_mismatch")
                        for offset in range(0, len(complete), 20):
                            batch = prepare_batch(complete[offset:offset+20], get_embeddings, model)
                            row = session.run(Query("""
                                UNWIND $batch AS item MATCH (c:Concept {key:item.key})
                                WHERE c.canonicalization_version='v2' AND c.semantic_input_hash=item.source_hash
                                  AND c.semantic_text=item.semantic_text AND c.fidelity_status='complete'
                                SET c.embedding_v2=item.vector, c.embedding_v2_source_hash=item.source_hash,
                                    c.embedding_v2_fingerprint=item.fingerprint, c.embedding_v2_created_at=$now,
                                    c.embedding_v2_manifest=$manifest
                                RETURN count(c) AS written
                            """, timeout=5), batch=batch, now=time.time(),
                                manifest=json.dumps(embedding_manifest(model), sort_keys=True)).single()
                            report["written"] += int(row["written"])
                    report["status"] = "applied" if report["written"] == len(complete) else "partial_source_changed"
                print(json.dumps(report, ensure_ascii=False))
        return 0
    except Exception:
        print(json.dumps({"status": "error", "error_code": "versioned_embedding_preparation_failed",
            "feature_enabled": False, "partial_writes_possible": bool(args.apply)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
