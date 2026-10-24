"""Read-only release rollback guard contracts; all Mongo access is mocked."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "rollback_retry_guard", REPO_ROOT / "scripts" / "rollback_retry_guard.py",
)
guard = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(guard)


TOKEN = "a" * 32


def completed_job(**overrides):
    job = {
        "_id": guard.SINGLETON_ID,
        "state": "completed",
        "job_kind": "weekly_cycle",
        "stage": "completed",
        "job_token": TOKEN,
        "lease_owner": "",
        "lease_expires_at": 0.0,
        "retry_at": 0.0,
        "retry_count": 0,
        "scheduled_for": 0.0,
        "run_number": 43,
        "last_schedule_key": "2026-W43",
        "started_at": 100.0,
        "finished_at": 200.0,
        "outcome": "success",
    }
    job.update(overrides)
    return job


def completed_run(token=TOKEN, **overrides):
    run = {
        "_id": token,
        "stage": "result",
        "users_prepared": True,
        "result": {"status": "success", "job_kind": "weekly_cycle", "cycle_id": token},
    }
    run.update(overrides)
    return run


def valid_checkpoint():
    return {
        "v": 1,
        "policy": guard.CHECKPOINT_POLICY,
        "owner": "requester-ref",
        "snapshot": "snapshot-hash",
        "model": None,
        "fallback_model": None,
        "completed": {},
        "partial": {},
        "actor_digests": {},
        "progress": {"requester-ref|*": {"next_index": 0, "signal_count": 0}},
    }


class Cursor(list):
    def max_time_ms(self, _timeout):
        return self

    def batch_size(self, _size):
        return self


class FakeCollection:
    def __init__(self, *, rows=(), document=None, fail_find=None, invalid_state_count=0):
        self.rows = list(rows)
        self.document = document
        self.fail_find = fail_find
        self.invalid_state_count = invalid_state_count
        self.queries = []

    def count_documents(self, query, **kwargs):
        self.queries.append((query, kwargs))
        if query == {"state": {"$nin": ["pending", "done"]}}:
            return self.invalid_state_count
        if query.get("state") != "pending":
            return 0
        outcome = query.get("outcome")
        run_filter = query.get("run_id")
        count = 0
        for row in self.rows:
            if row.get("state") != "pending":
                continue
            if isinstance(outcome, dict) and "$nin" in outcome:
                if row.get("outcome") in outcome["$nin"]:
                    continue
            elif outcome is not None and row.get("outcome") != outcome:
                continue
            if isinstance(run_filter, dict) and "$ne" in run_filter:
                if row.get("run_id") == run_filter["$ne"]:
                    continue
            count += 1
        return count

    def find(self, query, projection):
        self.queries.append((query, projection))
        if self.fail_find is not None:
            raise self.fail_find
        assert query == {"state": "pending"}
        assert "user_id" not in projection and projection["_id"] == 0
        return Cursor(row.copy() for row in self.rows if row.get("state") == "pending")

    def find_one(self, query, projection, **kwargs):
        self.queries.append((query, projection, kwargs))
        assert "max_time_ms" in kwargs
        if self.document is None or self.document.get("_id") != query.get("_id"):
            return None
        return self.document.copy()


class FakeDB:
    name = guard.EXPECTED_PRODUCTION_DB_NAME

    def __init__(self, *, rows=(), job=None, run=None, collection_names=None,
                 fail_collection_list=None, fail_find=None, invalid_state_count=0):
        self.collections = {
            "event_discovery_jobs": FakeCollection(document=job),
            "event_weekly_users": FakeCollection(
                rows=rows, fail_find=fail_find, invalid_state_count=invalid_state_count,
            ),
            "event_weekly_runs": FakeCollection(document=run),
        }
        self.collection_names = set(self.collections if collection_names is None else collection_names)
        self.fail_collection_list = fail_collection_list

    def __getitem__(self, name):
        return self.collections[name]

    def list_collection_names(self, **kwargs):
        if self.fail_collection_list is not None:
            raise self.fail_collection_list
        assert kwargs["maxTimeMS"] == guard.QUERY_TIMEOUT_MS
        return [name for name in self.collection_names
                if name in kwargs["filter"]["name"]["$in"]]


def make_snapshot(*, job=None, run=None, rows=(), now=1000.0, invalid_state_count=0):
    if job is None:
        job = completed_job()
    if run is None and job.get("job_kind") == "weekly_cycle":
        run = completed_run(job["job_token"])
    db = FakeDB(rows=rows, job=job, run=run, invalid_state_count=invalid_state_count)
    return guard.read_retry_snapshot(db, now=now), db


def queued_retry_job(token=TOKEN, **overrides):
    job = completed_job(
        state="queued", retry_count=1, retry_at=2000.0, started_at=500.0,
        finished_at=0.0, outcome="failed", stage="retry_wait",
        lease_owner="", lease_expires_at=0.0,
        job_token=token,
    )
    job.update(overrides)
    return job


def active_release_link(tmp_path):
    worktrees = tmp_path / ".worktrees"
    worktrees.mkdir()
    target = worktrees / "release-current"
    target.mkdir()
    link = worktrees / "active-release"
    link.symlink_to(target, target_is_directory=True)
    return link


def test_correct_database_valid_singleton_and_no_pending_pass():
    snapshot, _ = make_snapshot()
    assert guard.evaluate_retry_snapshot(snapshot) == []


def test_missing_singleton_is_blocked_even_when_database_is_empty():
    # Model a reachable database whose collections exist but whose singleton is absent.
    db = FakeDB(job=None, run=None)
    snapshot = guard.read_retry_snapshot(db, now=1000)
    assert "singleton job is missing" in guard.evaluate_retry_snapshot(snapshot)


def test_missing_required_collections_block_an_empty_or_wrong_database(tmp_path):
    empty = FakeDB(job=None, run=None, collection_names=set())
    with pytest.raises(guard.GuardCheckError, match="collections are missing"):
        guard.read_retry_snapshot(empty, now=1000)

    wrong = tmp_path / "wrong-db-fixture.env"
    wrong.write_text("MONGO_URI=mongodb://mock.invalid\nMONGO_DB_NAME=another_db\n")
    with pytest.raises(guard.GuardCheckError, match="does not match"):
        guard._load_mongo_settings(wrong)


def test_missing_mongo_db_name_blocks_without_default_or_shell_fallback(monkeypatch, tmp_path):
    env_file = tmp_path / "missing-db.env"
    env_file.write_text("MONGO_URI=mongodb://mock.invalid\n")
    monkeypatch.setenv("MONGO_DB_NAME", guard.EXPECTED_PRODUCTION_DB_NAME)
    with pytest.raises(guard.GuardCheckError, match="configuration is incomplete"):
        guard._load_mongo_settings(env_file)


def test_unknown_singleton_state_and_incomplete_snapshot_fail_closed():
    snapshot, _ = make_snapshot(job=completed_job(state="mystery"))
    assert "singleton job state is unknown" in guard.evaluate_retry_snapshot(snapshot)
    assert guard.evaluate_retry_snapshot({"job": completed_job()}) == ["retry snapshot is incomplete"]


def test_not_due_retry_requires_the_pinned_compatible_worker_contract():
    row = {"state": "pending", "run_id": TOKEN, "outcome": "retry_pending", "resume_at": 2001.0}
    snapshot, _ = make_snapshot(
        job=queued_retry_job(), run={"_id": TOKEN, "stage": "invitation_scan"}, rows=[row], now=1000,
    )
    assert "deferred retry rows require the pinned compatible rollback worker" in \
        guard.evaluate_retry_snapshot(snapshot)
    assert guard.evaluate_retry_snapshot(snapshot, compatibility_verified=True) == []


def test_active_lease_unknown_pending_and_malformed_checkpoint_block():
    active_job = completed_job(
        state="running", lease_owner="worker-id", lease_expires_at=1001.0,
        started_at=900.0, finished_at=0.0, outcome="", stage="scanning_invitations",
    )
    active, _ = make_snapshot(
        job=active_job, run={"_id": TOKEN, "stage": "invitation_scan"}, now=1000,
    )
    assert "singleton job lease is still active" in guard.evaluate_retry_snapshot(active)

    unknown_row = {"state": "pending", "run_id": TOKEN, "outcome": "mystery", "resume_at": 1100.0}
    unknown, _ = make_snapshot(
        job=queued_retry_job(), run={"_id": TOKEN, "stage": "invitation_scan"}, rows=[unknown_row],
    )
    assert "weekly rows contain an unknown state" not in guard.evaluate_retry_snapshot(unknown)
    assert "weekly job contains unclassified pending rows" in guard.evaluate_retry_snapshot(unknown)

    malformed = {"state": "pending", "run_id": TOKEN, "outcome": "resume_pending",
                 "resume_at": 1100.0, "checkpoint": {"v": 99}}
    snapshot, _ = make_snapshot(
        job=queued_retry_job(), run={"_id": TOKEN, "stage": "invitation_scan"}, rows=[malformed],
    )
    blockers = guard.evaluate_retry_snapshot(snapshot, compatibility_verified=True)
    assert "retry rows contain an unknown or malformed checkpoint" in blockers
    assert "resume_pending rows have no checkpoint" not in blockers


def test_resume_pending_without_checkpoint_and_unrecognized_run_are_blocked():
    row = {"state": "pending", "run_id": TOKEN, "outcome": "resume_pending", "resume_at": 999.0}
    snapshot, _ = make_snapshot(
        job=queued_retry_job(), run={"_id": TOKEN, "stage": "invitation_scan"}, rows=[row], now=1000,
    )
    blockers = guard.evaluate_retry_snapshot(snapshot, compatibility_verified=True)
    assert "resume_pending rows have no checkpoint" in blockers

    bad_run = {"_id": TOKEN, "stage": "invitation_scan", "invitation_scan": "malformed"}
    snapshot, _ = make_snapshot(
        job=queued_retry_job(), run=bad_run, rows=[
            {"state": "pending", "run_id": TOKEN, "outcome": "retry_pending",
             "resume_at": 999.0, "checkpoint": valid_checkpoint()},
        ], now=1000,
    )
    assert "weekly run invitation_scan checkpoint is malformed" in \
        guard.evaluate_retry_snapshot(snapshot, compatibility_verified=True)


def test_w43_to_w44_completed_schedule_key_is_not_hard_coded():
    w43, _ = make_snapshot(job=completed_job(last_schedule_key="2026-W43"), now=1792972800)
    assert guard.evaluate_retry_snapshot(w43) == []
    w44_job = completed_job(last_schedule_key="2026-W44", run_number=44)
    w44_run = completed_run(TOKEN)
    w44, _ = make_snapshot(job=w44_job, run=w44_run, now=1793577600)
    assert guard.evaluate_retry_snapshot(w44) == []


def test_missing_or_partial_mongo_reads_fail_closed():
    missing = FakeDB(job=completed_job(), run=completed_run(), collection_names={
        "event_discovery_jobs", "event_weekly_users",
    })
    with pytest.raises(guard.GuardCheckError, match="collections are missing"):
        guard.read_retry_snapshot(missing, now=1000)

    timeout = FakeDB(job=completed_job(), run=completed_run(), fail_collection_list=TimeoutError())
    with pytest.raises(guard.GuardCheckError, match="could not be verified"):
        guard.read_retry_snapshot(timeout, now=1000)

    partial = FakeDB(job=completed_job(), run=completed_run(), fail_find=TimeoutError("cursor timed out"))
    with pytest.raises(TimeoutError):
        guard.read_retry_snapshot(partial, now=1000)


def test_systemd_autostart_and_process_risk_must_be_clear(monkeypatch, tmp_path):
    autostart = tmp_path / "autostart"
    autostart.mkdir()
    active = autostart / "backend.desktop"
    active.write_text("[Desktop Entry]\nExec=/srv/ayue/start_all.sh ollama\n")
    with pytest.raises(guard.GuardCheckError, match="GNOME autostart"):
        guard.verify_automatic_launchers_disabled(autostart_dirs=(autostart,))

    active.write_text(
        "[Desktop Entry]\nExec=/srv/ayue/start_all.sh ollama\n"
        "Hidden=true\nX-GNOME-Autostart-enabled=false\n"
    )
    calls = []
    def disabled_systemd(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout=(
            "LoadState=loaded\nActiveState=inactive\nUnitFileState=disabled\n"
        ))
    monkeypatch.setattr(guard.subprocess, "run", disabled_systemd)
    guard.verify_automatic_launchers_disabled(autostart_dirs=(autostart,))
    assert len(calls) == 2

    def active_systemd(args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=(
            "LoadState=loaded\nActiveState=active\nUnitFileState=enabled\n"
        ))
    monkeypatch.setattr(guard.subprocess, "run", active_systemd)
    with pytest.raises(guard.GuardCheckError, match="systemd launcher is active"):
        guard.verify_automatic_launchers_disabled(autostart_dirs=(autostart,))

    release = tmp_path / "release"
    release.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    current = proc / "2000"
    current.mkdir()
    (current / "status").write_text("Name:\tguard\nPPid:\t1\n", encoding="ascii")
    worker = proc / "2001"
    worker.mkdir()
    (worker / "status").write_text("Name:\tpython\nPPid:\t1\n", encoding="ascii")
    (worker / "cwd").symlink_to(tmp_path / "other-release", target_is_directory=True)
    (worker / "cmdline").write_bytes(b"python\0-m\0uvicorn social.main:app\0")
    assert guard.count_release_processes([release], proc_root=proc, current_pid=2000) == 1


def test_active_entry_from_disabled_file_and_systemd_query_failure_block(monkeypatch):
    monkeypatch.setattr(
        guard.subprocess, "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    with pytest.raises(guard.GuardCheckError, match="systemd launcher state"):
        guard._systemd_unit_is_inactive_and_disabled("user")


def test_mongo_authorization_failure_blocks_before_snapshot_completion():
    denied = FakeDB(
        job=completed_job(), run=completed_run(),
        fail_collection_list=PermissionError("MongoDB not authorized"),
    )
    with pytest.raises(guard.GuardCheckError, match="collections could not be verified"):
        guard.read_retry_snapshot(denied, now=1000)


def test_checkpoint_validator_accepts_progress_only_and_rejects_unknown_shape():
    assert guard._valid_checkpoint(valid_checkpoint())
    malformed = {**valid_checkpoint(), "extra": "unknown"}
    assert not guard._valid_checkpoint(malformed)
    malformed = {**valid_checkpoint(), "progress": {"requester-ref|*": {"next_index": -1}}}
    assert not guard._valid_checkpoint(malformed)


def test_missing_or_wrong_database_name_never_uses_profiling_db_default(tmp_path):
    env_file = tmp_path / "wrong-db.env"
    env_file.write_text("MONGO_URI=mongodb://mock.invalid\nMONGO_DB_NAME=profiling_db\n")
    with pytest.raises(guard.GuardCheckError, match="does not match"):
        guard._load_mongo_settings(env_file)


def test_main_read_failure_is_sanitized_and_never_passes(monkeypatch, tmp_path, capsys):
    current_link = active_release_link(tmp_path)
    env_file = tmp_path / "production.env"
    env_file.write_text(
        "MONGO_URI=mongodb://sensitive-user:sensitive-password@private.invalid/db\n"
        f"MONGO_DB_NAME={guard.EXPECTED_PRODUCTION_DB_NAME}\n"
    )
    monkeypatch.setattr(guard, "_parse_args", lambda _argv=None: SimpleNamespace(
        expected_target_sha="c" * 40, current_release_dir=current_link, env_file=env_file,
        workers_stopped=True, proc_root=tmp_path / "proc",
    ))
    monkeypatch.setattr(guard, "_git_head_and_clean", lambda _root: ("c" * 40, True))
    monkeypatch.setattr(guard, "_rollback_code_matches", lambda _root, _sha: True)
    monkeypatch.setattr(guard, "verify_automatic_launchers_disabled", lambda: None)
    monkeypatch.setattr(guard, "count_release_processes", lambda *_args, **_kwargs: 0)

    class Client:
        def get_database(self, name, **_kwargs):
            return SimpleNamespace(name=name)
        def close(self):
            pass

    def fail_connection(*_args, **_kwargs):
        raise RuntimeError("mongodb://sensitive-user:sensitive-password@private.invalid/db")
    monkeypatch.setattr(guard, "MongoClient", fail_connection)
    assert guard.main([]) == guard.BLOCKED
    output = capsys.readouterr().err
    assert "RuntimeError" in output
    assert "sensitive-user" not in output
    assert "sensitive-password" not in output
    assert "private.invalid" not in output


def test_main_pass_requires_stopped_attestation_and_fresh_process_checks(monkeypatch, tmp_path, capsys):
    expected = "b" * 40
    current = active_release_link(tmp_path)
    args = SimpleNamespace(
        expected_target_sha=expected, current_release_dir=current, env_file=None,
        workers_stopped=True, proc_root=tmp_path / "proc",
    )
    monkeypatch.setattr(guard, "_parse_args", lambda _argv=None: args)
    monkeypatch.setattr(guard, "_git_head_and_clean", lambda _root: (expected, True))
    monkeypatch.setattr(guard, "_rollback_code_matches", lambda _root, _sha: True)
    monkeypatch.setattr(guard, "verify_automatic_launchers_disabled", lambda: None)
    monkeypatch.setenv("MONGO_URI", "mongodb://mock.invalid")
    monkeypatch.setenv("MONGO_DB_NAME", guard.EXPECTED_PRODUCTION_DB_NAME)
    db = FakeDB(job=completed_job(), run=completed_run())

    class Client:
        closed = False
        def get_database(self, name, **_kwargs):
            assert name == guard.EXPECTED_PRODUCTION_DB_NAME
            return db
        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(guard, "MongoClient", lambda *_args, **_kwargs: client)
    counts = iter([0, 0])
    monkeypatch.setattr(guard, "count_release_processes", lambda *_a, **_k: next(counts))
    assert guard.main([]) == 0
    output = capsys.readouterr().out
    assert guard.COMPATIBILITY_ID in output
    assert "not a full MongoDB or Neo4j integrity check" in output
    assert client.closed

    args.workers_stopped = False
    called = []
    monkeypatch.setattr(guard, "MongoClient", lambda *_args, **_kwargs: called.append(True))
    assert guard.main([]) == guard.BLOCKED
    assert called == []


def test_main_mongo_to_rollback_gate_accepts_only_pinned_future_retry(monkeypatch, tmp_path, capsys):
    expected = "d" * 40
    current = active_release_link(tmp_path)
    args = SimpleNamespace(
        expected_target_sha=expected, current_release_dir=current, env_file=None,
        workers_stopped=True, proc_root=tmp_path / "proc",
    )
    monkeypatch.setattr(guard, "_parse_args", lambda _argv=None: args)
    monkeypatch.setattr(guard, "_git_head_and_clean", lambda _root: (expected, True))
    monkeypatch.setattr(guard, "_rollback_code_matches", lambda _root, _sha: True)
    monkeypatch.setattr(guard, "verify_automatic_launchers_disabled", lambda: None)
    monkeypatch.setenv("MONGO_URI", "mongodb://mock.invalid")
    monkeypatch.setenv("MONGO_DB_NAME", guard.EXPECTED_PRODUCTION_DB_NAME)
    future = guard.time.time() + 3600
    row = {
        "state": "pending", "run_id": TOKEN, "outcome": "retry_pending",
        "resume_at": future, "checkpoint": valid_checkpoint(),
    }
    run = {"_id": TOKEN, "stage": "invitation_scan", "users_prepared": True,
           "invitation_scan": {"retry_pending_count": 1}}
    db = FakeDB(rows=[row], job=queued_retry_job(retry_at=future), run=run)

    class Client:
        closed = False
        def get_database(self, name, **_kwargs):
            assert name == guard.EXPECTED_PRODUCTION_DB_NAME
            return db
        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(guard, "MongoClient", lambda *_args, **_kwargs: client)
    counts = iter([0, 0])
    monkeypatch.setattr(guard, "count_release_processes", lambda *_a, **_k: next(counts))
    assert guard.main([]) == 0
    assert "retry_pending=1" in capsys.readouterr().out
    assert client.closed


def test_candidate_pin_accepts_only_documentation_descendants(tmp_path):
    repo = tmp_path / "rollback"
    repo.mkdir()
    def git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args], check=True,
            capture_output=True, text=True,
        )

    git("init", "-q")
    git("config", "user.name", "Rollback Test")
    git("config", "user.email", "rollback-test@example.invalid")
    (repo / "scripts").mkdir()
    (repo / "social/tests").mkdir(parents=True)
    (repo / "scripts/rollback_retry_guard.py").write_text("guard-v1\n")
    git("add", ".")
    git("commit", "-q", "-m", "rollback code")
    code_sha = git("rev-parse", "HEAD").stdout.strip()

    (repo / "docs").mkdir()
    (repo / "docs/production-cutover-runbook.md").write_text("runbook\n")
    (repo / "docs/rollback-runtime-manifest.json").write_text("{}\n")
    git("add", ".")
    git("commit", "-q", "-m", "document rollback")
    assert guard._rollback_code_matches(repo, code_sha)

    (repo / "scripts/rollback_retry_guard.py").write_text("guard-v2\n")
    git("add", ".")
    git("commit", "-q", "-m", "change rollback code")
    assert not guard._rollback_code_matches(repo, code_sha)
