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

    # Each worker pass observes the persisted backoff. Force the clock window
    # open here to exercise the next bounded attempts without sleeping.
    for _ in range(2):
        bad_row["resume_at"] = 0
        result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert calls == {"bad": 3, "good": 1}
    assert result["status"] == "partial"
    assert result["attempt_count"] == 4
    assert result["retry_pending_count"] == 0
    assert bad_row["state"] == "done" and bad_row["outcome"] == "error"
    assert bad_row["error_code"] == "event_graph_unavailable"


def test_retry_backoff_rows_do_not_starve_users_after_first_batch(monkeypatch, stores):
    runs, users, matches, profiles = stores
    profiles.rows = [{"user_id": f"owner-{i}"} for i in range(65)]
    now = [1000.0]
    monkeypatch.setattr(weekly.time, "time", lambda: now[0])
    calls = []
    fail_first_batch = [True]

    def create(uid, **_):
        calls.append(uid)
        if fail_first_batch[0] and len(calls) <= 30:
            return {"status": "error", "error_code": "event_graph_unavailable"}
        return {"status": "no_match"}

    monkeypatch.setattr(weekly, "create_event_opportunity", create)
    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)

    outcomes = [row.get("outcome", "unprocessed") for row in stores[1].rows]
    assert len(calls) == 65
    first_attempt_order = calls[:30]
    assert len(first_attempt_order) == 30
    assert result["retry_pending_count"] == 30
    assert outcomes.count("retry_pending") == 30
    assert outcomes.count("no_match") == 35
    assert outcomes.count("unprocessed") == 0

    # Backoff users remain pending but do not consume the next batch's slots.
    fail_first_batch[0] = False
    calls.clear()
    now[0] = 1059.0
    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert calls == []
    assert result["retry_pending_count"] == 30

    # At the exact due time, the same users become selectable again in stable
    # persisted order; the backoff cannot turn into permanent starvation.
    now[0] = 1060.0
    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert calls == first_attempt_order
    assert result["retry_pending_count"] == 0
    assert result["status"] == "success"

    # Exercise Mongo's null/missing/range semantics and the per-run index.
    import hashlib
    import mongomock

    now[0] = 2000.0
    mongo = mongomock.MongoClient().db
    mongo_runs, mongo_users = mongo.event_weekly_runs, mongo.event_weekly_users
    mongo_matches, mongo_profiles = mongo.matches, mongo.profiles
    monkeypatch.setattr(weekly, "RUNS", mongo_runs)
    monkeypatch.setattr(weekly, "USERS", mongo_users)
    monkeypatch.setattr(weekly, "matches_coll", mongo_matches)
    monkeypatch.setattr(weekly, "profiles_coll", mongo_profiles)
    mongo_runs.insert_one({
        "_id": "resume-states", "users_prepared": False,
        "discovery": {"status": "success"}, "cleanup": {"status": "success"},
    })
    states = [
        ("missing", None), ("null", None), ("expired", 1999.0),
        ("due", 2000.0), ("future", 2001.0),
    ]
    mongo_profiles.insert_many([{"user_id": uid} for uid, _resume_at in states])
    seeded = []
    for order, (uid, resume_at) in enumerate(states):
        row = {
            "_id": hashlib.sha256(f"resume-states:{uid}".encode()).hexdigest(),
            "run_id": "resume-states", "user_id": uid, "order": order,
            "state": "pending", "attempts": 1,
        }
        if uid != "missing":
            row["resume_at"] = resume_at
        if uid == "future":
            row["outcome"] = "retry_pending"
        seeded.append(row)
    mongo_users.insert_many(seeded)
    calls.clear()

    from services import event_cycle_service as cycle
    monkeypatch.setattr(cycle, "wait_for_event_relevance", lambda: {"ready": True})
    with pytest.raises(RuntimeError, match="event_semantic_resume_pending"):
        weekly.run_durable_cycle(
            "resume-states", lambda: True, lambda _: None,
            region="test", window_days=30, categories=[],
        )

    assert calls == ["missing", "null", "expired", "due"]
    assert mongo_users.find_one({"user_id": "future"})["state"] == "pending"
    assert mongo_runs.find_one({"_id": "resume-states"})["invitation_scan"]["retry_pending_count"] == 1
    assert mongo_users.index_information()["event_weekly_pending_users"]["key"] == [
        ("run_id", 1), ("state", 1), ("order", 1),
    ]


@pytest.mark.parametrize("error_code", [
    "event_semantic_disabled",
    "event_semantic_config_unavailable",
    "event_owner_ineligible",
    "event_negative_unavailable",
])
def test_deterministic_unavailable_is_terminal_once(monkeypatch, stores, error_code):
    stores[3].rows = [{"user_id": "owner"}]
    calls = []
    monkeypatch.setattr(weekly, "create_event_opportunity", lambda uid, **_: calls.append(uid) or {
        "status": "unavailable", "error_code": error_code,
    })

    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)

    row = stores[1].rows[0]
    assert calls == ["owner"]
    assert result["status"] == "partial"
    assert result["retry_pending_count"] == result["resume_pending_count"] == 0
    assert row["state"] == "done" and row["attempts"] == 1
    assert row["outcome"] == "unavailable"
    assert row["error_code"] == error_code


def test_identity_lookup_unavailable_remains_retryable(monkeypatch, stores):
    stores[3].rows = [{"user_id": "owner"}]
    calls = []
    monkeypatch.setattr(weekly, "create_event_opportunity", lambda uid, **_: calls.append(uid) or {
        "status": "unavailable", "error_code": "event_owner_eligibility_unavailable",
    })

    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    row = stores[1].rows[0]
    assert calls == ["owner"]
    assert result["retry_pending_count"] == 1
    assert row["state"] == "pending" and row["outcome"] == "retry_pending"
    assert row["error_code"] == "event_owner_eligibility_unavailable"
    assert row["resume_at"] > weekly.time.time()


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


def test_transient_retry_pending_requeues_saved_cycle(monkeypatch, stores):
    from services import event_cycle_service as cycle
    runs = stores[0]
    runs.rows = [{"_id": "week", "discovery": {"status": "success"},
                  "cleanup": {"status": "success"}, "users_prepared": True}]
    monkeypatch.setattr(cycle, "wait_for_event_relevance", lambda: {"ready": True})
    monkeypatch.setattr(weekly, "run_invitation_batches", lambda *args, **kwargs: {
        "status": "retry_pending", "retry_pending_count": 1, "resume_pending_count": 0,
    })

    with pytest.raises(RuntimeError, match="event_semantic_resume_pending"):
        weekly.run_durable_cycle("week", lambda: True, lambda _: None,
                                 region="高雄", window_days=30, categories=["市集"])

    assert runs.rows[0]["invitation_scan"]["retry_pending_count"] == 1
    assert "result" not in runs.rows[0]


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
