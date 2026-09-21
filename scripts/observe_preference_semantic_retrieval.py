#!/usr/bin/env python3
"""Run one explicit, read-only semantic observation outside live match latency."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "social"))

from services.preference_semantic_service import (
    PreferenceSemanticRetrievalError,
    semantic_observation_summary,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requester-user-id", required=True)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()
    try:
        result = semantic_observation_summary(
            args.requester_user_id,
            args.topic,
            excluded_user_ids=args.exclude,
        )
    except PreferenceSemanticRetrievalError as exc:
        result = {"status": "error", "error_code": exc.code}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())
