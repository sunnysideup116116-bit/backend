"""Deterministic regression for the progress-only checkpoint hotfix (social).

Run #31: the producer emitted `resumable=true, checkpoint=null` and Social
silently demoted the user into the legacy `attempts < 3` terminal path,
settling `done/unavailable` while a progress-only checkpoint was still on the
row. These tests pin the hardened consumer contract.
"""
import time
from unittest.mock import Mock

import pytest

from services import event_weekly_service as weekly
from tests.match_flow_store import Collection


def _checkpoint(owner='owner', next_index=0):
    return {"v": 1, "policy": "event_relevance_v2", "owner": owner, "snapshot": "s",
            "completed": {}, "partial": {}, "actor_digests": {owner: "d"},
            "progress": {f"{owner}|*": {"next_index": next_index, "signal_count": 1}}}


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


def test_progress_only_checkpoint_keeps_user_resume_pending(monkeypatch, stores):
    runs, users, matches, profiles = stores
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **_: {"status": "unavailable", "resumable": True,
                          "checkpoint": _checkpoint(uid, 0)})
    result = weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    row = users.rows[0]
    assert row["state"] == "pending" and row["outcome"] == "resume_pending"
    assert row["checkpoint"]["progress"]["owner|*"]["next_index"] == 0
    assert row["resume_at"] > time.time()


# TEST 5 — legacy attempts already == 3 must not terminalize resumable work.
def test_legacy_attempts_three_does_not_terminalize_resumable_work(monkeypatch, stores):
    runs, users, matches, profiles = stores
    users.rows.append({"_id": "week-owner", "run_id": "week", "user_id": "owner",
        "state": "pending", "order": 0, "attempts": 3, "checkpoint": _checkpoint()})
    runs.rows.append({"_id": "week", "users_prepared": True})
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **_: {"status": "unavailable", "resumable": True,
                          "checkpoint": _checkpoint(uid, 0)})
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    row = users.rows[0]
    assert row["outcome"] == "resume_pending", "async resume policy must govern, not legacy attempts<3"
    assert row["state"] == "pending"


def test_persisted_checkpoint_is_the_resume_authority_when_producer_returns_none(monkeypatch, stores):
    runs, users, matches, profiles = stores
    persisted = _checkpoint()
    users.rows.append({"_id": "week-owner", "run_id": "week", "user_id": "owner",
        "state": "pending", "order": 0, "attempts": 1, "checkpoint": persisted})
    runs.rows.append({"_id": "week", "users_prepared": True})
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **_: {"status": "unavailable", "resumable": True, "checkpoint": None})
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    row = users.rows[0]
    assert row["state"] == "pending" and row["outcome"] == "resume_pending"
    assert row["checkpoint"] == persisted  # preserved, never dropped


# TEST 8 — impossible producer inconsistency: resumable but no authority anywhere.
def test_contract_violation_fails_closed_and_non_terminal(monkeypatch, stores, caplog):
    runs, users, matches, profiles = stores
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **_: {"status": "unavailable", "resumable": True, "checkpoint": None})
    with caplog.at_level("ERROR"):
        weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    row = users.rows[0]
    assert row["state"] == "pending" and row["outcome"] == "resume_pending"
    assert any("event_resume_contract_violation" in record.message
               for record in caplog.records), "explicit contract diagnostic required"


# TEST 9 — async retry genuinely exhausted: existing bounded terminal policy.
def test_async_retry_exhaustion_keeps_bounded_terminal_policy(monkeypatch, stores):
    runs, users, matches, profiles = stores
    users.rows.append({"_id": "week-owner", "run_id": "week", "user_id": "owner",
        "state": "pending", "order": 0, "attempts": weekly.RESUME_MAX_ATTEMPTS - 1,
        "checkpoint": _checkpoint()})
    runs.rows.append({"_id": "week", "users_prepared": True})
    calls = []
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **_: calls.append(uid) or {"status": "unavailable", "resumable": True,
                                               "checkpoint": _checkpoint()})
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    row = users.rows[0]
    assert row["attempts"] == weekly.RESUME_MAX_ATTEMPTS
    assert row["state"] == "done" and row["outcome"] == "unavailable"
    assert len(calls) == 1


# Phase 7 — Run #31 structural reproduction.
def test_run31_circuit_open_user_becomes_resume_pending(monkeypatch, stores):
    """Run #31 user 6aa11ce9... : event_semantic_circuit_open, attempts already 3,
    progress-only checkpoint next_index 0. Base settled done/unavailable; the
    fix must keep it resume_pending with the checkpoint retained."""
    runs, users, matches, profiles = stores
    owner = '6aa11ce9f0f78a21b456'
    checkpoint = _checkpoint(owner, 0)
    users.rows.append({"_id": "week-6aa11ce9", "run_id": "week", "user_id": owner,
        "state": "pending", "order": 33, "attempts": 3, "checkpoint": checkpoint})
    runs.rows.append({"_id": "week", "users_prepared": True})
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **_: {"status": "unavailable", "resumable": True,
                          "error_code": "event_semantic_circuit_open",
                          "checkpoint": _checkpoint(uid, 0)})
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    row = users.rows[0]
    assert row["state"] == "pending"
    assert row["outcome"] == "resume_pending"
    assert row["checkpoint"]["progress"][f"{owner}|*"]["next_index"] == 0


def test_run31_validator_unavailable_user_resumes_after_provider_recovery(monkeypatch, stores):
    """Run #31 user 6a8aeb91... : semantic_validator_unavailable. After the
    circuit recovers the user must resume and complete, not stay terminal."""
    runs, users, matches, profiles = stores
    owner = '6a8aeb91cf8505de51d4'
    users.rows.append({"_id": "week-6a8aeb91", "run_id": "week", "user_id": owner,
        "state": "pending", "order": 42, "attempts": 3, "checkpoint": _checkpoint(owner, 0)})
    runs.rows.append({"_id": "week", "users_prepared": True})
    # First pass: circuit still open -> resumable pending.
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **_: {"status": "unavailable", "resumable": True,
                          "error_code": "semantic_validator_unavailable",
                          "checkpoint": _checkpoint(uid, 0)})
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert users.rows[0]["outcome"] == "resume_pending"
    # Provider recovers: the resumed attempt completes the user without
    # duplicating proposals (no prior proposal exists for this pair).
    users.rows[0]["resume_at"] = 0
    monkeypatch.setattr(weekly, "create_event_opportunity",
        lambda uid, **_: {"status": "no_match"})
    weekly.run_invitation_batches("week", lambda: True, lambda _: None)
    assert users.rows[0]["state"] == "done"
    assert users.rows[0]["outcome"] == "no_match"
