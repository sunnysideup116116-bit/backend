"""Deterministic durable-resume + circuit separation tests; synthetic only.

Covers the social side of the async Event execution model: per-user
checkpoint persist/resume, bounded backoff, duplicate-enqueue safety, manual
kill vs automatic circuit, and proposal idempotency.
"""
import time
from unittest.mock import Mock

import pytest

from services import event_semantic_monitor as monitor
from services import event_weekly_service as weekly
from tests.match_flow_store import Collection


@pytest.fixture
def stores(monkeypatch, tmp_path):
    runs, users, matches = Collection(), Collection(), Collection()
    profiles = Collection([{"user_id": "owner"}])
    for name, store in [("RUNS", runs), ("USERS", users),
                        ("matches_coll", matches), ("profiles_coll", profiles)]:
        monkeypatch.setattr(weekly, name, store)
    monkeypatch.setenv("EVENT_SEMANTIC_CIRCUIT_FILE", str(tmp_path / "circuit.json"))
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(tmp_path / "manual-kill"))
    return runs, users, matches, profiles


def _resumable(uid, **kwargs):
    return {"status": "unavailable", "resumable": True,
            "checkpoint": {"v": 1, "policy": "event_relevance_v2", "owner": uid,
                           "snapshot": "s", "completed": {}, "progress": {}}}


def test_resumable_boundary_persists_checkpoint_and_defers(monkeypatch, stores):
    runs, users, matches, profiles = stores
    monkeypatch.setattr(weekly, "create_event_opportunity", _resumable)
    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    row = users.rows[0]
    assert row["state"] == "pending"
    assert row["outcome"] == "resume_pending"
    assert row["checkpoint"]["owner"] == "owner"
    assert row["resume_at"] > time.time()
    assert result["resume_pending_count"] == 1


def test_resume_waits_for_backoff_then_continues(monkeypatch, stores):
    runs, users, matches, profiles = stores
    seen = []
    def create(uid, **kwargs):
        seen.append(kwargs.get("checkpoint"))
        if len(seen) == 1:
            return _resumable(uid)
        return {"status": "no_match"}
    monkeypatch.setattr(weekly, "create_event_opportunity", create)
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    # Second pass before the backoff elapses must not re-attempt.
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert len(seen) == 1
    # Force the backoff window open and resume: the stored checkpoint is passed
    # back to the next attempt.
    users.rows[0]["resume_at"] = 0
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert len(seen) == 2 and seen[1] is not None
    assert users.rows[0]["state"] == "done"


def test_resume_horizon_is_bounded(monkeypatch, stores):
    runs, users, matches, profiles = stores
    calls = []
    def always_resumable(uid, **kwargs):
        calls.append(uid)
        return _resumable(uid)
    monkeypatch.setattr(weekly, "create_event_opportunity", always_resumable)
    for _ in range(weekly.RESUME_MAX_ATTEMPTS + 3):
        users.update_many({}, {"$set": {"resume_at": 0}})
        weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert len(calls) == weekly.RESUME_MAX_ATTEMPTS
    assert users.rows[0]["state"] == "done"


def test_partial_progress_is_not_duplicated_across_resume(monkeypatch, stores):
    runs, users, matches, profiles = stores
    # A proposal already committed for this user/run is recovered verbatim by
    # the existing idempotency check, without another selection call.
    matches.rows.append({"_id": "saved", "event_cycle_id": "week",
                         "event_cycle_requester": "owner"})
    create = Mock(side_effect=AssertionError("must not re-select"))
    monkeypatch.setattr(weekly, "create_event_opportunity", create)
    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert result["created_count"] == 1
    create.assert_not_called()


def test_duplicate_scheduler_enqueue_creates_one_logical_work_item(monkeypatch, stores):
    runs, users, matches, profiles = stores
    profiles.rows = [{"user_id": "owner"}]
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **_: {"status": "no_match"})
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert len(users.rows) == 1  # deterministic run+user id, one work item


def test_resume_does_not_duplicate_trusted_evidence(monkeypatch, stores):
    runs, users, matches, profiles = stores
    checkpoint = {"v": 1, "policy": "event_relevance_v2", "owner": "owner",
        "snapshot": "s", "completed": {"owner|ev-1": [
            {"user_ref": "r1", "event_ref": "e1", "relation": "sibling_related", "score": 0.95}]},
        "progress": {}}
    users.rows.append({"_id": "week-owner", "run_id": "week", "user_id": "owner",
        "state": "pending", "order": 0, "attempts": 1, "checkpoint": checkpoint})
    runs.rows.append({"_id": "week", "users_prepared": True})
    seen = []
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **kwargs: seen.append(kwargs.get("checkpoint")) or {"status": "no_match"})
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    # The persisted checkpoint is handed to the next attempt exactly once.
    assert seen == [checkpoint]


def test_manual_kill_is_never_written_by_the_automatic_circuit(monkeypatch, tmp_path):
    kill = tmp_path / "manual-kill"
    assert not kill.exists()
    from tests.match_flow_store import matches as query_matches

    class Store(Collection):
        def delete_many(self, query):
            self.rows = [row for row in self.rows if not query_matches(row, query)]

    store = Store()
    engage = Mock()
    for index in range(3):
        monitor.on_result("unavailable", "semantic_validator_unavailable",
            {"semantic_triggered": True, "diagnostic_id": "a" * 32, "validator": {}},
            collection=store, now=lambda: 1000 + index, engage=engage)
    # The automatic circuit engaged (its own file), the manual kill did not move.
    engage.assert_called_once()
    assert not kill.exists()


def test_circuit_state_is_separate_from_manual_kill_file(monkeypatch, tmp_path):
    from matchmaker_agent import event_semantic_circuit as circuit
    monkeypatch.setenv("EVENT_SEMANTIC_CIRCUIT_FILE", str(tmp_path / "auto-circuit"))
    monkeypatch.setenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE", str(tmp_path / "manual-kill"))
    assert circuit.snapshot()["state"] == "closed"
    circuit.open_circuit(now=lambda: 100.0)
    assert (tmp_path / "auto-circuit").exists()
    assert not (tmp_path / "manual-kill").exists()
