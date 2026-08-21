"""Inspect or process one bounded Concept-embedding backfill batch."""

from __future__ import annotations

import argparse
import requests


PENDING_URL = "http://127.0.0.1:9001/api/concepts/missing-embeddings"
REBUILD_URL = "http://127.0.0.1:8000/api/match/events/relevance/rebuild"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    limit = max(1, min(args.limit, 20))

    if not args.apply:
        response = requests.get(
            PENDING_URL,
            params={"limit": limit},
            timeout=(3, 20),
        )
        response.raise_for_status()
        payload = response.json()
        print("mode dry-run")
        print("pending_in_next_batch", int(payload.get("count", 0) or 0))
        print("No embeddings or Neo4j relationships were changed.")
        return 0

    response = requests.post(REBUILD_URL, timeout=(3, 90))
    response.raise_for_status()
    payload = response.json()
    print("mode apply")
    print("embedded_count", int(payload.get("embedded_count", 0) or 0))
    print("pending_count", int(payload.get("pending_count", 0) or 0))
    print("relevance_count", int(payload.get("relevance_count", 0) or 0))
    print("avoidance_count", int(payload.get("avoidance_count", 0) or 0))
    if payload.get("deferred"):
        print("deferred_retry_after", payload.get("retry_after"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
