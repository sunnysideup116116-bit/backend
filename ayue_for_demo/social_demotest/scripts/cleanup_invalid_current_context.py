"""One-time, review-gated cleanup for legacy invalid recent-context summaries.

Default is dry-run.  Use --apply only after an approved review; this script never
rebuilds context from conversation and never touches durable memories.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from database import profiles_coll


INVALID_CONTEXT_RE = re.compile(
    r"(?:媒合|配對|提案|翻名單|等待(?:對方)?回覆|瞭解配對(?:物件|對象)|"
    r"seed_user|demo_user|user[_-]?\d+|match(?:ing)?|pending proposal)", re.IGNORECASE,
)


def anonymize(user_id: str) -> str:
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:10]


def main() -> int:
    parser = argparse.ArgumentParser(description="Dry-run cleanup for invalid current_context values")
    parser.add_argument("--apply", action="store_true", help="write the reviewed cleanup (default is dry-run)")
    parser.add_argument("--limit", type=int, default=5, help="anonymous examples to print")
    parser.add_argument("--user-id", help="review one explicit profile without baking an ID into cleanup rules")
    parser.add_argument("--expected-revision", type=int, help="required for an explicit --apply to avoid clearing a newer context")
    args = parser.parse_args()
    matched, examples = 0, []
    query = {"current_context": {"$type": "string", "$ne": ""}}
    if args.user_id:
        query["user_id"] = args.user_id
    cursor = profiles_coll.find(query, {"user_id": 1, "current_context": 1, "current_context_revision": 1})
    for profile in cursor:
        user_id = str(profile.get("user_id") or "")
        context = str(profile.get("current_context") or "").strip()
        explicit_target = bool(args.user_id and user_id == args.user_id)
        if not explicit_target and not INVALID_CONTEXT_RE.search(context):
            continue
        matched += 1
        if len(examples) < max(0, args.limit):
            examples.append({"user": anonymize(user_id), "context": context[:80]})
        if args.apply:
            if explicit_target and args.expected_revision is None:
                raise SystemExit("explicit --apply requires --expected-revision")
            revision = int(profile.get("current_context_revision", 0) or 0)
            if args.expected_revision is not None and revision != args.expected_revision:
                continue
            profiles_coll.update_one(
                {"_id": profile["_id"], "current_context": profile.get("current_context"), "current_context_revision": revision},
                {"$set": {
                    "current_context": "", "context_signals": {},
                    "recent_context_state": {"version": 2, "revision": revision + 1, "fields": {}},
                    "current_context_invalidated_reason": "dialog_domain_contamination" if explicit_target else "legacy_match_or_internal_state",
                    "current_context_invalidated_at": time.time(),
                }, "$inc": {"current_context_revision": 1}},
            )
    print({"mode": "apply" if args.apply else "dry-run", "matched": matched, "examples": examples})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
