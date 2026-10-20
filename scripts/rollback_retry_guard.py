#!/usr/bin/env python3
"""Read-only gate for rolling back to the retry-compatible Event release.

This command never updates MongoDB. It requires an operator stop attestation,
checks that no process still references either release, and only returns PASS
when the exact committed rollback candidate can safely resume the observed
weekly state.
"""
from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values
from pymongo import MongoClient, ReadPreference
from pymongo.read_concern import ReadConcern


COMPATIBILITY_ID = "event-weekly-retry-pending-v1"
BLOCKED = 75
USAGE = 64
QUERY_TIMEOUT_MS = 10_000
RETRY_OUTCOMES = ("retry_pending", "resume_pending")


class GuardCheckError(RuntimeError):
    """A read-only check could not produce a complete, trustworthy result."""


def _count(collection: Any, query: dict) -> int:
    return int(collection.count_documents(query, maxTimeMS=QUERY_TIMEOUT_MS))


def read_retry_snapshot(db: Any, *, now: float | None = None) -> dict[str, Any]:
    """Read counts and safe job metadata only; never retrieve user identities."""
    users = db["event_weekly_users"]
    jobs = db["event_discovery_jobs"]
    pending_filter = {"state": "pending"}
    job = jobs.find_one(
        {"_id": "event-discovery-singleton"},
        {"state": 1, "job_kind": 1, "job_token": 1, "lease_expires_at": 1,
         "retry_at": 1, "retry_count": 1},
        max_time_ms=QUERY_TIMEOUT_MS,
    )
    job_token = str((job or {}).get("job_token") or "")
    retry_other_run_filter = {"state": "pending", "outcome": "retry_pending"}
    resume_other_run_filter = {"state": "pending", "outcome": "resume_pending"}
    unclassified_other_run_filter = {
        "state": "pending", "outcome": {"$nin": list(RETRY_OUTCOMES)},
    }
    if job_token:
        retry_other_run_filter["run_id"] = {"$ne": job_token}
        resume_other_run_filter["run_id"] = {"$ne": job_token}
        unclassified_other_run_filter["run_id"] = {"$ne": job_token}
    retry_count = resume_count = 0
    retry_checkpoint_count = resume_checkpoint_count = 0
    pending_unclassified = invalid_resume_at = invalid_checkpoint = 0
    cursor = users.find(
        pending_filter,
        {"_id": 0, "outcome": 1, "resume_at": 1, "checkpoint": 1},
    ).max_time_ms(QUERY_TIMEOUT_MS).batch_size(100)
    for row in cursor:
        outcome = row.get("outcome")
        if outcome == "retry_pending":
            retry_count += 1
            retry_checkpoint_count += int("checkpoint" in row)
        elif outcome == "resume_pending":
            resume_count += 1
            resume_checkpoint_count += int("checkpoint" in row)
        else:
            pending_unclassified += 1
        resume_at = row.get("resume_at")
        if outcome in RETRY_OUTCOMES or "resume_at" in row:
            if (isinstance(resume_at, bool) or not isinstance(resume_at, (int, float))
                    or not math.isfinite(float(resume_at))
                    or (outcome in RETRY_OUTCOMES and float(resume_at) <= 0)):
                invalid_resume_at += 1
        if "checkpoint" in row and not isinstance(row.get("checkpoint"), dict):
            invalid_checkpoint += 1
    return {
        "retry_pending_count": retry_count,
        "resume_pending_count": resume_count,
        "pending_unclassified_count": pending_unclassified,
        "retry_checkpoint_present_count": retry_checkpoint_count,
        "retry_checkpoint_absent_count": retry_count - retry_checkpoint_count,
        "resume_checkpoint_present_count": resume_checkpoint_count,
        "resume_checkpoint_absent_count": resume_count - resume_checkpoint_count,
        "invalid_resume_at_count": invalid_resume_at,
        "invalid_checkpoint_count": invalid_checkpoint,
        "retry_other_run_count": _count(users, retry_other_run_filter),
        "resume_other_run_count": _count(users, resume_other_run_filter),
        "unclassified_other_run_count": _count(users, unclassified_other_run_filter),
        "job": job,
        "observed_at": time.time() if now is None else now,
    }


def evaluate_retry_snapshot(snapshot: dict[str, Any]) -> list[str]:
    """Return sanitized blockers for states the rollback candidate cannot prove safe."""
    blockers: list[str] = []
    if snapshot["invalid_resume_at_count"]:
        blockers.append("retry rows have missing or invalid resume_at values")
    if snapshot["invalid_checkpoint_count"]:
        blockers.append("retry rows contain malformed checkpoints")

    retry_count = snapshot["retry_pending_count"]
    resume_count = snapshot["resume_pending_count"]
    pending_count = retry_count + resume_count + snapshot["pending_unclassified_count"]
    job = snapshot["job"]
    if job is None:
        if pending_count:
            blockers.append("pending weekly rows have no singleton job")
        return blockers

    state = str(job.get("state") or "")
    if state not in {"queued", "running", "completed", "failed", "idle"}:
        blockers.append("singleton job state is unknown")

    if state == "running":
        lease = job.get("lease_expires_at")
        if not isinstance(lease, (int, float)) or not math.isfinite(float(lease)):
            blockers.append("running job lease is missing or invalid")
        elif float(lease) > float(snapshot["observed_at"]):
            blockers.append("singleton job lease is still active")
    if state == "queued" and "retry_at" in job:
        retry_at = job.get("retry_at")
        if (isinstance(retry_at, bool) or not isinstance(retry_at, (int, float))
                or not math.isfinite(float(retry_at))):
            blockers.append("queued job retry_at is missing a valid timestamp")

    if pending_count:
        if state not in {"queued", "running"}:
            blockers.append("pending weekly rows are not attached to a resumable job")
        if str(job.get("job_kind") or "") != "weekly_cycle":
            blockers.append("pending weekly rows are attached to a non-weekly job")
        job_token = str(job.get("job_token") or "")
        if not job_token:
            blockers.append("pending weekly rows have no recoverable job token")
        elif (snapshot["retry_other_run_count"] or snapshot["resume_other_run_count"]
              or snapshot["unclassified_other_run_count"]):
            blockers.append("pending weekly rows span a different run")

    if state in {"completed", "failed"} and pending_count:
        blockers.append("terminal job still has pending weekly rows")
    return list(dict.fromkeys(blockers))


def _ancestor_pids(proc_root: Path, current_pid: int) -> set[int]:
    result: set[int] = set()
    pid = current_pid
    while pid > 1 and pid not in result:
        result.add(pid)
        status = proc_root / str(pid) / "status"
        try:
            lines = status.read_text(encoding="ascii", errors="replace").splitlines()
        except FileNotFoundError:
            break
        except PermissionError as exc:
            raise GuardCheckError("process ancestry could not be inspected") from exc
        parent = next((line.split()[1] for line in lines if line.startswith("PPid:")), None)
        if not parent or not parent.isdigit():
            break
        pid = int(parent)
    return result


def count_release_processes(
    release_paths: list[Path], *, proc_root: Path = Path("/proc"), current_pid: int | None = None,
) -> int:
    """Count non-ancestor processes whose cwd or argv references either release."""
    roots = [path.resolve(strict=True) for path in release_paths]
    raw_paths = [str(path.absolute()).encode() for path in release_paths]
    pid = current_pid if current_pid is not None else os.getpid()
    ignored_pids = _ancestor_pids(proc_root, pid)
    active = 0
    try:
        entries = list(proc_root.iterdir())
    except (FileNotFoundError, PermissionError, OSError) as exc:
        raise GuardCheckError("process table could not be inspected") from exc
    for entry in entries:
        if not entry.name.isdigit() or int(entry.name) in ignored_pids:
            continue
        try:
            cwd = Path(os.readlink(entry / "cwd")).resolve(strict=False)
            cmdline = (entry / "cmdline").read_bytes()
        except FileNotFoundError:
            # A short-lived process exited during this read-only snapshot.
            continue
        except PermissionError as exc:
            raise GuardCheckError("process table is not fully readable") from exc
        references_path = any(
            cwd == root or root in cwd.parents for root in roots
        ) or any(raw in cmdline for raw in raw_paths)
        if references_path:
            active += 1
    return active


def _load_mongo_settings(env_file: Path | None) -> tuple[str, str]:
    values: dict[str, str | None] = {}
    if env_file is not None:
        if not env_file.is_file():
            raise GuardCheckError("Mongo configuration file is unavailable")
        try:
            values = dotenv_values(env_file, interpolate=False)
        except Exception as exc:
            raise GuardCheckError("Mongo configuration file could not be read") from exc
        uri = values.get("MONGO_URI")
        db_name = (values.get("MONGO_DB_NAME") or "profiling_db").strip()
    else:
        uri = os.environ.get("MONGO_URI")
        db_name = (os.environ.get("MONGO_DB_NAME") or "profiling_db").strip()
    if not uri or not db_name:
        raise GuardCheckError("Mongo configuration is incomplete")
    return str(uri), str(db_name)


def _git_head_and_clean(root: Path) -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            check=True, capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception as exc:
        raise GuardCheckError("rollback candidate commit could not be verified") from exc
    return head, not bool(status.strip())


def _rollback_code_matches(root: Path, expected_code_sha: str) -> bool:
    """Allow the pinned code commit plus documentation-only descendants."""
    try:
        ancestor = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", expected_code_sha, "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        if ancestor.returncode != 0:
            return False
        changed = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", expected_code_sha, "HEAD"],
            check=True, capture_output=True, text=True, timeout=3,
        ).stdout.splitlines()
    except Exception as exc:
        raise GuardCheckError("rollback code ancestry could not be verified") from exc
    return set(changed) <= {"docs/production-cutover-runbook.md"}


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-target-sha", required=True, help="full code commit SHA of the reviewed rollback candidate")
    parser.add_argument("--current-release-dir", required=True, type=Path, help="currently deployed release root")
    parser.add_argument("--env-file", type=Path, help="read Mongo settings from this file without printing values")
    parser.add_argument("--workers-stopped", action="store_true", help="operator confirms every automatic/manual worker entry is stopped")
    parser.add_argument("--proc-root", type=Path, default=Path("/proc"), help=argparse.SUPPRESS)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parse_args(argv)
    except SystemExit as exc:
        return USAGE if int(exc.code or 0) else 0

    if not re.fullmatch(r"[0-9a-fA-F]{40}", args.expected_target_sha):
        print("BLOCKED: expected rollback SHA is not a full commit SHA", file=sys.stderr)
        return BLOCKED
    if not args.workers_stopped:
        print("BLOCKED: pass --workers-stopped only after all launcher entries are stopped", file=sys.stderr)
        return BLOCKED

    candidate_root = Path(__file__).resolve().parents[1]
    current_root = args.current_release_dir
    if not current_root.is_dir() or not candidate_root.is_dir():
        print("BLOCKED: release directory is missing or inaccessible", file=sys.stderr)
        return BLOCKED
    try:
        actual_sha, clean = _git_head_and_clean(candidate_root)
        if not clean or not _rollback_code_matches(candidate_root, args.expected_target_sha.lower()):
            print("BLOCKED: rollback candidate code SHA does not match or tracked files are dirty", file=sys.stderr)
            return BLOCKED
        process_count_before = count_release_processes(
            [current_root, candidate_root], proc_root=args.proc_root,
        )
        if process_count_before:
            print(f"BLOCKED: {process_count_before} process(es) still reference a monitored release", file=sys.stderr)
            return BLOCKED

        uri, db_name = _load_mongo_settings(args.env_file)
        client = MongoClient(
            uri,
            appname="ayue-read-only-rollback-guard",
            serverSelectionTimeoutMS=8_000,
            connectTimeoutMS=4_000,
            socketTimeoutMS=12_000,
            retryWrites=False,
            read_preference=ReadPreference.SECONDARY_PREFERRED,
        )
        try:
            db = client.get_database(
                db_name,
                read_preference=ReadPreference.SECONDARY_PREFERRED,
                read_concern=ReadConcern("majority"),
            )
            snapshot = read_retry_snapshot(db)
        finally:
            client.close()

        process_count_after = count_release_processes(
            [current_root, candidate_root], proc_root=args.proc_root,
        )
        blockers = evaluate_retry_snapshot(snapshot)
        if process_count_after:
            blockers.append("a monitored release process appeared during the check")
        if blockers:
            print("BLOCKED: " + "; ".join(blockers), file=sys.stderr)
            return BLOCKED
        print(
            "PASS: read-only state is compatible with "
            f"{COMPATIBILITY_ID}; retry_pending={snapshot['retry_pending_count']} "
            f"resume_pending={snapshot['resume_pending_count']} "
            f"unclassified_pending={snapshot['pending_unclassified_count']} "
            f"retry_checkpoints={snapshot['retry_checkpoint_present_count']}/"
            f"{snapshot['retry_checkpoint_absent_count']} "
            f"resume_checkpoints={snapshot['resume_checkpoint_present_count']}/"
            f"{snapshot['resume_checkpoint_absent_count']} "
            "and no monitored worker process or active lease was observed.\n"
            "SCOPE: weekly retry/lease metadata only; this is not a full MongoDB or Neo4j integrity check."
        )
        return 0
    except GuardCheckError as exc:
        print(f"BLOCKED: {exc}; do not switch releases", file=sys.stderr)
        return BLOCKED
    except Exception as exc:
        # Exception messages can include hostnames or connection settings.
        print(f"BLOCKED: read-only preflight failed ({type(exc).__name__}); do not switch releases", file=sys.stderr)
        return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
