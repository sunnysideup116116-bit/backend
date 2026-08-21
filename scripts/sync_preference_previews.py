"""Backfill profile preference previews from canonical Mongo preference facts."""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient


ROOT = Path(__file__).resolve().parents[1]
STANCE_LABELS = {"dislike": "不喜歡", "avoid": "避免", "require": "需要", "like": "喜歡"}


def normalized_timestamp(value: object, fallback: float) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return fallback
    return timestamp / 1000 if timestamp > 100_000_000_000 else timestamp


def main() -> None:
    load_dotenv(ROOT / "social_demotest" / ".env", override=False)
    client = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=8_000)
    client.admin.command("ping")
    db = client["profiling_db"]
    now = time.time()
    normalized = 0
    for fact in db.preference_facts.find(
        {}, {"_id": 1, "first_seen_at": 1, "last_seen_at": 1, "migrated_at": 1}
    ):
        fallback = normalized_timestamp(fact.get("migrated_at"), now)
        first_seen_at = normalized_timestamp(fact.get("first_seen_at"), fallback)
        last_seen_at = normalized_timestamp(fact.get("last_seen_at"), first_seen_at)
        db.preference_facts.update_one(
            {"_id": fact["_id"]},
            {"$set": {"first_seen_at": first_seen_at, "last_seen_at": last_seen_at}},
        )
        normalized += 1
    synced = 0
    for user_id in db.profiles.distinct("user_id"):
        rows = list(
            db.preference_facts.find(
                {"user_id": user_id, "active": True},
                {
                    "_id": 0,
                    "concept_key": 1,
                    "label": 1,
                    "stance": 1,
                    "category": 1,
                    "confidence": 1,
                    "last_seen_at": 1,
                },
            ).sort([("confidence", -1), ("last_seen_at", -1)]).limit(12)
        )
        if not rows:
            continue
        preview = [
            {
                "key": row.get("concept_key"),
                "label": row.get("label"),
                "stance": row.get("stance"),
                "category": row.get("category"),
                "confidence": row.get("confidence", 0.7),
                "last_seen_at": row.get("last_seen_at", 0),
            }
            for row in rows
        ]
        summary = "、".join(
            STANCE_LABELS.get(str(item.get("stance")), "喜歡") + str(item.get("label") or "")
            for item in preview[:8]
        )[:300]
        db.profiles.update_one(
            {"user_id": user_id},
            {"$set": {
                "profile_memory_preview": preview,
                "profile_memory_summary": summary,
                "profile_memory_synced_at": now,
            }},
        )
        synced += 1
    print("mongo_preference_timestamps_normalized", normalized)
    print("mongo_profile_previews_synced", synced)


if __name__ == "__main__":
    main()
