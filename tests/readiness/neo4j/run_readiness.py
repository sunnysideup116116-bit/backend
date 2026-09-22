#!/usr/bin/env python3
"""Disposable R1-L harness. Never imports service startup or production dotenv."""
from __future__ import annotations

import argparse
import ast
from collections import Counter
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
import hashlib
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import statistics
import subprocess
import sys
import time
from types import ModuleType, SimpleNamespace
from typing import Any, Callable
import uuid

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
ARTIFACTS = HERE / "artifacts"
STATE = ARTIFACTS / "state.json"
MARKER = "disposable-synthetic-only"
MODEL = "r1l-deterministic-geometry-v1"
DEFAULT_IMAGE = "neo4j:2026.08.1"
FLAGS = ("MATCH_PREFERENCE_SEMANTIC_MODE", "MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED")
DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]
SOURCE_HASHES: dict[str, str] = {}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def json_file(path, data, *, private=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def command(args, *, env=None, check=True, timeout=240):
    result = subprocess.run(args, cwd=HERE, env=env, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"local command failed ({result.returncode}): {result.stderr[-1000:]}")
    return result


def settings():
    local = {}
    path = HERE / ".env.local"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                key, sep, value = line.partition("=")
                require(sep and key.strip() in {
                    "READINESS_NEO4J_IMAGE", "READINESS_BOLT_PORT", "READINESS_HTTP_PORT",
                }, "only local readiness image/port settings are allowed")
                local[key.strip()] = value.strip().strip("\"'")
    def value(key, default):
        return os.environ.get(key, local.get(key, default))
    checkout = hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]
    image = value("READINESS_NEO4J_IMAGE", DEFAULT_IMAGE)
    require(re.fullmatch(r"neo4j:\d+\.\d+\.\d+(?:-community)?", image),
            "image must be an explicit official Community version, never latest")
    bolt, http = int(value("READINESS_BOLT_PORT", "17687")), int(value("READINESS_HTTP_PORT", "17474"))
    require(bolt != http and all(1024 <= p <= 65535 and p not in {8000, 8001, 8081, 9001, 7474, 7687}
                                for p in (bolt, http)), "unsafe or conflicting local port")
    return {"image": image, "bolt": bolt, "http": http,
            "project": "ayue-r1l-" + checkout[:8], "checkout": checkout}


def read_state():
    require(STATE.is_file(), "run up first")
    state = json.loads(STATE.read_text())
    require(all(state.get(k) == v for k, v in settings().items()),
            "state/config mismatch: stop/destroy the old baseline before changing settings")
    return state


def compose(state, *args, check=True):
    # --env-file disables implicit project dotenv discovery. No production settings
    # are forwarded; this explicit compose model has no host bind mounts.
    env = dict(os.environ)
    env.update(READINESS_PROJECT=state["project"], READINESS_CHECKOUT_ID=state["checkout"],
               READINESS_NEO4J_IMAGE=state["image"], READINESS_PASSWORD=state["password"],
               READINESS_BOLT_PORT=str(state["bolt"]), READINESS_HTTP_PORT=str(state["http"]))
    return command([*DOCKER, "compose", "--env-file", "/dev/null", "--project-name", state["project"],
                    "--file", str(HERE / "compose.yaml"), *args], env=env, check=check)


def inspect_resource(kind, name):
    result = command([*DOCKER, kind, "inspect", name], check=False)
    return json.loads(result.stdout)[0] if result.returncode == 0 else None


def owned(resource, state, *, container=False):
    labels = (resource.get("Config", {}) if container else resource).get("Labels") or {}
    require(labels.get("io.ayue.readiness") == MARKER and labels.get("io.ayue.checkout") == state["checkout"],
            "refusing an unowned Docker resource")


def verify_container(state, *, running=True):
    name = state["project"] + "-neo4j"
    container = inspect_resource("container", name)
    require(container is not None, "local fixture container is missing")
    owned(container, state, container=True)
    require(container["Config"]["Image"] == state["image"], "unexpected image")
    if running:
        require(container["State"]["Running"], "local fixture container is not running")
    bindings = container["HostConfig"]["PortBindings"]
    require(set(bindings) == {"7687/tcp", "7474/tcp"}, "unexpected published ports")
    for port, local in (("7687/tcp", state["bolt"]), ("7474/tcp", state["http"])):
        require(bindings[port] == [{"HostIp": "127.0.0.1", "HostPort": str(local)}], "not loopback-only")
        if running:
            require(container["NetworkSettings"]["Ports"].get(port) == bindings[port], "requested ports were not actually published")
    require(container["HostConfig"]["NetworkMode"] != "host", "host networking is forbidden")
    expected = {state["project"] + "_data", state["project"] + "_logs"}
    require({m.get("Name") for m in container["Mounts"]} == expected, "unexpected mounts")
    for mount in container["Mounts"]:
        require(mount["Type"] == "volume", "host bind mounts are forbidden")
        owned(inspect_resource("volume", mount["Name"]), state)
    network_name = state["project"] + "_readiness"
    require(set(container["NetworkSettings"]["Networks"]) == {network_name}, "unexpected network")
    network = inspect_resource("network", network_name)
    owned(network, state)
    require(network["Driver"] == "bridge" and network["Options"].get("com.docker.network.bridge.host_binding_ipv4") == "127.0.0.1",
            "readiness requires a dedicated loopback-published bridge")
    peers = set(network["Containers"])
    require(peers <= {container["Id"]}, "unexpected peer on readiness network")
    if running:
        require(peers == {container["Id"]}, "readiness container is not attached to its network")
    # compose down acts on the project, not only this service. Check stopped
    # members too, so cleanup cannot remove an unexpected sibling container.
    members = command([*DOCKER, "ps", "-aq", "--no-trunc", "--filter",
                       "label=com.docker.compose.project=" + state["project"]]).stdout.splitlines()
    require(set(members) == {container["Id"]}, "unexpected member in readiness compose project")
    image = inspect_resource("image", container["Image"])
    return {"container": name, "image": state["image"], "image_id": container["Image"],
            "repo_digests": image.get("RepoDigests", []), "network": network_name,
            "volumes": sorted(expected), "host_ip": "127.0.0.1", "bolt_port": state["bolt"],
            "http_port": state["http"], "production_compatibility": "unknown"}


def up():
    config = settings()
    if STATE.exists():
        state = read_state()
    else:
        state = {**config, "password": "r1l-" + secrets.token_hex(20)}
        for kind, name in (("container", config["project"] + "-neo4j"),
                           ("network", config["project"] + "_readiness"),
                           ("volume", config["project"] + "_data"),
                           ("volume", config["project"] + "_logs")):
            require(inspect_resource(kind, name) is None, "existing resources need explicit ownership review")
        json_file(STATE, state, private=True)
    for kind, name in (("container", state["project"] + "-neo4j"),
                       ("network", state["project"] + "_readiness"),
                       ("volume", state["project"] + "_data"), ("volume", state["project"] + "_logs")):
        resource = inspect_resource(kind, name)
        if resource:
            owned(resource, state, container=kind == "container")
    model = json.loads(compose(state, "config", "--format", "json").stdout)
    service = model["services"]["graph"]
    require(service["image"] == state["image"], "compose image mismatch")
    require(all(p.get("host_ip") == "127.0.0.1" for p in service["ports"]), "unsafe compose ports")
    require(all(v["type"] == "volume" for v in service["volumes"]), "unsafe compose mount")
    compose(state, "up", "-d", "--wait", "--wait-timeout", "180")
    print(json.dumps(verify_container(state), indent=2))


@contextmanager
def local_network_only(port):
    original_connect, original_connect_ex, original_dns = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    def connect(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            require(isinstance(address, tuple) and address[0] == "127.0.0.1" and address[1] == port,
                    "network guard blocked a non-fixture endpoint")
        return original_connect(sock, address)
    def connect_ex(sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            require(isinstance(address, tuple) and address[0] == "127.0.0.1" and address[1] == port,
                    "network guard blocked a non-fixture endpoint")
        return original_connect_ex(sock, address)
    def resolve(host, *args, **kwargs):
        require(host in {"127.0.0.1", b"127.0.0.1"}, "DNS/network is disabled outside the fixture")
        return original_dns(host, *args, **kwargs)
    socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, resolve
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = original_connect, original_connect_ex, original_dns


def load_source(relative, names, supplied):
    """Compile unmodified definitions, not top-level service initialization.

    Recursively include same-file helpers. No imports, decorators, database globals
    or arbitrary top-level expressions execute. Unknown globals fail loudly.
    """
    path = ROOT / relative
    source = path.read_text(encoding="utf-8")
    SOURCE_HASHES[relative] = hashlib.sha256(source.encode()).hexdigest()
    tree = ast.parse(source)
    module_name = "_r1l_" + relative.replace("/", "_").replace(".", "_")
    module = ModuleType(module_name)
    sys.modules[module_name] = module
    env = module.__dict__
    env.update(supplied)
    env["__name__"] = module_name
    env["__file__"] = str(path)
    for node in tree.body:
        if isinstance(node, ast.Assign) and all(isinstance(t, ast.Name) for t in node.targets):
            try:
                value = ast.literal_eval(node.value)
            except (ValueError, TypeError):
                value_node = node.value
                if isinstance(value_node, ast.Call) and (
                    isinstance(value_node.func, ast.Name) and value_node.func.id == "frozenset"
                    or isinstance(value_node.func, ast.Attribute) and isinstance(value_node.func.value, ast.Name)
                    and value_node.func.value.id == "re" and value_node.func.attr == "compile"
                ):
                    value = eval(compile(ast.Expression(value_node), str(path), "eval"), {"re": re, "frozenset": frozenset})
                else:
                    continue
            for target in node.targets:
                env.setdefault(target.id, value)
    definitions = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    selected, pending = set(), list(names)
    while pending:
        name = pending.pop()
        if name in selected:
            continue
        require(name in definitions, f"source definition missing: {relative}:{name}")
        selected.add(name)
        pending.extend(n.id for n in ast.walk(definitions[name])
                       if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                       and n.id in definitions and n.id not in selected and n.id not in env)
    nodes = [deepcopy(n) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name in selected]
    for node in nodes:
        node.decorator_list = []  # Route registration is intentionally not loaded.
    future = ast.ImportFrom(module="__future__", names=[ast.alias(name="annotations")], level=0)
    compiled = ast.fix_missing_locations(ast.Module(body=[future, *nodes], type_ignores=[]))
    exec(compile(compiled, str(path), "exec"), env)
    return env


def canonical_module():
    path = ROOT / "matchmaker_agent/concept_identity.py"
    spec = importlib.util.spec_from_file_location("_r1l_identity", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)  # Audited pure deterministic module; no clients/dotenv.
    SOURCE_HASHES["matchmaker_agent/concept_identity.py"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return module


def vector(axis, cosine):
    result = [0.0] * 768
    result[axis] = cosine
    result[axis + 1] = math.sqrt(max(0.0, 1.0 - cosine * cosine))
    result[-1] = 0.001
    norm = math.sqrt(sum(v * v for v in result))
    return [v / norm for v in result]


def seed(driver, fixture, identity):
    marker = MARKER
    with driver.session(database="neo4j") as session:
        foreign = session.run("MATCH (n) WHERE coalesce(n.readiness_fixture, '') <> $marker RETURN count(n) AS n", marker=marker).single()["n"]
        require(foreign == 0, "refusing database containing non-readiness data")
        # Only nodes previously created by this harness are disposable.
        session.run("MATCH (n {readiness_fixture:$marker}) DETACH DELETE n", marker=marker).consume()
        ddl = "\n".join(line for line in (HERE / "schema.cypher").read_text().splitlines() if not line.startswith("//"))
        for statement in ddl.split(";"):
            if statement.strip():
                session.run(statement).consume()
        concepts, users = [], deepcopy(fixture["users"])
        for item in fixture["concepts"]:
            concept = identity.canonicalize_concept(item["label"])
            concepts.append({"key": concept.key, "label": concept.label,
                             "embedding": vector(item["axis"], item["cosine"])})
        capacity = fixture["capacity"]
        require(1 <= capacity["concepts"] <= 12 and 21 <= capacity["users_per_concept"] <= 100, "unbounded fixture")
        for group in range(capacity["concepts"]):
            label = f"Readiness Fanout {group:02}"
            concept = identity.canonicalize_concept(label)
            concepts.append({"key": concept.key, "label": concept.label,
                             "embedding": vector(capacity["axis"], .99 - group * .005)})
            users.extend({"id": f"r1l-hot-{group:02}-{number:03}", "prefers": [label], "avoids": []}
                         for number in range(capacity["users_per_concept"]))
        session.run("""UNWIND $rows AS row CREATE (c:Concept {key:row.key, label:row.label,
                    embedding:row.embedding, embedding_model:$model, embedding_task:'semantic_similarity',
                    readiness_fixture:$marker})""", rows=concepts, model=MODEL, marker=marker).consume()
        session.run("UNWIND $ids AS id CREATE (:User {id:id, readiness_fixture:$marker})",
                    ids=[u["id"] for u in users], marker=marker).consume()
        for relation, field in (("PREFERS", "prefers"), ("AVOIDS", "avoids")):
            rows = [{"id": u["id"], "key": identity.canonicalize_concept(label).key}
                    for u in users for label in u[field]]
            session.run(f"UNWIND $rows AS row MATCH (u:User {{id:row.id}}), (c:Concept {{key:row.key}}) CREATE (u)-[:{relation}]->(c)", rows=rows).consume()
        session.run("CALL db.awaitIndexes(120)").consume()
    return concepts, users


class Materialized(list):
    def __init__(self, rows, summary):
        super().__init__(rows)
        self.summary = summary

    def single(self):
        require(len(self) <= 1, "expected a single fixture row")
        return self[0] if self else None

    def consume(self):
        return self.summary


class CaptureSession:
    def __init__(self, session, capture):
        self.session, self.capture = session, capture

    def __enter__(self):
        self.session.__enter__()
        return self

    def __exit__(self, *args):
        return self.session.__exit__(*args)

    def run(self, query, **params):
        text = str(query)
        require(not re.search(r"\b(CREATE|MERGE|DELETE|SET|DROP|REMOVE)\b", text, re.I), "retrieval attempted a write")
        started = time.perf_counter()
        result = self.session.run(query, **params)
        rows, summary = list(result), result.consume()
        kind = ("ann" if "db.index.vector.queryNodes" in text else "expansion" if "UNWIND $concepts" in text
                else "exact" if "candidate:User" in text else "metadata_or_query_vector")
        self.capture.append({"kind": kind, "query": text, "params": params,
                             "rows": len(rows), "ms": (time.perf_counter() - started) * 1000})
        return Materialized(rows, summary)


class CaptureDriver:
    def __init__(self, driver, capture):
        self.driver, self.capture = driver, capture

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.driver.close()

    def session(self, **kwargs):
        require(kwargs.get("database") == "neo4j", "unexpected database")
        return CaptureSession(self.driver.session(**kwargs), self.capture)


def runtime(state, fixture, concepts):
    from neo4j import GraphDatabase, Query
    from pydantic import BaseModel, Field
    import requests
    from urllib3.util import Timeout
    identity = canonical_module()
    uri, auth = f"bolt://127.0.0.1:{state['bolt']}", ("neo4j", state["password"])
    capture, embedding_times = [], []
    def local_driver(target, *, auth=None, **kwargs):
        require(target == uri and auth == ("neo4j", state["password"]), "non-local driver config rejected")
        return CaptureDriver(GraphDatabase.driver(uri, auth=auth, **kwargs), capture)
    common = {"os": SimpleNamespace(getenv=lambda _key, default=None: default), "re": re, "math": math,
              "time": time, "Any": Any, "Callable": Callable, "Counter": Counter, "uuid": uuid,
              "hashlib": hashlib, "BaseModel": BaseModel, "Field": Field,
              "normalize_zh_tw": lambda value, max_length=None: str(value or "")[:max_length],
              "canonicalize_concept": identity.canonicalize_concept,
              "canonical_query_provenance": identity.canonical_query_provenance,
              "canonical_evidence_span": identity.canonical_evidence_span}
    graph = load_source("matchmaker_agent/agent_api.py", [
        "PreferenceCandidateRequest", "PreferenceSemanticCandidateRequest", "preference_candidates",
        "preference_semantic_candidates", "preference_semantic_readiness",
    ], {**common, "Query": Query, "GraphDatabase": SimpleNamespace(driver=local_driver),
        "_neo4j_config": lambda: (uri, auth, "neo4j")})
    for name in ("PreferenceCandidateRequest", "PreferenceSemanticCandidateRequest"):
        graph[name].model_rebuild()
    class Response:
        def __init__(self, body):
            self.body = body
        def raise_for_status(self):
            pass
        def json(self):
            return self.body
    def post(url, *, json, timeout):
        if url.endswith("/api/preferences/semantic-candidates"):
            return Response(graph["preference_semantic_candidates"](graph["PreferenceSemanticCandidateRequest"](**json)))
        require(url.endswith("/api/preferences/candidates"), "unexpected HTTP adapter target")
        return Response(graph["preference_candidates"](graph["PreferenceCandidateRequest"](**json)))
    vectors = {c["key"]: c["embedding"] for c in concepts}
    def embeddings(labels, **kwargs):
        require(len(labels) == 1 and kwargs.get("task_type") == "semantic_similarity"
                and kwargs.get("output_dimensionality") == 768, "unexpected embedding request")
        started = time.perf_counter()
        key = identity.canonicalize_concept(labels[0]).key
        require(key in vectors, "only versioned synthetic concepts may be embedded")
        result = [list(vectors[key])]
        embedding_times.append((time.perf_counter() - started) * 1000)
        return result
    bridge = SimpleNamespace(post=post, RequestException=requests.RequestException)
    semantic = load_source("social/services/preference_semantic_service.py", [
        "retrieve_semantic_preference_candidates", "semantic_config", "preference_semantic_mode",
        "semantic_embedding_space_confirmed", "qualified_exact_trigger_threshold", "PreferenceSemanticRetrievalError",
    ], {**common, "requests": bridge, "Timeout": Timeout, "GOOGLE_EMBEDDING_MODEL": MODEL, "get_embeddings": embeddings})
    exact = load_source("social/services/preference_candidate_service.py", [
        "retrieve_preference_candidate_ids", "PreferenceCandidateLookupError",
    ], {**common, "requests": bridge})
    context = load_source("social/services/match_search_context.py", [
        "safe_search_context", "search_context_for_turn", "provider_search_context", "context_embedding_source_hash",
    ], common)
    projection = load_source("social/services/profile_projection.py", [
        "recent_context_is_active", "without_expired_recent_context", "safe_recent_context",
    ], common)
    return SimpleNamespace(graph=graph, semantic=semantic, exact=exact, context=context, projection=projection,
                           common=common, identity=identity, capture=capture, vectors=vectors,
                           embedding_times=embedding_times, uri=uri, auth=auth)


def plan_nodes(plan):
    yield plan
    for child in plan.get("children", []):
        yield from plan_nodes(child)


def compact_plan(plan):
    args = plan.get("args", plan.get("arguments", {}))
    return {"operator": plan.get("operatorType"), "rows": plan.get("rows"), "db_hits": plan.get("dbHits"),
            "details": str(args.get("Details", ""))[:2000],
            "children": [compact_plan(c) for c in plan.get("children", [])]}


def verify_plans(driver, captured):
    results = {}
    with driver.session(database="neo4j", default_access_mode="READ") as session:
        for kind in ("exact", "ann", "expansion"):
            call = next(c for c in reversed(captured) if c["kind"] == kind)
            plans = {}
            for mode in ("EXPLAIN", "PROFILE"):
                result = session.run(mode + " " + call["query"], **call["params"])
                rows, summary = list(result), result.consume()
                plan = summary.profile if mode == "PROFILE" else summary.plan
                operators = [n["operatorType"] for n in plan_nodes(plan)]
                require(not any("AllNodesScan" in op or "NodeByLabelScan" in op for op in operators),
                        f"unexpected full/label scan in {kind}")
                details = " ".join(str(n.get("args", n.get("arguments", {})).get("Details", "")) for n in plan_nodes(plan))
                if kind == "ann":
                    require("db.index.vector.queryNodes" in details and call["params"]["index_name"] == "concept_embedding_index",
                            "ANN index procedure missing from actual plan")
                    require(any("Limit" in op or "Top" in op for op in operators), "missing ANN concept bound")
                else:
                    require(any("UniqueIndexSeek" in op for op in operators), "Concept.key index seek missing")
                if kind == "expansion":
                    query = call["query"]
                    require(query.index("LIMIT $per_concept_limit") < query.index("WHERE candidate.id"), "fanout bound moved")
                    limits = [n for n in plan_nodes(plan) if "Limit" in n["operatorType"]
                              and "per_concept_limit" in str(n.get("args", n.get("arguments", {})))]
                    require(limits, "per-concept limit missing from actual plan")
                    for limit in limits:
                        require(any("Expand" in n["operatorType"] for n in plan_nodes(limit)), "limit is not above expansion")
                        if mode == "PROFILE":
                            require(limit.get("rows", 0) <= len(call["params"]["concepts"]) * call["params"]["per_concept_limit"],
                                    "per-concept row bound exceeded")
                    if mode == "PROFILE":
                        require(len(rows) <= call["params"]["candidate_limit"], "candidate bound exceeded")
                plans[mode.lower()] = compact_plan(plan)
            results[kind] = plans
    return results


def digest_graph(driver):
    with driver.session(database="neo4j", default_access_mode="READ") as session:
        nodes = [r.data() for r in session.run("MATCH (n) RETURN labels(n) AS labels, properties(n) AS properties")]
        edges = [r.data() for r in session.run("MATCH (u:User)-[r]->(c:Concept) RETURN u.id AS user, type(r) AS relation, c.key AS concept")]
    return hashlib.sha256(json.dumps({"nodes": sorted(nodes, key=lambda x: json.dumps(x, sort_keys=True)),
                                      "edges": sorted(edges, key=lambda x: json.dumps(x, sort_keys=True))}, sort_keys=True).encode()).hexdigest()


def infrastructure(driver, rt):
    from pydantic import ValidationError
    with driver.session(database="neo4j", default_access_mode="READ") as session:
        index = session.run("SHOW VECTOR INDEXES YIELD name, state, options, labelsOrTypes, properties WHERE name='concept_embedding_index' RETURN *").single().data()
        config = index["options"]["indexConfig"]
        require(index["state"] == "ONLINE" and config["vector.dimensions"] == 768
                and str(config["vector.similarity_function"]).lower() == "cosine", "invalid index contract")
        vectors = list(session.run("MATCH (c:Concept)<-[:PREFERS]-(:User) RETURN DISTINCT c.key AS key, c.embedding AS vector"))
        norms = [math.sqrt(sum(v * v for v in r["vector"])) for r in vectors]
        require(all(len(r["vector"]) == 768 and all(math.isfinite(v) for v in r["vector"]) for r in vectors), "invalid vector")
        require(all(abs(n - 1.0) < 1e-8 for n in norms), "non-unit synthetic vectors")
    before = digest_graph(driver)
    aliases = []
    fixture = json.loads((HERE / "fixtures.json").read_text())
    for case in fixture["deterministic_alias_cases"]:
        require({rt.identity.canonicalize_concept(f).key for f in case["forms"]} == {case["canonical_key"]}, "alias mismatch")
        aliases.append({"canonical_key": case["canonical_key"], "forms_checked": len(case["forms"])})
    rt.exact["retrieve_preference_candidate_ids"]("r1l-owner", "Kpop", excluded_user_ids=[], limit=100)
    first_started = time.perf_counter()
    ordinary = rt.semantic["retrieve_semantic_preference_candidates"]("r1l-owner", "Board Games", excluded_user_ids=[])
    first_ms = (time.perf_counter() - first_started) * 1000
    require("r1l-multi" in ordinary["candidate_ids"], "multi-concept fixture missing")
    require(len(ordinary["candidate_ids"]) == len(set(ordinary["candidate_ids"])), "candidate duplicated")
    require(len(ordinary["evidence_by_candidate"]["r1l-multi"]) >= 2, "multi-concept evidence missing")
    require("r1l-avoid-only" not in ordinary["candidate_ids"], "AVOIDS used as positive retrieval evidence")
    require("r1l-negative" not in ordinary["candidate_ids"] and "r1l-borderline" not in ordinary["candidate_ids"],
            "synthetic sub-threshold vectors admitted")
    bounds = []
    for name, limits in (
        ("default", {}),
        ("small", dict(neighbor_limit=4, concept_limit=2, per_concept_limit=3, candidate_limit=4)),
        ("hard_max", dict(neighbor_limit=32, concept_limit=12, per_concept_limit=20, candidate_limit=50, evidence_limit=5)),
    ):
        req = rt.graph["PreferenceSemanticCandidateRequest"](requester_user_id="r1l-owner", topic="Readiness Capacity", embedding_model=MODEL, **limits)
        result = rt.graph["preference_semantic_candidates"](req)
        require(result.get("status") == "success", f"capacity retrieval failed: {result.get('error_code')}")
        require(len(result["candidates"]) == req.candidate_limit, "capacity fixture did not exercise candidate cap")
        require(len({c["candidate_id"] for c in result["candidates"]}) == len(result["candidates"]), "dedupe failed")
        bounds.append({"case": name, "ann_k": req.neighbor_limit, "concept_limit": req.concept_limit,
                       "per_concept_limit": req.per_concept_limit, "candidate_limit": req.candidate_limit,
                       "actual_candidates": len(result["candidates"])})
    expansion = next(c for c in reversed(rt.capture) if c["kind"] == "expansion")
    with driver.session(database="neo4j", default_access_mode="READ") as session:
        params = {**expansion["params"], "concepts": expansion["params"]["concepts"][:1],
                  "per_concept_limit": 10, "candidate_limit": 50}
        first_window = list(session.run(expansion["query"], **params))
        require(len(first_window) == 10, "hot-concept window must have ten rows")
        blocked_window = list(session.run(expansion["query"], **{**params, "excluded_user_ids": [r["candidate_id"] for r in first_window]}))
        require(not blocked_window, "early fanout limit incorrectly refilled after exclusions")
    for field, hard in {"neighbor_limit": 32, "concept_limit": 12, "per_concept_limit": 20,
                        "candidate_limit": 50, "evidence_limit": 5}.items():
        try:
            rt.graph["PreferenceSemanticCandidateRequest"](requester_user_id="r1l-owner", topic="Board Games", **{field: hard + 1})
        except ValidationError:
            continue
        raise RuntimeError("hard bound was not rejected: " + field)
    plans = verify_plans(driver, rt.capture)
    require(digest_graph(driver) == before, "retrieval mutated Graph identity/edges/data")
    latency = []
    for number in range(5):
        start, offset = time.perf_counter(), len(rt.capture)
        rt.semantic["retrieve_semantic_preference_candidates"]("r1l-owner", "Board Games", excluded_user_ids=[])
        calls = rt.capture[offset:]
        latency.append({"run": number + 1, "query_embedding": "reused synthetic Concept vector; no provider call",
                        "ann_ms": round(sum(c["ms"] for c in calls if c["kind"] == "ann"), 3),
                        "expansion_ms": round(sum(c["ms"] for c in calls if c["kind"] == "expansion"), 3),
                        "fallback_adapter_ms": round((time.perf_counter() - start) * 1000, 3)})
    return {"index": index, "coverage": {"prefers_concepts": len(vectors), "embedded": len(vectors), "missing": 0,
                                         "percent": 100.0, "norm_min": min(norms), "norm_max": max(norms)},
            "aliases": aliases, "bounds": bounds, "early_limit_no_refill": True, "prefers_only": True,
            "multi_concept_dedupe": True, "graph_unchanged_by_retrieval": True, "plans": plans,
            "first_fallback_adapter_ms": round(first_ms, 3), "latency_runs": latency}


def flow_scenarios(driver, rt, users):
    import mongomock
    fixture_db = mongomock.MongoClient()["readiness"]
    profiles, history = fixture_db.profiles, fixture_db.matches
    profiles.insert_many({"user_id": u["id"], "current_context": "", "deep_profile": {}} for u in users)
    base = {**rt.common, **{name: rt.context[name] for name in ("safe_search_context", "search_context_for_turn", "provider_search_context", "context_embedding_source_hash")},
            **{name: rt.projection[name] for name in ("recent_context_is_active", "without_expired_recent_context", "safe_recent_context")},
            "profiles_coll": profiles, "matches_coll": history, "MatchRequest": lambda **kwargs: SimpleNamespace(**kwargs),
            "semantic_config": rt.semantic["semantic_config"],
            "preference_semantic_mode": lambda: "active",  # Isolated namespace, never an env/runtime feature flag.
            "semantic_embedding_space_confirmed": lambda: True,  # Only our generated fixture space.
            "qualified_exact_trigger_threshold": rt.semantic["qualified_exact_trigger_threshold"],
            "retrieve_preference_candidate_ids": rt.exact["retrieve_preference_candidate_ids"],
            "retrieve_semantic_preference_candidates": rt.semantic["retrieve_semantic_preference_candidates"],
            "PreferenceCandidateLookupError": rt.exact["PreferenceCandidateLookupError"],
            "PreferenceSemanticRetrievalError": rt.semantic["PreferenceSemanticRetrievalError"],
            "MatchSearchPipelineError": RuntimeError, "RiskBlockServiceUnavailable": RuntimeError,
            "INVITE_ON_MATCH": "invite_on_match",
            "participant_pair_key": lambda a, b: "|".join(sorted([a, b])),
            "has_verified_acceptance": lambda _row: True}
    def stances(user):
        with driver.session(database="neo4j", default_access_mode="READ") as session:
            rows = session.run("MATCH (:User {id:$id})-[r:PREFERS|AVOIDS]->(c:Concept) RETURN type(r) AS relation, c.key AS key LIMIT 20", id=user)
            result = {}
            for row in rows:
                result.setdefault(row["key"], set()).add("like" if row["relation"] == "PREFERS" else "avoid")
            return result
    base["_trait_stances"] = stances
    router = load_source("social/routers/match.py", ["generate_matches_for_user", "candidate_qualification"], base)
    exact_ids = [u["id"] for u in users if u["id"].startswith("r1l-exact-")]
    results = []
    cases = [
        ("exact_zero", [], set(), None, "Board Games"),
        ("hard_conflict_exact_zero", ["r1l-exact-hard"], set(), None, "Board Games"),
        ("blocked_history_exact_zero", ["r1l-exact-blocked", "r1l-exact-history"], {"r1l-exact-blocked"}, None, "Board Games"),
        ("profile_missing_exact_zero", ["r1l-exact-ineligible"], set(), "missing", "Board Games"),
        ("cohort_mismatch_exact_zero", ["r1l-exact-ineligible"], set(), "cohort", "Board Games"),
        ("qualified_exact_no_fallback", ["r1l-exact-eligible"], set(), None, "Board Games"),
        ("alias_exact_no_fallback", [], set(), None, "Kpop"),
        ("bounded_twenty_candidate_pool", [], set(), None, "Readiness Capacity"),
    ]
    for name, active_exact, blocked, profile_case, topic in cases:
        with driver.session(database="neo4j") as session:
            session.run("MATCH (u:User)-[r:PREFERS]->(:Concept {key:'board_games'}) WHERE u.id IN $ids DELETE r", ids=exact_ids).consume()
            session.run("UNWIND $ids AS id MATCH (u:User {id:id}), (c:Concept {key:'board_games'}) MERGE (u)-[:PREFERS]->(c)", ids=active_exact).consume()
        profiles.replace_one({"user_id": "r1l-exact-ineligible"}, {"user_id": "r1l-exact-ineligible", "current_context": ""}, upsert=True)
        if profile_case == "missing":
            profiles.delete_one({"user_id": "r1l-exact-ineligible"})
        elif profile_case == "cohort":
            profiles.update_one({"user_id": "r1l-exact-ineligible"}, {"$set": {"test_match_cohort": "another-fixture-cohort"}})
        history.delete_many({})
        if name == "blocked_history_exact_zero":
            history.insert_one({"from_user": "r1l-owner", "to_user": "r1l-exact-history", "status": "pending", "created_at": time.time()})
        router["risk_block_service"] = SimpleNamespace(excluded_user_ids=lambda _owner: blocked)
        payloads = []
        def select(payload, **_kwargs):
            payloads.append(payload)
            return []  # Never create a real proposal or call an LLM.
        router["_request_matchmaker_selection"] = select
        concept = rt.identity.canonicalize_concept(topic)
        search = {"search_intent": "preference", "normalized_topic": concept.label,
                  "canonical_preference_key": concept.key, "query_text": f"find people who like {topic}"}
        offset, started = len(rt.capture), time.perf_counter()
        with redirect_stdout(io.StringIO()):
            result = router["generate_matches_for_user"]("r1l-owner", source="automatic", search_context=search,
                                                         report_progress=lambda _step: True, can_commit=lambda: False)
        diag = result["diagnostics"]
        eligible = [c["user_id"] for p in payloads for c in p["candidates"]]
        should_fallback = name not in {"qualified_exact_no_fallback", "alias_exact_no_fallback"}
        require(diag["semantic_fallback_triggered"] == should_fallback, f"incorrect fallback trigger: {name}")
        require((diag["qualified_exact_count"] == 0) == should_fallback, "fallback did not use qualified exact count")
        require(eligible and len(eligible) == len(set(eligible)), "no eligible candidates or duplicate slots")
        require("r1l-semantic-hard" not in eligible and "r1l-exact-hard" not in eligible, "hard conflict reached ranking")
        require(not set(eligible) & {"r1l-exact-blocked", "r1l-exact-history", "r1l-exact-ineligible", "r1l-avoid-only"}, "unsafe/ineligible candidate reached ranking")
        require(diag["candidate_pool_count"] <= 20, "final candidate pool unbounded")
        if name == "blocked_history_exact_zero":
            require(diag["candidate_count_before_filter"] == 2 and diag["candidate_count_after_retrieval_filter"] == 0, "block/history case lacked real exact hits")
        if name in {"hard_conflict_exact_zero", "profile_missing_exact_zero", "cohort_mismatch_exact_zero"}:
            require(diag["candidate_count_before_filter"] == 1, "filtered fixture lacked an exact Graph hit")
        if name == "alias_exact_no_fallback":
            require(diag["query_provenance"] == "deterministic_alias", "alias incorrectly classified")
        if name == "bounded_twenty_candidate_pool":
            require(diag["candidate_pool_count"] == 20, "20-person pool not saturated")
        require(any(c["kind"] == "ann" for c in rt.capture[offset:]) == should_fallback, "unexpected ANN execution")
        results.append({"case": name, "passed": True, "raw_exact": diag["candidate_count_before_filter"],
                        "qualified_exact": diag["qualified_exact_count"], "fallback": diag["semantic_fallback_triggered"],
                        "pool_count": diag["candidate_pool_count"], "eligible_count": diag["candidate_count_after_filter"],
                        "ranking_inputs": len(eligible), "ms": round((time.perf_counter() - started) * 1000, 3)})
    return results


def check():
    from neo4j import GraphDatabase
    require(all(os.environ.get(flag, "off").lower() == "off" for flag in FLAGS), "runtime semantic flags must remain OFF")
    state = read_state()
    topology = verify_container(state)
    fixture = json.loads((HERE / "fixtures.json").read_text())
    require(fixture["data_classification"] == "synthetic_non_user_data", "untrusted fixture classification")
    require(not os.environ.get("READINESS_DEV_GEMINI_API_KEY"),
            "explicit dev credential found: calibration requires separate reviewed provider path; never treat synthetic scores as model scores")
    with local_network_only(state["bolt"]):
        identity = canonical_module()
        with GraphDatabase.driver(f"bolt://127.0.0.1:{state['bolt']}", auth=("neo4j", state["password"]),
                                  connection_timeout=3, max_transaction_retry_time=0) as driver:
            with driver.session(database="neo4j") as session:
                version = session.run("CALL dbms.components() YIELD name, versions, edition WHERE name = 'Neo4j Kernel' RETURN versions, edition").single().data()
                require(version["edition"] == "community", "not Community Edition")
                require(version["versions"][0] == state["image"].split(":", 1)[1].removesuffix("-community"), "running server version differs from requested tag")
            concepts, users = seed(driver, fixture, identity)
            rt = runtime(state, fixture, concepts)
            started = time.perf_counter()
            infra = infrastructure(driver, rt)
            flows = flow_scenarios(driver, rt, users)
            result = {"local_readiness": "PASS", "topology": topology, "server": version,
                      "fixture_counts": {"concepts": len(concepts), "users": len(users)},
                      **infra, "flow_scenarios": flows, "source_sha256": SOURCE_HASHES,
                      "test_scope": "real local Neo4j + production definitions; in-memory profile/history, stub Risk/LLM; no proposal lifecycle I/O",
                      "configured_runtime_flags": {flag: "off" for flag in FLAGS},
                      "real_embedding_calibration": "NOT_RUN_NO_AUTHORIZED_DEV_CREDENTIAL",
                      "embedding_provider_latency_ms": None, "semantic_precision_validated": False,
                      "min_similarity": 0.82, "production_compatibility": "unknown",
                      "production_fingerprint": "unknown", "production_ready": False,
                      "verification_seconds": round(time.perf_counter() - started, 3)}
    json_file(ARTIFACTS / "report.json", result)
    print(json.dumps({k: v for k, v in result.items() if k not in {"plans", "source_sha256"}}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("up", "check", "stop", "destroy"))
    parser.add_argument("--confirm-disposable-destroy", action="store_true")
    args = parser.parse_args()
    if args.action == "up":
        up()
    elif args.action == "check":
        # Never leave an old PASS report looking current after a failed rerun.
        json_file(ARTIFACTS / "report.json", {"local_readiness": "RUNNING", "production_ready": False})
        try:
            check()
        except Exception as exc:
            json_file(ARTIFACTS / "report.json", {"local_readiness": "FAIL", "error_category": type(exc).__name__,
                                                  "production_ready": False})
            raise
    else:
        state = read_state()
        verify_container(state, running=False)
        if args.action == "stop":
            compose(state, "stop")
            print("Stopped only the verified disposable readiness container; synthetic volumes retained.")
        else:
            require(args.confirm_disposable_destroy, "explicit disposable destruction confirmation is required")
            compose(state, "down", "--volumes")
            STATE.unlink()
            print("Removed only the owned disposable container/network/volumes; generated reports retained.")


if __name__ == "__main__":
    main()
