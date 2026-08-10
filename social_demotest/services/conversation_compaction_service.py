"""Shadow conversation compaction and bounded continuity projections.

This service owns selection, generation, deterministic validation, persistence,
and local inspection data.  It intentionally does not delete chat messages.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from typing import Any

from pymongo.errors import DuplicateKeyError

from config import OLLAMA_FAST_CHAT_MODEL
from database import db, messages_coll
from services.ai_service import generate_chat_completion
from services.chat_service import generate_room_id

from .conversation_compaction_contracts import (
    COMPACTION_VERSION, MAX_SUMMARY_CHARS, POLICY_VERSION,
    SUMMARY_FIELDS, ConversationCompactionRecordV2, ConversationEvaluationV1,
    ConversationSummaryV1, summary_from_payload,
)


COMPACTIONS = db["conversation_compactions"]
REVISIONS = db["conversation_compaction_revisions"]
SHADOW_RUNS = db["conversation_compaction_shadow_runs"]
JOBS = db["conversation_compaction_jobs"]
COMPACTION_HIGH_WATERMARK = 30
COMPACTION_KEEP_RECENT_MESSAGES = 12
COMPACTION_BATCH_MESSAGE_LIMIT = 24
COMPACTION_QUERY_LIMIT = COMPACTION_HIGH_WATERMARK + COMPACTION_BATCH_MESSAGE_LIMIT + 1
COMPACTION_MESSAGE_CHAR_LIMIT = 900
COMPACTION_TOTAL_INPUT_CHAR_LIMIT = 9000
MAX_GENERATION_ATTEMPTS = 1
COMPACTION_LEASE_SECONDS = 180
COMPACTION_RETRY_COOLDOWN_SECONDS = 300
COMPACTION_REVISION_RETENTION_SECONDS = 30 * 86400
PRIVATE_PROJECTION_FIELDS = ("active_topics", "owner_goals", "known_continuity", "unresolved_questions")

COMPACTION_SYSTEM_PROMPT = """Create the next cumulative continuity projection for Public Ayue.
The input has a previously validated summary and a newer owner-to-Public-Ayue
message batch. Merge them into one bounded summary. Preserve still-relevant
continuity from the previous summary, let newer messages supersede stale
conversation state, and do not merely summarize the new batch. Never invent
facts, IDs, dates, private human-to-human content, calendar state, match state,
counterparty facts, or durable preferences. Return JSON with exactly six
arrays: active_topics, owner_goals, known_continuity, unresolved_questions,
ayue_commitments, recent_decisions. Each item must be short and grounded in
either the previous validated summary or the supplied messages."""


def compaction_mode() -> str:
    mode = os.getenv("AYUE_CONVERSATION_COMPACTION_MODE", "off").strip().lower()
    return mode if mode in {"off", "shadow"} else "off"


def public_continuity_enabled() -> bool:
    return os.getenv("AYUE_PUBLIC_CONVERSATION_CONTINUITY", "off").strip().lower() in {"1", "true", "on"}


def private_continuity_enabled() -> bool:
    return os.getenv("AYUE_PRIVATE_PUBLIC_CONTINUITY", "off").strip().lower() in {"1", "true", "on"}


def ensure_conversation_compaction_indexes() -> None:
    try:
        COMPACTIONS.create_index([("owner_user_id", 1), ("room_id", 1), ("version", 1)], unique=True)
        COMPACTIONS.create_index([("owner_user_id", 1), ("updated_at", -1)])
        REVISIONS.create_index(
            [("owner_user_id", 1), ("room_id", 1), ("version", 1), ("revision", 1)],
            unique=True,
        )
        REVISIONS.create_index("snapshot_created_at", expireAfterSeconds=COMPACTION_REVISION_RETENTION_SECONDS)
        SHADOW_RUNS.create_index("created_at", expireAfterSeconds=30 * 86400)
        SHADOW_RUNS.create_index([("owner_user_id", 1), ("created_at", -1)])
        JOBS.create_index([("owner_user_id", 1), ("updated_at", -1)])
    except Exception as exc:
        print(f"Conversation compaction index setup skipped: {type(exc).__name__}")


def _hash_message(item: dict[str, Any]) -> str:
    payload = f"{item.get('_id')}|{item.get('sender_id')}|{item.get('timestamp')}|{item.get('content', '')}"
    return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()


def _room(owner_user_id: str) -> str:
    return generate_room_id(owner_user_id, "ai_assistant")


def _record(owner_user_id: str) -> dict[str, Any] | None:
    try:
        return COMPACTIONS.find_one({"owner_user_id": owner_user_id, "room_id": _room(owner_user_id), "version": COMPACTION_VERSION})
    except Exception:
        return None


def _job_id(owner_user_id: str) -> str:
    value = f"{COMPACTION_VERSION}|{owner_user_id}|{_room(owner_user_id)}"
    return hashlib.sha256(value.encode("utf-8", "ignore")).hexdigest()


def _select_batch(
    owner_user_id: str, *, manual_debug: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str]:
    room_id = _room(owner_user_id)
    current = _record(owner_user_id)
    covered_ts = float((current or {}).get("covered_through_timestamp", 0) or 0)
    # The room name is canonical, but sender filtering is an additional hard
    # privacy boundary: only this owner and Public Ayue may enter the summary.
    query: dict[str, Any] = {"room_id": room_id, "sender_id": {"$in": [owner_user_id, "ai_assistant"]}}
    if covered_ts:
        query["timestamp"] = {"$gt": covered_ts}
    try:
        items = list(messages_coll.find(query, {"_id": 1, "sender_id": 1, "content": 1, "timestamp": 1}).sort("timestamp", 1).limit(COMPACTION_QUERY_LIMIT))
    except Exception:
        return [], current, "unavailable"
    threshold = COMPACTION_KEEP_RECENT_MESSAGES if manual_debug else COMPACTION_HIGH_WATERMARK
    if len(items) <= threshold:
        return [], current, "below_threshold"
    target_count = min(
        len(items) - COMPACTION_KEEP_RECENT_MESSAGES,
        COMPACTION_BATCH_MESSAGE_LIMIT,
    )
    batch: list[dict[str, Any]] = []
    input_chars = 0
    for item in items[:target_count]:
        bounded_chars = len(" ".join(str(item.get("content") or "").split())[:COMPACTION_MESSAGE_CHAR_LIMIT])
        if batch and input_chars + bounded_chars > COMPACTION_TOTAL_INPUT_CHAR_LIMIT:
            break
        batch.append(item)
        input_chars += bounded_chars
    return batch, current, "eligible"


def _message_payload(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{
        "role": "user" if item.get("sender_id") != "ai_assistant" else "assistant",
        "content": " ".join(str(item.get("content") or "").split())[:COMPACTION_MESSAGE_CHAR_LIMIT],
        "timestamp": float(item.get("timestamp", 0) or 0),
    } for item in items]


def _generation_error_code(exc: Exception) -> str:
    """Map provider/parser failures to a bounded, non-sensitive debug code."""
    if isinstance(exc, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(exc, TimeoutError) or "timeout" in type(exc).__name__.lower():
        return "provider_timeout"
    if isinstance(exc, ValueError):
        marker = str(exc)
        if marker in {"empty_content", "summary_schema_invalid", "summary_too_large"}:
            return marker
    return "provider_error"


def _generate_summary(
    items: list[dict[str, Any]], previous_summary: ConversationSummaryV1,
) -> tuple[ConversationSummaryV1, dict[str, Any]]:
    data = json.dumps({
        "previous_validated_summary": previous_summary.model_dump(mode="json"),
        "messages": _message_payload(items),
    }, ensure_ascii=False)
    errors: list[str] = []
    for attempt in range(1, MAX_GENERATION_ATTEMPTS + 1):
        try:
            response = generate_chat_completion(
                data, temperature=0, json_output=True, model=os.getenv("AYUE_COMPACTION_MODEL") or OLLAMA_FAST_CHAT_MODEL,
                max_tokens=1200, system_prompt=COMPACTION_SYSTEM_PROMPT,
            )
            raw_content = getattr(response, "content", response)
            if not isinstance(raw_content, str) or not raw_content.strip():
                raise ValueError("empty_content")
            payload = json.loads(raw_content)
            if (
                not isinstance(payload, dict)
                or any(name not in payload or not isinstance(payload.get(name), list) for name in SUMMARY_FIELDS)
            ):
                raise ValueError("summary_schema_invalid")
            summary = summary_from_payload(payload)
            if summary.char_count() > MAX_SUMMARY_CHARS:
                raise ValueError("summary_too_large")
            return summary, {"attempts": attempt, "generator": "provider", "error_code": None, "attempt_errors": errors[:2]}
        except Exception as exc:
            errors.append(_generation_error_code(exc))
    error_code = errors[-1] if errors else "provider_error"
    return ConversationSummaryV1(), {
        "attempts": MAX_GENERATION_ATTEMPTS, "generator": "unavailable",
        "error_code": error_code, "attempt_errors": errors[:2],
    }


def _evaluate(
    items: list[dict[str, Any]], summary: ConversationSummaryV1,
    previous_summary: ConversationSummaryV1 | None = None,
) -> ConversationEvaluationV1:
    retention = {name: bool(getattr(summary, name)) for name in SUMMARY_FIELDS}
    if not any(retention.values()):
        return ConversationEvaluationV1(status="fail", confidence=0.0, issues=["generation_unavailable"], retention=retention)
    # Deterministic safety checks run before any optional evaluator model.
    previous_summary = previous_summary or ConversationSummaryV1()
    joined = " ".join([
        *(item["content"] for item in _message_payload(items)),
        *(value for name in SUMMARY_FIELDS for value in getattr(previous_summary, name)),
    ])
    issues: list[str] = []
    for name in SUMMARY_FIELDS:
        for value in getattr(summary, name):
            if value and value not in joined and name not in {"active_topics", "owner_goals", "known_continuity", "unresolved_questions"}:
                issues.append(f"unsupported_content:{name}")
            if any(token in value.lower() for token in ("seed_user_", "objectid", "revision", "match_id", "event_id")):
                issues.append(f"canonical_state_leak:{name}")
    if issues:
        return ConversationEvaluationV1(status="fail", confidence=0.0, issues=sorted(set(issues)), retention=retention)
    return ConversationEvaluationV1(status="pass", confidence=0.8, issues=[], retention=retention)


def _claim_job(user_id: str, *, ignore_cooldown: bool = False) -> tuple[str, str | None]:
    """Atomically claim one queued worker per owner, including across processes."""
    now = time.time()
    job_id = _job_id(user_id)
    existing = JOBS.find_one({"_id": job_id})
    if existing:
        lease_expires_at = float(existing.get("lease_expires_at", 0) or 0)
        next_retry_at = float(existing.get("next_retry_at", 0) or 0)
        if existing.get("status") in {"queued", "running"} and lease_expires_at > now:
            JOBS.update_one({"_id": job_id}, {"$set": {"coalesced_at": now, "updated_at": now}})
            return "coalesced", None
        if next_retry_at > now and not ignore_cooldown:
            return "cooldown", None
        token = uuid.uuid4().hex
        result = JOBS.update_one(
            {"_id": job_id, "lease_expires_at": lease_expires_at},
            {"$set": {
                "owner_user_id": user_id, "room_id": _room(user_id), "version": COMPACTION_VERSION,
                "status": "queued", "lease_token": token,
                "lease_expires_at": now + COMPACTION_LEASE_SECONDS,
                "next_retry_at": 0.0, "updated_at": now,
            }},
        )
        return ("queued", token) if getattr(result, "matched_count", 0) else ("coalesced", None)
    token = uuid.uuid4().hex
    try:
        JOBS.insert_one({
            "_id": job_id, "owner_user_id": user_id, "room_id": _room(user_id),
            "version": COMPACTION_VERSION, "status": "queued", "lease_token": token,
            "lease_expires_at": now + COMPACTION_LEASE_SECONDS,
            "next_retry_at": 0.0, "created_at": now, "updated_at": now,
        })
    except DuplicateKeyError:
        return "coalesced", None
    return "queued", token


def _finish_job(user_id: str, lease_token: str, *, status: str, result_code: str) -> None:
    now = time.time()
    next_retry_at = now + COMPACTION_RETRY_COOLDOWN_SECONDS if status == "failed" else 0.0
    try:
        JOBS.update_one(
            {"_id": _job_id(user_id), "lease_token": lease_token},
            {"$set": {
                "status": status, "result_code": result_code, "next_retry_at": next_retry_at,
                "lease_expires_at": 0.0, "updated_at": now,
            }, "$unset": {"lease_token": ""}},
        )
    except Exception:
        # The owner lease expires independently. An observability write must
        # never turn an already accepted projection into a failed chat turn.
        pass


def queue_conversation_compaction_shadow(
    background_tasks, user_id: str, room_id: str | None = None, *,
    manual_debug: bool = False,
) -> dict[str, Any]:
    """Queue the normal oldest eligible batch; never accepts message IDs or force flags."""
    if compaction_mode() != "shadow":
        return {"status": "disabled", "mode": compaction_mode()}
    expected_room = _room(user_id)
    if room_id and room_id != expected_room:
        return {"status": "rejected", "code": "public_room_only"}
    batch, current, selection = _select_batch(user_id, manual_debug=manual_debug)
    if selection != "eligible":
        return {"status": selection, "mode": "shadow", "message_count": len(batch)}
    claim_status, lease_token = _claim_job(user_id, ignore_cooldown=manual_debug)
    if claim_status != "queued" or not lease_token:
        return {"status": claim_status, "mode": "shadow", "message_count": len(batch)}
    background_tasks.add_task(
        run_conversation_compaction_shadow, user_id, lease_token, manual_debug=manual_debug,
    )
    return {"status": "queued", "mode": "shadow", "message_count": len(batch), "covered_revision": int((current or {}).get("revision", 0) or 0)}


def run_conversation_compaction_shadow(
    user_id: str, lease_token: str | None = None, *, manual_debug: bool = False,
) -> dict[str, Any]:
    token = lease_token
    if token is None:
        claim_status, token = _claim_job(user_id)
        if claim_status != "queued" or not token:
            return {"status": claim_status, "mode": "shadow"}
    acquired = JOBS.update_one(
        {"_id": _job_id(user_id), "status": "queued", "lease_token": token},
        {"$set": {"status": "running", "started_at": time.time(), "updated_at": time.time()}},
    )
    if not getattr(acquired, "matched_count", 0):
        return {"status": "coalesced", "mode": "shadow"}
    try:
        return _run_claimed_compaction_shadow(user_id, token, manual_debug=manual_debug)
    except Exception:
        _finish_job(user_id, token, status="failed", result_code="internal_error")
        return {"status": "failed", "mode": "shadow", "result_code": "internal_error"}


def _run_claimed_compaction_shadow(
    user_id: str, token: str, *, manual_debug: bool = False,
) -> dict[str, Any]:
    batch, current, selection = _select_batch(user_id, manual_debug=manual_debug)
    if selection != "eligible":
        _finish_job(user_id, token, status="completed", result_code=selection)
        return {"status": selection, "mode": "shadow"}
    started = time.time()
    previous_summary = summary_from_payload((current or {}).get("summary") or {})
    summary, generation = _generate_summary(batch, previous_summary)
    if not any(getattr(summary, name) for name in SUMMARY_FIELDS) and generation.get("error_code"):
        retention = {name: False for name in SUMMARY_FIELDS}
        evaluation = ConversationEvaluationV1(
            status="fail", confidence=0.0,
            issues=[str(generation["error_code"])[:48]], retention=retention,
        )
    else:
        evaluation = _evaluate(batch, summary, previous_summary)
    source_hashes = [_hash_message(item) for item in batch]
    last = batch[-1]
    revision = int((current or {}).get("revision", 0) or 0) + 1
    record = ConversationCompactionRecordV2(
        owner_user_id=user_id, room_id=_room(user_id), revision=revision,
        covered_message_count=int((current or {}).get("covered_message_count", 0) or 0) + len(batch),
        covered_through_message_id=str(last.get("_id")),
        covered_through_timestamp=float(last.get("timestamp", 0) or 0), source_hashes=source_hashes,
        summary=summary, evaluation=evaluation,
        observability={"policy_version": POLICY_VERSION, "input_messages": len(batch),
                       "input_chars": sum(len(item["content"]) for item in _message_payload(batch)),
                       "summary_chars": summary.char_count(), "previous_summary_chars": previous_summary.char_count(),
                       "rolling": True, "generation": generation,
                       "latency_ms": round((time.time() - started) * 1000)},
        created_at=float((current or {}).get("created_at", started) or started), updated_at=time.time(),
    )
    payload = record.model_dump(mode="json")
    accepted = False
    result_code = str(generation.get("error_code") or "evaluation_failed") if evaluation.status != "pass" and not any(getattr(summary, name) for name in SUMMARY_FIELDS) else "evaluation_failed"
    if evaluation.status == "pass":
        current_filter = {
            "owner_user_id": user_id, "room_id": _room(user_id), "version": COMPACTION_VERSION,
            "revision": int((current or {}).get("revision", 0) or 0),
        }
        try:
            if current is None:
                write = COMPACTIONS.replace_one(current_filter, payload, upsert=True)
                accepted = bool(getattr(write, "upserted_id", None))
            else:
                write = COMPACTIONS.replace_one(current_filter, payload, upsert=False)
                accepted = bool(getattr(write, "matched_count", 0))
        except DuplicateKeyError:
            accepted = False
        if accepted:
            result_code = "accepted"
            snapshot = {**payload, "snapshot_created_at": time.time()}
            try:
                REVISIONS.insert_one(snapshot)
            except Exception:
                pass
        else:
            result_code = "superseded"
    try:
        SHADOW_RUNS.insert_one({
            "owner_user_id": user_id, "room_id": _room(user_id), "created_at": time.time(),
            "revision": revision, "status": evaluation.status, "accepted": accepted,
            "result_code": result_code, "confidence": evaluation.confidence,
            "issue_count": len(evaluation.issues), "issue_codes": list(evaluation.issues[:8]),
            "generation_error_code": generation.get("error_code"),
            "generation_attempt_errors": list(generation.get("attempt_errors") or [])[:2],
            "message_count": len(batch),
            "input_chars": record.observability.get("input_chars", 0),
            "summary_chars": record.observability.get("summary_chars", 0),
        })
    except Exception:
        pass
    job_status = "completed" if accepted or result_code == "superseded" else "failed"
    _finish_job(user_id, token, status=job_status, result_code=result_code)
    return {
        "status": "completed" if accepted else "rejected", "revision": revision,
        "accepted": accepted, "result_code": result_code,
        "evaluation": evaluation.model_dump(mode="json"),
    }


def load_public_continuity(owner_user_id: str) -> dict[str, Any] | None:
    if compaction_mode() != "shadow" or not public_continuity_enabled():
        return None
    record = _record(owner_user_id)
    if not record or record.get("mode") != "shadow":
        return None
    evaluation = record.get("evaluation") or {}
    if evaluation.get("status") != "pass":
        return None
    return {"summary": summary_from_payload(record.get("summary") or {}).model_dump(mode="json"),
            "revision": int(record.get("revision", 0) or 0), "covered_message_count": int(record.get("covered_message_count", 0) or 0)}


def load_private_continuity(owner_user_id: str) -> dict[str, Any] | None:
    if not private_continuity_enabled():
        return None
    public = load_public_continuity(owner_user_id)
    if not public:
        return None
    summary = summary_from_payload(public["summary"])
    return {"summary": summary.as_private_projection(), "revision": public["revision"], "covered_message_count": public["covered_message_count"]}


def inspect_conversation_context(owner_user_id: str) -> dict[str, Any]:
    record = _record(owner_user_id) or {}
    batch, _, selection = _select_batch(owner_user_id)
    summary = summary_from_payload(record.get("summary") or {})
    try:
        runs = list(SHADOW_RUNS.find({"owner_user_id": owner_user_id}, {"_id": 0, "owner_user_id": 0}).sort("created_at", -1).limit(10))
    except Exception:
        runs = []
    try:
        job = JOBS.find_one({"_id": _job_id(owner_user_id)}, {"_id": 0, "owner_user_id": 0, "lease_token": 0}) or {}
    except Exception:
        job = {}
    return {"user_id": owner_user_id, "room_id": _room(owner_user_id),
            "modes": {"compaction": compaction_mode(), "public_continuity": public_continuity_enabled(), "private_continuity": private_continuity_enabled()},
            "record": {"revision": int(record.get("revision", 0) or 0), "updated_at": record.get("updated_at"), "covered_message_count": int(record.get("covered_message_count", 0) or 0),
                       "evaluation": record.get("evaluation") or {}, "observability": record.get("observability") or {},
                       "summary": summary.model_dump(mode="json"), "private_projection": summary.as_private_projection()},
            "trigger_policy": {"high_watermark": COMPACTION_HIGH_WATERMARK, "keep_recent": COMPACTION_KEEP_RECENT_MESSAGES,
                               "manual_threshold": COMPACTION_KEEP_RECENT_MESSAGES,
                               "max_batch_messages": COMPACTION_BATCH_MESSAGE_LIMIT, "retry_cooldown_seconds": COMPACTION_RETRY_COOLDOWN_SECONDS},
            "job": job, "next_batch": {"status": selection, "message_count": len(batch)}, "latest_shadow_runs": runs}


def inspect_compaction_metrics() -> dict[str, Any]:
    try:
        total = int(SHADOW_RUNS.count_documents({}))
        passed = int(SHADOW_RUNS.count_documents({"status": "pass"}))
        failed = int(SHADOW_RUNS.count_documents({"status": "fail"}))
        accepted = int(SHADOW_RUNS.count_documents({"accepted": True}))
        active_jobs = int(JOBS.count_documents({"status": {"$in": ["queued", "running"]}}))
    except Exception:
        total = passed = failed = accepted = active_jobs = 0
    return {"mode": compaction_mode(), "public_continuity": public_continuity_enabled(), "private_continuity": private_continuity_enabled(),
            "shadow_runs": total, "pass_runs": passed, "fail_runs": failed, "accepted_runs": accepted,
            "active_jobs": active_jobs,
            "pass_rate": round(passed / total, 4) if total else 0.0}
