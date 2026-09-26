#!/usr/bin/env python3
"""Operator-only read-only count report. No labels/IDs/raw memory in output."""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "social"))


def collect_report(db, cohort, *, since, limit):
    """Bounded read-only operator report; private IDs are only used for joins."""
    from services.related_interest_telemetry import summarize
    from matchmaker_agent.related_interest_contract import POLICY, ACCEPTED
    from pymongo import timeout
    query = {"related_interest_pilot.policy_version": POLICY, "created_at": {"$gte": since}}
    with timeout(5):
        jobs = list(db["match_search_jobs"].find(query, {
            "_id": 0, "user_id": 1, "related_interest_pilot": 1, "retrieval_diagnostics.qualified_exact_count": 1,
            "error_code": 1, "started_at": 1, "completed_at": 1,
        }).sort("created_at", -1).limit(limit+1).max_time_ms(3000))
        proposals = list(db["matches"].find(query, {
            "_id": 1, "from_user": 1, "to_user": 1, "related_interest_pilot": 1,
            "status": 1, "state_history.to": 1,
            "friend_intro_v4.initiator_preview.reason_render_mode": 1,
            "friend_intro_v4.receiver_invitation.reason_render_mode": 1,
        }).sort("created_at", -1).limit(limit+1).max_time_ms(3000))
        selected = [p for p in proposals[:limit] if p.get("from_user") in cohort and p.get("to_user") in cohort]
        openings = list(db["messages"].find({
            "metadata.event_type": "match_pair_opening",
            "metadata.match_id": {"$in": [str(p["_id"]) for p in selected]},
        }, {"_id": 0, "metadata.opening_outcome": 1}).limit(limit+1).max_time_ms(3000)) if selected else []
    # Do not silently hide out-of-cohort activity from the safety report.
    return {"status": "success", "since": since, "cohort_size": len(cohort),
        "truncated": any(len(rows) > limit for rows in (jobs, proposals, openings)),
        "safety_findings": {
            "outside_cohort_semantic_jobs": sum(j.get("user_id") not in cohort
                and (j.get("related_interest_pilot") or {}).get("triggered") is True for j in jobs[:limit]),
            "outside_cohort_proposals": len(proposals[:limit])-len(selected),
            "rejected_relation_proposals": sum(any(r not in ACCEPTED for r in
                (p.get("related_interest_pilot") or {}).get("relations", [])) for p in proposals[:limit]),
        },
        **summarize([j for j in jobs[:limit] if j.get("user_id") in cohort], selected, openings[:limit])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, choices=range(1, 31), default=7)
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--since", type=float, help="Pilot activation Unix timestamp; overrides --days")
    parser.add_argument("--cohort-file", type=Path, help="Private operator JSON array; otherwise use server cohort env")
    args = parser.parse_args()
    if not 1 <= args.limit <= 5000:
        parser.error("limit must be 1..5000")
    from matchmaker_agent.related_interest_canary import COHORT_ENV, parse_cohort
    try:
        raw = args.cohort_file.read_text() if args.cohort_file else os.getenv(COHORT_ENV, "")
        cohort = parse_cohort(raw)
    except (OSError, UnicodeError):
        parser.error("cohort configuration unavailable")
    if not cohort:
        parser.error("explicit valid server cohort required")
    since = args.since if args.since is not None else time.time() - args.days * 86400
    if not math.isfinite(since) or not 0 <= since <= time.time():
        parser.error("invalid start timestamp")
    try:
        from database import db
        print(json.dumps(collect_report(db, cohort, since=since, limit=args.limit), ensure_ascii=False))
    except Exception:
        print(json.dumps({"status": "unavailable"})); return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
