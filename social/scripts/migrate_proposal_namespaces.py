"""Backfill proposal namespaces and replace the global live-proposal index.

Dry-run is the default.  Use ``--apply`` only during an explicit deployment
migration after the namespace-aware application code has been deployed.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from pymongo import UpdateOne


APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from database import db, matches_coll  # noqa: E402
from services.proposal_namespace import (  # noqa: E402
    LIVE_PROPOSAL_STATUSES,
    extend_match_status_validator,
    live_namespace_conflict_count,
    namespace_for_document,
    normalized_live_participants,
    participant_pair_key,
)


OLD_INDEX_NAME = "one_live_match_per_participant"
NEW_INDEX_NAME = "one_live_proposal_per_namespace_participant"


def collection_validation_options() -> dict:
    rows = list(db.list_collections(filter={"name": matches_coll.name}))
    return dict((rows[0].get("options") or {}) if rows else {})


def migrate_status_validator(*, apply: bool) -> dict[str, bool]:
    options = collection_validation_options()
    validator, changed, found = extend_match_status_validator(options.get("validator"))
    print(f"status_validator found={found} needs_expired={changed}")
    if apply and changed:
        command = {"collMod": matches_coll.name, "validator": validator}
        for key in ("validationLevel", "validationAction"):
            if key in options:
                command[key] = options[key]
        db.command(command)
        verified = collection_validation_options()
        _validator, still_changed, _found = extend_match_status_validator(
            verified.get("validator")
        )
        if still_changed:
            raise RuntimeError("matches validator did not retain the expired status")
        print("status_validator=updated")
    return {"found": found, "changed": changed}


def build_updates() -> tuple[list[UpdateOne], dict[str, int], list[dict]]:
    updates: list[UpdateOne] = []
    counts = {
        "documents": 0, "namespace": 0, "pair_key": 0,
        "live_participants": 0, "invalid_live": 0, "conflicts": 0,
    }
    projected_rows: list[dict] = []
    for row in matches_coll.find(
        {},
        {
            "_id": 1, "from_user": 1, "to_user": 1,
            "proposal_namespace": 1, "proposal_source": 1,
            "participant_pair_key": 1, "live_participants": 1, "status": 1,
        },
    ):
        counts["documents"] += 1
        namespace = namespace_for_document(row)
        pair_key = participant_pair_key(row.get("from_user"), row.get("to_user"))
        live_participants = normalized_live_participants(row)
        is_live = str(row.get("status") or "") in LIVE_PROPOSAL_STATUSES
        fields = {}
        if row.get("proposal_namespace") != namespace:
            fields["proposal_namespace"] = namespace
            counts["namespace"] += 1
        if pair_key and row.get("participant_pair_key") != pair_key:
            fields["participant_pair_key"] = pair_key
            counts["pair_key"] += 1
        if is_live and live_participants and row.get("live_participants") != live_participants:
            fields["live_participants"] = live_participants
            counts["live_participants"] += 1
        if is_live and not live_participants:
            counts["invalid_live"] += 1
        if fields:
            updates.append(UpdateOne({"_id": row["_id"]}, {"$set": fields}))
        projected_rows.append({**row, **fields})
    counts["conflicts"] = live_namespace_conflict_count(projected_rows)
    return updates, counts, projected_rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--apply", action="store_true",
        help="write the backfill and replace the old global live index",
    )
    args = parser.parse_args()
    updates, counts, _projected_rows = build_updates()
    print(
        "proposal namespace migration "
        f"documents={counts['documents']} namespace={counts['namespace']} "
        f"pair_key={counts['pair_key']} live_participants={counts['live_participants']} "
        f"invalid_live={counts['invalid_live']} conflicts={counts['conflicts']} "
        f"apply={args.apply}"
    )
    if counts["invalid_live"] or counts["conflicts"]:
        print("migration blocked; repair invalid or conflicting live proposals first")
        return 2
    migrate_status_validator(apply=args.apply)
    if not args.apply:
        print("dry-run only; rerun with --apply during the deployment migration")
        return 0
    if updates:
        result = matches_coll.bulk_write(updates, ordered=False)
        print(f"backfilled={result.modified_count}")
    created_index = matches_coll.create_index(
        [("proposal_namespace", 1), ("live_participants", 1)],
        unique=True,
        partialFilterExpression={"status": {"$in": ["draft", "pending"]}},
        name=NEW_INDEX_NAME,
    )
    indexes = matches_coll.index_information()
    if created_index != NEW_INDEX_NAME or NEW_INDEX_NAME not in indexes:
        print("migration blocked; new live proposal index was not verified")
        return 3
    if OLD_INDEX_NAME in indexes:
        matches_coll.drop_index(OLD_INDEX_NAME)
        print(f"dropped_index={OLD_INDEX_NAME}")
    print(f"active_index={NEW_INDEX_NAME}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
