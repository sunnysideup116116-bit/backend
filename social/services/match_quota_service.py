"""Daily limits for user initiated and proactive match introductions.

The quota is intentionally kept outside profile documents.  A profile update
or a stale cached status must never reset usage, and incoming invitations do
not consume the recipient's allowance.  Reservations are idempotent by the
search/job key so retries cannot spend a second slot.
"""

from __future__ import annotations

from datetime import datetime
import os
from typing import Any
from zoneinfo import ZoneInfo

from database import db
from pymongo.errors import DuplicateKeyError


MATCH_QUOTA_USAGE = db["match_quota_usage"]
MATCH_QUOTA_OPERATIONS = db["match_quota_operations"]
QUOTA_TIMEZONE = ZoneInfo("Asia/Taipei")


def _env_limit(name: str, default: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, maximum))


ACTIVE_DAILY_LIMIT = _env_limit("MATCH_ACTIVE_DAILY_LIMIT", 3, 30)
BACKGROUND_DAILY_LIMIT = _env_limit("MATCH_BACKGROUND_DAILY_LIMIT", 1, 7)


def _offline_test_collections() -> bool:
    """Keep unit tests hermetic when no Mongo server is configured."""
    if os.getenv("AYUE_TEST_MODE", "").strip().lower() not in {"1", "true", "on"}:
        return False
    return MATCH_QUOTA_USAGE.__class__.__module__.startswith("pymongo")


def local_quota_date(now: datetime | None = None) -> str:
    """Return the calendar date used by both API status and reservations."""
    value = now or datetime.now(QUOTA_TIMEZONE)
    if value.tzinfo is None:
        value = value.replace(tzinfo=QUOTA_TIMEZONE)
    return value.astimezone(QUOTA_TIMEZONE).date().isoformat()


def ensure_match_quota_indexes() -> None:
    try:
        MATCH_QUOTA_USAGE.create_index(
            [("user_id", 1), ("bucket", 1), ("local_date", 1)],
            unique=True,
            name="match_quota_user_bucket_date",
        )
        MATCH_QUOTA_OPERATIONS.create_index(
            [("user_id", 1), ("bucket", 1), ("local_date", 1), ("operation_key", 1)],
            unique=True,
            name="match_quota_operation_once",
        )
    except Exception as exc:
        # Startup must remain available when an old local database has not yet
        # received the optional quota indexes.  Atomic update filters below
        # still enforce the limit; the index migration can be retried later.
        print(f"[match-quota] index setup skipped: {type(exc).__name__}")


def _limit_for(bucket: str) -> int:
    return BACKGROUND_DAILY_LIMIT if bucket == "background" else ACTIVE_DAILY_LIMIT


def _clean(value: Any, limit: int = 160) -> str:
    return str(value or "").strip()[:limit]


def reserve_daily_quota(
    user_id: str,
    *,
    bucket: str = "active",
    operation_key: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Reserve one slot, safely under concurrent workers.

    ``operation_key`` is written before the counter increment.  If the same
    request is retried, the existing operation is returned as
    ``already_reserved``.  If the counter reached its limit, the operation
    marker is removed and no slot is spent.
    """
    safe_user = _clean(user_id, 120)
    safe_bucket = "background" if bucket == "background" else "active"
    safe_operation = _clean(operation_key)
    if not safe_user or not safe_operation:
        return {"status": "invalid", "bucket": safe_bucket}
    date = local_quota_date(now)
    if _offline_test_collections():
        return {
            "status": "reserved", "bucket": safe_bucket,
            "local_date": date, "limit": _limit_for(safe_bucket),
        }
    marker = {
        "user_id": safe_user,
        "bucket": safe_bucket,
        "local_date": date,
        "operation_key": safe_operation,
        "created_at": datetime.now(QUOTA_TIMEZONE).timestamp(),
    }
    try:
        try:
            MATCH_QUOTA_OPERATIONS.insert_one(marker)
        except DuplicateKeyError:
            return {
                "status": "already_reserved",
                "bucket": safe_bucket,
                "local_date": date,
                "limit": _limit_for(safe_bucket),
            }
        limit = _limit_for(safe_bucket)
        quota_key = {
            "user_id": safe_user,
            "bucket": safe_bucket,
            "local_date": date,
        }
        existing = MATCH_QUOTA_USAGE.find_one(quota_key, {"_id": 1, "used": 1})
        if not existing:
            try:
                MATCH_QUOTA_USAGE.insert_one({
                    **quota_key, "used": 1,
                    "created_at": datetime.now(QUOTA_TIMEZONE).timestamp(),
                    "updated_at": datetime.now(QUOTA_TIMEZONE).timestamp(),
                })
                result = type("Result", (), {"modified_count": 1, "upserted_id": True})()
            except DuplicateKeyError:
                # Another request created today's row.  Its guarded update is
                # the authoritative increment below.
                result = MATCH_QUOTA_USAGE.update_one(
                    {**quota_key, "used": {"$lt": limit}},
                    {
                        "$inc": {"used": 1},
                        "$set": {"updated_at": datetime.now(QUOTA_TIMEZONE).timestamp()},
                    },
                )
        else:
            result = MATCH_QUOTA_USAGE.update_one(
                {**quota_key, "used": {"$lt": limit}},
                {
                    "$inc": {"used": 1},
                    "$set": {"updated_at": datetime.now(QUOTA_TIMEZONE).timestamp()},
                },
            )
        if not getattr(result, "modified_count", 0) and not getattr(result, "upserted_id", None):
            MATCH_QUOTA_OPERATIONS.delete_one({
                "user_id": safe_user,
                "bucket": safe_bucket,
                "local_date": date,
                "operation_key": safe_operation,
            })
            return {
                "status": "exhausted",
                "bucket": safe_bucket,
                "local_date": date,
                "limit": limit,
            }
        return {
            "status": "reserved",
            "bucket": safe_bucket,
            "local_date": date,
            "limit": limit,
        }
    except Exception as exc:
        # The quota is a safety/product guard.  Do not silently continue when
        # its backing collection is unavailable.
        return {
            "status": "unavailable",
            "bucket": safe_bucket,
            "local_date": date,
            "error": type(exc).__name__,
        }


def release_daily_quota(
    user_id: str,
    *,
    bucket: str = "active",
    operation_key: str,
    now: datetime | None = None,
) -> bool:
    """Release a reservation after a proposal write failed."""
    safe_user = _clean(user_id, 120)
    safe_bucket = "background" if bucket == "background" else "active"
    date = local_quota_date(now)
    safe_operation = _clean(operation_key)
    if _offline_test_collections():
        return True
    try:
        marker = MATCH_QUOTA_OPERATIONS.delete_one({
            "user_id": safe_user,
            "bucket": safe_bucket,
            "local_date": date,
            "operation_key": safe_operation,
        })
        if not getattr(marker, "deleted_count", 0):
            return False
        result = MATCH_QUOTA_USAGE.update_one(
            {"user_id": safe_user, "bucket": safe_bucket, "local_date": date, "used": {"$gt": 0}},
            {"$inc": {"used": -1}},
        )
        return bool(getattr(result, "modified_count", 0))
    except Exception:
        return False


def daily_quota_status(user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Return the UI-safe usage and remaining counts for today's date."""
    date = local_quota_date(now)
    if _offline_test_collections():
        return {
            "status": "ok",
            "local_date": date,
            "timezone": "Asia/Taipei",
            "active": {"limit": ACTIVE_DAILY_LIMIT, "used": 0, "remaining": ACTIVE_DAILY_LIMIT},
            "background": {"limit": BACKGROUND_DAILY_LIMIT, "used": 0, "remaining": BACKGROUND_DAILY_LIMIT},
            "active_limit": ACTIVE_DAILY_LIMIT, "active_used": 0,
            "active_remaining": ACTIVE_DAILY_LIMIT,
            "background_limit": BACKGROUND_DAILY_LIMIT, "background_used": 0,
            "background_remaining": BACKGROUND_DAILY_LIMIT,
        }
    quota_read_error = False
    try:
        rows = list(MATCH_QUOTA_USAGE.find({
            "user_id": _clean(user_id, 120),
            "local_date": date,
            "bucket": {"$in": ["active", "background"]},
        }, {"_id": 0, "bucket": 1, "used": 1}))
    except Exception:
        rows = []
        quota_read_error = True
    if quota_read_error:
        return {
            "status": "unavailable",
            "local_date": date,
            "timezone": "Asia/Taipei",
            "active": {"limit": ACTIVE_DAILY_LIMIT, "used": None, "remaining": None},
            "background": {"limit": BACKGROUND_DAILY_LIMIT, "used": None, "remaining": None},
            "active_limit": ACTIVE_DAILY_LIMIT, "active_used": None,
            "active_remaining": None,
            "background_limit": BACKGROUND_DAILY_LIMIT, "background_used": None,
            "background_remaining": None,
        }
    used: dict[str, int] = {}
    for row in rows:
        try:
            value = max(0, int(row.get("used", 0) or 0))
        except (TypeError, ValueError):
            value = 0
        used[str(row.get("bucket") or "")] = value
    active_used = min(used.get("active", 0), ACTIVE_DAILY_LIMIT)
    background_used = min(used.get("background", 0), BACKGROUND_DAILY_LIMIT)
    return {
        "status": "ok",
        "local_date": date,
        "timezone": "Asia/Taipei",
        "active": {
            "limit": ACTIVE_DAILY_LIMIT,
            "used": active_used,
            "remaining": max(0, ACTIVE_DAILY_LIMIT - active_used),
        },
        "background": {
            "limit": BACKGROUND_DAILY_LIMIT,
            "used": background_used,
            "remaining": max(0, BACKGROUND_DAILY_LIMIT - background_used),
        },
        # Flat aliases make the contract easy for lightweight clients while
        # the nested objects remain the canonical shape.
        "active_limit": ACTIVE_DAILY_LIMIT,
        "active_used": active_used,
        "active_remaining": max(0, ACTIVE_DAILY_LIMIT - active_used),
        "background_limit": BACKGROUND_DAILY_LIMIT,
        "background_used": background_used,
        "background_remaining": max(0, BACKGROUND_DAILY_LIMIT - background_used),
    }
