"""Durable weekly progress and fair, bounded invitation batches."""

import hashlib
import os
import time

from database import db, matches_coll, profiles_coll
from services.event_opportunity_service import create_event_opportunity
from services.proposal_namespace import EVENT_INVITATION_NAMESPACE, live_proposal_query

RUNS = db["event_weekly_runs"]
USERS = db["event_weekly_users"]
RETRYABLE = {"error", "failed", "agent_error", "risk_block_unavailable", "quota_unavailable",
             "invalid_agent_projection", "unavailable", "stale"}


def weekly_progress(run_id):
    """Counts only: never expose the population's identities or preference text."""
    run = RUNS.find_one({"_id": run_id}, {"users_prepared": 1})
    if not run:
        return {"status": "not_recorded"}
    total = processed = failed = 0
    for row in USERS.aggregate([
        {"$match": {"run_id": run_id}},
        {"$group": {"_id": {"state": "$state", "outcome": "$outcome"}, "count": {"$sum": 1}}},
    ]):
        count = row["count"]
        total += count
        if row["_id"].get("state") == "done":
            processed += count
            if row["_id"].get("outcome") in RETRYABLE:
                failed += count
    created = saved = pending = 0
    for row in matches_coll.aggregate([
        {"$match": {"event_cycle_id": run_id, "proposal_namespace": EVENT_INVITATION_NAMESPACE}},
        {"$group": {"_id": {"status": "$status", "initiator": "$event_delivery.initiator",
                             "receiver": "$event_delivery.receiver"}, "count": {"$sum": 1}}},
    ]):
        count, state = row["count"], row["_id"]
        created += count
        saved += count * (int(bool(state.get("initiator"))) + int(bool(state.get("receiver"))))
        if state.get("status") == "draft" and not state.get("initiator"):
            pending += count
        if state.get("status") == "pending" and not state.get("receiver"):
            pending += count
    return {"status": "available", "population_ready": bool(run.get("users_prepared")),
            "population_count": total, "processed_user_count": processed,
            "pending_user_count": total - processed, "failed_user_count": failed,
            "created_proposal_count": created, "saved_card_count": saved,
            "pending_delivery_count": pending}


def require_owner(is_current):
    if not is_current():
        raise RuntimeError("event_cycle_ownership_lost")


def prepare_users(run_id, is_current):
    """Snapshot the population once, oldest last invitation first.

    Rows have deterministic ids; interrupted initialization is repeatable.
    Eligibility remains checked just before creation and for both participants.
    """
    run = RUNS.find_one({"_id": run_id}) or {}
    if run.get("users_prepared"):
        return
    last = {}
    for doc in matches_coll.find({"proposal_namespace": EVENT_INVITATION_NAMESPACE},
                                 {"from_user": 1, "to_user": 1, "created_at": 1}):
        for uid in (doc.get("from_user"), doc.get("to_user")):
            if uid:
                last[uid] = max(last.get(uid, 0), float(doc.get("created_at") or 0))
    population = sorted({str(p.get("user_id") or "").strip() for p in
                         profiles_coll.find({"user_id": {"$exists": True}}, {"user_id": 1})} - {""},
                        key=lambda uid: (last.get(uid, 0), hashlib.sha256(f"{run_id}:{uid}".encode()).hexdigest()))
    for order, uid in enumerate(population):
        require_owner(is_current)
        key = hashlib.sha256(f"{run_id}:{uid}".encode()).hexdigest()
        USERS.update_one({"_id": key}, {"$setOnInsert": {
            "run_id": run_id, "user_id": uid, "order": order,
            "state": "pending", "attempts": 0, "created_at": time.time(),
        }}, upsert=True)
    require_owner(is_current)
    RUNS.update_one({"_id": run_id}, {"$set": {"users_prepared": True, "population_count": len(population)}})


def run_invitation_batches(run_id, is_current, notify):
    prepare_users(run_id, is_current)
    batch_limit = max(1, min(int(os.getenv("EVENT_OPPORTUNITY_MAX_PROPOSALS_PER_SCAN", "3")), 10))
    while True:
        require_owner(is_current)
        rows = list(USERS.find({"run_id": run_id, "state": "pending"}).sort("order", 1).limit(30))
        if not rows:
            break
        created = 0
        for row in rows:
            require_owner(is_current)
            uid = row["user_id"]
            # Insertion may have committed before a process died saving its
            # checkpoint. Recover that exact result without another LLM call.
            existing = matches_coll.find_one({"event_cycle_id": run_id, "event_cycle_requester": uid}, {"_id": 1})
            if existing:
                result = {"status": "created", "match_id": str(existing["_id"])}
            elif matches_coll.count_documents(live_proposal_query(uid, EVENT_INVITATION_NAMESPACE), limit=1):
                result = {"status": "already_active"}
            else:
                try:
                    busy = set()
                    for match in matches_coll.find({"proposal_namespace": EVENT_INVITATION_NAMESPACE,
                                                    "status": {"$in": ["draft", "pending"]}},
                                                   {"from_user": 1, "to_user": 1}):
                        busy.update(str(value) for value in (match.get("from_user"), match.get("to_user")) if value)
                    result = create_event_opportunity(uid, excluded_user_ids=busy,
                                                      cycle_id=run_id, can_commit=is_current)
                except Exception as exc:
                    result = {"status": "error", "error_code": type(exc).__name__}
            require_owner(is_current)
            status = str(result.get("status") or "error")
            attempts = int(row.get("attempts", 0)) + 1
            retry = status in RETRYABLE and attempts < 3
            USERS.update_one({"_id": row["_id"]}, {"$set": {
                "state": "pending" if retry else "done", "outcome": status,
                "attempts": attempts, "updated_at": time.time(),
                "match_id": str(result.get("match_id") or ""),
            }})
            created += int(status == "created")
            if created >= batch_limit:
                break
        notify("scanning_invitations")
    counts = {}
    attempts = 0
    for row in USERS.find({"run_id": run_id}, {"outcome": 1, "attempts": 1}):
        outcome = row.get("outcome", "unprocessed")
        counts[outcome] = counts.get(outcome, 0) + 1
        attempts += int(row.get("attempts", 0))
    failures = sum(v for k, v in counts.items() if k in RETRYABLE)
    return {"status": "partial" if failures else "success",
            "created_count": counts.get("created", 0), "scanned_count": sum(counts.values()),
            "attempt_count": attempts, "failed_user_count": failures,
            "max_proposals_per_batch": batch_limit, "status_counts": counts}


def run_durable_cycle(run_id, is_current, notify, *, region, window_days, categories):
    from services.event_cycle_service import wait_for_event_relevance
    from services.event_discovery_service import discover_and_ingest_events
    from services.event_lifecycle_service import run_event_lifecycle_once

    require_owner(is_current)
    USERS.create_index([("run_id", 1), ("state", 1), ("order", 1)], name="event_weekly_pending_users")
    RUNS.create_index("created_at", name="event_weekly_history")
    RUNS.update_one({"_id": run_id}, {"$setOnInsert": {
        "created_at": time.time(), "region": region, "window_days": window_days,
        "categories": categories,
    }}, upsert=True)
    record = RUNS.find_one({"_id": run_id}) or {}
    if record.get("result"):
        return record["result"]

    def checkpoint(key, result):
        require_owner(is_current)
        RUNS.update_one({"_id": run_id}, {"$set": {key: result, "stage": key, "updated_at": time.time()}})

    # Additive refresh keeps unexpired inventory and all historical proposals.
    # Cleanup removes expired items only, never resets all Event nodes first.
    discovery = record.get("discovery")
    if not discovery:
        notify("discovering")
        discovery = discover_and_ingest_events(region=region, window_days=window_days,
                                               categories=categories, request_invitation_scan=False)
        if discovery.get("status") not in {"success", "partial"}:
            raise RuntimeError("event_discovery_unavailable")
        checkpoint("discovery", discovery)
    cleanup = record.get("cleanup")
    if not cleanup:
        notify("cleaning_expired")
        require_owner(is_current)
        cleanup = run_event_lifecycle_once()
        checkpoint("cleanup", cleanup)
    notify("waiting_relevance")
    readiness = wait_for_event_relevance()
    checkpoint("relevance_readiness", readiness)
    if not readiness.get("ready"):
        # Leave the stage checkpoint recoverable; do not report a skipped scan
        # as a completed weekly population pass.
        raise RuntimeError("event_relevance_not_ready")
    notify("scanning_invitations")
    scan = run_invitation_batches(run_id, is_current, notify)
    result = {**discovery, "job_kind": "weekly_cycle", "cycle_id": run_id,
              "reset": {"status": "not_required", "policy": "preserve_unexpired_inventory"},
              "cleanup": cleanup, "relevance_readiness": readiness, "invitation_scan": scan,
              "status": "success" if discovery.get("status") == scan.get("status") == cleanup.get("status") == "success" else "partial"}
    checkpoint("result", result)
    notify("completed")
    return result
