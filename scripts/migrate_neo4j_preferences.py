"""Migrate Aura preference metadata to Mongo and simplify the graph schema.

The default mode is read-only. Pass ``--apply`` only after reviewing the
printed counts. User content is never printed.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
from pathlib import Path
from typing import Any

from dotenv import dotenv_values, load_dotenv
from neo4j import GraphDatabase
from pymongo import ASCENDING, MongoClient, UpdateOne


ROOT = Path(__file__).resolve().parents[1]


def concept_key(raw_key: Any, label: str) -> str:
    existing = str(raw_key or "").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{1,50}", existing):
        return existing
    normalized = re.sub(r"\s+", "", label.strip().lower())
    return "concept_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def stance_for(properties: dict[str, Any]) -> str:
    stance = str(properties.get("stance") or "").lower()
    if stance in {"like", "dislike", "require", "avoid"}:
        return stance
    return "avoid" if properties.get("type") == "DISLIKES_TRAIT" else "like"


def normalized_timestamp(value: Any, fallback: float) -> float:
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return fallback
    return timestamp / 1000 if timestamp > 100_000_000_000 else timestamp


def graph_config() -> tuple[str, tuple[str, str], str]:
    values = dotenv_values(ROOT / "matchmaker_agent" / ".env")
    return (
        str(values.get("NEO4J_URI") or ""),
        (str(values.get("NEO4J_USERNAME") or ""), str(values.get("NEO4J_PASSWORD") or "")),
        str(values.get("NEO4J_DATABASE") or "neo4j"),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="write Mongo backup and mutate Aura")
    args = parser.parse_args()

    load_dotenv(ROOT / "social_demotest" / ".env", override=False)
    mongo = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=8_000)
    mongo.admin.command("ping")
    db = mongo["profiling_db"]
    uri, auth, database = graph_config()

    with GraphDatabase.driver(uri, auth=auth) as driver:
        driver.verify_connectivity()
        with driver.session(database=database) as session:
            old_rows = [dict(row) for row in session.run("""
                MATCH (u:User)-[r:HAS_PREFERENCE]->(t:Trait)
                RETURN u.id AS user_id, elementId(t) AS trait_element_id,
                       properties(t) AS concept, properties(r) AS preference
            """)]
            user_property_counts = {
                row["property"]: row["count"]
                for row in session.run("""
                    MATCH (u:User)
                    UNWIND keys(u) AS property
                    RETURN property, count(*) AS count ORDER BY property
                """)
            }
            label_counts = {
                row["label"]: row["count"]
                for row in session.run("""
                    MATCH (n) UNWIND labels(n) AS label
                    RETURN label, count(*) AS count ORDER BY label
                """)
            }

    facts: list[dict[str, Any]] = []
    migration_time = time.time()
    for row in old_rows:
        node = row["concept"] or {}
        relation = row["preference"] or {}
        label = str(
            relation.get("display_label_zh_tw") or node.get("name") or node.get("key") or ""
        ).strip()[:40]
        if not row.get("user_id") or not label:
            continue
        stance = stance_for(relation)
        first_seen_at = normalized_timestamp(
            relation.get("first_seen_at"),
            normalized_timestamp(relation.get("updated_at"), migration_time),
        )
        last_seen_at = normalized_timestamp(
            relation.get("last_seen_at"),
            normalized_timestamp(relation.get("updated_at"), first_seen_at),
        )
        facts.append(
            {
                "user_id": row["user_id"],
                "trait_element_id": row["trait_element_id"],
                "concept_key": concept_key(node.get("key"), label),
                "label": label,
                "stance": stance,
                "category": str(node.get("category") or "lifestyle")[:30],
                "confidence": float(relation.get("confidence") or 0.7),
                "active": bool(relation.get("active", True)),
                "first_seen_at": first_seen_at,
                "last_seen_at": last_seen_at,
                "evidence_count": int(relation.get("evidence_count") or 1),
                "last_source": relation.get("source") or "neo4j_migration",
                "last_match_id": relation.get("match_id"),
                "last_reason": str(relation.get("reason") or "")[:240],
            }
        )

    profile_ids = set(db.profiles.distinct("user_id"))
    graph_user_ids = {fact["user_id"] for fact in facts}
    print("mode", "apply" if args.apply else "dry-run")
    print("graph_labels", label_counts)
    print("old_preference_relationships", len(old_rows))
    print("migratable_preference_facts", len(facts))
    print("graph_user_property_counts", user_property_counts)
    print("graph_preference_users_missing_from_mongo_profiles", len(graph_user_ids - profile_ids))
    print("mongo_existing_preference_facts", db.preference_facts.count_documents({}))

    if not args.apply:
        return

    now = time.time()
    db.preference_facts.create_index(
        [("user_id", ASCENDING), ("concept_key", ASCENDING)],
        unique=True,
        name="one_preference_per_user_concept",
    )
    db.preference_facts.create_index(
        [("user_id", ASCENDING), ("active", ASCENDING), ("last_seen_at", ASCENDING)],
        name="active_preferences_by_user",
    )
    operations = []
    for fact in facts:
        stored = {key: value for key, value in fact.items() if key != "trait_element_id"}
        stored["migrated_at"] = now
        operations.append(
            UpdateOne(
                {"user_id": fact["user_id"], "concept_key": fact["concept_key"]},
                {"$set": stored, "$setOnInsert": {"evidence_ids": []}},
                upsert=True,
            )
        )
    if operations:
        db.preference_facts.bulk_write(operations, ordered=False)

    stance_labels = {"dislike": "不喜歡", "avoid": "避免", "require": "需要", "like": "喜歡"}
    preview_users = 0
    for user_id in db.profiles.distinct("user_id"):
        rows = list(
            db.preference_facts.find(
                {"user_id": user_id, "active": True},
                {
                    "_id": 0,
                    "concept_key": 1,
                    "label": 1,
                    "stance": 1,
                    "category": 1,
                    "confidence": 1,
                    "last_seen_at": 1,
                },
            ).sort([("confidence", -1), ("last_seen_at", -1)]).limit(12)
        )
        if not rows:
            continue
        preview = [
            {
                "key": row.get("concept_key"),
                "label": row.get("label"),
                "stance": row.get("stance"),
                "category": row.get("category"),
                "confidence": row.get("confidence", 0.7),
                "last_seen_at": row.get("last_seen_at", 0),
            }
            for row in rows
        ]
        summary = "、".join(
            stance_labels.get(str(item.get("stance")), "喜歡") + str(item.get("label") or "")
            for item in preview[:8]
        )[:300]
        db.profiles.update_one(
            {"user_id": user_id},
            {"$set": {
                "profile_memory_preview": preview,
                "profile_memory_summary": summary,
                "profile_memory_synced_at": now,
            }},
        )
        preview_users += 1

    with GraphDatabase.driver(uri, auth=auth) as driver:
        with driver.session(database=database) as session:
            session.run(
                "CREATE CONSTRAINT concept_key IF NOT EXISTS FOR (c:Concept) REQUIRE c.key IS UNIQUE"
            ).consume()
            for fact in facts:
                session.run("""
                    MATCH (t) WHERE elementId(t)=$element_id
                    SET t:Concept, t.key=$key, t.label=$label
                    WITH t
                    MATCH (u:User {id:$user_id})-[old:HAS_PREFERENCE]->(t)
                    DELETE old
                    WITH u,t
                    FOREACH (_ IN CASE WHEN $relation='AVOIDS' AND $active THEN [1] ELSE [] END |
                        MERGE (u)-[:AVOIDS]->(t))
                    FOREACH (_ IN CASE WHEN $relation='PREFERS' AND $active THEN [1] ELSE [] END |
                        MERGE (u)-[:PREFERS]->(t))
                """,
                    element_id=fact["trait_element_id"],
                    key=fact["concept_key"],
                    label=fact["label"],
                    user_id=fact["user_id"],
                    relation="AVOIDS" if fact["stance"] in {"dislike", "avoid"} else "PREFERS",
                    active=fact["active"],
                ).consume()
            session.run("""
                MATCH (c:Concept)
                REMOVE c:Trait, c.name, c.category
            """).consume()
            session.run("MATCH (o:MemoryObservation) DETACH DELETE o").consume()
            session.run("DROP CONSTRAINT memory_observation_message_id IF EXISTS").consume()

            verification = session.run("""
                MATCH (u:User)-[r:PREFERS|AVOIDS]->(c:Concept)
                RETURN count(r) AS relationships,
                       count(DISTINCT c) AS concepts,
                       count(CASE WHEN size(keys(r))=0 THEN 1 END) AS property_free_relationships
            """).single()
            old_remaining = session.run(
                "MATCH ()-[r:HAS_PREFERENCE]->() RETURN count(r) AS count"
            ).single()["count"]
    print("mongo_preference_facts_after", db.preference_facts.count_documents({}))
    print("mongo_profile_previews_synced", preview_users)
    print("graph_verification", dict(verification) if verification else {})
    print("old_relationships_remaining", old_remaining)


if __name__ == "__main__":
    main()
