#!/usr/bin/env python3
"""Operator-only read-only count report. No labels/IDs/raw memory in output."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "social"))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, choices=range(1, 31), default=7)
    parser.add_argument("--limit", type=int, default=1000)
    args = parser.parse_args()
    if not 1 <= args.limit <= 5000:
        parser.error("limit must be 1..5000")
    from database import db
    from services.related_interest_telemetry import summarize
    from matchmaker_agent.related_interest_contract import POLICY
    query = {"related_interest_pilot.policy_version": POLICY,
             "created_at": {"$gte": time.time() - args.days * 86400}}
    try:
        jobs = list(db["match_search_jobs"].find(query,
            {"_id": 0, "related_interest_pilot": 1}).limit(args.limit+1))
        proposals = list(db["matches"].find(query,
            {"_id": 0, "related_interest_pilot": 1, "status": 1, "state_history.to": 1}).limit(args.limit+1))
        print(json.dumps({"status": "success", "days": args.days,
            "truncated": len(jobs) > args.limit or len(proposals) > args.limit,
            **summarize(jobs[:args.limit], proposals[:args.limit])}, ensure_ascii=False))
    except Exception:
        print(json.dumps({"status": "unavailable"})); return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
