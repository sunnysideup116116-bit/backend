"""Opt-in real disposable Graph/Mongo rehearsal, with synthetic owner auth.

No production service process, env file, provider, account or endpoint is used.
"""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import asyncio
import json
import logging
import os
from pathlib import Path
import socket
import sys
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "social"), str(ROOT / "matchmaker_agent"),
               str(ROOT / "tests/readiness/neo4j")]
import run_readiness as local_graph
import local_mongo


@contextmanager
def local_only(ports):
    connect, connect_ex, resolve = socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo
    def checked(original, sock, address):
        if sock.family in {socket.AF_INET, socket.AF_INET6}:
            assert isinstance(address, tuple) and address[0] == "127.0.0.1" and address[1] in ports
        return original(sock, address)
    def dns(host, *args, **kwargs):
        assert host in {"127.0.0.1", b"127.0.0.1"}
        return resolve(host, *args, **kwargs)
    socket.socket.connect = lambda sock, address: checked(connect, sock, address)
    socket.socket.connect_ex = lambda sock, address: checked(connect_ex, sock, address)
    socket.getaddrinfo = dns
    try:
        yield
    finally:
        socket.socket.connect, socket.socket.connect_ex, socket.getaddrinfo = connect, connect_ex, resolve


def main():
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    state = local_graph.read_state()
    topology = local_graph.verify_container(state)
    mongo = local_mongo.verify()
    assert topology["image"] == "neo4j:2026.08.1"
    os.environ.update(
        AYUE_SKIP_DOTENV="1", DOTENV_DISABLED="1",
        NEO4J_URI=f"bolt://127.0.0.1:{state['bolt']}", NEO4J_USERNAME="neo4j", NEO4J_PASSWORD=state["password"],
        NEO4J_DATABASE="neo4j", MONGO_URI=f"mongodb://127.0.0.1:{mongo['port']}/?directConnection=true&replicaSet=bootstrap_test",
        MONGO_DB_NAME="bootstrap_fixture", APPWRITE_API_KEY="synthetic-bootstrap-signing-only",
        APPWRITE_PROJECT_ID="synthetic-project", LLM_API_KEY="synthetic-unused", LLM_BASE_URL="http://provider.invalid/v1",
        LLM_MODEL_ID="synthetic-unused", PREFERENCE_BOOTSTRAP_ENABLED="on",
        MATCH_PREFERENCE_SEMANTIC_MODE="off", MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED="off",
        MATCH_RELATED_INTEREST_ENABLED="off", DEMO_DESTRUCTIVE_TOOLS_ENABLED="off",
    )
    import dotenv
    dotenv.load_dotenv = lambda *_a, **_k: False
    from neo4j import GraphDatabase
    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    # Bootstrap/memory transactions make no generative calls. The deployed
    # services have separate SDK venvs; don't instantiate a Matchmaker provider
    # with Social's OpenAI version in this in-process transport harness.
    import matchmaker
    from types import SimpleNamespace
    with patch.object(matchmaker, "MatchmakerAgent", return_value=SimpleNamespace(model="synthetic-unused", client=None)):
        import agent_api
    from matchmaker_agent.preference_bootstrap import BootstrapGraph
    from matchmaker_agent import preference_bootstrap as core
    from matchmaker_agent.preference_bootstrap_contract import BootstrapError, digest, open_preview
    from services import preference_bootstrap_service as social
    from routers import preference_bootstrap as public
    from database import db

    results = []
    with local_only({state["bolt"], mongo["port"]}), GraphDatabase.driver(
            os.environ["NEO4J_URI"], auth=("neo4j", state["password"]), max_transaction_retry_time=0) as driver:
        assert db.name == "bootstrap_fixture"
        db.client.admin.command("ping")
        graph = BootstrapGraph(driver)
        with driver.session() as session:
            labels = {label for row in session.run("MATCH (n) RETURN labels(n) AS labels") for label in row["labels"]}
            assert labels <= {"BootstrapHarness", "User", "Concept", "MemoryObservation", "PreferenceBootstrapOperation"}
            for label, prop in (("User", "id"), ("Concept", "key"), ("PreferenceBootstrapOperation", "id")):
                session.run(f"CREATE CONSTRAINT bootstrap_{label.lower()} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE").consume()
            session.run("MERGE (n:BootstrapHarness {id:$id})", id=state["checkout"]).consume()
        app = FastAPI()
        app.include_router(public.router)
        def synthetic_auth(request):
            token = request.headers.get("authorization", "")
            if token not in {"Bearer synthetic-a", "Bearer synthetic-b"}:
                raise HTTPException(401, detail={"code": "synthetic_owner_auth_required"})
            return "synthetic_a" if token.endswith("-a") else "synthetic_b"
        # Dependency override is confined to this synthetic in-process app.
        from fastapi import Request
        synthetic_auth.__annotations__["request"] = Request
        app.dependency_overrides[public.authenticated] = synthetic_auth
        client, graph_client = TestClient(app), TestClient(agent_api.app)
        real_post = social.requests.post
        def bridge(url, **kwargs):
            assert url.startswith("http://127.0.0.1:9001/api/v2/preferences/bootstrap/")
            return graph_client.post(url.removeprefix("http://127.0.0.1:9001"), json=kwargs["json"], headers=kwargs["headers"])
        headers = {"Authorization": "Bearer synthetic-a"}
        path = "/api/profile/preferences/bootstrap"
        consent = {"confirm_items": True, "complete_set": True, "reviewed_prefers": True,
                   "reviewed_avoids": True, "retire_legacy": True}

        def reset():
            with driver.session() as session:
                assert session.run("MATCH (n:BootstrapHarness {id:$id}) RETURN count(n) AS n", id=state["checkout"]).single()["n"] == 1
                assert not session.run("MATCH (u:User) WHERE NOT u.id STARTS WITH 'synthetic_' RETURN count(u) AS n").single()["n"]
                session.run("MATCH (n) WHERE NOT n:BootstrapHarness DETACH DELETE n").consume()
                session.run("""CREATE (a:User {id:'synthetic_a'}),(b:User {id:'synthetic_b'}),
                    (x:Concept {key:'legacy_like',label:'舊截斷偏好'}),(y:Concept {key:'legacy_avoid',label:'舊避免條件'}),
                    (a)-[:PREFERS {confidence:0.8}]->(x),(b)-[:PREFERS]->(x),(a)-[:AVOIDS {weight:0.9}]->(y)""").consume()
            # Exactly this named DB on the verified disposable container.
            for name in db.list_collection_names():
                db[name].delete_many({})
            db.profiles.insert_many([{"user_id": "synthetic_a", "profile_memory_revision": 0,
                "profile_memory_preview": [{"key": "legacy_like", "label": "舊截斷偏好", "stance": "like"}]},
                {"user_id": "synthetic_b", "profile_memory_revision": 0}])
            db.preference_facts.insert_one({"user_id": "synthetic_a", "concept_key": "legacy_avoid", "stance": "avoid", "active": True, "evidence_count": 2})

        def get_preview(mode="complete_set"):
            response = client.post(path + "/preview", json={"mode": mode, "prefers": ["Kpop", "安静的咖啡厅"], "avoids": ["吸菸"]}, headers=headers)
            assert response.status_code == 200, response.json()
            return response.json()

        def submit(p):
            return client.post(path + "/commit", json={"preview_token": p["preview_token"], "consent": consent}, headers=headers)

        def mark(name, **extra):
            results.append({"scenario": name, "status": "PASS", **extra})
            print(json.dumps(results[-1]), flush=True)

        def current():
            return graph.read(core.snapshot, "synthetic_a")[0]

        def ordinary(text="Board Games"):
            identity = agent_api.canonicalize_fresh_concept(text)
            return asyncio.run(agent_api.apply_memory(agent_api.MemoryApplyRequest(user_id="synthetic_a", message_id="synthetic-" + text,
                source_created_at=time.time(), memories=[{**identity.as_dict(), "stance": "like", "confidence": 1.0}])))

        with patch.object(social.requests, "post", bridge):
            reset()
            before = current(); before_mongo = list(db.profiles.find())
            p = get_preview()
            assert current() == before and list(db.profiles.find()) == before_mongo
            assert db.preference_bootstrap_operations.count_documents({}) == 0
            assert len(p["create_concepts"]) == 3 and len(p["retire_edges"]) == 2
            mark("preview_read_only")
            assert client.post(path + "/preview", json={"mode": "complete_set", "prefers": ["K-pop"]}, headers=headers).status_code == 422
            assert client.post(path + "/preview", json={"mode": "complete_set", "prefers": ["K-pop"], "avoids": []}).status_code == 401
            assert client.post(path + "/commit", json={"preview_token": p["preview_token"], "consent": consent}, headers={"Authorization": "Bearer synthetic-b"}).status_code == 403
            bad = {**consent, "reviewed_avoids": False}
            assert client.post(path + "/commit", json={"preview_token": p["preview_token"], "consent": bad}, headers=headers).status_code == 422
            assert current() == before
            mark("owner_and_explicit_both_polarities")

            response = submit(p); assert response.status_code == 200, response.json()
            assert response.json()["status"] == "committed", response.json()
            after = current(); assert after["revision"] == 1 and len(after["rows"]) == 3
            assert all(row["concept"]["canonicalization_version"] == "v2" for row in after["rows"])
            assert not db.preference_facts.find_one({"user_id": "synthetic_a"})["active"]
            assert graph.read(core.snapshot, "synthetic_b")[0]["rows"][0]["concept"]["key"] == "legacy_like"
            again = submit(p); assert again.json()["status"] == "committed" and current() == after
            mark("complete_set_atomic_idempotent")

            projected = db.profiles.find_one({"user_id": "synthetic_a"})
            assert social._prepare("synthetic_a", open_preview(p["preview_token"], "synthetic_a")) is False
            duplicate_profile = db.profiles.find_one({"user_id": "synthetic_a"})
            assert not duplicate_profile.get("preference_bootstrap_pending")
            assert duplicate_profile["profile_memory_preview"] == projected["profile_memory_preview"]
            assert duplicate_profile["profile_memory_revision"] == projected["profile_memory_revision"]
            mark("late_duplicate_prepare_never_reopens_projection")

            rolled = client.post(path + "/rollback", json={"operation_id": p["preview_id"], "revision": 1, "confirmed": True}, headers=headers)
            assert rolled.status_code == 200 and rolled.json()["status"] == "rolled_back", rolled.json()
            restored = current(); assert restored["revision"] == 2
            normalize = lambda snap: sorted((r["relation"], r["concept"]["key"], json.dumps(r["properties"], sort_keys=True)) for r in snap["rows"])
            assert normalize(restored) == normalize(before)
            assert db.preference_facts.find_one({"user_id": "synthetic_a"})["active"]
            assert submit(p).json()["status"] == "rolled_back"
            mark("precise_rollback_and_consumed_receipt")

            reset(); p = get_preview("add_only"); assert not p["retire_edges"]
            assert submit(p).json()["status"] == "committed"
            assert len(current()["rows"]) == 5 and current()["epoch"] == 0
            mark("add_only_preserves_legacy_and_source_epoch")

            reset(); p = get_preview(); assert ordinary()["status"] == "success"
            before = current(); r = submit(p)
            assert r.status_code == 409 and current() == before and db.preference_bootstrap_operations.count_documents({}) == 0
            mark("stale_preview_no_mutation")

            reset(); p = get_preview(); real_writer = core.write_preference_edges
            def fail_midway(*args):
                real_writer(*args)
                raise RuntimeError("synthetic-transaction-failure")
            before = current()
            with patch.object(core, "write_preference_edges", fail_midway):
                try: graph.commit("synthetic_a", p["preview_token"], consent)
                except RuntimeError: pass
                else: raise AssertionError("must fail")
            assert current() == before
            assert graph.status("synthetic_a", p["preview_id"]) is None
            mark("graph_mid_transaction_rolls_back_all")

            reset(); p = get_preview(); original_project = social._project
            with patch.object(social, "_project", side_effect=RuntimeError("synthetic-mongo-outage")):
                r = submit(p)
            assert r.json()["status"] == "reconciliation_required" and current()["revision"] == 1
            assert db.profiles.find_one({"user_id": "synthetic_a"})["profile_memory_preview"] == []
            blocked = ordinary(); assert blocked["error_code"] == "preference_projection_pending"
            r = client.post(path + "/operations/" + p["preview_id"] + "/reconcile", headers=headers)
            assert r.json()["status"] == "committed" and current()["revision"] == 1
            mark("graph_committed_mongo_failure_reconciles_without_rewrite")

            reset(); p = get_preview(); before_fact = db.preference_facts.find_one({"user_id": "synthetic_a"})
            original_replace = social.FACTS.replace_one
            def fail_mongo_midway(*args, **kwargs):
                original_replace(*args, **kwargs)
                raise RuntimeError("synthetic-mongo-mid-transaction-failure")
            with patch.object(social.FACTS, "replace_one", fail_mongo_midway): r = submit(p)
            assert r.json()["status"] == "reconciliation_required" and current()["revision"] == 1
            assert db.preference_facts.find_one({"user_id": "synthetic_a"}) == before_fact
            assert db.profiles.find_one({"user_id": "synthetic_a"}).get("preference_bootstrap_pending") == p["preview_id"]
            r = client.post(path + "/operations/" + p["preview_id"] + "/reconcile", headers=headers)
            assert r.json()["status"] == "committed" and current()["revision"] == 1
            mark("mongo_mid_transaction_rolls_back_projection_only")

            reset(); p = get_preview(); original_call = social.graph_call
            def lose_ack(action, body):
                data = original_call(action, body)
                if action == "ack": raise BootstrapError("bootstrap_graph_outcome_unknown", 503)
                return data
            with patch.object(social, "graph_call", lose_ack): r = submit(p)
            assert r.json()["status"] == "reconciliation_required"
            projected = db.profiles.find_one({"user_id": "synthetic_a"})
            r = client.post(path + "/operations/" + p["preview_id"] + "/reconcile", headers=headers)
            assert r.json()["status"] == "committed" and current()["revision"] == 1
            assert db.profiles.find_one({"user_id": "synthetic_a"})["profile_memory_revision"] == projected["profile_memory_revision"]
            mark("lost_ack_idempotent_mongo_projection")

            reset(); p = get_preview(); original_call = social.graph_call
            def lose_response(action, body):
                data = original_call(action, body)
                if action == "commit": raise BootstrapError("bootstrap_graph_outcome_unknown", 503)
                return data
            with patch.object(social, "graph_call", lose_response):
                r = submit(p)
            assert r.json()["status"] == "committed" and current()["revision"] == 1
            mark("client_timeout_after_commit_resolves_journal")

            reset(); p = get_preview(); original_call = social.graph_call
            def fail_before(action, body):
                if action == "commit": raise BootstrapError("bootstrap_graph_outcome_unknown", 503)
                return original_call(action, body)
            before = current()
            with patch.object(social, "graph_call", fail_before): r = submit(p)
            assert r.json()["status"] == "aborted" and current() == before
            assert graph.commit("synthetic_a", p["preview_token"], consent)["status"] == "aborted"
            mark("before_graph_failure_and_late_delivery_tombstone")

            reset(); p = get_preview(); q = get_preview(); barrier = threading.Barrier(2)
            def simultaneous(receipt):
                barrier.wait()
                try: return graph.commit("synthetic_a", receipt["preview_token"], consent)["status"]
                except Exception as exc:
                    # Synthetic, loopback-only DB diagnostics; never provider output.
                    print(json.dumps({"race_error": type(exc).__name__, "detail": str(exc)}))
                    return type(exc).__name__
            with ThreadPoolExecutor(max_workers=2) as pool:
                outputs = list(pool.map(simultaneous, [p, q]))
            assert outputs.count("committed") == 1 and current()["revision"] == 1
            mark("two_bootstraps_one_winner", outcomes=outputs)

            reset(); p = get_preview(); barrier = threading.Barrier(2)
            with ThreadPoolExecutor(max_workers=2) as pool:
                outputs = list(pool.map(simultaneous, [p, p]))
            assert outputs == ["committed", "committed"] and current()["revision"] == 1
            mark("concurrent_duplicate_commit_one_mutation")

            reset(); p = get_preview(); barrier = threading.Barrier(2)
            def normal_racer():
                barrier.wait(); return ordinary()
            def bootstrap_racer():
                barrier.wait()
                try: return graph.commit("synthetic_a", p["preview_token"], consent)["status"]
                except Exception as exc: return type(exc).__name__
            with ThreadPoolExecutor(max_workers=2) as pool:
                one, two = pool.submit(normal_racer), pool.submit(bootstrap_racer)
                normal, bootstrap = one.result(), two.result()
            assert (normal["status"] == "success") != (bootstrap == "committed")
            assert current()["revision"] == 1
            mark("bootstrap_vs_ordinary_writer_serialized")

            reset(); p = get_preview(); assert submit(p).json()["status"] == "committed"
            assert ordinary()["status"] == "success"
            before = current(); before_profile = db.profiles.find_one({"user_id": "synthetic_a"})
            r = client.post(path + "/rollback", json={"operation_id": p["preview_id"], "revision": 1, "confirmed": True}, headers=headers)
            assert r.status_code == 409 and current() == before
            assert db.profiles.find_one({"user_id": "synthetic_a"}) == before_profile
            mark("rollback_after_later_write_stops_without_clobber")

            reset(); p = get_preview(); source_time = time.time(); assert submit(p).json()["status"] == "committed"
            identity = agent_api.canonicalize_fresh_concept("Old queued memory")
            r = asyncio.run(agent_api.apply_memory(agent_api.MemoryApplyRequest(user_id="synthetic_a", source_created_at=source_time,
                memories=[{**identity.as_dict(), "stance": "like", "confidence": 1.0}])))
            assert r["error_code"] == "preference_source_superseded" and current()["revision"] == 1
            mark("old_source_cannot_resurrect_after_complete_set")

            from services.preference_projection_fence import projection_write
            before_fact = db.preference_facts.find_one({"user_id": "synthetic_a"})
            try:
                with projection_write("synthetic_a", source_created_at=source_time):
                    raise AssertionError("stale Mongo source must not enter transaction body")
            except BootstrapError as exc:
                assert exc.code == "preference_source_superseded"
            assert db.preference_facts.find_one({"user_id": "synthetic_a"}) == before_fact
            mark("old_mongo_feedback_source_cannot_resurrect")

            # All actual ordinary mutation APIs, not only the shared lock helper.
            pending = graph.preview("synthetic_a", "add_only", ["Science Fiction"], [])
            graph.commit("synthetic_a", pending["preview_token"], consent)
            v2key = current()["rows"][0]["concept"]["key"]
            for action in ("disable", "restore", "correct"):
                outcome = asyncio.run(agent_api.memory_action(agent_api.MemoryActionRequest(
                    user_id="synthetic_a", key=v2key, action=action,
                    value="Fresh correction" if action == "correct" else None, source_created_at=time.time())))
                assert outcome["error_code"] == "preference_projection_pending"
            mark("ordinary_disable_restore_correction_share_pending_fence")

            reset(); p = get_preview(); assert submit(p).json()["status"] == "committed"
            new_key = p["create_concepts"][0]["key"]
            with driver.session() as session:
                session.run("MATCH (u:User {id:'synthetic_b'}),(c:Concept {key:$key}) CREATE (u)-[:PREFERS]->(c)", key=new_key).consume()
            r = client.post(path + "/rollback", json={"operation_id": p["preview_id"], "revision": 1, "confirmed": True}, headers=headers)
            assert r.json()["status"] == "rolled_back" and r.json()["retained_concept_count"] == 1
            with driver.session() as session:
                assert session.run("MATCH (:User {id:'synthetic_b'})-[:PREFERS]->(:Concept {key:$key}) RETURN count(*) AS n", key=new_key).single()["n"] == 1
            mark("rollback_never_deletes_other_owner_reference")

        report = {"status": "PASS", "synthetic_only": True, "production_ready": False,
                  "graph": topology, "mongo": mongo, "scenarios": results,
                  "authentication": "synthetic principal override; real HMAC internal boundary",
                  "semantic_flags": "OFF", "provider_calls": 0}
        local_graph.json_file(ROOT / ".runtime/bootstrap-integration.json", report)
        print(json.dumps({"status": "PASS", "scenario_count": len(results)}))


if __name__ == "__main__":
    main()
