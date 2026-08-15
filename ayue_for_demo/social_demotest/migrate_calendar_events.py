"""Idempotently backfill legacy established_dates records into calendar_events.

Run manually after deployment. Records without an exact date, start time and end
time are intentionally skipped rather than inventing a calendar appointment.
"""

from datetime import datetime, timezone
from uuid import uuid4

from database import calendar_events_coll, matches_coll
from services.calendar_service import _parse_local_interval, ensure_calendar_indexes, normalize_form


def migrate() -> tuple[int, int]:
    ensure_calendar_indexes()
    migrated = 0
    skipped = 0
    for match in matches_coll.find({"established_dates.0": {"$exists": True}}):
        participants = [match.get("from_user"), match.get("to_user")]
        if not all(participants):
            skipped += 1
            continue
        for index, legacy in enumerate(match.get("established_dates", [])):
            form = normalize_form(legacy.get("form", {}))
            try:
                start_at, end_at, zone_name = _parse_local_interval(form)
            except Exception:
                skipped += 1
                continue
            completed_at = legacy.get("completed_at", index)
            coordination_id = f"legacy:{match['_id']}:{completed_at}"
            now = datetime.now(timezone.utc)
            event = {
                "event_id": uuid4().hex,
                "source_type": "date",
                "coordination_id": coordination_id,
                "match_id": str(match["_id"]),
                "participants": participants,
                "title": "與對方的約會",
                "start_at": start_at,
                "end_at": end_at,
                "timezone": zone_name,
                "activity": form["activity"],
                "location": form["location"],
                "budget": form["budget"],
                "notes": form["notes"],
                "status": "confirmed",
                "revision": 1,
                "updated_at": now,
            }
            result = calendar_events_coll.update_one(
                {"coordination_id": coordination_id},
                {"$setOnInsert": {**event, "created_at": now}},
                upsert=True,
            )
            migrated += int(bool(result.upserted_id))
    return migrated, skipped


if __name__ == "__main__":
    count, invalid = migrate()
    print(f"Migrated {count} legacy calendar events; skipped {invalid} incomplete records.")
