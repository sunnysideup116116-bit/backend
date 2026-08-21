"""Report memory-field coverage without printing user content or credentials."""

from __future__ import annotations

import os
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase
from pymongo import MongoClient


ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / "social_demotest" / ".env", override=False)


def present_count(items: list[dict], field: str) -> int:
    return sum(field in item and item.get(field) is not None for item in items)


def main() -> None:
    client = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=8_000)
    client.admin.command("ping")
    db = client["profiling_db"]

    profile_docs = list(db.profiles.find({}, {"_id": 0, "profile_memory_preview": 1}))
    preview_items = [
        item
        for profile in profile_docs
        for item in (profile.get("profile_memory_preview") or [])
        if isinstance(item, dict)
    ]
    outbox_docs = list(db.profile_memory_outbox.find({}, {"_id": 0}))
    outbox_items = [
        item
        for document in outbox_docs
        for item in (document.get("memories") or [])
        if isinstance(item, dict)
    ]
    skill_runs = list(
        db.profile_skill_runs.find(
            {},
            {
                "_id": 0,
                "memory": 1,
                "created_at": 1,
                "message_id": 1,
                "match_id": 1,
                "surface": 1,
            },
        )
    )
    matches = list(
        db.matches.find({}, {"_id": 0, "last_decision": 1, "state_history": 1})
    )

    fields = (
        "key",
        "label",
        "stance",
        "category",
        "confidence",
        "last_seen_at",
        "source",
        "reason",
        "match_id",
        "evidence_count",
        "first_seen_at",
        "active",
    )
    fact_docs = list(db.preference_facts.find({}, {"_id": 0}))
    print("mongo_ping=ok")
    print(
        "profiles",
        {
            "documents": len(profile_docs),
            "with_preview": sum(bool(doc.get("profile_memory_preview")) for doc in profile_docs),
            "preview_items": len(preview_items),
            "preview_max_per_user": max(
                (len(doc.get("profile_memory_preview") or []) for doc in profile_docs),
                default=0,
            ),
        },
    )
    print("preview_field_counts", {field: present_count(preview_items, field) for field in fields})
    fact_fields = (
        "user_id",
        "concept_key",
        "label",
        "stance",
        "category",
        "confidence",
        "active",
        "first_seen_at",
        "last_seen_at",
        "evidence_count",
        "last_source",
        "last_reason",
        "last_match_id",
    )
    print(
        "preference_facts",
        {
            "documents": len(fact_docs),
            "field_counts": {field: present_count(fact_docs, field) for field in fact_fields},
        },
    )
    print(
        "outbox",
        {
            "documents": len(outbox_docs),
            "items": len(outbox_items),
            "statuses": dict(Counter(doc.get("status") for doc in outbox_docs)),
        },
    )
    print("outbox_field_counts", {field: present_count(outbox_items, field) for field in fields})
    print(
        "profile_skill_runs",
        {
            "documents": len(skill_runs),
            "with_candidates": sum(
                bool((run.get("memory") or {}).get("candidate_count")) for run in skill_runs
            ),
            "with_saved": sum(
                bool((run.get("memory") or {}).get("saved_count")) for run in skill_runs
            ),
            "with_match_id": sum(bool(run.get("match_id")) for run in skill_runs),
        },
    )
    print(
        "matches",
        {
            "documents": len(matches),
            "last_decision_keys": sorted(
                {
                    key
                    for match in matches
                    for key in (match.get("last_decision") or {}).keys()
                }
            ),
            "history_events": sum(len(match.get("state_history") or []) for match in matches),
            "history_keys": sorted(
                {
                    key
                    for match in matches
                    for event in (match.get("state_history") or [])
                    if isinstance(event, dict)
                    for key in event.keys()
                }
            ),
        },
    )

    graph_env = __import__("dotenv").dotenv_values(ROOT / "matchmaker_agent" / ".env")
    with GraphDatabase.driver(
        graph_env.get("NEO4J_URI"),
        auth=(graph_env.get("NEO4J_USERNAME"), graph_env.get("NEO4J_PASSWORD")),
    ) as driver:
        with driver.session(database=graph_env.get("NEO4J_DATABASE", "neo4j")) as session:
            labels = {
                row["label"]: row["count"]
                for row in session.run(
                    "MATCH (n) UNWIND labels(n) AS label RETURN label,count(*) AS count ORDER BY label"
                )
            }
            relationships = {}
            for row in session.run("""
                MATCH ()-[r]->()
                RETURN type(r) AS type, count(*) AS count, collect(keys(r)) AS key_lists
                ORDER BY type
            """):
                relationships[row["type"]] = {
                    "count": row["count"],
                    "property_keys": sorted({key for keys in row["key_lists"] for key in keys}),
                }
            concept_properties = {
                row["property"]: row["count"]
                for row in session.run("""
                    MATCH (c:Concept) UNWIND keys(c) AS property
                    RETURN property,count(*) AS count ORDER BY property
                """)
            }
    print("neo4j_labels", labels)
    print("neo4j_relationships", relationships)
    print("neo4j_concept_property_counts", concept_properties)


if __name__ == "__main__":
    main()
