"""Persistent singleton queue for the low-priority Event discovery worker."""

from __future__ import annotations

import time
import uuid
from datetime import datetime
from typing import Any

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import db
from services.event_discovery_service import (
    DEFAULT_REGION, DEFAULT_WINDOW_DAYS, SUPPORTED_CATEGORIES, TAIPEI,
)


_jobs = db["event_discovery_jobs"]
_JOB_ID = "event-discovery-singleton"
_LIVE_STATES = {"queued", "running"}


def open_event_discovery_job_change_stream(
    *, max_await_time_ms: int = 60_000,
):
    """Watch only transitions that make the singleton job claimable.

    The caller owns the returned stream and must close it. Existing queued or
    expired-lease jobs are still recovered by ``claim_event_discovery_job``;
    this stream is only the low-latency wake-up path.
    """
    bounded_await_ms = max(1_000, min(int(max_await_time_ms or 60_000), 300_000))
    return _jobs.watch(
        [{"$match": {
            "operationType": {"$in": ["insert", "update", "replace"]},
            "documentKey._id": _JOB_ID,
            "fullDocument.state": "queued",
        }}],
        full_document="updateLookup",
        max_await_time_ms=bounded_await_ms,
    )


def ensure_event_discovery_job_indexes() -> None:
    try:
        _jobs.create_index("state", name="event_discovery_state")
        _jobs.create_index("lease_expires_at", name="event_discovery_lease")
    except Exception:
        pass


def _public_snapshot(job: dict[str, Any] | None) -> dict[str, Any]:
    if not job:
        return {
            "state": "idle", "run_number": 0,
            "started_at": 0.0, "finished_at": 0.0,
        }
    allowed = {
        "state", "run_number", "started_at", "finished_at", "outcome",
        "searched_results", "ingested_count", "active_category_counts",
        "category_counts", "validation_counts", "reconciliation", "supplemental",
        "coverage", "graph_integrity", "error_codes", "error_code", "source",
        "job_kind", "stage", "reset", "relevance_readiness", "invitation_scan",
    }
    return {key: job[key] for key in allowed if key in job}


def event_discovery_job_snapshot() -> dict[str, Any]:
    try:
        return _public_snapshot(_jobs.find_one({"_id": _JOB_ID}))
    except Exception:
        return {"state": "unavailable", "run_number": 0,
                "started_at": 0.0, "finished_at": 0.0}


def enqueue_event_discovery_job(
    *, region: str = DEFAULT_REGION, window_days: int = DEFAULT_WINDOW_DAYS,
    categories: list[str] | tuple[str, ...] = SUPPORTED_CATEGORIES,
    source: str = "demo", schedule_key: str = "", job_kind: str = "discovery",
) -> dict[str, Any]:
    now = time.time()
    clean_categories = list(dict.fromkeys(
        str(value).strip() for value in categories
        if str(value).strip() in SUPPORTED_CATEGORIES
    )) or list(SUPPORTED_CATEGORIES)
    query: dict[str, Any] = {"_id": _JOB_ID, "state": {"$nin": list(_LIVE_STATES)}}
    if schedule_key:
        query["last_schedule_key"] = {"$ne": schedule_key}
    update = {
        "$set": {
            "state": "queued", "job_token": uuid.uuid4().hex,
            "source": str(source or "demo")[:30],
            "region": " ".join(str(region or DEFAULT_REGION).split())[:40],
            "window_days": max(1, min(int(window_days or DEFAULT_WINDOW_DAYS), 60)),
            "categories": clean_categories, "queued_at": now,
            "job_kind": "weekly_cycle" if job_kind == "weekly_cycle" else "discovery",
            "stage": "queued",
            "started_at": 0.0, "finished_at": 0.0,
            "lease_owner": "", "lease_expires_at": 0.0,
            "outcome": "", "error_code": "", "error_codes": [],
        },
        "$inc": {"run_number": 1},
        "$unset": {
            "searched_results": "", "ingested_count": "",
            "active_category_counts": "", "category_counts": "",
            "validation_counts": "", "reconciliation": "", "supplemental": "",
            "coverage": "",
            "graph_integrity": "", "reset": "", "relevance_readiness": "",
            "invitation_scan": "",
        },
    }
    if schedule_key:
        update["$set"]["last_schedule_key"] = schedule_key
    try:
        job = _jobs.find_one_and_update(
            query, update, upsert=True, return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        job = _jobs.find_one({"_id": _JOB_ID})
        return {"status": "already_running", **_public_snapshot(job)}
    if not job:
        job = _jobs.find_one({"_id": _JOB_ID})
        return {"status": "already_running", **_public_snapshot(job)}
    return {"status": "queued", **_public_snapshot(job)}


def enqueue_weekly_event_discovery_if_due(now: datetime | None = None) -> dict[str, Any] | None:
    now = now or datetime.now(TAIPEI)
    import os
    weekday = max(0, min(int(os.getenv("EVENT_DISCOVERY_WEEKDAY", "0") or 0), 6))
    hour = max(0, min(int(os.getenv("EVENT_DISCOVERY_HOUR", "8") or 8), 23))
    if now.weekday() != weekday or now.hour != hour:
        return None
    schedule_key = now.strftime("%G-W%V")
    return enqueue_event_discovery_job(
        region=os.getenv("EVENT_DISCOVERY_REGION", DEFAULT_REGION),
        window_days=int(os.getenv(
            "EVENT_DISCOVERY_WINDOW_DAYS", str(DEFAULT_WINDOW_DAYS),
        ) or DEFAULT_WINDOW_DAYS),
        categories=[value.strip() for value in os.getenv(
            "EVENT_DISCOVERY_CATEGORIES", ",".join(SUPPORTED_CATEGORIES),
        ).split(",") if value.strip()],
        source="scheduled", schedule_key=schedule_key, job_kind="weekly_cycle",
    )


def claim_event_discovery_job(worker_id: str, lease_seconds: int = 1200) -> dict[str, Any] | None:
    now = time.time()
    return _jobs.find_one_and_update(
        {
            "_id": _JOB_ID,
            "$or": [
                {"state": "queued"},
                {"state": "running", "lease_expires_at": {"$lte": now}},
            ],
        },
        {"$set": {
            "state": "running", "started_at": now, "finished_at": 0.0,
            "stage": "starting",
            "lease_owner": worker_id,
            "lease_expires_at": now + max(120, min(int(lease_seconds or 1200), 3600)),
        }},
        return_document=ReturnDocument.AFTER,
    )


def renew_event_discovery_job_lease(
    job: dict[str, Any], worker_id: str, lease_seconds: int = 1200,
) -> bool:
    now = time.time()
    result = _jobs.update_one(
        {
            "_id": _JOB_ID,
            "job_token": job.get("job_token"),
            "state": "running",
            "lease_owner": worker_id,
        },
        {"$set": {
            "lease_expires_at": now + max(
                120, min(int(lease_seconds or 1200), 3600),
            ),
        }},
    )
    return bool(result.modified_count)


def update_event_discovery_job_stage(
    job: dict[str, Any], worker_id: str, stage: str,
) -> None:
    _jobs.update_one(
        {
            "_id": _JOB_ID,
            "job_token": job.get("job_token"),
            "state": "running",
            "lease_owner": worker_id,
        },
        {"$set": {"stage": str(stage or "running")[:40]}},
    )


def finish_event_discovery_job(job: dict[str, Any], result: dict[str, Any]) -> None:
    summary = {
        "state": "completed", "finished_at": time.time(),
        "lease_owner": "", "lease_expires_at": 0.0,
        "outcome": str(result.get("status") or "unknown")[:30],
        "searched_results": int(result.get("searched_results", 0) or 0),
        "ingested_count": int(result.get("ingested_count", 0) or 0),
        "category_counts": dict(result.get("category_counts") or {}),
        "active_category_counts": dict(result.get("active_category_counts") or {}),
        "validation_counts": dict(result.get("validation_counts") or {}),
        "reconciliation": dict(result.get("reconciliation") or {}),
        "supplemental": dict(result.get("supplemental") or {}),
        "coverage": dict(result.get("coverage") or {}),
        "graph_integrity": dict(result.get("graph_integrity") or {}),
        "error_codes": [str(value)[:80] for value in list(result.get("error_codes") or [])[:10]],
        "job_kind": str(result.get("job_kind") or job.get("job_kind") or "discovery")[:30],
        "stage": "completed",
        "reset": dict(result.get("reset") or {}),
        "relevance_readiness": dict(result.get("relevance_readiness") or {}),
        "invitation_scan": dict(result.get("invitation_scan") or {}),
    }
    _jobs.update_one(
        {"_id": _JOB_ID, "job_token": job.get("job_token"),
         "lease_owner": job.get("lease_owner")},
        {"$set": summary},
    )


def fail_event_discovery_job(job: dict[str, Any], exc: Exception) -> None:
    _jobs.update_one(
        {"_id": _JOB_ID, "job_token": job.get("job_token"),
         "lease_owner": job.get("lease_owner")},
        {"$set": {
            "state": "failed", "finished_at": time.time(),
            "lease_owner": "", "lease_expires_at": 0.0,
            "outcome": "failed", "error_code": type(exc).__name__,
            "stage": "failed",
        }},
    )
