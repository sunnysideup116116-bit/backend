#!/usr/bin/env python3
"""Audit registration Graph coverage; --apply queues verified accounts only.

Run from Server using the Social environment. Dry-run is the default. No events,
invitations, embeddings or raw profile details are emitted by this command.
"""
import argparse
from collections import Counter
import json
import logging
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "social"))


def unique_profile_ids(rows, counts):
    """Legacy Mongo may contain multiple profile documents for one account."""
    seen = set()
    result = []
    for item in rows:
        counts["mongo_profiles"] += 1
        user_id = str(item.get("user_id") or "").strip()
        if not user_id:
            counts["missing_user_id"] += 1
            continue
        if user_id in seen:
            counts["duplicate_profile_rows"] += 1
            continue
        seen.add(user_id)
        result.append(user_id)
    counts["unique_account_ids"] = len(result)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Queue after reviewing dry-run and restarting both services")
    args = parser.parse_args()
    from dotenv import dotenv_values, load_dotenv
    load_dotenv(ROOT / "social" / ".env", override=False)
    from database import profiles_coll
    from services.registration_graph_service import read_registration_profile, enqueue_registration_bootstrap, VERSION
    import requests
    from neo4j import GraphDatabase
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    if args.apply:
        response = requests.get("http://127.0.0.1:8000/api/registration-graph/status", timeout=5)
        response.raise_for_status()
        status = response.json()
        if status.get("version") != VERSION or status.get("worker_running") is not True:
            raise RuntimeError("restart_new_social_worker_before_apply")
        response = requests.get("http://127.0.0.1:9001/openapi.json", timeout=5)
        response.raise_for_status()
        if "/api/users/registration-projection" not in response.json().get("paths", {}):
            raise RuntimeError("restart_new_matchmaker_before_apply")
    config = dotenv_values(ROOT / "matchmaker_agent" / ".env")
    counts = Counter()
    with GraphDatabase.driver(config["NEO4J_URI"], auth=(
        config.get("NEO4J_USERNAME") or config.get("NEO4J_USER") or "neo4j", config["NEO4J_PASSWORD"]
    )) as driver, driver.session(database=config.get("NEO4J_DATABASE") or "neo4j") as session:
        # Complete the read-only audit before any enqueue, so unavailable stores
        # never result in a partially selected migration population.
        verified = []
        for user_id in unique_profile_ids(profiles_coll.find({}, {"user_id": 1}), counts):
            profile = read_registration_profile(user_id)
            if profile is None:
                counts["missing_appwrite_profile"] += 1
                continue
            row = session.run("""OPTIONAL MATCH (u:User {id:$user_id})
                OPTIONAL MATCH (u)-[r:PREFERS|AVOIDS|CURRENTLY_WANTS|MEMORY_DISABLED]->(:Concept)
                RETURN count(u)>0 AS exists,count(r)>0 AS has_memory,
                       count(u.registration_seed_finished_at)>0 AS seeded
            """, user_id=user_id).single()
            counts["verified_appwrite_accounts"] += 1
            counts["existing_graph_user" if row["exists"] else "missing_graph_user"] += 1
            if row["has_memory"] or row["seeded"]:
                counts["name_only_existing_memory"] += 1
            elif profile["interest"]:
                counts["interest_pending_semantic_validation"] += 1
            else:
                counts["name_only_empty_interest"] += 1
            verified.append(user_id)
        if args.apply:
            for user_id in verified:
                if not enqueue_registration_bootstrap(user_id):
                    raise RuntimeError("registration_enqueue_failed")
                if not enqueue_registration_bootstrap(user_id, name_revision="backfill"):
                    raise RuntimeError("registration_identity_enqueue_failed")
                counts["queued_accounts"] += 1
    print(json.dumps({"mode": "apply" if args.apply else "dry-run", "counts": dict(counts)}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        # Never expose credentials, response bodies or private interests.
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__}))
        raise SystemExit(1)
