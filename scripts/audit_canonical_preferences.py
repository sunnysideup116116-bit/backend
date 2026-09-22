#!/usr/bin/env python3
"""Read-only audit for alias normalization and conservative Concept splits.

This tool intentionally has no apply mode. It prints no user IDs and never
mutates Neo4j. Review its bounded proposal before designing a later migration.
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sys

from dotenv import dotenv_values
from neo4j import GraphDatabase


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "matchmaker_agent"))

from concept_identity import (  # noqa: E402
    canonicalize_concept_v1 as canonicalize_concept,
    PreferenceTextError,
    split_compound_concept_label,
)


def proposal_for_row(row: dict) -> dict:
    old_key = str(row.get("key") or "")
    old_label = str(row.get("label") or old_key)[:80]
    try:
        atoms = split_compound_concept_label(old_label)
    except PreferenceTextError:
        # This historical audit remains v1/read-only, never a v2 migration.
        atoms = []
    proposed = []
    for atom in atoms:
        identity = canonicalize_concept(atom)
        if identity:
            proposed.append({"key": identity.key, "label": identity.label})
    identity = canonicalize_concept(old_label, old_key)
    alias = (
        {"key": identity.key, "label": identity.label}
        if identity and identity.alias and identity.key != old_key and not proposed else None
    )
    return {
        "old_concept": {"key": old_key, "label": old_label},
        "proposed_atomic_concepts": proposed,
        "affected_user_count": int(row.get("users") or 0),
        "affected_edges": int(row.get("edges") or 0),
        "alias_normalization": alias,
        "auto_proposable": bool(proposed or alias),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    limit = max(1, min(args.limit, 500))
    values = dotenv_values(ROOT / "matchmaker_agent" / ".env")
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    with GraphDatabase.driver(
        str(values.get("NEO4J_URI") or ""),
        auth=(
            str(values.get("NEO4J_USERNAME") or values.get("NEO4J_USER") or "neo4j"),
            str(values.get("NEO4J_PASSWORD") or ""),
        ),
    ) as driver, driver.session(
        database=str(values.get("NEO4J_DATABASE") or "neo4j"),
    ) as session:
        constraints = [dict(row) for row in session.run("""
            SHOW CONSTRAINTS YIELD type,labelsOrTypes,properties
            WHERE 'Concept' IN labelsOrTypes AND properties=['key']
            RETURN type,labelsOrTypes,properties
        """)]
        rows = [dict(row) for row in session.run("""
            MATCH (concept:Concept)<-[relation:PREFERS|AVOIDS]-(user:User)
            WITH concept,count(DISTINCT user) AS users,count(relation) AS edges
            RETURN concept.key AS key,coalesce(concept.label,concept.key) AS label,
                   users,edges
            ORDER BY concept.key
            LIMIT $limit
        """, limit=limit)]
    proposals = [proposal_for_row(row) for row in rows]
    actionable = [item for item in proposals if item["auto_proposable"]]
    ambiguous = [
        item for item in proposals
        if any(separator in item["old_concept"]["label"] for separator in ("、", ",", "，"))
        and not item["proposed_atomic_concepts"]
    ]
    print(json.dumps({
        "mode": "dry-run",
        "concept_key_unique_constraint": bool(constraints),
        "scanned_concepts": len(rows),
        "actionable_count": len(actionable),
        "ambiguous_compound_count": len(ambiguous),
        "proposals": actionable,
        "ambiguous_compounds": ambiguous,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
