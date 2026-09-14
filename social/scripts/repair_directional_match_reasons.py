"""Review-gated repair for live proposals created before directional_reason_v3.

Default is dry-run.  It only uses the proposal's creation-time snapshot and
never reads the participants' current profiles, conversation, or memories.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import matches_coll
from routers.match import build_directional_reason_v3_from_snapshot


def _anon(value: object) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:10]


def main() -> int:
    parser = argparse.ArgumentParser(description="Repair role-bound reasons on live match proposals")
    parser.add_argument("--apply", action="store_true", help="persist reviewed changes; default is dry-run")
    parser.add_argument("--limit", type=int, default=5, help="maximum anonymous examples")
    args = parser.parse_args()

    matched = repaired = skipped = 0
    examples: list[dict] = []
    cursor = matches_coll.find(
        {"status": {"$in": ["draft", "pending"]}},
        {"from_user": 1, "to_user": 1, "status": 1, "proposal_revision": 1,
         "match_context_snapshot": 1, "recommendation_tier": 1, "directional_reason_v3": 1},
    )
    for doc in cursor:
        if isinstance(doc.get("directional_reason_v3"), list) and len(doc["directional_reason_v3"]) == 2:
            continue
        matched += 1
        entries = build_directional_reason_v3_from_snapshot(doc)
        if len(entries) != 2:
            skipped += 1
            if len(examples) < max(0, args.limit):
                examples.append({"proposal": _anon(doc.get("_id")), "status": doc.get("status"), "outcome": "skipped_invalid_snapshot"})
            continue
        by_viewer = {entry.get("viewer_id"): entry for entry in entries}
        from_reason = str((by_viewer.get(doc.get("from_user")) or {}).get("viewer_text") or "")
        to_reason = str((by_viewer.get(doc.get("to_user")) or {}).get("viewer_text") or "")
        if not from_reason or not to_reason:
            skipped += 1
            continue
        if args.apply:
            result = matches_coll.update_one(
                {"_id": doc["_id"], "status": doc.get("status"), "proposal_revision": int(doc.get("proposal_revision", 0) or 0),
                 "directional_reason_v3": {"$exists": False}},
                {"$set": {
                    "directional_reason_v3": entries,
                    "reason_version": "v3",
                    # Legacy clients keep receiving their old field names, but
                    # the values now come from the canonical viewer bindings.
                    "reason": from_reason,
                    "receiver_reason": to_reason,
                    "directional_reason_v3_repaired_at": time.time(),
                }},
            )
            repaired += int(result.modified_count == 1)
        if len(examples) < max(0, args.limit):
            examples.append({"proposal": _anon(doc.get("_id")), "status": doc.get("status"), "outcome": "ready" if not args.apply else "applied"})
    print({"mode": "apply" if args.apply else "dry-run", "matched": matched, "repaired": repaired, "skipped": skipped, "examples": examples})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
