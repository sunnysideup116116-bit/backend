"""Durable, owner-scoped background jobs for Public Ayue match searches."""

from __future__ import annotations

import threading
import time
import uuid
import logging
from typing import Any, Callable

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import db, profiles_coll
from services.mediator_event_service import queue_mediator_event
from services.proposal_namespace import RELATIONSHIP_MATCH_NAMESPACE


MATCH_SEARCH_JOBS = db["match_search_jobs"]
JOB_ACTIVE_STATUSES = frozenset({"queued", "running"})
JOB_TERMINAL_STATUSES = frozenset({"completed", "no_candidates", "failed", "cancelled", "stale"})
JOB_STEPS = {
    "loading_profile": 15,
    "vector_search": 40,
    "candidate_qualification": 55,
    "matchmaker_request": 65,
    "matchmaker_response": 75,
    "proposal_write": 85,
}
LEASE_SECONDS = 180
POLL_SECONDS = 0.5

MatchPipeline = Callable[..., dict[str, Any]]
_pipeline: MatchPipeline | None = None
_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
LOGGER = logging.getLogger(__name__)


class MatchSearchPipelineError(RuntimeError):
    """Safe, stage-bound failure raised by the candidate pipeline."""

    def __init__(self, code: str, stage: str) -> None:
        self.code = str(code or "unexpected_pipeline_error")[:80]
        self.stage = str(stage or "unknown")[:40]
        super().__init__(self.code)


_FAILURE_MESSAGES = {
    "matchmaker_timeout": "這次媒人評估逾時，搜尋已停止。可以稍後重新搜尋。",
    "matchmaker_graph_timeout": "讀取配對依據逾時，這次搜尋沒有完成。可以稍後再試。",
    "matchmaker_graph_unavailable": "目前無法讀取配對依據，這次搜尋沒有完成。",
    "matchmaker_empty_response": "媒人服務沒有回傳有效結果，這次搜尋沒有完成。",
    "matchmaker_output_truncated": "媒人評估結果不完整，這次搜尋沒有完成。",
    "matchmaker_provider_error": "配對服務目前發生錯誤，請稍後再試。",
    "proposal_write_failed": "配對結果已找到，但儲存結果時失敗，請稍後再試。",
    "pipeline_unavailable": "配對服務目前還沒準備好，請稍後再試。",
    "vector_search_unavailable": "我目前無法讀取候選資料，請稍後再試。",
    "matchmaker_unavailable": "配對服務暫時連不上，請稍後再試。",
    "matchmaker_http_error": "配對服務回應失敗，請稍後再試。",
    "matchmaker_invalid_response": "配對服務回傳的結果不完整，請稍後再試。",
    "unexpected_pipeline_error": "配對流程中途發生問題，請稍後再試。",
}


def register_match_search_pipeline(pipeline: MatchPipeline) -> None:
    """Register the existing candidate pipeline; the job service owns scheduling."""
    global _pipeline
    _pipeline = pipeline


def ensure_match_search_job_indexes() -> None:
    try:
        MATCH_SEARCH_JOBS.create_index(
            [("active_user_id", 1)], unique=True, sparse=True,
            name="one_active_match_search_per_user",
        )
        MATCH_SEARCH_JOBS.create_index([("status", 1), ("lease_until", 1), ("updated_at", 1)])
        MATCH_SEARCH_JOBS.create_index([("user_id", 1), ("created_at", -1)])
        MATCH_SEARCH_JOBS.create_index([("user_id", 1), ("idempotency_key", 1)], unique=True)
    except Exception as exc:
        print(f"[match-search-job] index setup skipped: {type(exc).__name__}")


def _safe_revision(profile: dict[str, Any]) -> int:
    try:
        return max(0, int(profile.get("current_context_revision", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _profile_search_projection(status: str, source: str, *, step: str = "", progress_percent: int = 0, **extra: Any) -> dict[str, Any]:
    return {
        "status": status,
        "source": str(source or "automatic")[:40],
        "step": step if step in JOB_STEPS else "",
        "progress_percent": max(0, min(100, int(progress_percent or 0))),
        "updated_at": time.time(),
        **extra,
    }


def _live_match(user_id: str) -> dict[str, Any] | None:
    # Avoid an import cycle with match_state_service at module import time.
    from services.match_state_service import reconcile_live_match
    return reconcile_live_match(user_id)


def _has_live_match(user_id: str) -> bool:
    from services.match_state_service import load_match_state
    state = load_match_state(user_id)
    return bool(state["active_proposal"] or state["ambiguous"])


def enqueue_match_search(
    user_id: str,
    *,
    source: str,
    idempotency_key: str,
    force_new: bool = False,
    origin_room_id: str = "",
) -> dict[str, Any]:
    """Create one queued job. This call never runs candidate ranking inline."""
    if _has_live_match(user_id):
        return {"status": "already_active"}
    now = time.time()
    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"_id": 0, "current_context_revision": 1},
    ) or {}
    job = {
        "job_id": uuid.uuid4().hex,
        "user_id": user_id,
        "active_user_id": user_id,
        "status": "queued",
        "step": "loading_profile",
        "progress_percent": 0,
        "context_revision": _safe_revision(profile),
        "source": str(source or "automatic")[:40],
        "origin_room_id": str(origin_room_id or "")[:240],
        "idempotency_key": str(idempotency_key)[:120],
        "lease_id": "",
        "lease_until": 0.0,
        "attempt": 0,
        "created_at": now,
        "updated_at": now,
    }
    try:
        MATCH_SEARCH_JOBS.insert_one(job)
    except DuplicateKeyError:
        replay = MATCH_SEARCH_JOBS.find_one(
            {"user_id": user_id, "idempotency_key": job["idempotency_key"]},
            {"_id": 0, "status": 1},
        ) or {}
        replay_status = str(replay.get("status") or "")
        if replay_status == "queued":
            return {"status": "already_queued"}
        if replay_status == "running":
            return {"status": "already_searching"}
        if replay_status in JOB_TERMINAL_STATUSES:
            return {"status": replay_status}
        existing = MATCH_SEARCH_JOBS.find_one(
            {"user_id": user_id, "active_user_id": user_id},
            {"_id": 0, "status": 1, "idempotency_key": 1},
        ) or {}
        return {"status": "already_searching" if existing else "failed"}
    profiles_coll.update_one(
        {"user_id": user_id},
        {"$set": {
            "matchmaking_in_progress": True,
            "active_match_search_job_id": job["job_id"],
            "match_search": _profile_search_projection("queued", job["source"], step="loading_profile", progress_percent=0),
        }},
        upsert=True,
    )
    return {"status": "queued"}


def active_match_search_job(user_id: str) -> dict[str, Any] | None:
    """Return server-private authority for the user's one active search."""
    return MATCH_SEARCH_JOBS.find_one(
        {
            "user_id": user_id,
            "active_user_id": user_id,
            "status": {"$in": list(JOB_ACTIVE_STATUSES)},
        },
        {"_id": 0, "job_id": 1, "status": 1, "origin_room_id": 1},
        sort=[("created_at", -1)],
    )


def cancel_match_search(
    user_id: str,
    *,
    source: str = "manual",
    expected_job_id: str | None = None,
) -> dict[str, Any]:
    """Cancel a queued/running job; a worker checks this before proposal writes."""
    now = time.time()
    query: dict[str, Any] = {
        "user_id": user_id,
        "active_user_id": user_id,
        "status": {"$in": list(JOB_ACTIVE_STATUSES)},
    }
    if expected_job_id:
        query["job_id"] = expected_job_id
    job = MATCH_SEARCH_JOBS.find_one_and_update(
        query,
        {"$set": {"status": "cancelled", "updated_at": now, "completed_at": now}, "$unset": {"active_user_id": ""}},
        return_document=ReturnDocument.BEFORE,
    )
    if not job:
        return {"status": "already_active" if _has_live_match(user_id) else "idle"}
    profiles_coll.update_one(
        {"user_id": user_id, "active_match_search_job_id": job.get("job_id")},
        {"$set": {
            "matchmaking_in_progress": False,
            "match_search": _profile_search_projection("cancelled", source, progress_percent=0, completed_at=now),
        }, "$unset": {"active_match_search_job_id": ""}},
    )
    return {"status": "cancelled"}


def _claim_next_job(now: float) -> dict[str, Any] | None:
    lease_id = uuid.uuid4().hex
    return MATCH_SEARCH_JOBS.find_one_and_update(
        {
            "active_user_id": {"$exists": True},
            "$or": [
                {"status": "queued"},
                {"status": "running", "lease_until": {"$lte": now}},
            ],
        },
        {"$set": {
            "status": "running", "lease_id": lease_id, "lease_until": now + LEASE_SECONDS,
            "updated_at": now,
        }, "$inc": {"attempt": 1}},
        sort=[("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def _job_has_lease(job: dict[str, Any]) -> bool:
    current = MATCH_SEARCH_JOBS.find_one(
        {
            "_id": job.get("_id"), "status": "running",
            "active_user_id": job.get("user_id"), "lease_id": job.get("lease_id"),
        },
        {"_id": 1},
    )
    return bool(current)


def _job_context_matches(job: dict[str, Any]) -> bool:
    profile = profiles_coll.find_one(
        {"user_id": job.get("user_id")}, {"_id": 0, "current_context_revision": 1},
    ) or {}
    return _safe_revision(profile) == int(job.get("context_revision", 0) or 0)


def _job_has_ownership(job: dict[str, Any]) -> bool:
    return _job_has_lease(job) and _job_context_matches(job)


def _job_is_current(job: dict[str, Any]) -> bool:
    if not _job_has_lease(job):
        return False
    live_match = _live_match(str(job.get("user_id") or ""))
    if live_match:
        # A proposal already committed by this same durable job is recoverable
        # after a lease handoff; any other live proposal makes the job stale.
        return str(live_match.get("search_job_id") or "") == str(job.get("job_id") or "")
    return _job_context_matches(job)


def _report_progress(job: dict[str, Any], step: str) -> bool:
    if step not in JOB_STEPS:
        return False
    now = time.time()
    result = MATCH_SEARCH_JOBS.update_one(
        {
            "_id": job.get("_id"), "status": "running", "active_user_id": job.get("user_id"),
            "lease_id": job.get("lease_id"),
        },
        {"$set": {"step": step, "progress_percent": JOB_STEPS[step], "updated_at": now, "lease_until": now + LEASE_SECONDS}},
    )
    if not getattr(result, "modified_count", 0):
        return False
    profiles_coll.update_one(
        {"user_id": job.get("user_id"), "active_match_search_job_id": job.get("job_id")},
        {"$set": {"match_search": _profile_search_projection("running", str(job.get("source") or "automatic"), step=step, progress_percent=JOB_STEPS[step])}},
    )
    return _job_is_current(job)


def _finish_job(
    job: dict[str, Any], status: str, *, error_code: str = "", failure_stage: str = "",
) -> bool:
    if status not in JOB_TERMINAL_STATUSES:
        status = "failed"
    now = time.time()
    result = MATCH_SEARCH_JOBS.update_one(
        {"_id": job.get("_id"), "status": "running", "active_user_id": job.get("user_id"), "lease_id": job.get("lease_id")},
        {"$set": {
            "status": status,
            "updated_at": now,
            "completed_at": now,
            "error_code": error_code[:80],
            "failure_stage": failure_stage[:40],
        }, "$unset": {"active_user_id": "", "lease_id": "", "lease_until": ""}},
    )
    if not getattr(result, "modified_count", 0):
        return False
    profiles_coll.update_one(
        {"user_id": job.get("user_id"), "active_match_search_job_id": job.get("job_id")},
        {"$set": {
            "matchmaking_in_progress": False,
            "match_search": _profile_search_projection(
                status,
                str(job.get("source") or "automatic"),
                progress_percent=100 if status == "completed" else 0,
                completed_at=now,
                reason_code=error_code[:80] if status == "failed" else "",
            ),
        }, "$unset": {"active_match_search_job_id": ""}},
    )
    return True


def run_one_match_search_job() -> bool:
    """Claim and execute at most one job. Safe to invoke from many workers."""
    job = _claim_next_job(time.time())
    if not job:
        return False
    if _pipeline is None:
        if _finish_job(job, "failed", error_code="pipeline_unavailable", failure_stage="loading_profile"):
            queue_mediator_event(
                str(job.get("user_id") or ""), _FAILURE_MESSAGES["pipeline_unavailable"],
                "match_search_failed", event_key=f"match-search-job:{job.get('job_id')}:failed",
                origin_room_id=str(job.get("origin_room_id") or ""),
            )
        return True
    if not _report_progress(job, "loading_profile"):
        _finish_job(job, "stale", error_code="ownership_or_context_changed")
        return True
    try:
        result = _pipeline(
            str(job.get("user_id") or ""), str(job.get("source") or "automatic"),
            report_progress=lambda step: _report_progress(job, step),
            can_commit=lambda: _job_is_current(job),
            search_job_id=str(job.get("job_id") or ""),
        )
    except MatchSearchPipelineError as exc:
        if _finish_job(job, "failed", error_code=exc.code, failure_stage=exc.stage):
            queue_mediator_event(
                str(job.get("user_id") or ""),
                _FAILURE_MESSAGES.get(exc.code, _FAILURE_MESSAGES["unexpected_pipeline_error"]),
                "match_search_failed", event_key=f"match-search-job:{job.get('job_id')}:failed",
                origin_room_id=str(job.get("origin_room_id") or ""),
            )
        return True
    except Exception as exc:
        LOGGER.exception(
            "match search pipeline failed stage=unknown error=%s job=%s",
            type(exc).__name__, str(job.get("job_id") or "")[:80],
        )
        if _finish_job(job, "failed", error_code="unexpected_pipeline_error", failure_stage="unknown"):
            queue_mediator_event(
                str(job.get("user_id") or ""), _FAILURE_MESSAGES["unexpected_pipeline_error"],
                "match_search_failed", event_key=f"match-search-job:{job.get('job_id')}:failed",
                origin_room_id=str(job.get("origin_room_id") or ""),
            )
        return True
    if str((result or {}).get("status") or "") == "stale":
        _finish_job(job, "stale", error_code="ownership_or_context_changed")
        return True
    matches = list((result or {}).get("matches") or [])[:1]
    if not matches:
        if not _job_has_ownership(job):
            _finish_job(job, "stale", error_code="ownership_or_context_changed")
            return True
        if _finish_job(job, "no_candidates"):
            queue_mediator_event(
                str(job.get("user_id") or ""), "這輪我暫時沒看到合適的新對象，等資料多一點我再幫你看。",
                "match_search_empty", event_key=f"match-search-job:{job.get('job_id')}:empty",
                origin_room_id=str(job.get("origin_room_id") or ""),
            )
        return True
    # The pipeline checked ownership immediately before proposal insertion. Once
    # the sole draft exists, it is the canonical result of this job.
    if not _job_has_lease(job):
        # A new lease owner will recover the proposal by search_job_id and
        # publish the single terminal event.
        return True
    if _finish_job(job, "completed"):
        first = matches[0]
        tone_doc = profiles_coll.find_one({"user_id": job.get("user_id")}, {"_id": 0, "mediator_tone": 1}) or {}
        proposal_message = {
            "friend": "我翻到一位可以介紹給你的人，先看這張阿月牽線提案。",
            "gentle": "我幫你留意到一位可能合拍的人，先看看這個提案。",
            "enthusiastic": "我找到一位有機會聊起來的人，快看看這張牽線提案！",
        }.get(str(tone_doc.get("mediator_tone") or "friend"), "我翻到一位可以介紹給你的人，先看這張阿月牽線提案。")
        queue_mediator_event(
            str(job.get("user_id") or ""), proposal_message, "match_proposal",
            event_key=f"match-search-job:{job.get('job_id')}:proposal",
            match_id=first.get("match_id"), proposal_role="initiator",
            proposal_namespace=RELATIONSHIP_MATCH_NAMESPACE,
            origin_room_id=str(job.get("origin_room_id") or ""),
        )
    return True


def match_search_snapshot(user_id: str) -> dict[str, Any]:
    """Read-only job truth, with a bounded legacy-lock fallback when no job exists."""
    job = MATCH_SEARCH_JOBS.find_one(
        {"user_id": user_id, "status": {"$in": sorted(JOB_ACTIVE_STATUSES)}},
        sort=[("created_at", -1)],
    ) or MATCH_SEARCH_JOBS.find_one(
        {"user_id": user_id},
        {"_id": 0, "status": 1, "step": 1, "progress_percent": 1, "updated_at": 1, "completed_at": 1, "error_code": 1},
        sort=[("created_at", -1)],
    ) or {}
    if not job:
        profile = profiles_coll.find_one(
            {"user_id": user_id},
            {"_id": 0, "match_search": 1, "matchmaking_in_progress": 1, "matchmaking_started_at": 1},
        ) or {}
        legacy = profile.get("match_search") or {}
        started = profile.get("matchmaking_started_at") or legacy.get("started_at") or legacy.get("updated_at") or 0
        active = bool(profile.get("matchmaking_in_progress") or legacy.get("status") in {"searching", "queued", "running"})
        if active and float(started) > time.time() - 300:
            job = {**legacy, "status": "searching"}
        elif not active and legacy.get("status") in JOB_TERMINAL_STATUSES:
            job = legacy
    status = str(job.get("status") or "idle")
    if status not in {"idle", "searching", *JOB_ACTIVE_STATUSES, *JOB_TERMINAL_STATUSES}:
        status = "failed"
    step = str(job.get("step") or "") if status in JOB_ACTIVE_STATUSES else ""
    if step not in JOB_STEPS:
        step = ""
    try:
        percent = max(0, min(100, int(job.get("progress_percent", 0) or 0)))
    except (TypeError, ValueError):
        percent = 0
    # Keep old job checkpoints for diagnostics, not public in-progress UI.
    if status not in JOB_ACTIVE_STATUSES:
        percent = 100 if status == "completed" else 0
    reason_code = str(job.get("error_code") or "") if status == "failed" else ""
    if reason_code not in _FAILURE_MESSAGES:
        reason_code = "unexpected_pipeline_error" if status == "failed" else ""
    return {
        "status": status, "step": step, "progress_percent": percent,
        "cancellable": status in JOB_ACTIVE_STATUSES,
        "estimated_seconds_min": 60 if status in JOB_ACTIVE_STATUSES else None,
        "estimated_seconds_max": 180 if status in JOB_ACTIVE_STATUSES else None,
        "reason_code": reason_code,
        "updated_at": job.get("updated_at"), "completed_at": job.get("completed_at"),
    }


def public_match_search_status(user_id: str) -> dict[str, Any]:
    """Public status projection: no job ID, lease or context revision."""
    return {key: value for key, value in match_search_snapshot(user_id).items()
            if key not in {"updated_at", "completed_at"}}


def cleanup_legacy_search_locks() -> None:
    """Worker-only housekeeping; status/Agent reads never mutate profile locks."""
    cutoff = time.time() - 300
    for profile in profiles_coll.find({
        "matchmaking_in_progress": True, "matchmaking_started_at": {"$lt": cutoff},
    }, {"user_id": 1, "matchmaking_started_at": 1}):
        if active_match_search_job(str(profile.get("user_id") or "")):
            continue
        profiles_coll.update_one({
            "user_id": profile["user_id"], "matchmaking_in_progress": True,
            "matchmaking_started_at": profile["matchmaking_started_at"],
        }, {"$set": {"matchmaking_in_progress": False}, "$unset": {"matchmaking_started_at": ""}})


def _worker_loop() -> None:
    next_cleanup = 0.0
    while not _stop_event.wait(POLL_SECONDS):
        try:
            if time.monotonic() >= next_cleanup:
                cleanup_legacy_search_locks()
                next_cleanup = time.monotonic() + 60
            while run_one_match_search_job():
                pass
        except Exception as exc:
            print(f"[match-search-job] worker loop failed: {type(exc).__name__}")


def start_match_search_worker() -> None:
    global _worker_thread
    ensure_match_search_job_indexes()
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(target=_worker_loop, name="match-search-worker", daemon=True)
    _worker_thread.start()


def stop_match_search_worker() -> None:
    _stop_event.set()
