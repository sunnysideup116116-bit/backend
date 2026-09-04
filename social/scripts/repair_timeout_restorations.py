"""Auditable rollback of untouched automatic timeout restorations.

Default: emit a review manifest without writes. Applying a separately approved
manifest requires --apply, --manifest and an explicit backup directory. This
script is never called by server startup, GET routes, polling or workers.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from bson import ObjectId, json_util

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from services.proposal_namespace import RELATIONSHIP_MATCH_NAMESPACE, namespace_for_document


def eligible(document: dict[str, Any]) -> bool:
    last = document.get("last_decision") or {}
    history = document.get("state_history") or []
    return bool(
        namespace_for_document(document) == RELATIONSHIP_MATCH_NAMESPACE
        and document.get("status") in {"draft", "pending"}
        and last.get("action") == "proposal_timeout_reverted"
        and last.get("actor") == "system" and last.get("from") == "expired"
        and last.get("to") == document.get("status")
        and isinstance(last.get("at"), (int, float)) and last["at"] > 0
        and isinstance(history, list) and history and history[-1] == last
        and all(isinstance(item, dict) and float(item.get("at") or 0) <= last["at"] for item in history)
        and isinstance(document.get("proposal_revision"), int)
    )


def candidate(document: dict[str, Any]) -> dict[str, Any]:
    return {
        "match_id": str(document["_id"]), "expected_status": document["status"],
        "expected_revision": document["proposal_revision"],
        "expected_last_decision": document["last_decision"],
        "created_at": document.get("created_at"),
    }


def make_manifest(matches: Any, user_id: str) -> dict[str, Any]:
    rows = matches.find({
        "$or": [{"from_user": user_id}, {"to_user": user_id}],
        "status": {"$in": ["draft", "pending"]},
        "last_decision.action": "proposal_timeout_reverted",
    })
    return {"version": 1, "user_id": user_id, "generated_at": time.time(),
            "candidates": [candidate(row) for row in rows if eligible(row)]}


def _entry_key(entry: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def invalidate_choices(confirmations: Any, messages: Any, match_id: str) -> int:
    from services.ayue_agent.v3.confirmation import public_choice_projection
    count = 0
    for record in confirmations.find({
        "payload.match_id": match_id, "payload.proposal_namespace": RELATIONSHIP_MATCH_NAMESPACE,
        "status": {"$in": ["prepared", "pending"]},
    }):
        result = confirmations.update_one({"_id": record["_id"], "status": record["status"]}, {
            "$set": {"status": "superseded", "resolution_reason": "proposal_state_repaired", "resolved_at": time.time()},
        })
        if result.modified_count:
            count += 1
            projection = public_choice_projection({**record, "status": "superseded"})
            messages.update_many({"room_id": record.get("room_id"), "metadata.choice_prompt.id": str(record["_id"])}, {
                "$set": {"metadata.choice_prompt": projection},
            })
    return count


def apply_manifest(matches: Any, confirmations: Any, messages: Any, manifest: dict[str, Any], backup_dir: Path) -> list[dict[str, Any]]:
    if manifest.get("version") != 1 or not isinstance(manifest.get("user_id"), str) or not manifest["user_id"]:
        raise ValueError("invalid review manifest")
    entries = manifest.get("candidates")
    if not isinstance(entries, list):
        raise ValueError("manifest candidates must be a list")
    results = []
    for entry in entries:
        match_id = str(entry.get("match_id") or "")
        if not ObjectId.is_valid(match_id):
            raise ValueError("invalid manifest match id")
        key = _entry_key(entry)
        document = matches.find_one({"_id": ObjectId(match_id)})
        if not document or manifest["user_id"] not in {document.get("from_user"), document.get("to_user")}:
            results.append({"match_id": match_id, "status": "not_owned"})
            continue
        if (document.get("last_decision") or {}).get("repair_key") == key:
            count = invalidate_choices(confirmations, messages, match_id)
            results.append({"match_id": match_id, "status": "already_repaired", "invalidated_choices": count})
            continue
        if not eligible(document) or candidate(document) != entry:
            results.append({"match_id": match_id, "status": "stale_or_not_eligible"})
            continue
        backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        backup = backup_dir / f"{match_id}-{key}.json"
        raw = json_util.dumps(document, sort_keys=True, ensure_ascii=False)
        try:
            with backup.open("x", encoding="utf-8") as handle:
                os.chmod(backup, 0o600)
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            if backup.read_text(encoding="utf-8") != raw:
                raise ValueError("backup mismatch; refusing mutation")
        now = time.time()
        transition = {"from": document["status"], "to": "expired", "actor": "system",
                      "action": "legacy_timeout_restore_rolled_back", "repair_key": key, "at": now}
        result = matches.update_one({
            "_id": document["_id"], "status": entry["expected_status"],
            "proposal_revision": entry["expected_revision"], "last_decision": entry["expected_last_decision"],
            "state_history": document["state_history"],
        }, {"$set": {"status": "expired", "expired_reason": "legacy_timeout_restore_rolled_back",
                      "expired_at": now, "updated_at": now, "last_decision": transition},
            "$inc": {"proposal_revision": 1}, "$push": {"state_history": transition},
            "$unset": {"live_participants": ""}})
        if not result.modified_count:
            results.append({"match_id": match_id, "status": "cas_conflict"})
            continue
        count = invalidate_choices(confirmations, messages, match_id)
        results.append({"match_id": match_id, "status": "repaired", "backup": str(backup), "invalidated_choices": count})
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", help="Owner to inspect in default read-only mode")
    parser.add_argument("--manifest", type=Path, help="Separately reviewed manifest to apply")
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    from database import db
    if args.apply:
        if not args.manifest or not args.backup_dir:
            parser.error("--apply requires an approved --manifest and --backup-dir")
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        result = apply_manifest(db.matches, db.v3_pending_confirmations, db.messages, manifest, args.backup_dir)
    else:
        if not args.user_id or args.manifest or args.backup_dir:
            parser.error("dry-run requires --user-id only")
        result = make_manifest(db.matches, args.user_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
