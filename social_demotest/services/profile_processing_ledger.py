"""Owner-message processing ledger for durable profile/memory coverage."""

from __future__ import annotations

import time
from typing import Any

from database import db


PROFILE_PROCESSING_LEDGER = db["profile_processing_ledger"]
PROFILE_PROCESSING_CONTROL = db["profile_processing_control"]
LEASE_SECONDS = 120
MAX_ATTEMPTS = 3
ROLLOUT_ID = "profile-processing-ledger-v1"


def ensure_profile_processing_ledger_indexes() -> None:
    try:
        PROFILE_PROCESSING_LEDGER.create_index([("user_id", 1), ("message_id", 1)], unique=True)
        PROFILE_PROCESSING_LEDGER.create_index([("user_id", 1), ("status", 1), ("updated_at", -1)])
        PROFILE_PROCESSING_LEDGER.create_index("updated_at", expireAfterSeconds=90 * 86400)
        PROFILE_PROCESSING_CONTROL.update_one(
            {"_id": ROLLOUT_ID}, {"$setOnInsert": {"_id": ROLLOUT_ID, "created_at": time.time(), "message_cutover_at": time.time()}}, upsert=True,
        )
    except Exception as exc:
        print(f"Profile processing ledger index setup skipped: {type(exc).__name__}")


def claim_profile_message(user_id: str, message_id: str, *, mode: str) -> bool:
    now = time.time()
    try:
        current = PROFILE_PROCESSING_LEDGER.find_one({"user_id": user_id, "message_id": message_id}) or {}
        status = str(current.get("status") or "")
        attempts = int(current.get("attempts", 0) or 0)
        lease_until = float(current.get("lease_until", 0) or 0)
        if status in {"applied", "no_signal", "rejected", "failed"} and not (status == "failed" and attempts < MAX_ATTEMPTS):
            return False
        if status == "processing" and lease_until > now:
            return False
        result = PROFILE_PROCESSING_LEDGER.update_one(
            {"user_id": user_id, "message_id": message_id, "$or": [
                {"status": {"$exists": False}}, {"status": {"$nin": ["processing", "applied", "no_signal", "rejected"]}},
                {"lease_until": {"$lte": now}},
            ]},
            {"$set": {"user_id": user_id, "message_id": message_id, "status": "processing", "mode": mode,
                      "updated_at": now, "lease_until": now + LEASE_SECONDS, "attempts": attempts + 1}}, upsert=True,
        )
        return bool(getattr(result, "modified_count", 0) or getattr(result, "upserted_id", None))
    except Exception as exc:
        # Ledger observability must not stop the owner-only extractor. The
        # existing profile_skill_runs idempotency gate remains authoritative.
        print(f"Profile processing ledger claim skipped: {type(exc).__name__}")
        return True


def finish_profile_message(user_id: str, message_id: str, status: str, *, error_code: str | None = None) -> None:
    if status not in {"applied", "no_signal", "rejected", "failed"}:
        status = "failed"
    try:
        update: dict[str, Any] = {"status": status, "updated_at": time.time(), "lease_until": 0}
        if error_code:
            update["error_code"] = str(error_code)[:80]
        PROFILE_PROCESSING_LEDGER.update_one({"user_id": user_id, "message_id": message_id}, {"$set": update})
    except Exception:
        pass


def ledger_counts(user_id: str) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        for status in ("processing", "applied", "no_signal", "rejected", "failed"):
            result[status] = int(PROFILE_PROCESSING_LEDGER.count_documents({"user_id": user_id, "status": status}))
    except Exception:
        result = {status: 0 for status in ("processing", "applied", "no_signal", "rejected", "failed")}
    return result


def coverage_status(user_id: str) -> dict[str, int]:
    counts = ledger_counts(user_id)
    return {"ledger_total": sum(counts.values()), "retryable": counts.get("failed", 0), "terminal": sum(counts.get(s, 0) for s in ("applied", "no_signal", "rejected"))}
