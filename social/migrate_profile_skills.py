"""Rebuild profile skills from owner-authored messages; dry-run by default."""

from __future__ import annotations

import argparse
from collections import Counter

from database import messages_coll, profiles_coll
from services.profile_skills import analyze_profile_message, process_profile_message


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--user-id", default="demo_user")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=30)
    args = parser.parse_args()
    if args.all and args.user_id != "demo_user":
        parser.error("use either --all or --user-id")
    users = [doc["user_id"] for doc in profiles_coll.find({}, {"user_id": 1}) if doc.get("user_id")] if args.all else [args.user_id]
    totals = Counter()
    for user_id in users:
        profile = profiles_coll.find_one({"user_id": user_id}, {"current_context": 1}) or {}
        messages = list(messages_coll.find({"sender_id": user_id}, {"content": 1}).sort("timestamp", -1).limit(max(1, min(args.limit, 100))))[::-1]
        if args.apply and profile.get("current_context"):
            profiles_coll.update_one({"user_id": user_id}, {"$push": {"legacy_context_archive": profile["current_context"]}})
        for index, message in enumerate(messages):
            message_id = f"migration-v1:{user_id}:{index}"
            if args.apply:
                result = process_profile_message(user_id, str(message.get("content") or ""), message_id, "migration")
                totals["contexts_applied"] += int(bool(result.get("recent_changed")))
                totals["memories_saved"] += len(result.get("saved_memories") or [])
            else:
                decision = analyze_profile_message(str(message.get("content") or ""), profile.get("current_context", ""))
                totals["contexts_candidate"] += int(bool(decision["recent_context"].get("should_update")))
                totals["memories_candidate"] += len(decision["memories"])
                totals.update(decision.get("memory_codes") or [])
        totals["users"] += 1
    print(dict(totals))


if __name__ == "__main__":
    main()
