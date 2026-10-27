#!/usr/bin/env python3
"""Read-only gate for rolling back to the retry-compatible Event release.

The guard never updates MongoDB. It requires an operator stop attestation,
verifies the automatic launch sources are disabled, scans process state before
and after its Mongo snapshot, and passes only when the expected weekly state is
complete enough for the pinned compatibility release to resume safely.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from bson.regex import Regex
from dotenv import dotenv_values
from pymongo import MongoClient, ReadPreference
from pymongo.read_concern import ReadConcern


COMPATIBILITY_ID = "event-weekly-retry-pending-v1"
EXPECTED_PRODUCTION_DB_NAME = "profiling_fordemo"
SINGLETON_ID = "event-discovery-singleton"
REQUIRED_COLLECTIONS = (
    "event_discovery_jobs", "event_weekly_users", "event_weekly_runs",
)
CHECKPOINT_POLICY = "event_relevance_v2"
CHECKPOINT_KEYS = {
    "v", "policy", "owner", "snapshot", "model", "fallback_model",
    "completed", "partial", "actor_digests", "progress",
}
REQUIRED_SNAPSHOT_KEYS = {
    "retry_pending_count", "resume_pending_count", "pending_unclassified_count",
    "retry_checkpoint_present_count", "retry_checkpoint_absent_count",
    "resume_checkpoint_present_count", "resume_checkpoint_absent_count",
    "missing_resume_checkpoint_count", "invalid_resume_at_count",
    "invalid_checkpoint_count", "retry_not_due_count", "resume_not_due_count",
    "invalid_user_state_count", "retry_other_run_count", "resume_other_run_count",
    "unclassified_other_run_count", "job", "run", "observed_at",
}
MAX_CHECKPOINT_BYTES = 256_000
BLOCKED = 75
USAGE = 64
QUERY_TIMEOUT_MS = 10_000
RETRY_OUTCOMES = ("retry_pending", "resume_pending")
VALID_JOB_STATES = {"queued", "running", "completed", "failed"}
VALID_JOB_KINDS = {"weekly_cycle", "discovery"}
VALID_JOB_STAGES = {
    "queued", "retry_wait", "failed", "starting", "discovering",
    "cleaning_expired", "waiting_relevance", "scanning_invitations", "completed",
}
VALID_RUNNING_STAGES = {
    "starting", "discovering", "cleaning_expired", "waiting_relevance",
    "scanning_invitations", "completed",
}
AUTOSTART_DIRS = (
    Path.home() / ".config" / "autostart",
    Path("/etc/xdg/autostart"),
)
SYSTEMD_UNIT = "ayue-backend.service"


class GuardCheckError(RuntimeError):
    """A read-only check could not produce a complete, trustworthy result."""


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _valid_evidence_map(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for key, items in value.items():
        if not isinstance(key, str) or key.count("|") != 1 or not isinstance(items, list):
            return False
        for item in items:
            if not isinstance(item, dict):
                return False
            if any(not isinstance(item.get(field), str) or not item[field]
                   for field in ("user_ref", "event_ref", "relation")):
                return False
            if "score" in item and item["score"] is not None and not _finite_number(item["score"]):
                return False
            if item.get("source_kind", "durable") not in {"durable", "recent"}:
                return False
    return True


def _valid_checkpoint(value: Any) -> bool:
    """Validate the exact v1 semantic checkpoint shape accepted by this release."""
    if not isinstance(value, dict) or set(value) != CHECKPOINT_KEYS:
        return False
    if type(value.get("v")) is not int or value["v"] != 1:
        return False
    if value.get("policy") != CHECKPOINT_POLICY:
        return False
    if any(not isinstance(value.get(field), str) or not value[field]
           for field in ("owner", "snapshot")):
        return False
    if any(value.get(field) is not None and not isinstance(value.get(field), str)
           for field in ("model", "fallback_model")):
        return False
    if not _valid_evidence_map(value.get("completed")):
        return False
    if not _valid_evidence_map(value.get("partial")):
        return False
    digests = value.get("actor_digests")
    if not isinstance(digests, dict) or any(
        not isinstance(key, str) or not isinstance(item, str)
        for key, item in digests.items()
    ):
        return False
    progress = value.get("progress")
    if not isinstance(progress, dict):
        return False
    for key, slot in progress.items():
        if not isinstance(key, str) or not isinstance(slot, dict):
            return False
        next_index = slot.get("next_index")
        signal_count = slot.get("signal_count")
        if type(next_index) is not int or next_index < 0:
            return False
        if signal_count is not None and (type(signal_count) is not int or signal_count < 0):
            return False
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError):
        return False
    return len(encoded.encode("utf-8")) <= MAX_CHECKPOINT_BYTES


def _count(collection: Any, query: dict) -> int:
    return int(collection.count_documents(query, maxTimeMS=QUERY_TIMEOUT_MS))


def read_retry_snapshot(db: Any, *, now: float | None = None) -> dict[str, Any]:
    """Read bounded metadata from the expected collections; never return user ids."""
    try:
        required_names = "^(?:" + "|".join(
            re.escape(name) for name in REQUIRED_COLLECTIONS
        ) + ")$"
        names = set(db.list_collection_names(
            # Atlas listCollections requires the name filter to be a BSON regex;
            # a query-operator document such as {"$in": [...]} fails closed there.
            # Keep the regex fully anchored to this small, fixed allowlist.
            filter={"name": Regex(required_names)},
            maxTimeMS=QUERY_TIMEOUT_MS,
        ))
    except Exception as exc:
        raise GuardCheckError("required Mongo collections could not be verified") from exc
    if not set(REQUIRED_COLLECTIONS).issubset(names):
        raise GuardCheckError("required Mongo collections are missing")

    users = db["event_weekly_users"]
    jobs = db["event_discovery_jobs"]
    runs = db["event_weekly_runs"]
    job = jobs.find_one(
        {"_id": SINGLETON_ID},
        {"_id": 1, "state": 1, "job_kind": 1, "job_token": 1, "stage": 1,
         "lease_owner": 1, "lease_expires_at": 1, "retry_at": 1,
         "retry_count": 1, "scheduled_for": 1, "run_number": 1,
         "last_schedule_key": 1, "started_at": 1, "finished_at": 1,
         "outcome": 1},
        max_time_ms=QUERY_TIMEOUT_MS,
    )
    job_token = str((job or {}).get("job_token") or "")
    run = None
    if job and job.get("job_kind") == "weekly_cycle" and job_token:
        run = runs.find_one(
            {"_id": job_token},
            {"_id": 1, "stage": 1, "users_prepared": 1, "discovery": 1,
             "cleanup": 1, "invitation_scan": 1, "result": 1},
            max_time_ms=QUERY_TIMEOUT_MS,
        )

    retry_other_run_filter = {"state": "pending", "outcome": "retry_pending", "run_id": {"$ne": job_token}}
    resume_other_run_filter = {"state": "pending", "outcome": "resume_pending", "run_id": {"$ne": job_token}}
    unclassified_other_run_filter = {
        "state": "pending", "outcome": {"$nin": list(RETRY_OUTCOMES)},
        "run_id": {"$ne": job_token},
    }
    retry_count = resume_count = 0
    retry_checkpoint_count = resume_checkpoint_count = 0
    pending_unclassified = invalid_resume_at = invalid_checkpoint = 0
    missing_resume_checkpoint = retry_not_due = resume_not_due = 0
    pending_filter = {"state": "pending"}
    cursor = users.find(
        pending_filter,
        {"_id": 0, "run_id": 1, "outcome": 1, "resume_at": 1, "checkpoint": 1},
    ).max_time_ms(QUERY_TIMEOUT_MS).batch_size(100)
    observed_at = time.time() if now is None else now
    for row in cursor:
        outcome = row.get("outcome")
        has_checkpoint = "checkpoint" in row and row.get("checkpoint") is not None
        if outcome == "retry_pending":
            retry_count += 1
            retry_checkpoint_count += int(has_checkpoint)
        elif outcome == "resume_pending":
            resume_count += 1
            resume_checkpoint_count += int(has_checkpoint)
            missing_resume_checkpoint += int(not has_checkpoint)
        else:
            pending_unclassified += 1

        if has_checkpoint and not _valid_checkpoint(row.get("checkpoint")):
            invalid_checkpoint += 1
        resume_at = row.get("resume_at")
        if outcome in RETRY_OUTCOMES or "resume_at" in row:
            if not _finite_number(resume_at) or (outcome in RETRY_OUTCOMES and float(resume_at) <= 0):
                invalid_resume_at += 1
            elif float(resume_at) > observed_at:
                if outcome == "retry_pending":
                    retry_not_due += 1
                elif outcome == "resume_pending":
                    resume_not_due += 1

    return {
        "retry_pending_count": retry_count,
        "resume_pending_count": resume_count,
        "pending_unclassified_count": pending_unclassified,
        "retry_checkpoint_present_count": retry_checkpoint_count,
        "retry_checkpoint_absent_count": retry_count - retry_checkpoint_count,
        "resume_checkpoint_present_count": resume_checkpoint_count,
        "resume_checkpoint_absent_count": resume_count - resume_checkpoint_count,
        "missing_resume_checkpoint_count": missing_resume_checkpoint,
        "invalid_resume_at_count": invalid_resume_at,
        "invalid_checkpoint_count": invalid_checkpoint,
        "retry_not_due_count": retry_not_due,
        "resume_not_due_count": resume_not_due,
        "invalid_user_state_count": _count(users, {"state": {"$nin": ["pending", "done"]}}),
        "retry_other_run_count": _count(users, retry_other_run_filter),
        "resume_other_run_count": _count(users, resume_other_run_filter),
        "unclassified_other_run_count": _count(users, unclassified_other_run_filter),
        "job": job,
        "run": run,
        "observed_at": observed_at,
    }


def _valid_weekly_run(run: Any, job: dict[str, Any], *, required: bool) -> list[str]:
    blockers: list[str] = []
    if run is None:
        if required:
            blockers.append("weekly run record is missing")
        return blockers
    if not isinstance(run, dict) or run.get("_id") != job.get("job_token"):
        return ["weekly run record does not match the singleton token"]
    if "stage" in run and (not isinstance(run["stage"], str) or not run["stage"]):
        blockers.append("weekly run stage is malformed")
    if "users_prepared" in run and type(run["users_prepared"]) is not bool:
        blockers.append("weekly population checkpoint is malformed")
    for key in ("discovery", "cleanup", "invitation_scan", "result"):
        if key in run and not isinstance(run[key], dict):
            blockers.append(f"weekly run {key} checkpoint is malformed")
    result = run.get("result")
    if result is not None:
        if not isinstance(result, dict):
            blockers.append("weekly terminal result is malformed")
        elif (
            result.get("job_kind") != "weekly_cycle"
            or result.get("cycle_id") != job.get("job_token")
            or result.get("status") not in {"success", "partial"}
        ):
            blockers.append("weekly terminal result is unknown or mismatched")
    if job.get("state") == "completed" and (
        run.get("stage") != "result" or not isinstance(result, dict)
    ):
        blockers.append("completed weekly job has no durable result checkpoint")
    return blockers


def evaluate_retry_snapshot(
    snapshot: dict[str, Any], *, compatibility_verified: bool = False,
) -> list[str]:
    """Return blockers unless the singleton and every durable retry are provable."""
    blockers: list[str] = []
    if not REQUIRED_SNAPSHOT_KEYS.issubset(snapshot):
        return ["retry snapshot is incomplete"]
    for key, message in (
        ("invalid_resume_at_count", "retry rows have missing or invalid resume_at values"),
        ("invalid_checkpoint_count", "retry rows contain an unknown or malformed checkpoint"),
        ("missing_resume_checkpoint_count", "resume_pending rows have no checkpoint"),
        ("invalid_user_state_count", "weekly rows contain an unknown state"),
    ):
        if snapshot.get(key, 0):
            blockers.append(message)

    job = snapshot.get("job")
    if not isinstance(job, dict):
        blockers.append("singleton job is missing")
        return blockers
    if job.get("_id") != SINGLETON_ID:
        blockers.append("singleton job identity is invalid")
    state = job.get("state")
    kind = job.get("job_kind")
    stage = job.get("stage")
    token = job.get("job_token")
    if state not in VALID_JOB_STATES:
        blockers.append("singleton job state is unknown")
    if kind not in VALID_JOB_KINDS:
        blockers.append("singleton job kind is unknown")
    if stage not in VALID_JOB_STAGES:
        blockers.append("singleton job stage is unknown")
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        blockers.append("singleton job token is missing or malformed")
    run_number = job.get("run_number")
    if type(run_number) is not int or run_number < 1:
        blockers.append("singleton run number is missing or malformed")
    retry_count = job.get("retry_count")
    if type(retry_count) is not int or retry_count < 0:
        blockers.append("singleton retry count is missing or malformed")
    elif kind in VALID_JOB_KINDS:
        retry_limit = 8 if kind == "weekly_cycle" else 3
        if retry_count > retry_limit + 1:
            blockers.append("singleton retry count exceeds the supported retry horizon")

    schedule_key = job.get("last_schedule_key")
    if schedule_key is not None and (
        not isinstance(schedule_key, str)
        or not re.fullmatch(r"\d{4}-W(?:0[1-9]|[1-4]\d|5[0-3])", schedule_key)
    ):
        blockers.append("singleton schedule key is malformed")

    for field in ("retry_at", "scheduled_for", "started_at", "finished_at"):
        value = job.get(field)
        if not _finite_number(value) or float(value) < 0:
            blockers.append(f"singleton {field} is missing or malformed")

    lease_owner = job.get("lease_owner")
    lease = job.get("lease_expires_at")
    if state == "running":
        if not isinstance(lease_owner, str) or not lease_owner or not _finite_number(lease) or float(lease) <= 0:
            blockers.append("running singleton lease metadata is missing or malformed")
        elif float(lease) > float(snapshot.get("observed_at", 0)):
            blockers.append("singleton job lease is still active")
    elif lease_owner != "" or not _finite_number(lease) or float(lease) != 0:
        blockers.append("non-running singleton has unknown or uncleared lease metadata")

    if state in {"completed", "failed"}:
        finished_at = job.get("finished_at")
        if _finite_number(finished_at) and float(finished_at) <= 0:
            blockers.append("terminal singleton has no finish time")
        expected_outcome = {"completed": {"success", "partial"}, "failed": {"failed"}}[state]
        if job.get("outcome") not in expected_outcome:
            blockers.append("terminal singleton outcome is unknown")
        if stage != state:
            blockers.append("terminal singleton stage does not match its state")
    else:
        if job.get("finished_at") != 0.0:
            blockers.append("live singleton has an unexpected finish time")
        started_at = job.get("started_at")
        if state == "running" and _finite_number(started_at) and float(started_at) <= 0:
            blockers.append("running singleton has no start time")
        if state == "queued":
            if stage not in {"queued", "retry_wait"}:
                blockers.append("queued singleton has an incompatible stage")
            if job.get("outcome") not in {"", "failed"}:
                blockers.append("queued singleton outcome is unknown")
        if state == "running" and job.get("outcome") not in {"", "failed"}:
            blockers.append("running singleton outcome is unknown")
        if state == "running" and stage not in VALID_RUNNING_STAGES:
            blockers.append("running singleton has an incompatible stage")
    if kind == "weekly_cycle" and state == "completed" and job.get("outcome") not in {"success", "partial"}:
        blockers.append("completed weekly job has an unsupported outcome")
    if kind == "discovery" and state in {"completed", "failed"}:
        outcome = job.get("outcome")
        if not isinstance(outcome, str) or not outcome or len(outcome) > 30:
            blockers.append("terminal discovery outcome is missing or malformed")

    retry_pending = snapshot.get("retry_pending_count", 0)
    resume_pending = snapshot.get("resume_pending_count", 0)
    unclassified = snapshot.get("pending_unclassified_count", 0)
    pending_count = retry_pending + resume_pending + unclassified
    if kind != "weekly_cycle" and pending_count:
        blockers.append("pending weekly rows are attached to a non-weekly singleton")
    if pending_count and state not in {"queued", "running"}:
        blockers.append("pending weekly rows are not attached to a resumable job")
    if pending_count and kind == "weekly_cycle":
        if not isinstance(token, str) or not token:
            blockers.append("pending weekly rows have no recoverable job token")
        if (snapshot.get("retry_other_run_count", 0)
                or snapshot.get("resume_other_run_count", 0)
                or snapshot.get("unclassified_other_run_count", 0)):
            blockers.append("pending weekly rows span a different run")
        if retry_count and retry_count > 3:
            blockers.append("retry_pending rows exceed the retryable worker horizon")
        if snapshot.get("retry_not_due_count", 0) or snapshot.get("resume_not_due_count", 0):
            if not compatibility_verified:
                blockers.append("deferred retry rows require the pinned compatible rollback worker")

    if pending_count and kind == "weekly_cycle" and not retry_pending and not resume_pending:
        blockers.append("weekly job contains unclassified pending rows")
    if state == "completed" and pending_count:
        blockers.append("terminal singleton still has pending weekly rows")

    run_required = bool(
        kind == "weekly_cycle"
        and (state in {"running", "completed", "failed"} or retry_count or pending_count)
    )
    blockers.extend(_valid_weekly_run(snapshot.get("run"), job, required=run_required))
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


def _looks_like_backend_writer(cmdline: bytes) -> bool:
    text = cmdline.decode("utf-8", errors="replace").lower()
    if any(marker in text for marker in (
        "start_all.sh", "event_worker.py", "event-discovery-worker",
        "social.main", "risk_backend", "matchmaker_agent", "guardrail_server",
    )):
        return True
    return "uvicorn" in text and any(marker in text for marker in (
        "social", "matchmaker", "risk", "guardrail",
    ))


def count_release_processes(
    release_paths: list[Path], *, proc_root: Path = Path("/proc"), current_pid: int | None = None,
) -> int:
    """Count backend processes by release path or known launcher/service command."""
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
        references_path = any(cwd == root or root in cwd.parents for root in roots)
        references_path = references_path or any(raw in cmdline for raw in raw_paths)
        if references_path or _looks_like_backend_writer(cmdline):
            active += 1
    return active


def _systemd_unit_is_inactive_and_disabled(scope: str) -> None:
    command = ["systemctl"]
    if scope == "user":
        command.append("--user")
    command.extend((
        "show", SYSTEMD_UNIT, "--property=LoadState", "--property=ActiveState",
        "--property=UnitFileState",
    ))
    try:
        result = subprocess.run(
            command, check=False, capture_output=True, text=True, timeout=3,
        )
    except Exception as exc:
        raise GuardCheckError("systemd launcher state could not be verified") from exc
    if result.returncode != 0:
        raise GuardCheckError("systemd launcher state could not be verified")
    properties = {}
    for line in result.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep:
            properties[key.strip()] = value.strip().lower()
    load_state = properties.get("LoadState")
    active_state = properties.get("ActiveState")
    unit_file_state = properties.get("UnitFileState")
    if load_state == "not-found":
        if active_state not in {None, "inactive"}:
            raise GuardCheckError("systemd launcher is active or in an unknown state")
        return
    if load_state != "loaded" or active_state != "inactive":
        raise GuardCheckError("systemd launcher is active or in an unknown state")
    if unit_file_state not in {"disabled", "masked", "masked-runtime"}:
        raise GuardCheckError("systemd launcher boot entry is enabled or unknown")


def _autostart_exec_is_backend(value: str) -> bool:
    text = value.lower()
    if any(marker in text for marker in ("start_all.sh", "ayue-backend", "event_worker.py")):
        return True
    return "uvicorn" in text and any(marker in text for marker in (
        "social", "matchmaker", "risk", "guardrail",
    ))


def _active_backend_autostart_exists(directories: tuple[Path, ...] = AUTOSTART_DIRS) -> bool:
    for directory in directories:
        try:
            directory.stat()
            if not directory.is_dir():
                raise GuardCheckError("GNOME autostart location is not a directory")
            entries = [entry for entry in directory.iterdir() if entry.name.endswith(".desktop")]
        except FileNotFoundError:
            continue
        except GuardCheckError:
            raise
        except (PermissionError, OSError) as exc:
            raise GuardCheckError("GNOME autostart entries could not be inspected") from exc
        for entry in entries:
            try:
                lines = entry.read_text(encoding="utf-8", errors="replace").splitlines()
            except (PermissionError, OSError) as exc:
                raise GuardCheckError("GNOME autostart entries could not be inspected") from exc
            values = {}
            for line in lines:
                key, sep, value = line.partition("=")
                if sep:
                    values[key.strip().lower()] = value.strip().lower()
            if not _autostart_exec_is_backend(values.get("exec", "")):
                continue
            if values.get("hidden") == "true" or values.get("x-gnome-autostart-enabled") == "false":
                continue
            return True
    return False


def verify_automatic_launchers_disabled(
    *, autostart_dirs: tuple[Path, ...] = AUTOSTART_DIRS,
) -> None:
    """Require both GNOME entries and user/system systemd launchers disabled."""
    if _active_backend_autostart_exists(autostart_dirs):
        raise GuardCheckError("an active backend GNOME autostart entry remains")
    _systemd_unit_is_inactive_and_disabled("user")
    _systemd_unit_is_inactive_and_disabled("system")


def _load_mongo_settings(env_file: Path | None) -> tuple[str, str]:
    if env_file is not None:
        if not env_file.is_file():
            raise GuardCheckError("Mongo configuration file is unavailable")
        try:
            values = dotenv_values(env_file, interpolate=False)
        except Exception as exc:
            raise GuardCheckError("Mongo configuration file could not be read") from exc
    else:
        values = os.environ
    uri = values.get("MONGO_URI")
    raw_db_name = values.get("MONGO_DB_NAME")
    db_name = raw_db_name.strip() if isinstance(raw_db_name, str) else ""
    if not uri or not db_name:
        raise GuardCheckError("Mongo configuration is incomplete")
    if db_name != EXPECTED_PRODUCTION_DB_NAME:
        raise GuardCheckError("Mongo database name does not match the pinned Production target")
    return str(uri), db_name


def _git_head_and_clean(root: Path) -> tuple[str, bool]:
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all"],
            check=True, capture_output=True, text=True, timeout=3,
        ).stdout
    except Exception as exc:
        raise GuardCheckError("rollback candidate commit could not be verified") from exc
    return head, not bool(status.strip())


def _validate_current_release_dir(path: Path) -> Path:
    """Require the active-release symlink, preventing a typo from hiding writers."""
    if path.name != "active-release" or not path.is_symlink():
        raise GuardCheckError("current release must be the active-release symlink")
    try:
        resolved = path.resolve(strict=True)
    except Exception as exc:
        raise GuardCheckError("active-release target could not be verified") from exc
    if not resolved.is_dir():
        raise GuardCheckError("active-release target is unavailable")
    return resolved


def _rollback_code_matches(root: Path, expected_code_sha: str) -> bool:
    """Allow the pinned code commit plus reviewed documentation descendants."""
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
    allowed_docs = {
        "docs/production-cutover-runbook.md",
        "docs/rollback-runtime-manifest.json",
        "docs/rollback-runtime-artifact.sha256",
    }
    return set(changed) <= allowed_docs


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
        print("BLOCKED: pass --workers-stopped only after stopping and disabling all launch sources", file=sys.stderr)
        return BLOCKED

    candidate_root = Path(__file__).resolve().parents[1]
    current_root = args.current_release_dir
    if not current_root.is_dir() or not candidate_root.is_dir():
        print("BLOCKED: release directory is missing or inaccessible", file=sys.stderr)
        return BLOCKED
    try:
        _validate_current_release_dir(current_root)
        _actual_sha, clean = _git_head_and_clean(candidate_root)
        if not clean or not _rollback_code_matches(candidate_root, args.expected_target_sha.lower()):
            print("BLOCKED: rollback candidate code SHA does not match or tracked files are dirty", file=sys.stderr)
            return BLOCKED

        verify_automatic_launchers_disabled()
        process_count_before = count_release_processes(
            [current_root, candidate_root], proc_root=args.proc_root,
        )
        if process_count_before:
            print(f"BLOCKED: {process_count_before} backend worker/launcher process(es) remain", file=sys.stderr)
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
            if getattr(db, "name", None) != db_name:
                raise GuardCheckError("Mongo selected database could not be verified")
            snapshot = read_retry_snapshot(db)
        finally:
            client.close()

        process_count_after = count_release_processes(
            [current_root, candidate_root], proc_root=args.proc_root,
        )
        verify_automatic_launchers_disabled()
        blockers = evaluate_retry_snapshot(snapshot, compatibility_verified=True)
        if process_count_after:
            blockers.append("a backend worker/launcher appeared during the check")
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
            "and no monitored writer/launcher or active lease was observed.\n"
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
