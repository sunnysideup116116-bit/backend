"""Owner-bootstrap coordinator: signed Graph authority + recoverable Mongo projection."""
from copy import deepcopy
import json
import threading
import time

from bson import json_util
import requests

from database import db, profiles_coll
from services.preference_projection_fence import projection_write
from matchmaker_agent.preference_bootstrap_contract import (
    BootstrapError, enabled, require, digest, internal_headers, open_preview, operation_id,
)

OPERATIONS = db["preference_bootstrap_operations"]
FACTS = db["preference_facts"]
GRAPH_PREFIX = "/api/v2/preferences/bootstrap/"
_stop = threading.Event()
_thread = None


def graph_call(action, body):
    path = GRAPH_PREFIX + action
    try:
        response = requests.post("http://127.0.0.1:9001" + path, json=body,
                                 headers=internal_headers(path, body), timeout=(3, 16))
        result = response.json()
    except (requests.RequestException, ValueError):
        raise BootstrapError("bootstrap_graph_outcome_unknown", 503) from None
    if response.status_code != 200:
        code = (result.get("detail") or {}).get("code", "bootstrap_graph_unavailable")
        raise BootstrapError(code, response.status_code)
    return result


def _key(owner, op_id):
    return digest([owner, op_id])


def _facts(owner, session=None):
    rows = list(FACTS.find({"user_id": owner}, session=session).sort("_id", 1).limit(101))
    require(len(rows) <= 100, "bootstrap_fact_inventory_overflow", 422)
    return rows


def _facts_hash(rows):
    return digest(json.loads(json_util.dumps(rows, sort_keys=True)))


def _public(record):
    if not record:
        return {"status": "reconciliation_required", "graph_status": "unknown"}
    return {"operation_id": record["operation_id"], "mode": record["mode"],
            "status": record["status"] if record["projection_status"] == "synced" else "reconciliation_required",
            "graph_status": record["status"], "projection_status": record["projection_status"],
            "revision_before": record["revision_before"], "revision_after": record["revision_after"],
            "current_effect_revision": record["effect"]["revision"],
            "created_concept_count": len(record["created_concepts"]),
            "reused_concept_count": len(record["reused_concepts"]),
            "created_edge_count": len(record["created_edges"]),
            "retired_edge_count": len(record["retired_edges"]),
            "retained_concept_count": len(record.get("retained_concepts", []))}


def preview(owner, mode, prefers, avoids):
    require(enabled(), "preference_bootstrap_disabled", 503)
    profile = profiles_coll.find_one({"user_id": owner}, {"preference_bootstrap_pending": 1})
    require(profile is not None, "profile_missing", 404)
    require(not profile.get("preference_bootstrap_pending"), "preference_projection_pending")
    facts = _facts(owner)
    result = graph_call("preview", {"owner": owner, "mode": mode, "prefers": prefers, "avoids": avoids,
                                    "mongo_snapshot_hash": _facts_hash(facts)})
    active = {(e["concept"]["key"], e["relation"]) for e in result["active_edges"]}
    for fact in facts:
        if fact.get("active"):
            relation = "PREFERS" if fact.get("stance") in {"like", "require"} else "AVOIDS"
            require((fact.get("concept_key"), relation) in active, "unmapped_active_preference_fact", 422)
    # Owner-only preview, never prompt/trace. Physical properties stay in the
    # signed private token and journal; visible retirement keeps bounded facts.
    result.pop("active_edges", None)
    result["retire_edges"] = [{"id": e["id"], "relation": e["relation"], "key": e["concept"]["key"],
        "legacy_label": e["concept"].get("label", ""), "fidelity": "legacy_unknown"} for e in result["retire_edges"]]
    result.pop("concept_preconditions", None)
    return result


def _prepare(owner, payload, *, rollback=False):
    op_id = payload["preview_id"]
    key = _key(owner, op_id)
    with projection_write(owner, op_id) as session:
        existing = OPERATIONS.find_one({"_id": key, "owner": owner}, session=session)
        if existing:
            require(existing["plan_hash"] == payload["plan_hash"], "operation_payload_conflict")
            # A duplicate can pass the initial read, pause, then reach here
            # after the first request has fully projected. Never invalidate its
            # cache again or revive the pending flag for a consumed receipt.
            if not rollback or existing.get("graph_record", {}).get("status") == "rolled_back":
                return False
        facts = _facts(owner, session)
        if not rollback:
            require(_facts_hash(facts) == payload["mongo_snapshot_hash"] or existing is not None,
                    "stale_preview")
        else:
            require(existing is not None, "operation_not_found", 404)
            for saved in existing.get("changed_facts", []):
                current = FACTS.find_one({"_id": saved["before"]["_id"], "user_id": owner}, session=session)
                require(current == saved["after"], "rollback_fact_changed")
        OPERATIONS.update_one({"_id": key}, {"$setOnInsert": {
            "owner": owner, "operation_id": op_id, "plan_hash": payload["plan_hash"],
            "mode": payload.get("mode"), "facts_before": facts, "created_at": time.time(),
        }, "$set": {"status": "submitting_rollback" if rollback else "submitting", "updated_at": time.time()}},
            upsert=True, session=session)
        if rollback:
            OPERATIONS.update_one({"_id": key}, {"$unset": {"projected_revision": ""},
                "$set": {"repair_projection": True}}, session=session)
        profiles_coll.update_one({"user_id": owner}, {"$set": {
            "preference_bootstrap_pending": op_id, "profile_memory_preview": [],
            "profile_memory_summary": "", "profile_memory_synced_at": 0,
        }, "$inc": {"profile_memory_revision": 1}}, session=session)
    return True


def _project(owner, record):
    from services.memory_service import memory_summary
    op_id = record["operation_id"]
    effect = record["effect"]
    memories = []
    for edge in effect["rows"]:
        if edge["relation"] in {"PREFERS", "AVOIDS"}:
            memories.append({**edge["concept"], "stance": "like" if edge["relation"] == "PREFERS" else "avoid",
                             "last_seen_at": edge["properties"].get("last_seen_at", 0)})
    memories.sort(key=lambda m: (m["stance"] != "avoid", -float(m.get("last_seen_at") or 0), m["key"]))
    key = _key(owner, op_id)
    with projection_write(owner, op_id) as session:
        op = OPERATIONS.find_one({"_id": key, "owner": owner}, session=session)
        require(op is not None, "operation_intent_missing")
        # Exact idempotency: a lost Graph ack must not increment cache revision,
        # evidence count or change another preference on retry.
        if op.get("projected_revision") == effect["revision"]:
            return
        changes = deepcopy(op.get("changed_facts", []))
        if record["status"] == "committed":
            retiring = {r["concept"]["key"] for r in record["retired_edges"]}
            for before in op.get("facts_before", []):
                if before.get("active") and before.get("concept_key") in retiring:
                    current = FACTS.find_one({"_id": before["_id"], "user_id": owner}, session=session)
                    prior = next((change for change in changes if change["before"]["_id"] == before["_id"]), None)
                    if prior:
                        require(current == prior["after"], "projection_fact_changed")
                        continue
                    require(current == before, "projection_fact_changed")
                    after = {**before, "active": False, "bootstrap_superseded_by": op_id}
                    FACTS.replace_one({"_id": before["_id"], "user_id": owner}, after, session=session)
                    changes.append({"before": before, "after": after})
        elif record["status"] == "rolled_back":
            for change in changes:
                current = FACTS.find_one({"_id": change["before"]["_id"], "user_id": owner}, session=session)
                require(current == change["after"], "rollback_fact_changed")
                FACTS.replace_one({"_id": change["before"]["_id"], "user_id": owner}, change["before"], session=session)
        profiles_coll.update_one({"user_id": owner}, {"$set": {
            "profile_memory_preview": memories[:12], "profile_memory_summary": memory_summary(memories[:12]),
            "profile_memory_synced_at": time.time(), "profile_memory_retry_at": 0,
            "preference_projection_revision": effect["revision"], "preference_projection_epoch": effect["epoch"],
            "preference_projection_epoch_at": effect["epoch_at"],
        }, "$inc": {"profile_memory_revision": 1}, "$unset": {"preference_bootstrap_pending": ""}}, session=session)
        OPERATIONS.update_one({"_id": key}, {"$set": {"graph_record": record, "changed_facts": changes,
            "projected_revision": effect["revision"], "status": "projected", "updated_at": time.time()}}, session=session)


def reconcile(owner, op_id):
    operation_id(op_id)
    intent = OPERATIONS.find_one({"_id": _key(owner, op_id), "owner": owner})
    require(intent is not None, "operation_not_found", 404)
    if intent.get("status") == "complete":
        return _public(intent["graph_record"])
    record = None
    try:
        record = graph_call("resolve", {"owner": owner, "operation_id": op_id, "plan_hash": intent["plan_hash"],
                                        "repair_projection": bool(intent.get("repair_projection"))})
        _project(owner, record)
        record = graph_call("ack", {"owner": owner, "operation_id": op_id, "revision": record["effect"]["revision"]})
        OPERATIONS.update_one({"_id": intent["_id"], "projected_revision": record["effect"]["revision"]},
                             {"$set": {"status": "complete", "graph_record": record, "updated_at": time.time()}})
        return _public(record)
    except Exception:
        # No exception text/provider response/raw preference goes to logs.
        try:
            OPERATIONS.update_one({"_id": intent["_id"], "status": {"$ne": "complete"}}, {"$set": {
                "status": "reconciliation_required", "updated_at": time.time()}})
            latest = OPERATIONS.find_one({"_id": intent["_id"]})
            if latest and latest.get("status") == "complete":
                return _public(latest["graph_record"])
        except Exception:
            pass
        return {**_public(record), "operation_id": op_id, "status": "reconciliation_required"}


def commit(owner, token, consent):
    from matchmaker_agent.preference_bootstrap import _consent
    payload = open_preview(token, owner, allow_expired=True)
    _consent(payload["mode"], consent)
    prior = OPERATIONS.find_one({"_id": _key(owner, payload["preview_id"]), "owner": owner})
    if prior:
        require(prior["plan_hash"] == payload["plan_hash"], "operation_payload_conflict")
        if prior.get("status") == "submitting" and time.time() - prior["updated_at"] < 25:
            return {"status": "reconciliation_required", "operation_id": payload["preview_id"], "graph_status": "unknown"}
        return reconcile(owner, payload["preview_id"])
    require(enabled(), "preference_bootstrap_disabled", 503)
    graph_call("check", {"owner": owner, "preview_token": token})  # stale → no Mongo/Graph mutation
    if not _prepare(owner, payload):
        # Another request prepared this receipt while this request was paused.
        # Report in-flight state; reconciliation never replays Graph mutation.
        return {"status": "reconciliation_required", "operation_id": payload["preview_id"], "graph_status": "unknown"}
    error = None
    record = None
    try:
        record = graph_call("commit", {"owner": owner, "preview_token": token, "consent": consent})
    except BootstrapError as exc:
        error = exc
    try:
        result = reconcile(owner, payload["preview_id"])
    except Exception:
        return {**_public(record), "operation_id": payload["preview_id"], "status": "reconciliation_required"}
    if error and error.status in {400, 403, 409, 422} and result.get("graph_status") == "aborted":
        raise error
    return result


def rollback(owner, op_id, revision, confirmed):
    require(confirmed is True, "rollback_not_confirmed", 422)
    operation_id(op_id)
    intent = OPERATIONS.find_one({"_id": _key(owner, op_id), "owner": owner})
    require(intent is not None, "operation_not_found", 404)
    record = graph_call("rollback-check", {"owner": owner, "operation_id": op_id, "revision": revision})
    require(record is not None, "operation_not_found", 404)
    if record["status"] == "rolled_back":
        return reconcile(owner, op_id)
    require(record["revision_after"] == revision, "rollback_revision_mismatch")
    if not _prepare(owner, {"preview_id": op_id, "plan_hash": intent["plan_hash"]}, rollback=True):
        return reconcile(owner, op_id)
    error = None
    rollback_record = None
    try:
        rollback_record = graph_call("rollback", {"owner": owner, "operation_id": op_id, "revision": revision})
    except BootstrapError as exc:
        error = exc
    try:
        result = reconcile(owner, op_id)
    except Exception:
        return {**_public(rollback_record), "operation_id": op_id, "status": "reconciliation_required"}
    if error and error.status in {400, 403, 409, 422} and result.get("graph_status") != "rolled_back":
        raise error
    return result


def _worker():
    while not _stop.wait(15):
        try:
            # Owner Mongo transactions serialize projection; every phase is idempotent.
            for row in OPERATIONS.find({"status": {"$ne": "complete"}}).sort("updated_at", 1).limit(10):
                if time.time() - float(row.get("updated_at") or 0) >= 30:
                    reconcile(row["owner"], row["operation_id"])
        except Exception:
            pass


def start_worker():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_worker, name="preference-bootstrap-reconciler", daemon=True)
    _thread.start()


def stop_worker():
    _stop.set()
    if _thread:
        _thread.join(timeout=2)
