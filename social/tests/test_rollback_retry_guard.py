"""Read-only release rollback guard contracts; all Mongo access is mocked."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import subprocess
from types import SimpleNamespace

import mongomock
import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "rollback_retry_guard", REPO_ROOT / "scripts" / "rollback_retry_guard.py",
)
guard = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(guard)


class FakeCollection:
    def __init__(self, *, counts, retry_runs=(), resume_runs=(), job=None):
        self.counts = counts
        self.retry_runs = list(retry_runs)
        self.resume_runs = list(resume_runs)
        self.job = job
        self.queries = []
        self.pending_rows = []
        for outcome, count, checkpoint_count in (
            ("retry_pending", counts.get("retry", 0), counts.get("retry_checkpoint", 0)),
            ("resume_pending", counts.get("resume", 0), counts.get("resume_checkpoint", 0)),
        ):
            for index in range(count):
                row = {"outcome": outcome, "resume_at": "not-a-time"
                       if index < counts.get("invalid_resume_at", 0) else 1001}
                if index < checkpoint_count:
                    row["checkpoint"] = {}
                if index < counts.get("invalid_checkpoint", 0):
                    row["checkpoint"] = "malformed"
                self.pending_rows.append(row)
        for _ in range(counts.get("unclassified", 0)):
            self.pending_rows.append({"resume_at": 1001})

    def count_documents(self, query, **kwargs):
        self.queries.append((query, kwargs))
        if isinstance(query.get("run_id"), dict):
            if query.get("outcome") == "retry_pending":
                return self.counts.get("retry_other", 0)
            if query.get("outcome") == "resume_pending":
                return self.counts.get("resume_other", 0)
            return self.counts.get("unclassified_other", 0)
        return 0

    def find(self, query, projection):
        self.queries.append((query, projection))
        assert query == {"state": "pending"}
        assert "user_id" not in projection and "_id" in projection and projection["_id"] == 0
        class Cursor(list):
            def max_time_ms(self, _timeout):
                return self
            def batch_size(self, _size):
                return self
        return Cursor(self.pending_rows)

    def find_one(self, query, projection, **kwargs):
        assert query == {"_id": "event-discovery-singleton"}
        assert "max_time_ms" in kwargs
        return self.job


class FakeDB(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


def make_snapshot(*, retry=0, resume=0, unclassified=0, job=None, retry_runs=(), resume_runs=(),
                  retry_checkpoint=0, resume_checkpoint=0, invalid_resume_at=0,
                  invalid_checkpoint=0, retry_other=0, resume_other=0,
                  unclassified_other=0, now=1000.0):
    users = FakeCollection(counts={
        "retry": retry, "resume": resume,
        "retry_checkpoint": retry_checkpoint,
        "resume_checkpoint": resume_checkpoint,
        "invalid_resume_at": invalid_resume_at,
        "invalid_checkpoint": invalid_checkpoint,
        "retry_other": retry_other,
        "resume_other": resume_other,
        "unclassified": unclassified,
        "unclassified_other": unclassified_other,
    }, retry_runs=retry_runs, resume_runs=resume_runs)
    jobs = FakeCollection(counts={"retry": 0}, job=job)
    snapshot = guard.read_retry_snapshot(FakeDB(
        event_weekly_users=users, event_discovery_jobs=jobs,
    ), now=now)
    return snapshot, users


def test_snapshot_reports_retry_resume_checkpoint_counts_without_identities():
    snapshot, users = make_snapshot(
        retry=2, resume=1, retry_checkpoint=1, resume_checkpoint=0,
        job={"state": "queued", "job_kind": "weekly_cycle", "job_token": "same-run"},
    )
    assert snapshot["retry_pending_count"] == 2
    assert snapshot["resume_pending_count"] == 1
    assert snapshot["retry_checkpoint_present_count"] == 1
    assert snapshot["retry_checkpoint_absent_count"] == 1
    assert snapshot["resume_checkpoint_present_count"] == 0
    assert snapshot["resume_checkpoint_absent_count"] == 1
    assert snapshot["pending_unclassified_count"] == 0
    assert guard.evaluate_retry_snapshot(snapshot) == []
    # No query returns row documents or user identifiers; all are counts or run ids.
    assert all("user_id" not in query for query, _ in users.queries)
    assert all("checkpoint" not in query or query["checkpoint"] == {"$exists": True}
               or "$not" in query["checkpoint"] for query, _ in users.queries)


def test_realistic_mongomock_snapshot_and_malformed_state_fail_closed():
    db = mongomock.MongoClient().rollback
    db.event_weekly_users.insert_many([
        {"run_id": "same-run", "state": "pending", "outcome": "retry_pending",
         "resume_at": 990, "checkpoint": {"v": 1}},
        {"run_id": "same-run", "state": "pending", "outcome": "resume_pending",
         "resume_at": 1010},
        {"run_id": "same-run", "state": "pending", "resume_at": 1005},
    ])
    db.event_discovery_jobs.insert_one({
        "_id": "event-discovery-singleton", "state": "queued",
        "job_kind": "weekly_cycle", "job_token": "same-run", "retry_at": 1010,
    })

    snapshot = guard.read_retry_snapshot(db, now=1000)
    assert snapshot["retry_pending_count"] == 1
    assert snapshot["resume_pending_count"] == 1
    assert snapshot["pending_unclassified_count"] == 1
    assert snapshot["retry_checkpoint_present_count"] == 1
    assert snapshot["resume_checkpoint_absent_count"] == 1
    assert guard.evaluate_retry_snapshot(snapshot) == []

    db.event_weekly_users.insert_one({
        "run_id": "same-run", "state": "pending", "outcome": "retry_pending",
        "resume_at": "not-a-timestamp", "checkpoint": "malformed",
    })
    db.event_weekly_users.insert_many([
        {"run_id": "same-run", "state": "pending", "outcome": "retry_pending",
         "resume_at": float("nan")},
        {"run_id": "same-run", "state": "pending", "outcome": "resume_pending",
         "resume_at": float("inf")},
    ])
    malformed = guard.read_retry_snapshot(db, now=1000)
    blockers = guard.evaluate_retry_snapshot(malformed)
    assert malformed["invalid_resume_at_count"] == 3
    assert malformed["invalid_checkpoint_count"] == 1
    assert blockers


def test_guard_fails_closed_for_active_or_ambiguous_worker_state():
    active, _ = make_snapshot(
        retry=1, retry_runs=["same-run"], now=1000,
        job={"state": "running", "job_kind": "weekly_cycle", "job_token": "same-run",
             "lease_expires_at": 1001},
    )
    assert "singleton job lease is still active" in guard.evaluate_retry_snapshot(active)

    malformed, _ = make_snapshot(
        retry=1, retry_runs=["same-run"], invalid_resume_at=1, invalid_checkpoint=1,
        job={"state": "queued", "job_kind": "weekly_cycle", "job_token": "same-run"},
    )
    blockers = guard.evaluate_retry_snapshot(malformed)
    assert "retry rows have missing or invalid resume_at values" in blockers
    assert "retry rows contain malformed checkpoints" in blockers

    orphaned, _ = make_snapshot(retry=1, retry_runs=["old-run"], job=None)
    assert "pending weekly rows have no singleton job" in guard.evaluate_retry_snapshot(orphaned)

    terminal_unknown, _ = make_snapshot(
        unclassified=1,
        job={"state": "completed", "job_kind": "weekly_cycle", "job_token": "same-run"},
    )
    assert "terminal job still has pending weekly rows" in guard.evaluate_retry_snapshot(terminal_unknown)


def test_expired_lease_is_reclaimable_only_for_same_weekly_job():
    expired, _ = make_snapshot(
        resume=1, resume_runs=["same-run"], now=1000,
        job={"state": "running", "job_kind": "weekly_cycle", "job_token": "same-run",
             "lease_expires_at": 999},
    )
    assert guard.evaluate_retry_snapshot(expired) == []

    wrong_job, _ = make_snapshot(
        retry=1, retry_other=1, now=1000,
        job={"state": "queued", "job_kind": "discovery", "job_token": "same-run",
             "retry_at": "not-a-time"},
    )
    assert guard.evaluate_retry_snapshot(wrong_job)


def test_process_check_detects_release_cwd_without_exposing_command_line(tmp_path):
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
    (worker / "cwd").symlink_to(release, target_is_directory=True)
    (worker / "cmdline").write_bytes(b"python\0service\0")

    assert guard.count_release_processes([release], proc_root=proc, current_pid=2000) == 1


def test_partial_database_cursor_raises_and_cannot_be_certified():
    class IncompleteCollection(FakeCollection):
        def find(self, _query, _projection):
            raise TimeoutError("server cursor timed out after a partial read")

    users = IncompleteCollection(counts={})
    db = FakeDB(event_weekly_users=users, event_discovery_jobs=FakeCollection(
        counts={}, job={"state": "queued", "job_kind": "weekly_cycle", "job_token": "run"},
    ))
    with pytest.raises(TimeoutError):
        guard.read_retry_snapshot(db, now=1000)


def test_explicit_missing_env_file_does_not_fall_back_to_shell_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("MONGO_URI", "mongodb://should-not-be-used.invalid/db")
    with pytest.raises(guard.GuardCheckError, match="configuration file is unavailable"):
        guard._load_mongo_settings(tmp_path / "missing.env")


def test_pinned_code_sha_accepts_only_documentation_descendants(tmp_path):
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
    (repo / "social/services").mkdir(parents=True)
    (repo / "scripts/rollback_retry_guard.py").write_text("guard-v1\n")
    (repo / "social/services/event_weekly_service.py").write_text("weekly-v1\n")
    git("add", ".")
    git("commit", "-q", "-m", "rollback code")
    code_sha = git("rev-parse", "HEAD").stdout.strip()

    (repo / "docs").mkdir()
    (repo / "docs/production-cutover-runbook.md").write_text("runbook\n")
    git("add", ".")
    git("commit", "-q", "-m", "document rollback")
    assert guard._rollback_code_matches(repo, code_sha)

    (repo / "scripts/rollback_retry_guard.py").write_text("guard-v2\n")
    git("add", ".")
    git("commit", "-q", "-m", "change rollback code")
    assert not guard._rollback_code_matches(repo, code_sha)


def test_main_does_not_print_connection_errors_or_secret_values(monkeypatch, tmp_path, capsys):
    expected = "a" * 40
    current = tmp_path / "current"
    current.mkdir()
    monkeypatch.setattr(guard, "_parse_args", lambda _argv=None: SimpleNamespace(
        expected_target_sha=expected, current_release_dir=current, env_file=None,
        workers_stopped=True, proc_root=tmp_path / "proc",
    ))
    monkeypatch.setattr(guard, "_git_head_and_clean", lambda _root: (expected, True))
    monkeypatch.setattr(guard, "_rollback_code_matches", lambda _root, _sha: True)
    monkeypatch.setattr(guard, "count_release_processes", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(guard, "_load_mongo_settings", lambda _path: (
        "mongodb://sensitive-user:sensitive-password@example.invalid/db", "db",
    ))

    def fail_connection(*_args, **_kwargs):
        raise RuntimeError("mongodb://sensitive-user:sensitive-password@example.invalid/db")

    monkeypatch.setattr(guard, "MongoClient", fail_connection)
    assert guard.main([]) == guard.BLOCKED
    output = capsys.readouterr()
    assert "RuntimeError" in output.err
    assert "sensitive-user" not in output.err
    assert "sensitive-password" not in output.err
    assert "example.invalid" not in output.err


def test_main_pass_is_scoped_and_refuses_any_release_process(monkeypatch, tmp_path, capsys):
    expected = "b" * 40
    current = tmp_path / "current"
    current.mkdir()
    args = SimpleNamespace(
        expected_target_sha=expected, current_release_dir=current, env_file=None,
        workers_stopped=True, proc_root=tmp_path / "proc",
    )
    monkeypatch.setattr(guard, "_parse_args", lambda _argv=None: args)
    monkeypatch.setattr(guard, "_git_head_and_clean", lambda _root: (expected, True))
    monkeypatch.setattr(guard, "_rollback_code_matches", lambda _root, _sha: True)
    monkeypatch.setattr(guard, "_load_mongo_settings", lambda _path: ("mock-uri", "mock-db"))
    snapshot = {
        "retry_pending_count": 0, "resume_pending_count": 0,
        "pending_unclassified_count": 0,
        "retry_checkpoint_present_count": 0, "retry_checkpoint_absent_count": 0,
        "resume_checkpoint_present_count": 0, "resume_checkpoint_absent_count": 0,
        "invalid_resume_at_count": 0, "invalid_checkpoint_count": 0,
        "retry_other_run_count": 0, "resume_other_run_count": 0,
        "unclassified_other_run_count": 0,
        "job": None, "observed_at": 1000,
    }

    class Client:
        closed = False
        def get_database(self, *_args, **_kwargs):
            return object()
        def close(self):
            self.closed = True

    client = Client()
    monkeypatch.setattr(guard, "MongoClient", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(guard, "read_retry_snapshot", lambda _db: snapshot)
    counts = iter([0, 0])
    monkeypatch.setattr(guard, "count_release_processes", lambda *_a, **_k: next(counts))
    assert guard.main([]) == 0
    output = capsys.readouterr().out
    assert "SCOPE: weekly retry/lease metadata only" in output
    assert "not a full MongoDB or Neo4j integrity check" in output
    assert client.closed

    called = []
    monkeypatch.setattr(guard, "count_release_processes", lambda *_a, **_k: 1)
    monkeypatch.setattr(guard, "MongoClient", lambda *_a, **_k: called.append(True))
    assert guard.main([]) == guard.BLOCKED
    assert called == []
