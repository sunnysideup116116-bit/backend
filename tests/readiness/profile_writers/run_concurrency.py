"""Opt-in synthetic-only real Mongo replica-set writer race rehearsal.

Reuses the owned localhost Docker harness. No production env, Graph, providers,
API listener, schema or data is used. Unique index is LOCAL TEST ONLY.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import os
from pathlib import Path
import sys
import threading
import time
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(ROOT), str(ROOT / "social"), str(ROOT / "tests/readiness/preference_bootstrap")]
import local_mongo
from run_runtime_integration import local_only


def main():
    topology = local_mongo.verify()
    assert topology["image"] == "mongo:8.2.5"
    local_mongo.wait_primary()
    os.environ.update(AYUE_SKIP_DOTENV="1", DOTENV_DISABLED="1", APPWRITE_API_KEY="synthetic-unused",
        MONGO_URI=f"mongodb://127.0.0.1:{topology['port']}/?directConnection=true&replicaSet=bootstrap_test",
        MONGO_DB_NAME="profile_writer_fixture", LLM_API_KEY="synthetic-unused", LLM_BASE_URL="http://provider.invalid/v1",
        LLM_MODEL_ID="synthetic-unused", GOOGLE_API_KEY="", OLLAMA_API_KEY="synthetic-unused",
        PREFERENCE_BOOTSTRAP_ENABLED="off", MATCH_PREFERENCE_SEMANTIC_MODE="off",
        MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED="off", MATCH_RELATED_INTEREST_ENABLED="off")
    import dotenv
    dotenv.load_dotenv = lambda *_a, **_k: False
    from pymongo import MongoClient
    from pymongo.errors import AutoReconnect, DuplicateKeyError
    from services.profile_writer import ensure_profile, update_profile, ProfileWriteConflict

    results, latencies = [], []
    with local_only({topology["port"]}), MongoClient(os.environ["MONGO_URI"],
            serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000) as client:
        assert client.admin.command("hello")["setName"] == "bootstrap_test"
        db = client["profile_writer_fixture"]
        assert db.name == "profile_writer_fixture"
        if db.profiles.estimated_document_count():
            assert db.fixture_marker.find_one({"_id": str(ROOT), "synthetic_only": True})
        assert not db.profiles.count_documents({"user_id": {"$not": {"$regex": "^synthetic_"}}})
        db.fixture_marker.update_one({"_id": str(ROOT)}, {"$setOnInsert": {"synthetic_only": True}}, upsert=True)
        col = db.profiles
        col.create_index("user_id", unique=True, name="profiles_user_id_unique")
        assert col.index_information()["profiles_user_id_unique"]["unique"] is True

        def reset():
            assert local_mongo.verify() == topology
            assert not col.count_documents({"user_id": {"$not": {"$regex": "^synthetic_"}}})
            col.delete_many({"user_id": {"$regex": "^synthetic_"}})

        def parallel(callbacks):
            barrier = threading.Barrier(len(callbacks))
            def run(callback):
                barrier.wait(timeout=15); started = time.monotonic()
                result = callback()
                latencies.append((time.monotonic() - started) * 1000)
                return result
            with ThreadPoolExecutor(max_workers=len(callbacks)) as pool:
                return list(pool.map(run, callbacks))

        def passed(name, **metadata):
            results.append({"scenario": name, "status": "PASS", **metadata})
            print(json.dumps(results[-1]), flush=True)

        reset(); owner = "synthetic_create"
        parallel([lambda: ensure_profile(col, owner, {"name": "Created once"}) for _ in range(32)])
        assert col.count_documents({"user_id": owner}) == 1 and col.find_one()["name"] == "Created once"
        passed("32_concurrent_insert_only_creators", workers=32)

        reset(); owner = "synthetic_condition"
        def guarded_interest():
            ensure_profile(col, owner)
            return update_profile(col, {"user_id": owner, "initial_interest": {"$exists": False}},
                                  {"$set": {"initial_interest": "Complete qualified preference"}})
        parallel([guarded_interest] + [lambda: ensure_profile(col, owner) for _ in range(31)])
        assert col.count_documents({"user_id": owner}) == 1
        before = col.find_one()
        assert before["initial_interest"] == "Complete qualified preference"
        miss = update_profile(col, {"user_id": owner, "initial_interest": ""}, {"$set": {"initial_interest": "stub"}})
        assert miss.matched_count == 0 and col.find_one() == before
        try: update_profile(col, {"user_id": owner, "initial_interest": ""}, {"$set": {"initial_interest": "stub"}}, upsert=True)
        except ValueError as exc: assert str(exc) == "conditional_profile_upsert_forbidden"
        else: raise AssertionError("conditional insert allowed")
        passed("create_vs_conditional_update_and_forbidden_upsert")

        # Actual registration mirror and memory facade. Only the Graph transport
        # and registration enqueue are stubbed; profile operations use real Mongo.
        from routers import system
        from models import ProfileUpdateRequest
        from services import memory_service, registration_graph_service
        from matchmaker_agent.concept_identity import canonicalize_fresh_concept
        memory = {**canonicalize_fresh_concept("Synthetic Reading").as_dict(), "stance": "like", "confidence": 1.0}
        response = MagicMock(status_code=200)
        response.json.return_value = {"status": "success", "memories": [memory]}
        with patch.object(system, "profiles_coll", col), patch.object(memory_service, "profiles_coll", col), \
                patch.object(registration_graph_service, "enqueue_registration_bootstrap", return_value=True), \
                patch.object(memory_service.requests, "post", return_value=response), \
                patch.object(memory_service, "get_graph_memory_snapshot", return_value={"available": True, "items": [memory]}):
            for round_id in range(12):
                reset(); owner = "synthetic_registration_memory"
                def register():
                    return system.update_profile(ProfileUpdateRequest(user_id=owner, name="Synthetic rich profile", interest="Board Games", age=27))
                def remember():
                    return memory_service.apply_profile_memory_proposals(owner, [memory], "synthetic", None)
                parallel([register, remember] + [lambda: ensure_profile(col, owner) for _ in range(14)])
                doc = col.find_one({"user_id": owner})
                assert col.count_documents({"user_id": owner}) == 1
                assert doc["name"] == "Synthetic rich profile" and doc["initial_interest"] == "Board Games"
                assert doc["age"] == 27 and len(doc["memory_notices"]) == 1
            passed("actual_registration_vs_memory_initialization", rounds=12, workers_per_round=16, graph_transport="stub_only")

        reset(); owner = "synthetic_rich"
        rich = {"user_id": owner, "name": "Keep", "initial_interest": "Specific qualified interest",
                "big_five": {"O": 8}, "profile_memory_revision": 14, "preference_bootstrap_pending": "synthetic-operation"}
        col.insert_one(deepcopy(rich)); before = col.find_one()
        parallel([lambda: ensure_profile(col, owner, {"name": "", "initial_interest": "", "big_five": {}, "profile_memory_revision": 0}) for _ in range(32)])
        assert col.find_one() == before and col.count_documents({"user_id": owner}) == 1
        passed("rich_profile_vs_32_empty_stub_writers")

        reset(); owner = "synthetic_duplicate"
        # Actual server E11000 carrying real keyPattern/keyValue, no synthetic
        # exception metadata: another writer wins before this simulated insert.
        class InsertRace:
            calls = []
            def update_one(self, query, update, **kwargs):
                self.calls.append((query, kwargs))
                if len(self.calls) == 1:
                    col.insert_one({"user_id": owner, "name": "Canonical winner", "retained": {"full": True}})
                    col.insert_one({"user_id": owner})  # Real unique-key violation; insert aborts.
                return col.update_one(query, update, **kwargs)
            def find(self, *args, **kwargs): return col.find(*args, **kwargs)
        race = InsertRace()
        update_profile(race, {"user_id": owner}, {"$push": {"notices": {"event_id": "synthetic-e1"}}}, upsert=True)
        doc = col.find_one()
        assert doc["name"] == "Canonical winner" and doc["retained"] == {"full": True}
        assert len(doc["notices"]) == 1 and col.count_documents({"user_id": owner}) == 1
        assert len(race.calls) == 2 and race.calls[1][1]["upsert"] is False and race.calls[1][0]["_id"] == doc["_id"]
        passed("real_server_E11000_canonical_recovery_one_update_only")

        reset(); owner = "synthetic_transport"
        class LostResponse:
            calls = 0
            def update_one(self, *args, **kwargs):
                self.calls += 1; col.update_one(*args, **kwargs)
                raise AutoReconnect("synthetic lost acknowledgement")
        transport = LostResponse()
        try: update_profile(transport, {"user_id": owner}, {"$push": {"notices": "once"}}, upsert=True)
        except AutoReconnect: pass
        else: raise AssertionError("unknown outcome was hidden")
        assert transport.calls == 1 and col.find_one()["notices"] == ["once"] and col.count_documents({"user_id": owner}) == 1
        passed("successful_write_lost_response_no_blind_replay")

        reset(); owner = "synthetic_tx"
        col.insert_one({"user_id": owner, "revision": 1})
        with client.start_session() as session:
            try:
                with session.start_transaction():
                    # Keep a genuine conditional-CAS miss + upsert inside a test
                    # adapter so the server emits E11000 and aborts this txn.
                    class TransactionRace:
                        reads = 0
                        def update_one(self, query, update, **kwargs):
                            return col.update_one({**query, "revision": 0}, update, **kwargs)
                        def find(self, *args, **kwargs):
                            self.reads += 1; return col.find(*args, **kwargs)
                    adapter = TransactionRace()
                    ensure_profile(adapter, owner, session=session)
            except DuplicateKeyError: pass
            else: raise AssertionError("aborted transaction unexpectedly recovered")
            assert adapter.reads == 0
        assert col.count_documents({"user_id": owner}) == 1 and col.find_one()["revision"] == 1
        passed("real_transaction_duplicate_propagates_without_in_tx_retry")

        # Repeat from ABSENT, not a pre-created document. Registration supplies
        # rich fields while losing initializers may only propose insert defaults.
        for round_id in range(20):
            reset(); owner = "synthetic_high_contention"
            def operation(i):
                if i == 0:
                    update_profile(col, {"user_id": owner}, {"$set": {"rich": {"nested": [1, 2, 3]}, "name": "Keep"}}, upsert=True)
                else:
                    ensure_profile(col, owner, {"rich": {}, "name": ""})
                update_profile(col, {"user_id": owner}, {"$set": {f"writers.w{i}": i}}, upsert=True)
            parallel([lambda i=i: operation(i) for i in range(48)])
            doc = col.find_one()
            assert col.count_documents({"user_id": owner}) == 1
            assert doc["rich"] == {"nested": [1, 2, 3]} and doc["name"] == "Keep"
            assert doc["writers"] == {f"w{i}": i for i in range(48)}
        passed("high_concurrency_repeat_content_preserved", rounds=20, workers_per_round=48)
        reset()
        # Even expected failures never left a duplicate user_id.
        assert col.count_documents({}) == 0
        latencies.sort()
        report = {"status": "PASS", "scenarios": results, "local_only": True, "production_access": False,
                  "mongo_image": topology["image"], "unique_index": "local fixture only",
                  "provider_calls": 0, "graph_calls": 0, "bootstrap_semantic_flags": "OFF",
                  "parallel_operations": len(latencies), "latency_ms_median": latencies[len(latencies)//2],
                  "latency_ms_p95": latencies[int(len(latencies)*.95)], "remaining_synthetic_profiles": 0}
        (ROOT / ".runtime/profile-writer-concurrency.json").write_text(json.dumps(report, indent=2))
        print(json.dumps({k:v for k,v in report.items() if k != "scenarios"}))


if __name__ == "__main__":
    main()
