import mongomock
from services import event_weekly_service as weekly


def test_progress_distinguishes_missing_history_from_empty_population(monkeypatch):
    db = mongomock.MongoClient().db
    monkeypatch.setattr(weekly, "RUNS", db.runs)
    monkeypatch.setattr(weekly, "USERS", db.users)
    monkeypatch.setattr(weekly, "matches_coll", db.matches)
    assert weekly.weekly_progress("old-week") == {"status": "not_recorded"}
    db.runs.insert_one({"_id": "week", "users_prepared": True})
    db.users.insert_many([
        {"run_id": "week", "user_id": "private-a", "state": "done", "outcome": "created"},
        {"run_id": "week", "user_id": "private-b", "state": "pending"},
        {"run_id": "week", "user_id": "private-c", "state": "done", "outcome": "error"},
    ])
    db.matches.insert_many([
        {"event_cycle_id": "week", "proposal_namespace": "event_invitation", "status": "draft"},
        {"event_cycle_id": "week", "proposal_namespace": "event_invitation", "status": "pending",
         "event_delivery": {"initiator": True}},
    ])
    result = weekly.weekly_progress("week")
    assert result["population_count"] == 3
    assert result["processed_user_count"] == 2
    assert result["failed_user_count"] == 1
    assert result["pending_delivery_count"] == 2
    assert result["saved_card_count"] == 1
    assert "private" not in str(result)
