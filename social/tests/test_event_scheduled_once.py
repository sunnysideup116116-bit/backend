import mongomock
import pytest
from services import event_discovery_job_service as jobs


def test_one_off_waits_until_due_and_preserves_weekly_key(monkeypatch):
    coll = mongomock.MongoClient().db.jobs
    coll.insert_one({"_id": jobs._JOB_ID, "state": "completed", "last_schedule_key": "2026-W37"})
    monkeypatch.setattr(jobs, "_jobs", coll)
    monkeypatch.setattr(jobs.time, "time", lambda: 1000.0)
    assert jobs.enqueue_event_discovery_job(job_kind="weekly_cycle", source="scheduled_once", not_before=2000)["status"] == "queued"
    assert jobs.claim_event_discovery_job("worker") is None
    row = coll.find_one()
    assert row["last_schedule_key"] == "2026-W37"
    assert row["scheduled_for"] == 2000
    # An attempted second enqueue cannot overwrite the waiting job.
    assert jobs.enqueue_event_discovery_job()["status"] == "already_running"
    monkeypatch.setattr(jobs.time, "time", lambda: 2000.0)
    claimed = jobs.claim_event_discovery_job("worker")
    assert claimed["job_kind"] == "weekly_cycle"
    assert jobs.claim_event_discovery_job("another-worker") is None


@pytest.mark.parametrize("value", [float("nan"), float("inf"), 999])
def test_invalid_schedule_is_rejected_before_writing(monkeypatch, value):
    coll = mongomock.MongoClient().db.jobs
    monkeypatch.setattr(jobs, "_jobs", coll)
    monkeypatch.setattr(jobs.time, "time", lambda: 1000)
    with pytest.raises(ValueError):
        jobs.enqueue_event_discovery_job(not_before=value)
    assert coll.count_documents({}) == 0
