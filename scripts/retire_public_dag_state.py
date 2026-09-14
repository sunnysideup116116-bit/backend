#!/usr/bin/env python3
"""Dry-run, apply, or verify retirement of unfinished public DAG state."""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

from bson import ObjectId, json_util


SERVER_ROOT = Path(__file__).resolve().parents[1]
SOCIAL_ROOT = SERVER_ROOT / "social"
sys.path.insert(0, str(SOCIAL_ROOT))

from database import db  # noqa: E402


MIGRATION_ID = "retire-public-dag-v1"
TARGETS = (
    ("v3_pending_confirmations", ("prepared", "pending"), "expired"),
    ("v3_contact_selections", ("prepared", "pending"), "expired"),
    ("v3_operation_batches", ("active", "paused"), "cancelled"),
)


def _target_query(statuses: tuple[str, ...]) -> dict:
    return {
        "surface": "public_ayue",
        "source_engine": {"$ne": "pi"},
        "status": {"$in": list(statuses)},
    }


def _ambiguous_query(statuses: tuple[str, ...]) -> dict:
    return {
        "status": {"$in": list(statuses)},
        "$or": [
            {"surface": {"$exists": False}},
            {"surface": ""},
            {"source_engine": {"$exists": False}},
            {"source_engine": ""},
        ],
    }


def inspect_state() -> tuple[dict, list[dict]]:
    report: dict[str, dict[str, int]] = {}
    backup: list[dict] = []
    message_ids: set[str] = set()
    run_ids: set[str] = set()
    for name, statuses, _terminal in TARGETS:
        target_rows = list(db[name].find(_target_query(statuses)))
        ambiguous = list(db[name].find(_ambiguous_query(statuses)))
        executing = (
            db[name].count_documents({
                "surface": "public_ayue",
                "source_engine": {"$ne": "pi"},
                "status": "executing",
            }) if name == "v3_pending_confirmations" else 0
        )
        report[name] = {
            "retirable": len(target_rows),
            "ambiguous": len(ambiguous),
            "executing": int(executing),
        }
        backup.extend({"collection": name, "document": row} for row in target_rows)
        message_ids.update(
            str(row.get("presented_message_id") or "") for row in target_rows
            if row.get("presented_message_id")
        )
        run_ids.update(
            str(row.get("origin_run_id") or "") for row in target_rows
            if row.get("origin_run_id")
        )
    message_query: list[dict] = []
    if message_ids:
        values: list[object] = list(message_ids)
        values.extend(ObjectId(value) for value in message_ids if ObjectId.is_valid(value))
        message_query.extend([{"_id": {"$in": values}}, {"message_id": {"$in": list(message_ids)}}])
    if run_ids:
        message_query.append({"metadata.agent_run_id": {"$in": list(run_ids)}})
    if message_query:
        backup.extend(
            {"collection": "messages", "document": row}
            for row in db.messages.find({"$or": message_query})
        )
    return report, backup


def apply_retirement(backup_dir: Path) -> dict:
    report, backup = inspect_state()
    if any(item["ambiguous"] or item["executing"] for item in report.values()):
        raise RuntimeError("unresolved_or_executing_records")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"{MIGRATION_ID}-{uuid.uuid4().hex}.json"
    backup_path.write_text(json_util.dumps(backup, ensure_ascii=False, indent=2), encoding="utf-8")
    now = time.time()
    changed: dict[str, int] = {}
    for name, statuses, terminal in TARGETS:
        result = db[name].update_many(
            _target_query(statuses),
            {"$set": {
                "status": terminal,
                "resolved_at": now,
                "resolution_reason": "dag_retired",
                "migration_id": MIGRATION_ID,
            }},
        )
        changed[name] = int(result.modified_count)
    return {"migration_id": MIGRATION_ID, "backup": str(backup_path), "changed": changed}


def verify_state() -> dict:
    report, _backup = inspect_state()
    remaining = sum(item["retirable"] for item in report.values())
    protected_private = sum(
        db[name].count_documents({"surface": "private_ayue", "status": {"$in": list(statuses)}})
        for name, statuses, _terminal in TARGETS
    )
    return {"ok": remaining == 0, "remaining": remaining, "protected_private": protected_private, "details": report}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()
    if args.apply:
        if args.backup_dir is None:
            parser.error("--apply requires --backup-dir")
        result = apply_retirement(args.backup_dir)
    elif args.verify:
        result = verify_state()
    else:
        report, _backup = inspect_state()
        result = {"mode": "dry-run", "migration_id": MIGRATION_ID, "details": report}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not args.verify or result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
