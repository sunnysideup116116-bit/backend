#!/usr/bin/env python3
"""Opt-in synthetic identity-v2 component transactions on owned local Neo4j only.

This is not a Server/API deployment or production-compatibility certification.
Start/stop the existing run_readiness.py harness separately. No provider calls.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
from copy import deepcopy
import hashlib
import inspect
import io
import json
import logging
import math
import os
from pathlib import Path
import re
from contextlib import redirect_stdout
from types import ModuleType, SimpleNamespace
import sys
import time

import run_readiness as local

HERE = Path(__file__).resolve().parent
REPORT = HERE / "artifacts" / "identity-v2-local.json"
MARKER = local.MARKER


def component_source(relative, names, supplied):
    """Compile exact selected sync/async definitions without startup/imports."""
    path = local.ROOT / relative
    text = path.read_text()
    local.SOURCE_HASHES[relative] = hashlib.sha256(text.encode()).hexdigest()
    tree = ast.parse(text)
    definitions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}
    selected, pending = set(), list(names)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        local.require(name in definitions, "component definition unavailable")
        selected.add(name)
        pending.extend(n.id for n in ast.walk(definitions[name])
                       if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                       and n.id in definitions and n.id not in supplied and n.id not in selected)
    module = ModuleType("_identity_v2_local_" + relative.replace("/", "_").replace(".", "_"))
    sys.modules[module.__name__] = module
    namespace = module.__dict__
    namespace.update(supplied)
    nodes = [deepcopy(n) for n in tree.body
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name in selected]
    for node in nodes:
        node.decorator_list = []
    compiled = ast.fix_missing_locations(ast.Module(body=[
        ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0), *nodes
    ], type_ignores=[]))
    exec(compile(compiled, str(path), "exec"), namespace)
    return namespace


def snapshot(driver, legacy=False):
    clause = ":LegacyFixture" if legacy else ""
    with driver.session(database="neo4j") as session:
        nodes = [r.data() for r in session.run(
            f"MATCH (n{clause}) RETURN elementId(n) AS id,labels(n) AS labels,properties(n) AS props ORDER BY id")]
        edges = [r.data() for r in session.run(
            f"MATCH (a{clause})-[r]->(b{clause}) RETURN elementId(r) AS id,elementId(a) AS a,"
            "elementId(b) AS b,type(r) AS type,properties(r) AS props ORDER BY id")]
    return json.dumps({"nodes": nodes, "edges": edges}, ensure_ascii=False, sort_keys=True, default=str)


def operators(plan):
    if not plan:
        return []
    return [plan.get("operatorType", "")] + [
        item for child in plan.get("children", []) for item in operators(child)
    ]


def run():
    from neo4j import GraphDatabase, Query
    from fastapi import HTTPException
    from pydantic import BaseModel, Field, ValidationError, field_validator

    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    local.SOURCE_HASHES[str(Path(__file__).relative_to(local.ROOT))] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    local.require(all(os.environ.get(f, "off").lower() == "off" for f in local.FLAGS), "semantic flags must remain OFF")
    os.environ.update(AYUE_SKIP_DOTENV="1", DOTENV_DISABLED="1",
                      LLM_API_KEY="offline-test", LLM_BASE_URL="http://provider.invalid/v1", LLM_MODEL_ID="offline-test")
    state = local.read_state()
    topology = local.verify_container(state)
    local.require(state["image"] == "neo4j:2026.08.1", "this recorded local baseline requires 2026.08.1")
    uri, auth = f"bolt://127.0.0.1:{state['bolt']}", ("neo4j", state["password"])
    identity = local.canonical_module()
    captures, scenarios, errors = [], [], []
    common = {name: getattr(identity, name) for name in dir(identity) if not name.startswith("__")}
    common.update(re=re, math=math, time=time, hashlib=hashlib, BaseModel=BaseModel, Field=Field,
                  field_validator=field_validator, HTTPException=HTTPException, Query=Query,
                  os=SimpleNamespace(getenv=lambda _name, default=None: default))
    registration = component_source("matchmaker_agent/registration_graph.py",
        ["RegistrationProjection", "project_identity", "seed_registration", "registration_message_id",
         "assert_existing_preference_identities"], {**common, "VERSION": "registration-bootstrap-v1"})
    registration["RegistrationProjection"].model_rebuild()

    class Session:
        def __init__(self, session):
            self.inner = session
        def __enter__(self):
            self.inner.__enter__()
            return self
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
        def run(self, query, **params):
            captures.append((str(query), deepcopy(params)))
            try:
                return self.inner.run(query, **params)
            except Exception as exc:
                errors.append({"category": type(exc).__name__, "code": getattr(exc, "code", ""), "query": str(query)})
                raise
        def execute_write(self, callback, *args):
            return self.inner.execute_write(lambda tx: callback(Session(tx), *args))

    class Driver:
        def __init__(self):
            self.inner = GraphDatabase.driver(uri, auth=auth, connection_timeout=3, max_transaction_retry_time=0)
        def __enter__(self):
            return self
        def __exit__(self, *_args):
            self.inner.close()
        def session(self, **kwargs):
            local.require(kwargs.get("database") == "neo4j", "unexpected database")
            return Session(self.inner.session(**kwargs))

    def local_driver(target, auth=None, **kwargs):
        local.require(target == uri and auth == ("neo4j", state["password"]), "non-fixture Graph refused")
        return Driver()

    api = component_source("matchmaker_agent/agent_api.py", [
        "MemoryApplyRequest", "MemoryActionRequest", "PreferenceCandidateRequest",
        "ConceptEmbeddingProjectionRequest", "apply_memory", "memory_action",
        "registration_projection", "preference_candidates", "project_concept_embeddings",
    ], {**common, **{name: registration[name] for name in [
        "RegistrationProjection", "project_identity", "seed_registration", "assert_existing_preference_identities"]},
        "_neo4j_config": lambda: (uri, auth, "neo4j"),
        "GraphDatabase": SimpleNamespace(driver=local_driver)})
    for name in ("MemoryApplyRequest", "MemoryActionRequest", "PreferenceCandidateRequest", "ConceptEmbeddingProjectionRequest"):
        api[name].model_rebuild()

    with local.local_network_only(state["bolt"]):
        with GraphDatabase.driver(uri, auth=auth, connection_timeout=3, max_transaction_retry_time=0) as driver:
            def invoke(name, request):
                started = time.perf_counter()
                with redirect_stdout(io.StringIO()):
                    result = api[name](request)
                    result = asyncio.run(result) if inspect.isawaitable(result) else result
                scenarios.append({"operation": name, "status": result.get("status"),
                                  "error_code": result.get("error_code"),
                                  "ms": (time.perf_counter() - started) * 1000})
                # Only this verified, reset synthetic database is touched.
                with driver.session(database="neo4j") as session:
                    session.run("MATCH (n) WHERE n.readiness_fixture IS NULL SET n.readiness_fixture=$marker", marker=MARKER).consume()
                return result

            def preference(text, stance="like", category="activity"):
                return {**identity.canonicalize_concept(text).as_dict(), "stance": stance,
                        "confidence": 1.0, "category": category, "evidence_span": text}

            def apply(owner, texts, **kwargs):
                return invoke("apply_memory", api["MemoryApplyRequest"](
                    user_id=owner, memories=[preference(text) for text in texts],
                    message_id="local-v2:" + owner + ":" + str(len(scenarios)), **kwargs))

            def exact(topic, **kwargs):
                return invoke("preference_candidates", api["PreferenceCandidateRequest"](
                    requester_user_id="p01-searcher", topic=topic, **kwargs))

            with driver.session(database="neo4j") as session:
                version = session.run("CALL dbms.components() YIELD name,versions,edition WHERE name='Neo4j Kernel' RETURN versions,edition").single().data()
                local.require(version["edition"] == "community" and version["versions"][0] == "2026.08.1", "wrong server baseline")
                foreign = session.run("MATCH(n) WHERE coalesce(n.readiness_fixture,'')<>$marker RETURN count(n) AS n", marker=MARKER).single()["n"]
                local.require(foreign == 0, "refusing non-readiness data")
                session.run("MATCH(n {readiness_fixture:$marker}) DETACH DELETE n", marker=MARKER).consume()
                ddl = "\n".join(line for line in (HERE / "schema.cypher").read_text().splitlines() if not line.startswith("//"))
                for statement in ddl.split(";"):
                    if statement.strip():
                        session.run(statement).consume()
                session.run("CALL db.awaitIndexes(60)").consume()

            prefix = "Walking through historical gardens and riverside paths "
            first, second = prefix + "with step-free wheelchair access", prefix + "with steep rocky stairs"
            one, two = identity.canonicalize_concept(first), identity.canonicalize_concept(second)
            legacy = identity.canonicalize_concept_v1(first)
            local.require(legacy.key == identity.canonicalize_concept_v1(second).key and one.key != two.key, "collision fixture malformed")
            with driver.session(database="neo4j") as session:
                session.run("CREATE (u:User:LegacyFixture {id:'p01-legacy',readiness_fixture:$marker}),"
                            "(c:Concept:LegacyFixture {key:$key,label:$label,readiness_fixture:$marker}) "
                            "CREATE (u)-[:PREFERS {old_property:'unchanged'}]->(c),"
                            "(u)-[:AVOIDS {old_property:'also-unchanged'}]->(c)",
                            marker=MARKER, key=legacy.key, label=legacy.label).consume()
            legacy_before = snapshot(driver, True)
            local.require(apply("p01-one", [first])["status"] == "success", "first full preference apply failed")
            local.require(apply("p01-two", [second])["status"] == "success", "second full preference apply failed")
            local.require(exact(first)["candidate_ids"] == ["p01-one"], "exact lookup used colliding legacy prefix")
            local.require(exact(second)["candidate_ids"] == ["p01-two"], "exact qualifier distinction failed")
            local.require(exact(prefix + "with a different unsupported qualifier")["candidate_ids"] == [], "unknown legacy prefix became exact evidence")
            for number, alias in enumerate(("Kpop", "K-pop", "K pop", "k-pop")):
                local.require(apply(f"p01-alias-{number}", [alias])["status"] == "success", "alias apply failed")
            local.require(len(exact("K-pop")["candidate_ids"]) == 4, "deterministic alias exact retrieval failed")
            with driver.session(database="neo4j") as session:
                session.run("UNWIND range(1,110) AS number CREATE(u:User {id:'p01-pool-'+toString(number),readiness_fixture:$marker}) "
                            "WITH u MATCH(c:Concept {key:$key}) CREATE(u)-[:PREFERS]->(c)", marker=MARKER, key=one.key).consume()
            bounded = exact(first, limit=100)
            local.require(len(bounded["candidate_ids"]) == 100, "exact candidate hard bound failed")

            before = snapshot(driver)
            bad = preference(first)
            bad.update(semantic_text="x" * 501, label="x" * 501)
            rejected = invoke("apply_memory", api["MemoryApplyRequest"](
                user_id="p01-invalid", memories=[preference(second), bad], message_id="over-limit-operation"))
            local.require(rejected["status"] == "error" and snapshot(driver) == before, "overlimit batch partially wrote")

            registration_request = registration["RegistrationProjection"](user_id="p01-registration", name="Synthetic Readiness", observed_at=100)
            local.require(invoke("registration_projection", registration_request)["status"] == "success", "registration identity failed")
            reg = invoke("apply_memory", api["MemoryApplyRequest"](
                user_id="p01-registration", memories=[preference(first, category="lifestyle")],
                surface="registration_interest", message_id=registration["registration_message_id"]("p01-registration")))
            local.require(reg["status"] == "success", "registration seed failed")
            with driver.session(database="neo4j") as session:
                registered = session.run("MATCH(:User {id:'p01-registration'})-[:PREFERS]->(c:Concept) RETURN c.key AS key,c.semantic_text AS text").single()
                local.require(registered and registered["key"] == one.key and registered["text"] == first, "registration seed lost semantic source")

            projection = [{**item.as_dict(), "kind": "interest", "embedding": local.vector(0, .9),
                           "embedding_model": local.MODEL, "embedding_task": "semantic_similarity"} for item in (one, two)]
            with driver.session(database="neo4j") as session:
                session.run("MATCH(c:Concept {key:$key}) SET c.semantic_input_hash='corrupt-fixture'", key=two.key).consume()
            before = snapshot(driver)
            rejected = invoke("project_concept_embeddings", api["ConceptEmbeddingProjectionRequest"](concepts=projection))
            local.require(rejected["status"] == "error" and snapshot(driver) == before, "embedding target mismatch partially wrote")
            with driver.session(database="neo4j") as session:
                session.run("MATCH(c:Concept {key:$key}) SET c.semantic_input_hash=$hash", key=two.key, hash=two.semantic_input_hash).consume()
            valid = invoke("project_concept_embeddings", api["ConceptEmbeddingProjectionRequest"](concepts=projection))
            local.require(valid["status"] == "success" and valid["embedded_count"] == 2, "valid embedding projection failed")
            with driver.session(database="neo4j") as session:
                rows = [r.data() for r in session.run("MATCH(c:Concept) WHERE c.key IN $keys RETURN c.key AS key,c.semantic_text AS semantic_text,c.label AS label,size(c.embedding) AS dimensions", keys=[one.key, two.key])]
                expected_sources = {one.key: first, two.key: second}
                local.require(len(rows) == 2 and all(r["semantic_text"] == r["label"] == expected_sources[r["key"]] and r["dimensions"] == 768 for r in rows), "projection changed full source")
            before = snapshot(driver)
            stale = invoke("project_concept_embeddings", api["ConceptEmbeddingProjectionRequest"](concepts=[
                {"key": one.key, "label": first[:60], "kind": "interest", "embedding": local.vector(0, .9)}]))
            local.require(stale["status"] == "error" and snapshot(driver) == before, "old worker damaged v2 source")

            for owner, key, expected in (("p01-wrong-owner", one.key, "not_found"), ("p01-legacy", legacy.key, "error")):
                before = snapshot(driver)
                correction = invoke("memory_action", api["MemoryActionRequest"](user_id=owner, key=key, action="correct", value="Different full preference"))
                local.require(correction["status"] == expected and snapshot(driver) == before, "unauthorized/legacy correction mutated data")
            corrected = invoke("memory_action", api["MemoryActionRequest"](user_id="p01-two", key=two.key, action="correct", value=second + " only during daylight"))
            local.require(corrected["status"] == "success", "v2 owner correction failed")
            before = snapshot(driver)
            alias_correction = invoke("memory_action", api["MemoryActionRequest"](user_id="p01-alias-0", key=identity.canonicalize_concept("Kpop").key, action="correct", value="K pop"))
            local.require(alias_correction["status"] == "success" and snapshot(driver) == before, "same-identity alias correction removed the existing edge")

            # Real persistence at the semantic boundary. This is the durable
            # Graph component contract, not a larger registration form limit.
            boundary_prefix, boundary_suffix = "Reading long-form stories ", " only while seated"
            boundary_texts = {
                size: boundary_prefix + "x" * (size - len(boundary_prefix) - len(boundary_suffix)) + boundary_suffix
                for size in (499, 500, 501)
            }
            boundary_identities = {size: identity.canonicalize_concept(boundary_texts[size]) for size in (499, 500)}
            for size, concept in boundary_identities.items():
                owner = f"p01-boundary-{size}"
                local.require(len(boundary_texts[size]) == size, "boundary fixture length mismatch")
                local.require(apply(owner, [boundary_texts[size]])["status"] == "success", "boundary durable write failed")
                local.require(exact(boundary_texts[size])["candidate_ids"] == [owner], "boundary exact lookup failed")
            boundary_projection = [
                {**concept.as_dict(), "kind": "interest", "embedding": local.vector(0, .9),
                 "embedding_model": local.MODEL, "embedding_task": "semantic_similarity"}
                for concept in boundary_identities.values()
            ]
            result = invoke("project_concept_embeddings", api["ConceptEmbeddingProjectionRequest"](concepts=boundary_projection))
            local.require(result["status"] == "success" and result["embedded_count"] == 2, "boundary vector projection failed")
            for old_size, new_size in ((499, 500), (500, 499)):
                corrected = invoke("memory_action", api["MemoryActionRequest"](
                    user_id=f"p01-boundary-{old_size}", key=boundary_identities[old_size].key,
                    action="correct", value=boundary_texts[new_size]))
                local.require(corrected["status"] == "success" and corrected["key"] == boundary_identities[new_size].key,
                              "boundary owner correction failed")
            for size, expected_owner in ((499, "p01-boundary-500"), (500, "p01-boundary-499")):
                local.require(exact(boundary_texts[size])["candidate_ids"] == [expected_owner], "boundary corrected exact lookup failed")
            with driver.session(database="neo4j") as session:
                stored = [r.data() for r in session.run(
                    "UNWIND $keys AS key MATCH(c:Concept {key:key}) "
                    "RETURN c.key AS key,c.semantic_text AS semantic_text,c.label AS label,"
                    "c.display_label AS display_label,c.semantic_input_hash AS semantic_input_hash,"
                    "c.embedding_source_hash AS embedding_source_hash,size(c.embedding) AS dimensions",
                    keys=[concept.key for concept in boundary_identities.values()])]
                expected = {concept.key: concept for concept in boundary_identities.values()}
                local.require(len(stored) == 2 and all(
                    row["semantic_text"] == row["label"] == expected[row["key"]].semantic_text
                    and row["semantic_input_hash"] == row["embedding_source_hash"] == expected[row["key"]].semantic_input_hash
                    and row["display_label"] == expected[row["key"]].display_label
                    and row["dimensions"] == 768 for row in stored), "boundary stored source/hash roundtrip failed")
            before = snapshot(driver)
            oversized = {**boundary_identities[500].as_dict(), "semantic_text": boundary_texts[501], "label": boundary_texts[501]}
            rejected = invoke("apply_memory", api["MemoryApplyRequest"](
                user_id="p01-boundary-reject", message_id="p01-boundary-501",
                memories=[preference(boundary_texts[499]), {**oversized, "stance": "like", "confidence": 1.0}]))
            local.require(rejected.get("error_code") == "preference_text_too_long" and snapshot(driver) == before,
                          "501-character durable batch partially wrote")
            rejected = invoke("project_concept_embeddings", api["ConceptEmbeddingProjectionRequest"](concepts=[
                boundary_projection[0], {**boundary_projection[1], **oversized}]))
            local.require(rejected.get("error_code") == "preference_text_too_long" and snapshot(driver) == before,
                          "501-character vector batch partially wrote")
            rejected = invoke("memory_action", api["MemoryActionRequest"](
                user_id="p01-boundary-499", key=boundary_identities[500].key, action="correct", value=boundary_texts[501]))
            local.require(rejected.get("error_code") == "preference_text_too_long" and snapshot(driver) == before,
                          "501-character correction mutated the owner edge")
            try:
                api["PreferenceCandidateRequest"](requester_user_id="p01-searcher", topic=boundary_texts[501])
            except ValidationError:
                pass
            else:
                raise RuntimeError("501-character exact API input was not rejected")
            local.require(snapshot(driver) == before, "boundary rejection changed Graph state")
            boundary_matrix = {
                "499": "PASS: write/storage/hash/exact/projection/correction",
                "500": "PASS: write/storage/hash/exact/projection/correction",
                "501": "PASS: entire write/projection/correction operation rejected; exact API schema rejected",
                "registration_initial_interest_ingress_limit": identity.MAX_REGISTRATION_INTEREST_CHARS,
                "provider_precision": "not tested; deterministic geometry only",
            }

            # Fresh ingress converts with the pinned policy. Previously stored
            # v2 semantic text is a different contract and remains immutable.
            simplified = "阅读科幻小说"
            fresh = identity.canonicalize_fresh_concept(simplified)
            local.require(fresh.semantic_text == "閱讀科幻小說", "fresh script normalization mismatch")
            result = invoke("apply_memory", api["MemoryApplyRequest"](
                user_id="p01-fresh-language", message_id="p01-fresh-language",
                memories=[{**fresh.as_dict(), "stance": "like", "confidence": 1.0}]))
            local.require(result["status"] == "success", "fresh language preference write failed")
            for topic in (simplified, "閱讀科幻小說"):
                result = exact(topic)
                local.require(result["canonical_key"] == fresh.key
                              and result["candidate_ids"] == ["p01-fresh-language"], "fresh script exact queries diverged")

            stored_simplified = identity.canonicalize_concept(simplified)
            local.require(stored_simplified.key != fresh.key, "historical source control must remain distinct")
            with driver.session(database="neo4j") as session:
                session.run("CREATE(u:User {id:'p01-stored-language',readiness_fixture:$marker}) "
                            "CREATE(c:Concept) SET c += $identity,c.readiness_fixture=$marker "
                            "CREATE(u)-[:PREFERS]->(c)", marker=MARKER, identity=stored_simplified.as_dict()).consume()
            before = snapshot(driver)
            packet = {field: stored_simplified.as_dict()[field] for field in (
                "canonical_key", "canonicalization_version", "semantic_text", "semantic_input_hash")}
            result = exact(stored_simplified.semantic_text, **packet)
            local.require(result["canonical_key"] == stored_simplified.key
                          and result["normalized_topic"] == simplified
                          and result["candidate_ids"] == ["p01-stored-language"]
                          and snapshot(driver) == before, "stored v2 script was converted or re-keyed during read")
            language_matrix = {
                "fresh_simplified_traditional_exact": "PASS: same fresh identity and candidate",
                "stored_v2_simplified_packet": "PASS: original text/key/hash and graph snapshot unchanged",
                "normalizer_distribution": identity.FRESH_PREFERENCE_CONVERTER_DISTRIBUTION,
                "normalizer_version": identity.FRESH_PREFERENCE_CONVERTER_VERSION,
                "normalizer_config": identity.FRESH_PREFERENCE_CONVERTER_CONFIG,
                "historical_rekey_or_alias_creation": False,
            }
            local.require(snapshot(driver, True) == legacy_before, "legacy nodes/edges changed")

            plans = []
            with driver.session(database="neo4j") as session:
                seen = set()
                for query, params in captures:
                    if "candidate:User" not in query or query in seen:
                        continue
                    seen.add(query)
                    plan = session.run("EXPLAIN " + query, **params).consume().plan
                    ops = operators(plan)
                    local.require(any("IndexSeek" in op for op in ops), "exact retrieval did not use key index")
                    local.require(not any("AllNodesScan" in op or "NodeByLabelScan" in op for op in ops), "exact retrieval contains full scan")
                    plans.append({"query": query, "operators": ops})
                indexes = [r.data() for r in session.run("SHOW VECTOR INDEXES YIELD name,state,options WHERE name='concept_embedding_index' RETURN name,state,options")]
            local.require(plans and indexes[0]["state"] == "ONLINE", "index/plan missing")
    return {"status": "PASS", "scope": "local synthetic component transactions; not Server/API or production readiness",
            "topology": topology, "server": version, "scenarios": scenarios, "exact_plans": plans,
            "index": indexes, "legacy_nodes_edges_unchanged": True,
            "semantic_boundary_matrix": boundary_matrix,
            "language_boundary_matrix": language_matrix,
            "candidate_hard_limit": 100, "source_sha256": local.SOURCE_HASHES,
            "production_ready": False, "production_fingerprint": "unknown", "production_compatibility": "unknown",
            "semantic_flags": {flag: "off" for flag in local.FLAGS}, "runtime_threshold_unchanged": .82,
            "real_provider_calls": 0, "component_query_errors": errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-disposable-synthetic-reset", action="store_true")
    args = parser.parse_args()
    local.require(args.confirm_disposable_synthetic_reset, "explicit owned synthetic reset required")
    local.json_file(REPORT, {"status": "RUNNING", "production_ready": False})
    try:
        result = run()
    except Exception as exc:
        local.json_file(REPORT, {"status": "FAIL", "error_category": type(exc).__name__, "production_ready": False})
        raise
    local.json_file(REPORT, result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"exact_plans", "source_sha256"}}, indent=2))


if __name__ == "__main__":
    main()
