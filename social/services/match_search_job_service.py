"""Durable, owner-scoped background jobs for Public Ayue match searches."""

from __future__ import annotations

from contextlib import nullcontext
from agent_quota.service import SCOPE, service as quota_service, task_scope

import threading
import time
import uuid
import logging
import math
import re
from typing import Any, Callable

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from database import db, matches_coll, profiles_coll
from services.mediator_event_service import queue_mediator_event
from services.proposal_namespace import RELATIONSHIP_MATCH_NAMESPACE
from services.match_search_context import safe_search_context, validate_persisted_search_context
from matchmaker_agent.concept_identity import PreferenceTextError


MATCH_SEARCH_JOBS = db["match_search_jobs"]
JOB_ACTIVE_STATUSES = frozenset({"queued", "running"})
PREVIEW_ON_MATCH = "preview_on_match"
INVITE_ON_MATCH = "invite_on_match"
JOB_TERMINAL_STATUSES = frozenset({
    "completed", "no_candidates", "insufficient_common_ground",
    "failed", "cancelled", "stale", "quota_exceeded",
})
JOB_STEPS = {
    "loading_profile": 15,
    "vector_search": 40,
    "preference_graph_search": 40,
    "preference_semantic_search": 47,
    "candidate_qualification": 55,
    "matchmaker_request": 65,
    "matchmaker_response": 75,
    "proposal_write": 85,
}
LEASE_SECONDS = 180
POLL_SECONDS = 0.5
MAX_CONTEXT_CHANGE_REQUEUES = 1

MatchPipeline = Callable[..., dict[str, Any]]
_pipeline: MatchPipeline | None = None
_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_owned_lease_lock = threading.Lock()
_owned_lease_ids: set[str] = set()
LOGGER = logging.getLogger(__name__)


class MatchSearchPipelineError(RuntimeError):
    """Safe, stage-bound failure raised by the candidate pipeline."""

    def __init__(self, code: str, stage: str) -> None:
        self.code = str(code or "unexpected_pipeline_error")[:80]
        self.stage = str(stage or "unknown")[:40]
        super().__init__(self.code)


_FAILURE_MESSAGES = {
    "semantic_policy_disabled": "相關興趣配對目前未啟用，這次搜尋沒有送出邀請。",
    "semantic_validator_unavailable": "相關興趣驗證暫時無法完成，這次沒有送出邀請。請稍後再試。",
    "ownership_or_context_changed": "你的近況或配對狀態剛更新，這次搜尋已停止。請再開始一次配對。",
    "matchmaker_timeout": "這次媒人評估逾時，搜尋已停止。可以稍後重新搜尋。",
    "matchmaker_graph_timeout": "讀取配對依據逾時，這次搜尋沒有完成。可以稍後再試。",
    "matchmaker_graph_unavailable": "目前無法讀取配對依據，這次搜尋沒有完成。",
    "matchmaker_empty_response": "媒人服務沒有回傳有效結果，這次搜尋沒有完成。",
    "matchmaker_output_truncated": "媒人評估結果不完整，這次搜尋沒有完成。",
    "matchmaker_provider_error": "配對服務目前發生錯誤，請稍後再試。",
    "proposal_write_failed": "配對結果已找到，但儲存結果時失敗，請稍後再試。",
    "legacy_live_match_index_conflict": "配對資料還沒完成更新，這次沒有送出邀請。請稍後再試。",
    "proposal_pair_already_active": "你和這位人選已經有一張進行中的邀請，這次沒有重複送出。",
    "quota_unavailable": "目前無法確認今天還能找幾位，這次搜尋沒有啟動，請稍後再試。",
    "pipeline_unavailable": "配對服務目前還沒準備好，請稍後再試。",
    "vector_search_unavailable": "我目前無法讀取候選資料，請稍後再試。",
    "preference_graph_unavailable": "我目前無法讀取偏好候選資料，請稍後再試。",
    "preference_graph_invalid_response": "偏好候選資料不完整，這次搜尋沒有完成。",
    "preference_search_reconfirmation_required": "這次偏好搜尋的完整條件無法驗證，請重新提供偏好並確認搜尋。",
    "semantic_readiness_unconfirmed": "語意偏好搜尋尚未通過向量相容性檢查，這次搜尋沒有完成。",
    "semantic_index_unavailable": "語意偏好索引目前尚未就緒，請稍後再試。",
    "semantic_graph_unavailable": "目前無法讀取語意偏好候選資料，請稍後再試。",
    "semantic_graph_invalid_response": "語意偏好候選資料不完整，這次搜尋沒有完成。",
    "semantic_query_embedding_unavailable": "目前無法理解這項偏好主題，請稍後再試。",
    "semantic_query_embedding_invalid": "偏好主題的搜尋資料不完整，請稍後再試。",
    "semantic_retrieval_timeout": "語意偏好搜尋逾時，這次沒有完成，請稍後再試。",
    "candidate_profile_unavailable": "我目前無法確認候選人的資料，請稍後再試。",
    "matchmaker_unavailable": "配對服務暫時連不上，請稍後再試。",
    "matchmaker_http_error": "配對服務回應失敗，請稍後再試。",
    "matchmaker_invalid_response": "配對服務回傳的結果不完整，請稍後再試。",
    "insufficient_common_ground": "我暫時找不到足夠的共同依據；你可以補充這次想一起做的事或主題，再決定是否重新搜尋。",
    "insufficient_semantic_ground": "這次沒有找到證據足夠、又通過安全檢查的相關偏好人選；你可以換個更明確的偏好再試。",
    "daily_active_quota_exceeded": "今天已經幫你介紹 3 位新朋友了，明天可以再找；已送出的邀請還是能繼續回覆。",
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
    from services.match_state_service import load_match_state
    return load_match_state(user_id).get("active_proposal")


def _has_live_match(user_id: str) -> bool:
    from services.match_state_service import load_match_state
    state = load_match_state(user_id)
    active_rows = state.get("all_live_proposals") or state.get("active_proposals") or []
    if active_rows:
        return any(
            row.get("status") == "draft" and row.get("from_user") == user_id
            for row in active_rows
        )
    active = state.get("active_proposal") or {}
    return bool(active.get("status") == "draft" and active.get("from_user") == user_id)


def enqueue_match_search(
    user_id: str,
    *,
    source: str,
    idempotency_key: str,
    force_new: bool = False,
    origin_room_id: str = "",
    search_context: dict[str, Any] | None = None,
    delivery_mode: str = PREVIEW_ON_MATCH,
) -> dict[str, Any]:
    """Create one queued job. This call never runs candidate ranking inline."""
    # Replay a known request before applying today's quota guard.  A retry of a
    # previously queued/completed job must return its original outcome rather
    # than looking like a new search after the user has used today's slots.
    if idempotency_key:
        try:
            prior = MATCH_SEARCH_JOBS.find_one(
                {"user_id": user_id, "idempotency_key": str(idempotency_key)[:120]},
                {"_id": 0, "status": 1},
            )
        except Exception:
            prior = None
        if isinstance(prior, dict) and prior.get("status"):
            prior_status = str(prior.get("status"))
            if prior_status == "queued":
                return {"status": "already_queued"}
            if prior_status == "running":
                return {"status": "already_searching"}
            if prior_status in JOB_TERMINAL_STATUSES:
                return {"status": prior_status}
    if str(source or "").strip().lower() not in {
        "automatic", "background", "event_opportunity", "proactive_event",
    }:
        # This is a fast read guard.  The worker reserves the slot again just
        # before writing the proposal, closing the race between queueing and
        # completion without charging searches that return no candidate.
        try:
            from services.match_quota_service import daily_quota_status
            if daily_quota_status(user_id)["active"]["remaining"] <= 0:
                return {"status": "quota_exceeded", "reason_code": "daily_active_quota_exceeded"}
        except Exception:
            # The authoritative reservation will fail closed in the worker.
            pass
    quota_scope = SCOPE.get()
    # Only authenticated foreground adapters establish this scope. Background
    # jobs cannot become billable because of a source label (nor vice versa).
    billable = quota_scope is not None
    if billable and not (quota_scope and quota_scope[0] == user_id and quota_scope[1] == 'matching'):
        quota_service.check(user_id)
    if _has_live_match(user_id):
        return {"status": "already_active"}
    now = time.time()
    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"_id": 0, "current_context_revision": 1},
    ) or {}
    bound_context = safe_search_context(search_context)
    bound_delivery_mode = (
        INVITE_ON_MATCH
        if str(delivery_mode or "").strip() == INVITE_ON_MATCH
        and str(bound_context.get("invitation_topic") or "").strip()
        else PREVIEW_ON_MATCH
    )
    job = {
        "quota_billable": billable,
        "job_id": uuid.uuid4().hex,
        "user_id": user_id,
        "active_user_id": user_id,
        "status": "queued",
        "step": "loading_profile",
        "progress_percent": 0,
        "context_revision": _safe_revision(profile),
        "source": str(source or "automatic")[:40],
        "origin_room_id": str(origin_room_id or "")[:240],
        "search_context": bound_context,
        "delivery_mode": bound_delivery_mode,
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
    job = MATCH_SEARCH_JOBS.find_one_and_update(
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
    if job:
        with _owned_lease_lock:
            _owned_lease_ids.add(lease_id)
    return job


def _forget_owned_lease(job: dict[str, Any]) -> None:
    lease_id = str(job.get("lease_id") or "")
    if not lease_id:
        return
    with _owned_lease_lock:
        _owned_lease_ids.discard(lease_id)


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
    user_id = str(job.get("user_id") or "")
    live_match = _live_match(user_id)
    if live_match and str(live_match.get("search_job_id") or "") == str(job.get("job_id") or ""):
        # A proposal already committed by this same durable job is recoverable
        # after a lease handoff.  Other waiting cards do not stale this job.
        return True
    if live_match and live_match.get("status") == "draft" and live_match.get("from_user") == user_id:
        return False
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
    _forget_owned_lease(job)
    profiles_coll.update_one(
        {"user_id": job.get("user_id"), "active_match_search_job_id": job.get("job_id")},
        {"$set": {"match_search": _profile_search_projection("running", str(job.get("source") or "automatic"), step=step, progress_percent=JOB_STEPS[step])}},
    )
    return _job_is_current(job)


def _bounded_diagnostics(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    if isinstance(value.get("related_interest_validator"), dict):
        from matchmaker_agent.related_interest_contract import bounded_counts
        result["related_interest_validator"] = bounded_counts(value["related_interest_validator"])
    for key in (
        "search_intent", "normalized_topic", "canonical_preference_key",
        "retrieval_source",
    ):
        text = str(value.get(key) or "").strip()
        if text:
            result[key] = text[:80]
    for key, allowed in (
        ("query_provenance", {"exact_canonical", "deterministic_alias"}),
        ("semantic_mode", {"off", "shadow", "active"}),
        ("semantic_shadow_status", {"separate_observation_only"}),
    ):
        item = value.get(key)
        if isinstance(item, str) and item in allowed:
            result[key] = item
    semantic_error = value.get("semantic_fallback_error")
    if isinstance(semantic_error, str) and semantic_error in _FAILURE_MESSAGES:
        result["semantic_fallback_error"] = semantic_error
    for key in (
        "candidate_count_before_filter", "candidate_pool_count",
        "candidate_count_after_retrieval_filter", "candidate_count_after_filter",
        "qualified_exact_count", "semantic_trigger_threshold",
        "semantic_candidate_count_before_filter",
    ):
        try:
            result[key] = max(0, min(int(value.get(key, 0) or 0), 100))
        except (TypeError, ValueError):
            result[key] = 0
    for key in ("semantic_fallback_eligible", "semantic_fallback_triggered"):
        result[key] = bool(value.get(key))
    concepts = value.get("semantic_concepts_considered")
    if isinstance(concepts, list):
        clean_concepts = []
        for item in concepts[:12]:
            if not isinstance(item, dict):
                continue
            concept_key = str(item.get("concept_key") or "").strip()[:100]
            try:
                similarity = round(float(item.get("similarity", 0.0)), 4)
            except (TypeError, ValueError):
                continue
            if (re.fullmatch(r"[a-z][a-z0-9_]{1,50}", concept_key)
                    and math.isfinite(similarity) and 0 <= similarity <= 1):
                clean_concepts.append({
                    "concept_key": concept_key,
                    "similarity": max(0.0, min(similarity, 1.0)),
                })
        result["semantic_concepts_considered"] = clean_concepts
    for key in ("shared_preferences", "hard_conflicts"):
        values = value.get(key) if isinstance(value.get(key), list) else []
        result[key] = [str(item)[:52] for item in values[:8] if str(item).strip()]
    reasons = value.get("qualification_reason_codes")
    if isinstance(reasons, dict):
        result["qualification_reason_codes"] = {
            str(key)[:60]: max(0, min(int(count or 0), 100))
            for key, count in list(reasons.items())[:12]
        }
    return result


def _finish_job(
    job: dict[str, Any], status: str, *, error_code: str = "", failure_stage: str = "",
    diagnostics: dict[str, Any] | None = None,
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
            "retrieval_diagnostics": _bounded_diagnostics(diagnostics),
        }, "$unset": {"active_user_id": "", "lease_id": "", "lease_until": ""}},
    )
    if not getattr(result, "modified_count", 0):
        return False
    _forget_owned_lease(job)
    profiles_coll.update_one(
        {"user_id": job.get("user_id"), "active_match_search_job_id": job.get("job_id")},
        {"$set": {
            "matchmaking_in_progress": False,
            "match_search": _profile_search_projection(
                status,
                str(job.get("source") or "automatic"),
                progress_percent=100 if status == "completed" else 0,
                completed_at=now,
                reason_code=error_code[:80] if status in {"failed", "insufficient_common_ground", "quota_exceeded"} else "",
            ),
        }, "$unset": {"active_match_search_job_id": ""}},
    )
    return True


def _requeue_after_context_change(job: dict[str, Any]) -> bool:
    """Retry one search from the newest profile snapshot without hiding progress."""
    try:
        attempt = int(job.get("attempt", 0) or 0)
        previous_revision = int(job.get("context_revision", 0) or 0)
    except (TypeError, ValueError):
        return False
    if attempt > MAX_CONTEXT_CHANGE_REQUEUES or not _job_has_lease(job):
        return False
    user_id = str(job.get("user_id") or "")
    profile = profiles_coll.find_one(
        {"user_id": user_id}, {"_id": 0, "current_context_revision": 1},
    ) or {}
    current_revision = _safe_revision(profile)
    if current_revision == previous_revision:
        return False
    now = time.time()
    result = MATCH_SEARCH_JOBS.update_one(
        {
            "_id": job.get("_id"),
            "status": "running",
            "active_user_id": user_id,
            "lease_id": job.get("lease_id"),
            "attempt": {"$lte": MAX_CONTEXT_CHANGE_REQUEUES},
        },
        {
            "$set": {
                "status": "queued",
                "step": "loading_profile",
                "progress_percent": 0,
                "context_revision": current_revision,
                "updated_at": now,
            },
            "$unset": {
                "lease_id": "",
                "lease_until": "",
                "completed_at": "",
                "error_code": "",
                "failure_stage": "",
            },
        },
    )
    if not getattr(result, "modified_count", 0):
        return False
    profiles_coll.update_one(
        {"user_id": user_id, "active_match_search_job_id": job.get("job_id")},
        {"$set": {
            "matchmaking_in_progress": True,
            "match_search": _profile_search_projection(
                "queued",
                str(job.get("source") or "automatic"),
                step="loading_profile",
                progress_percent=0,
            ),
        }},
    )
    LOGGER.info(
        "match search requeued after context revision changed job=%s attempt=%s",
        str(job.get("job_id") or "")[:80], attempt,
    )
    return True


def _settle_stale_job(job: dict[str, Any]) -> None:
    """Requeue one context race, otherwise surface the terminal stale outcome."""
    if _requeue_after_context_change(job):
        return
    error_code = "ownership_or_context_changed"
    if _finish_job(job, "stale", error_code=error_code):
        queue_mediator_event(
            str(job.get("user_id") or ""),
            _FAILURE_MESSAGES[error_code],
            "match_search_failed",
            event_key=f"match-search-job:{job.get('job_id')}:stale",
            origin_room_id=str(job.get("origin_room_id") or ""),
        )


def _deliver_invitation_on_match(job: dict[str, Any], matches: list[dict[str, Any]]) -> None:
    """Use the canonical draft -> pending CAS for a confirmed topic search.

    The search worker and the proposal live in separate Mongo documents, so a
    cross-document transaction is not assumed here.  The proposal transition
    itself is atomic and keyed by the durable job id; a lease retry therefore
    either observes the already-pending decision or applies it once.  The
    action service owns receiver notification and its event keys make delivery
    idempotent as well.
    """
    if str(job.get("delivery_mode") or PREVIEW_ON_MATCH) != INVITE_ON_MATCH:
        return
    first = matches[0] if matches else {}
    match_id = str(first.get("match_id") or "").strip()
    if not match_id:
        raise MatchSearchPipelineError("proposal_write_failed", "proposal_write")
    try:
        from services.match_action_service import decide_match

        outcome = decide_match(
            user_id=str(job.get("user_id") or ""),
            match_id=match_id,
            action="accept",
            expected_status="draft",
            expected_revision=int(first.get("proposal_revision", 0) or 0),
            expected_namespace=RELATIONSHIP_MATCH_NAMESPACE,
            idempotency_key=f"match-search-job:{job.get('job_id')}:invite-on-match",
        )
    except Exception as exc:
        LOGGER.warning(
            "invite-on-match transition failed job=%s error=%s",
            str(job.get("job_id") or "")[:80], type(exc).__name__,
        )
        raise MatchSearchPipelineError("proposal_write_failed", "proposal_write") from exc
    if str(outcome.get("status") or "") != "success":
        # A concurrent transition can safely win the CAS only when the
        # proposal is already pending/accepted. Other outcomes are stale and
        # must not be presented as a sent invitation.
        current_status = str(outcome.get("current_status") or "")
        if not outcome.get("stale") or current_status not in {"pending", "accepted"}:
            raise MatchSearchPipelineError("proposal_write_failed", "proposal_write")
    # ``decide_match`` invokes transition effects for a fresh CAS. If this is
    # lease recovery and the same idempotency key was already committed, replay
    # the effects from the canonical proposal so a crash between the state write
    # and notification delivery can heal without another transition.
    if outcome.get("idempotent"):
        try:
            from services.match_action_service import apply_transition_effects
            proposal = matches_coll.find_one({
                "search_job_id": str(job.get("job_id") or ""),
                "from_user": str(job.get("user_id") or ""),
                "status": {"$in": ["pending", "accepted"]},
            })
            if proposal:
                apply_transition_effects(proposal, "accept", "draft", [])
        except Exception as exc:
            LOGGER.warning(
                "invite-on-match effect recovery failed job=%s error=%s",
                str(job.get("job_id") or "")[:80], type(exc).__name__,
            )


def _live_proposal_for_job(job: dict[str, Any]) -> dict[str, Any] | None:
    """Read a proposal committed by an earlier lease owner, if any."""
    job_id = str(job.get("job_id") or "").strip()
    user_id = str(job.get("user_id") or "").strip()
    if not job_id or not user_id:
        return None
    return matches_coll.find_one({
        "search_job_id": job_id,
        "from_user": user_id,
        # The receiver can accept while the original worker is down. Keep the
        # accepted row tied to this job so reclaim never reruns ranking/quota.
        "status": {"$in": ["draft", "pending", "accepted"]},
        "proposal_namespace": RELATIONSHIP_MATCH_NAMESPACE,
    })


def _proposal_result_from_document(proposal: dict[str, Any]) -> dict[str, Any]:
    """Create the minimum pipeline result needed by recovery delivery."""
    matched_id = proposal.get("to_user")
    return {
        "match_id": str(proposal.get("_id") or ""),
        "matched_user_id": str(matched_id or ""),
        "proposal_revision": int(proposal.get("proposal_revision", 0) or 0),
    }


def _queue_completed_match_event(job: dict[str, Any], first: dict[str, Any]) -> None:
    """Publish one stable requester receipt after a completed search."""
    topic = str(
        ((job.get("search_context") or {}).get("invitation_topic") or "")
    ).strip()[:80]
    if str(job.get("delivery_mode") or PREVIEW_ON_MATCH) == INVITE_ON_MATCH and topic:
        message = f"已替你送出「{topic}」邀請，正在等對方回覆。"
    elif str(job.get("delivery_mode") or PREVIEW_ON_MATCH) == INVITE_ON_MATCH:
        message = "已替你送出邀請，正在等對方回覆。"
    else:
        tone_doc = profiles_coll.find_one(
            {"user_id": job.get("user_id")}, {"_id": 0, "mediator_tone": 1},
        ) or {}
        message = {
            "friend": "我翻到一位可以介紹給你的人，先看這張阿月牽線提案。",
            "gentle": "我幫你留意到一位可能合拍的人，先看看這個提案。",
            "enthusiastic": "我找到一位有機會聊起來的人，快看看這張牽線提案！",
        }.get(
            str(tone_doc.get("mediator_tone") or "friend"),
            "我翻到一位可以介紹給你的人，先看這張阿月牽線提案。",
        )
    queue_mediator_event(
        str(job.get("user_id") or ""), message, "match_proposal",
        event_key=f"match-search-job:{job.get('job_id')}:proposal",
        match_id=first.get("match_id"), proposal_role="initiator",
        proposal_namespace=RELATIONSHIP_MATCH_NAMESPACE,
        origin_room_id=str(job.get("origin_room_id") or ""),
    )


def _recover_committed_job(job: dict[str, Any]) -> bool:
    """Finish a lease-reclaimed job whose proposal was already inserted."""
    try:
        proposal = _live_proposal_for_job(job)
    except Exception as exc:
        # An unavailable proposal read is not evidence that no proposal exists;
        # rerunning selection could double-charge the user. Leave the job for
        # the normal lease retry instead.
        LOGGER.warning(
            "match proposal recovery read failed job=%s error=%s",
            str(job.get("job_id") or "")[:80], type(exc).__name__,
        )
        return True
    if not proposal:
        return False
    recovered = _proposal_result_from_document(proposal)
    try:
        _deliver_invitation_on_match(job, [recovered])
    except MatchSearchPipelineError as exc:
        if _finish_job(job, "failed", error_code=exc.code, failure_stage=exc.stage):
            queue_mediator_event(
                str(job.get("user_id") or ""),
                _FAILURE_MESSAGES.get(exc.code, _FAILURE_MESSAGES["proposal_write_failed"]),
                "match_search_failed", event_key=f"match-search-job:{job.get('job_id')}:failed",
                origin_room_id=str(job.get("origin_room_id") or ""),
            )
        return True
    if _finish_job(job, "completed"):
        _queue_completed_match_event(job, recovered)
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
        _settle_stale_job(job)
        return True
    recovery_checkpoint = str(job.get("step") or "") == "proposal_write"
    try:
        recovery_checkpoint = recovery_checkpoint or int(job.get("progress_percent", 0) or 0) >= JOB_STEPS["proposal_write"]
    except (TypeError, ValueError):
        pass
    if recovery_checkpoint and _recover_committed_job(job):
        # A previous lease owner may have inserted the proposal before the
        # process died. Recovery finalizes that exact proposal and skips the
        # candidate pipeline, quota reservation and duplicate insert.
        return True
    try:
        # A committed checkpoint above only reconciles an existing proposal.
        # Before any NEW selection/quota work, require intact persisted v2
        # preference source. Never feed a legacy prefix to the fresh sanitizer.
        try:
            replay_context = validate_persisted_search_context(job.get("search_context"))
        except PreferenceTextError as exc:
            raise MatchSearchPipelineError(exc.code, "preference_input") from None
        pipeline_kwargs = {
            "report_progress": lambda step: _report_progress(job, step),
            "can_commit": lambda: _job_is_current(job),
            "search_job_id": str(job.get("job_id") or ""),
            "search_context": replay_context,
        }
        if str(job.get("origin_room_id") or "").strip():
            pipeline_kwargs["origin_room_id"] = str(job.get("origin_room_id") or "")[:240]
        if str(job.get("delivery_mode") or PREVIEW_ON_MATCH) == INVITE_ON_MATCH:
            pipeline_kwargs["delivery_mode"] = INVITE_ON_MATCH
        scope = task_scope(str(job['user_id']), 'matching', str(job['job_id'])) if job.get('quota_billable') else nullcontext()
        with scope:
            result = _pipeline(
                str(job.get("user_id") or ""), str(job.get("source") or "automatic"),
                **pipeline_kwargs,
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
        _settle_stale_job(job)
        return True
    matches = list((result or {}).get("matches") or [])[:1]
    if not matches:
        if not _job_has_ownership(job):
            _settle_stale_job(job)
            return True
        result_reason = str((result or {}).get("reason_code") or "")
        terminal_status = (
            "insufficient_common_ground"
            if result_reason in {
                "insufficient_common_ground", "insufficient_semantic_ground",
            }
            else "quota_exceeded"
            if result_reason == "daily_active_quota_exceeded"
            else "no_candidates"
        )
        finish_kwargs = {}
        if result_reason:
            finish_kwargs["error_code"] = result_reason
        diagnostics = (result or {}).get("diagnostics")
        if isinstance(diagnostics, dict) and diagnostics:
            finish_kwargs["diagnostics"] = diagnostics
        if _finish_job(
            job, terminal_status,
            **finish_kwargs,
        ):
            empty_reply = _FAILURE_MESSAGES.get(
                result_reason, "這輪暫時沒有合適的新對象；你可以補充想認識的對象或想一起做的事，再決定是否搜尋。",
            )
            queue_mediator_event(
                str(job.get("user_id") or ""),
                empty_reply,
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
    try:
        _deliver_invitation_on_match(job, matches)
    except MatchSearchPipelineError as exc:
        if _finish_job(job, "failed", error_code=exc.code, failure_stage=exc.stage):
            queue_mediator_event(
                str(job.get("user_id") or ""),
                _FAILURE_MESSAGES.get(exc.code, _FAILURE_MESSAGES["proposal_write_failed"]),
                "match_search_failed", event_key=f"match-search-job:{job.get('job_id')}:failed",
                origin_room_id=str(job.get("origin_room_id") or ""),
            )
        return True
    if _finish_job(
        job, "completed", diagnostics=(result or {}).get("diagnostics"),
    ):
        _queue_completed_match_event(job, matches[0])
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
    reason_code = str(job.get("error_code") or "") if status in {"failed", "insufficient_common_ground", "quota_exceeded"} else ""
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
    # A graceful development reload must not make the next process wait for
    # the full lease window. Release only leases claimed by this process; an
    # unrelated worker's lease remains untouched.
    with _owned_lease_lock:
        lease_ids = list(_owned_lease_ids)
    for lease_id in lease_ids:
        job = MATCH_SEARCH_JOBS.find_one_and_update(
            {"status": "running", "lease_id": lease_id},
            {
                "$set": {
                    "status": "queued",
                    "step": "loading_profile",
                    "progress_percent": 0,
                    "updated_at": time.time(),
                },
                "$unset": {"lease_id": "", "lease_until": ""},
            },
            return_document=ReturnDocument.BEFORE,
        )
        if job:
            profiles_coll.update_one(
                {
                    "user_id": job.get("user_id"),
                    "active_match_search_job_id": job.get("job_id"),
                },
                {"$set": {
                    "matchmaking_in_progress": True,
                    "match_search": _profile_search_projection(
                        "queued", str(job.get("source") or "automatic"),
                        step="loading_profile", progress_percent=0,
                    ),
                }},
            )
        with _owned_lease_lock:
            _owned_lease_ids.discard(lease_id)
