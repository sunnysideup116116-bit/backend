"""Functional v1 integration only: disposable Graph, fake validator, synthetic vectors.

Not R3 qualification, semantic precision scoring, or a production readiness claim.
Uses the existing ownership + local-only socket guards; never imports Server startup.
"""
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch

import run_readiness as harness

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from neo4j import GraphDatabase
from matchmaker_agent.concept_identity import canonicalize_concept
from matchmaker_agent.related_interest_contract import INDEX_NAME, embedding_fingerprint
from matchmaker_agent import related_interest_retrieval as service
from matchmaker_agent import related_interest_validator as validator


def run():
    state = harness.read_state()
    topology = harness.verify_container(state)
    model = "models/gemini-embedding-2"
    fp = embedding_fingerprint(model)
    vector = [1.0]+[0.0]*767
    texts = {"Playing Football": "role_mismatch", "Watching Basketball": "sibling_related",
             "Whisky Tasting": "unrelated"}
    identities = {text: canonicalize_concept(text) for text in texts}
    query = canonicalize_concept("Watching Football")
    client = SimpleNamespace()
    def create(**request):
        rows = [{"id": p["id"], "relation": texts[p["C"]]} for p in
                json.loads(request["messages"][1]["content"])["pairs"]]
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop",
            message=SimpleNamespace(content=json.dumps({"results": rows})))])
    client.with_options = lambda **_options: client
    client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    req = SimpleNamespace(embedding_model=model, embedding_fingerprint=fp, query_embedding=vector,
        request_budget_seconds=27.0,
        neighbor_limit=24, concept_limit=8, min_similarity=.82, requester_user_id="pilot-owner",
        excluded_user_ids=[], per_concept_limit=10, evidence_limit=3, candidate_limit=12)
    with harness.local_network_only(state["bolt"]), GraphDatabase.driver(
            f"bolt://127.0.0.1:{state['bolt']}", auth=("neo4j", state["password"]),
            max_transaction_retry_time=0) as driver:
        with driver.session(database="neo4j") as session:
            session.run("CREATE CONSTRAINT related_local_key IF NOT EXISTS FOR (c:Concept) REQUIRE c.key IS UNIQUE").consume()
            session.run(f"""CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS FOR (c:Concept) ON c.embedding_v2
                OPTIONS {{indexConfig: {{`vector.dimensions`:768, `vector.similarity_function`:'cosine'}}}}""").consume()
            for text, identity in identities.items():
                session.run("""MERGE (c:Concept {key:$key}) SET c.semantic_text=$text,
                    c.canonicalization_version='v2', c.semantic_input_hash=$hash,
                    c.fidelity_status='complete',
                    c.embedding_v2=$vector, c.embedding_v2_source_hash=$hash,
                    c.embedding_v2_fingerprint=$fp, c.synthetic_fixture=true
                """, key=identity.key, text=text, hash=identity.semantic_input_hash, vector=vector, fp=fp).consume()
                # Saturate fanout; one candidate deliberately owns two Concepts.
                session.run("""UNWIND range(0,24) AS i MERGE (u:User {id:'pilot-synthetic-'+toString(i)})
                    WITH u MATCH (c:Concept {key:$key}) MERGE (u)-[:PREFERS]->(c)
                """, key=identity.key).consume()
            session.run("""MERGE (u:User {id:'pilot-avoid-only'}) WITH u
                MATCH (c:Concept {key:$key}) MERGE (u)-[:AVOIDS]->(c)
            """, key=identities["Playing Football"].key).consume()
            session.run("CALL db.awaitIndexes(30)").consume()
        before = harness.digest_graph(driver)
        captured = []
        with driver.session(database="neo4j", default_access_mode="READ") as session:
            class Capture:
                def run(self, statement, **params):
                    text = str(statement)
                    if "db.index.vector.queryNodes" in text or "UNWIND $concepts" in text:
                        captured.append((text, params))
                    return session.run(statement, **params)
            start = time.perf_counter()
            with patch.object(service, "enabled", return_value=True), patch.object(
                    validator, "_completion", lambda _client, **kwargs: create(**{k: v for k, v in kwargs.items() if k != "timeout"})):
                result = service.retrieve(Capture(), req, query, client, "deepseek-test-double", model)
            elapsed = time.perf_counter()-start
            assert result["status"] == "success" and result["validator_counts"]["accepted"] == 2
            assert result["validator_counts"]["rejected"] == 1
            ids = [r["candidate_id"] for r in result["candidates"]]
            assert ids and len(ids) == len(set(ids)) <= req.candidate_limit and "pilot-avoid-only" not in ids
            assert all(e["relation"] in {"role_mismatch", "sibling_related"}
                for r in result["candidates"] for e in r["evidence"])
            plans = []
            for statement, params in captured:
                summary = session.run("PROFILE "+statement, **params).consume()
                nodes = harness.plan_nodes(summary.profile)
                operators = [n["operatorType"] for n in nodes]
                assert not any("AllNodesScan" in op or "NodeByLabelScan" in op for op in operators)
                if "UNWIND" in statement:
                    assert len(params["concepts"]) == 2
                    assert all(c["relation"] in {"role_mismatch", "sibling_related"} for c in params["concepts"])
                    assert statement.index("LIMIT $per_concept_limit") < statement.index("WHERE candidate.id")
                plans.append({"kind": "ANN" if "queryNodes" in statement else "expansion", "operators": operators})
            index = service.index_metadata(session)
        assert before == harness.digest_graph(driver)
    report = {"status": "PASS", "synthetic_only": True, "real_semantic_precision": "not_evaluated",
        "production_ready": False, "topology": topology, "index": index, "plans": plans,
        "dedupe": True, "prefers_only": True, "validator_before_expansion": True,
        "identity_edges_unchanged_by_retrieval": True, "candidate_count": len(ids),
        "validator_counts": result["validator_counts"], "fallback_ms": round(elapsed*1000, 3)}
    harness.json_file(harness.ARTIFACTS/"related_interest_local.json", report)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    run()
