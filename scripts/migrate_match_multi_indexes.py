#!/usr/bin/env python3
"""Audit and optionally switch the Match collection to multi-card indexes.

Run without ``--apply`` first.  It only reports existing live duplicates and
the legacy participant-wide index.  Applying drops that one legacy index and
creates the pair-scoped unique index; no proposal rows or messages are
rewritten.
"""

from __future__ import annotations

import argparse
from collections import Counter
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "social"))

from database import matches_coll
from services.proposal_namespace import namespace_for_document, participant_pair_key


PAIR_INDEX_NAME = "one_live_proposal_per_pair"
LEGACY_INDEX_NAME = "one_live_proposal_per_namespace_participant"


def _pair_index_compatible(indexes: dict) -> bool:
    spec = (indexes or {}).get(PAIR_INDEX_NAME) or {}
    return bool(spec.get("unique")) and list(spec.get("key") or []) == [
        ("live_pair_key", 1),
    ]


def _live_rows():
    return list(matches_coll.find({"status": {"$in": ["draft", "pending"]}}, {
        "_id": 1, "from_user": 1, "to_user": 1, "proposal_namespace": 1,
        "live_pair_key": 1,
    }))


def audit() -> dict:
    rows = _live_rows()
    missing_keys = sum(1 for row in rows if not row.get("live_pair_key"))
    pair_counts = Counter(
        str(row.get("live_pair_key") or "")
        or f"{namespace_for_document(row)}:{participant_pair_key(row.get('from_user'), row.get('to_user'))}"
        for row in rows
    )
    duplicates = {key: count for key, count in pair_counts.items() if key and count > 1}
    try:
        indexes = matches_coll.index_information()
    except Exception:
        indexes = {}
    return {
        "live_count": len(rows),
        "duplicate_pair_count": len(duplicates),
        "duplicate_pairs": duplicates,
        "legacy_index_present": LEGACY_INDEX_NAME in indexes,
        "pair_index_present": PAIR_INDEX_NAME in indexes,
        "pair_index_compatible": _pair_index_compatible(indexes),
        "missing_live_pair_key_count": missing_keys,
    }


def apply() -> dict:
    report = audit()
    if report["duplicate_pair_count"]:
        raise SystemExit(
            "Refusing to apply: resolve duplicate live pairs first: "
            + repr(report["duplicate_pairs"])
        )
    # Bind legacy rows to the same pair key used by new writes.  This updates
    # only the canonical proposal metadata; messages and proposal history are
    # untouched.
    for row in _live_rows():
        if row.get("live_pair_key"):
            continue
        pair = participant_pair_key(row.get("from_user"), row.get("to_user"))
        if pair:
            matches_coll.update_one(
                {"_id": row.get("_id")},
                {"$set": {
                    "live_pair_key": f"{namespace_for_document(row)}:{pair}",
                }},
            )
    indexes = matches_coll.index_information()
    if PAIR_INDEX_NAME in indexes and not _pair_index_compatible(indexes):
        raise SystemExit(
            f"Refusing to apply: existing {PAIR_INDEX_NAME} is not a unique live_pair_key index"
        )
    if PAIR_INDEX_NAME not in indexes:
        matches_coll.create_index(
            [("live_pair_key", 1)],
            unique=True,
            partialFilterExpression={
                "status": {"$in": ["draft", "pending"]},
                "live_pair_key": {"$exists": True},
            },
            name=PAIR_INDEX_NAME,
        )
    # Keep the participant-wide guard until the pair index is verified. This
    # avoids any interval where concurrent writers have no uniqueness guard.
    indexes = matches_coll.index_information()
    if not _pair_index_compatible(indexes):
        raise SystemExit("Refusing to apply: pair index verification failed")
    if LEGACY_INDEX_NAME in indexes:
        matches_coll.drop_index(LEGACY_INDEX_NAME)
    return audit()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="drop legacy index after a clean audit")
    args = parser.parse_args()
    print(apply() if args.apply else audit())
