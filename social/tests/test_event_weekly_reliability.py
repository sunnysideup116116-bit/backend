"""Offline persistence contracts for weekly coverage and unattended delivery."""

from datetime import datetime
from unittest.mock import MagicMock

import pytest

from services import event_delivery_service as delivery
from services import event_weekly_service as weekly
from services import event_discovery_job_service as jobs
from tests.match_flow_store import Collection


@pytest.fixture
def stores(monkeypatch):
    runs, users, matches = Collection(), Collection(), Collection()
    profiles = Collection([{"user_id": f"owner-{i}"} for i in range(65)])
    for name, store in [("RUNS", runs), ("USERS", users), ("matches_coll", matches), ("profiles_coll", profiles)]:
        monkeypatch.setattr(weekly, name, store)
    return runs, users, matches, profiles


def test_all_users_are_considered_beyond_thirty_and_three(monkeypatch, stores):
    runs, users, matches, profiles = stores
    seen = []
    def create(uid, **kwargs):
        seen.append(uid)
        return {"status": "created", "match_id": uid}
    monkeypatch.setattr(weekly, "create_event_opportunity", create)
    monkeypatch.setenv("EVENT_OPPORTUNITY_MAX_PROPOSALS_PER_SCAN", "3")
    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert len(set(seen)) == 65
    assert result["created_count"] == 65
    assert len(users.rows) == 65
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert len(seen) == 65


def test_restart_recovers_committed_result_without_new_selection(monkeypatch, stores):
    runs, users, matches, profiles = stores
    profiles.rows = [{"user_id": "owner"}]
    matches.rows = [{"_id": "saved", "event_cycle_id": "week", "event_cycle_requester": "owner"}]
    create = MagicMock(side_effect=AssertionError("must not select twice"))
    monkeypatch.setattr(weekly, "create_event_opportunity", create)
    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert result["created_count"] == 1
    assert users.rows[0]["match_id"] == "saved"


def test_failures_retry_bounded_and_do_not_starve_other_users(monkeypatch, stores):
    profiles = stores[3]
    profiles.rows = [{"user_id": "bad"}, {"user_id": "good"}]
    calls = {}
    def create(uid, **_):
        calls[uid] = calls.get(uid, 0) + 1
        return {"status": "error", "error_code": "event_graph_unavailable"} if uid == "bad" else {"status": "no_match"}
    monkeypatch.setattr(weekly, "create_event_opportunity", create)
    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert result["status"] == "retry_pending"
    assert result["failed_user_count"] == 1
    assert result["attempt_count"] == 2
    assert result["retry_pending_count"] == 1
    bad_row = next(row for row in stores[1].rows if row["user_id"] == "bad")
    assert bad_row["state"] == "pending" and bad_row["outcome"] == "retry_pending"
    assert bad_row["resume_at"] > weekly.time.time()

    # A pass before the due time cannot select or spin on the failed row.
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert calls == {"bad": 1, "good": 1}

    # Once due, the existing three-attempt limit still bounds retries.
    for _ in range(2):
        bad_row["resume_at"] = 0
        result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert calls == {"bad": 3, "good": 1}
    assert result["status"] == "partial"
    assert result["attempt_count"] == 4
    assert result["retry_pending_count"] == 0
    assert bad_row["state"] == "done" and bad_row["outcome"] == "error"
    assert bad_row["error_code"] == "event_graph_unavailable"


@pytest.mark.parametrize("checkpoint", [None, {"v": 1, "progress": {"page": 2}}])
def test_retry_pending_waits_for_due_and_preserves_checkpoint(monkeypatch, stores, checkpoint):
    runs, users, matches, profiles = stores
    runs.rows.append({"_id": "week", "users_prepared": True})
    row = {"_id": "week-owner", "run_id": "week", "user_id": "owner",
           "state": "pending", "outcome": "retry_pending", "attempts": 1,
           "resume_at": weekly.time.time() + 3600}
    if checkpoint is not None:
        row["checkpoint"] = checkpoint
    users.rows.append(row)
    seen = []
    monkeypatch.setattr(weekly, "create_event_opportunity", lambda uid, **kwargs:
                        seen.append(kwargs.get("checkpoint")) or {"status": "no_match"})

    waiting = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert seen == []
    assert waiting["retry_pending_count"] == 1
    assert row["state"] == "pending" and row["outcome"] == "retry_pending"

    row["resume_at"] = 0
    due = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert seen == [checkpoint]
    assert due["retry_pending_count"] == 0
    assert row["state"] == "done" and row["outcome"] == "no_match"
    if checkpoint is not None:
        assert row["checkpoint"] == checkpoint

    # Repeating the same weekly run cannot reprocess completed work.
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert seen == [checkpoint]


def test_pending_retry_requeues_same_week_after_cycle_failure_and_worker_crash(monkeypatch, stores):
    from services import event_cycle_service as cycle
    runs, users, matches, profiles = stores
    runs.rows.append({"_id": "week", "discovery": {"status": "success"},
                      "cleanup": {"status": "success"}, "users_prepared": True})
    users.rows.append({"_id": "week-owner", "run_id": "week", "user_id": "owner",
                      "state": "pending", "outcome": "retry_pending", "attempts": 1,
                      "resume_at": weekly.time.time() + 3600,
                      "checkpoint": {"v": 1, "progress": {"page": 2}}})
    monkeypatch.setattr(cycle, "wait_for_event_relevance", lambda: {"ready": True})
    monkeypatch.setattr(weekly, "create_event_opportunity",
                        MagicMock(side_effect=AssertionError("future retry must not run")))
    queue = Collection([{"_id": jobs._JOB_ID, "state": "running", "job_kind": "weekly_cycle",
                         "job_token": "same-week-token", "lease_owner": "dead-worker",
                         "lease_expires_at": 0, "retry_count": 0}])
    monkeypatch.setattr(jobs, "_jobs", queue)

    # Simulate an old process dying after the per-user state was saved. The
    # expired singleton lease is atomically reclaimable under the same token.
    claimed = jobs.claim_event_discovery_job("rollback-worker")
    assert claimed["job_token"] == "same-week-token"
    claimed = {**claimed, "job_kind": "weekly_cycle"}
    with pytest.raises(RuntimeError, match="event_semantic_resume_pending"):
        weekly.run_durable_cycle("week", lambda: True, lambda _: None,
                                 region="高雄", window_days=30, categories=["市集"])
    assert "result" not in runs.rows[-1]
    assert runs.rows[-1]["invitation_scan"]["retry_pending_count"] == 1

    jobs.fail_event_discovery_job(claimed, RuntimeError("event_semantic_resume_pending"))
    queued = queue.find_one({"_id": jobs._JOB_ID})
    assert queued["state"] == "queued"
    assert queued["job_token"] == "same-week-token"
    assert queued["retry_count"] == 1


def test_unclassified_future_pending_never_saves_weekly_success(monkeypatch, stores):
    from services import event_cycle_service as cycle
    runs, users, matches, profiles = stores
    runs.rows.append({"_id": "week", "discovery": {"status": "success"},
                      "cleanup": {"status": "success"}, "users_prepared": True})
    users.rows.append({"_id": "week-owner", "run_id": "week", "user_id": "owner",
                      "state": "pending", "attempts": 1,
                      "resume_at": weekly.time.time() + 3600,
                      "checkpoint": {"v": 1, "progress": {"page": 2}}})
    monkeypatch.setattr(cycle, "wait_for_event_relevance", lambda: {"ready": True})
    create = MagicMock(side_effect=AssertionError("future or unclassified row must not run"))
    monkeypatch.setattr(weekly, "create_event_opportunity", create)

    with pytest.raises(RuntimeError, match="event_semantic_resume_pending"):
        weekly.run_durable_cycle("week", lambda: True, lambda _: None,
                                 region="高雄", window_days=30, categories=["市集"])

    scan = runs.rows[-1]["invitation_scan"]
    assert scan["pending_user_count"] == 1
    assert scan["pending_unclassified_count"] == 1
    assert "result" not in runs.rows[-1]
    create.assert_not_called()


def test_lease_loss_preserves_pending_progress(monkeypatch, stores):
    stores[3].rows = [{"user_id": "owner"}]
    owner = [True]
    def create(*args, **kwargs):
        owner[0] = False
        return {"status": "no_match"}
    monkeypatch.setattr(weekly, "create_event_opportunity", create)
    with pytest.raises(RuntimeError, match="ownership_lost"):
        weekly.run_invitation_batches("week", lambda: owner[0], lambda _: None)
    assert stores[1].rows[0]["state"] == "pending"


def test_population_prioritizes_never_invited(monkeypatch, stores):
    stores[3].rows = [{"user_id": "recent"}, {"user_id": "never"}]
    stores[2].rows = [{"proposal_namespace": "event_invitation", "from_user": "recent", "created_at": 100}]
    stores[0].rows = [{"_id": "week"}]
    weekly.prepare_users("week", lambda: True)
    assert stores[1].find({}).sort("order", 1)[0]["user_id"] == "never"


def test_discovery_failure_never_clears_inventory(monkeypatch, stores):
    from services import event_discovery_service as discovery, event_lifecycle_service as lifecycle
    monkeypatch.setattr(discovery, "discover_and_ingest_events", MagicMock(side_effect=RuntimeError("provider failed")))
    cleanup = MagicMock()
    monkeypatch.setattr(lifecycle, "run_event_lifecycle_once", cleanup)
    with pytest.raises(RuntimeError):
        weekly.run_durable_cycle("week", lambda: True, lambda _: None, region="高雄", window_days=30, categories=["市集"])
    cleanup.assert_not_called()


def test_resume_uses_saved_discovery_and_population(monkeypatch, stores):
    from services import event_discovery_service as discovery, event_lifecycle_service as lifecycle, event_cycle_service as cycle
    stores[0].rows = [{"_id": "week", "discovery": {"status": "partial", "active_category_counts": {"市集": 3}},
                      "cleanup": {"status": "success"}, "users_prepared": True}]
    discover = MagicMock(side_effect=AssertionError("already done"))
    monkeypatch.setattr(discovery, "discover_and_ingest_events", discover)
    monkeypatch.setattr(cycle, "wait_for_event_relevance", lambda: {"ready": True})
    result = weekly.run_durable_cycle("week", lambda: True, lambda _: None, region="高雄", window_days=30, categories=["市集"])
    assert result["status"] == "partial"
    assert result["invitation_scan"]["status"] == "success"
    assert stores[0].rows[0]["result"] == result


@pytest.fixture
def delivery_stores(monkeypatch):
    matches = Collection([{"_id": "m", "from_user": "a", "to_user": "b", "status": "draft", "proposal_namespace": "event_invitation"}])
    messages = Collection()
    profiles = MagicMock()
    monkeypatch.setattr(delivery, "matches_coll", matches)
    monkeypatch.setattr(delivery, "messages_coll", messages)
    monkeypatch.setattr(delivery, "profiles_coll", profiles)
    monkeypatch.setattr(delivery, "match_hub_v1_enabled", lambda: True)
    def persist(user, event, metadata):
        messages.update_one({"_id": f"{user}:m"}, {"$setOnInsert": {
            "room_id": delivery.match_hub_room_id(user), "message_type": "mediator_card", "metadata": metadata,
        }}, upsert=True)
    monkeypatch.setattr(delivery, "_deliver_global_event", persist)
    return matches, messages, profiles


def test_offline_delivery_preserves_two_party_consent_and_deduplicates(delivery_stores):
    matches, messages, profiles = delivery_stores
    assert delivery.deliver_event_proposals_once()["delivered"] == 1
    assert [r["_id"] for r in messages.rows] == ["a:m"]
    assert delivery.deliver_event_proposals_once()["delivered"] == 0
    matches.rows[0]["status"] = "pending"
    assert delivery.deliver_event_proposals_once()["delivered"] == 1
    assert len(messages.rows) == 2
    matches.rows[0]["status"] = "accepted"
    assert delivery.deliver_event_proposals_once()["delivered"] == 0


def test_failed_delivery_keeps_queue_and_recovers(monkeypatch, delivery_stores):
    matches, messages, profiles = delivery_stores
    persist = delivery._deliver_global_event
    monkeypatch.setattr(delivery, "_deliver_global_event", MagicMock(side_effect=RuntimeError("offline")))
    assert delivery.deliver_event_proposals_once()["failed"] == 1
    profiles.update_one.assert_not_called()
    assert not matches.rows[0]["event_delivery"].get("initiator")
    monkeypatch.setattr(delivery, "_deliver_global_event", persist)
    matches.rows[0]["event_delivery"]["retry_at"] = 0
    assert delivery.deliver_event_proposals_once()["delivered"] == 1


def test_expired_and_suppressed_proposals_are_not_delivered(delivery_stores):
    matches, messages, _ = delivery_stores
    matches.rows[0]["status"] = "expired"
    assert delivery.deliver_event_proposals_once()["delivered"] == 0
    matches.rows[0].update(status="draft", proposal_suppressed=True)
    assert delivery.deliver_event_proposals_once()["delivered"] == 0


def test_schedule_catches_up_after_monday_window(monkeypatch):
    enqueue = MagicMock(return_value={"status": "queued"})
    monkeypatch.setattr(jobs, "enqueue_event_discovery_job", enqueue)
    monkeypatch.setenv("EVENT_DISCOVERY_WEEKDAY", "0")
    monkeypatch.setenv("EVENT_DISCOVERY_HOUR", "8")
    assert jobs.enqueue_weekly_event_discovery_if_due(datetime(2026, 9, 7, 7, tzinfo=jobs.TAIPEI)) is None
    assert jobs.enqueue_weekly_event_discovery_if_due(datetime(2026, 9, 8, 10, tzinfo=jobs.TAIPEI))["status"] == "queued"
    assert enqueue.call_args.kwargs["schedule_key"] == "2026-W37"


def test_weekly_failure_requeues_same_token_but_bounds_retries(monkeypatch):
    collection = MagicMock()
    monkeypatch.setattr(jobs, "_jobs", collection)
    job = {"job_kind": "weekly_cycle", "job_token": "same", "lease_owner": "worker"}
    jobs.fail_event_discovery_job(job, RuntimeError("temporary"))
    query, update = collection.update_one.call_args.args
    assert query["job_token"] == "same"
    assert update["$set"]["state"] == "queued"
    jobs.fail_event_discovery_job({**job, "retry_count": 3}, RuntimeError("still unavailable"))
    assert collection.update_one.call_args.args[1]["$set"]["state"] == "failed"
