"""Leased, bounded retry worker for validated durable-memory proposals."""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

from pymongo import ReturnDocument

from database import db


MEMORY_OUTBOX = db["profile_memory_outbox"]
MAX_ATTEMPTS = 8
LEASE_SECONDS = 90
_stop_event = threading.Event()
_worker_thread: threading.Thread | None = None
_thread_lock = threading.Lock()


def _worker_enabled() -> bool:
    return os.getenv("AYUE_MEMORY_OUTBOX_WORKER_ENABLED", "on").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _poll_seconds() -> float:
    try:
        value = float(os.getenv("AYUE_MEMORY_OUTBOX_POLL_SECONDS", "30") or 30)
    except (TypeError, ValueError):
        value = 30.0
    return max(10.0, min(value, 300.0))


def ensure_memory_outbox_indexes() -> None:
    try:
        MEMORY_OUTBOX.create_index("message_id", unique=True, sparse=True,
                                   name="memory_outbox_message_id")
        MEMORY_OUTBOX.create_index(
            [("status", 1), ("next_attempt_at", 1), ("lease_until", 1)],
            name="memory_outbox_retry_queue",
        )
    except Exception as exc:
        print(f"[MEMORY][outbox] index setup skipped error={type(exc).__name__}")


def _claim_one(*, now: float | None = None) -> dict[str, Any] | None:
    current = time.time() if now is None else float(now)
    lease_token = uuid.uuid4().hex
    return MEMORY_OUTBOX.find_one_and_update(
        {"$or": [
            {"$and": [
                {"status": "pending"},
                {"$or": [
                    {"next_attempt_at": {"$lte": current}},
                    {"next_attempt_at": None},
                ]},
            ]},
            {"status": "processing", "lease_until": {"$lte": current}},
        ]},
        {"$set": {
            "status": "processing", "lease_token": lease_token,
            "lease_until": current + LEASE_SECONDS, "updated_at": current,
        }, "$inc": {"attempt_count": 1}},
        sort=[("next_attempt_at", 1), ("created_at", 1)],
        return_document=ReturnDocument.AFTER,
    )


def _finish_failure(record: dict[str, Any], error_code: str, *, now: float) -> None:
    attempts = max(1, int(record.get("attempt_count", 1) or 1))
    terminal = attempts >= MAX_ATTEMPTS
    delay = min(3600.0, 30.0 * (2 ** min(attempts - 1, 7)))
    update: dict[str, Any] = {"$set": {
        "status": "failed" if terminal else "pending",
        "last_error_code": str(error_code or "memory_retry_failed")[:80],
        "updated_at": now,
    }, "$unset": {"lease_token": "", "lease_until": ""}}
    if terminal:
        update["$unset"]["next_attempt_at"] = ""
    else:
        update["$set"]["next_attempt_at"] = now + delay
    MEMORY_OUTBOX.update_one(
        {"_id": record["_id"], "lease_token": record.get("lease_token")}, update,
    )


def process_memory_outbox_once(limit: int = 3) -> dict[str, int]:
    """Retry a small batch; records contain typed proposals, never raw chat."""
    from services.memory_service import MemoryWriteError, apply_profile_memory_proposals

    processed = applied = failed = 0
    for _ in range(max(1, min(int(limit or 1), 10))):
        try:
            record = _claim_one()
        except Exception:
            break
        if not record:
            break
        processed += 1
        try:
            apply_profile_memory_proposals(
                str(record.get("user_id") or ""),
                list(record.get("memories") or [])[:3],
                str(record.get("surface") or "outbox_retry")[:40],
                str(record.get("message_id")) if record.get("message_id") else None,
                str(record.get("match_id")) if record.get("match_id") else None,
            )
        except MemoryWriteError as exc:
            _finish_failure(record, exc.error_code, now=time.time())
            failed += 1
            continue
        except Exception as exc:
            _finish_failure(record, type(exc).__name__, now=time.time())
            failed += 1
            continue
        result = MEMORY_OUTBOX.update_one(
            {"_id": record["_id"], "lease_token": record.get("lease_token")},
            {"$set": {"status": "applied", "updated_at": time.time()},
             "$unset": {"lease_token": "", "lease_until": "",
                         "next_attempt_at": "", "last_error_code": ""}},
        )
        applied += int(bool(getattr(result, "matched_count", 0)))
    return {"processed": processed, "applied": applied, "failed": failed}


def _worker_loop() -> None:
    while not _stop_event.is_set():
        try:
            process_memory_outbox_once()
        except Exception as exc:
            print(f"[MEMORY][outbox] worker error={type(exc).__name__}")
        _stop_event.wait(_poll_seconds())


def start_memory_outbox_worker() -> None:
    global _worker_thread
    if not _worker_enabled():
        return
    with _thread_lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        _stop_event.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop, name="memory-outbox-worker", daemon=True,
        )
        _worker_thread.start()


def stop_memory_outbox_worker() -> None:
    global _worker_thread
    _stop_event.set()
    thread = _worker_thread
    if thread and thread.is_alive():
        thread.join(timeout=2.0)
    _worker_thread = None
