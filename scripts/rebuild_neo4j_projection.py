"""Rebuild the compact Neo4j user projection from canonical Mongo data.

Dry-run is the default. Pass --apply to replace only User-owned graph data.
Event, GlobalRule, Agent, and their relationships are preserved.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import time
from pathlib import Path

from dotenv import dotenv_values, load_dotenv
from neo4j import GraphDatabase
from pymongo import MongoClient


ROOT = Path(__file__).resolve().parents[1]


def concept_key(label: str, existing: str = "") -> str:
    key = str(existing or "").strip().lower()
    if re.fullmatch(r"[a-z][a-z0-9_]{1,50}", key):
        return key
    normalized = re.sub(r"\s+", "", str(label or "").strip().lower())
    return "concept_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def context_concepts(profile: dict) -> list[dict]:
    fields = ((profile.get("recent_context_state") or {}).get("fields") or {})
    concepts, seen = [], set()
    for field_name in ("activity", "destination"):
        label = re.sub(r"\s+", " ", str((fields.get(field_name) or {}).get("value") or "").strip())[:40]
        normalized = re.sub(r"\s+", "", label.lower())
        if label and normalized not in seen:
            seen.add(normalized)
            concepts.append({"key": concept_key(label), "label": label})
    return concepts


def rebuild_projection_transaction(tx, profiles: list[dict], facts: list[dict], intents: list[dict]):
    """Replace only the Mongo-owned User projection in one atomic transaction."""
    user_ids = [str(row["user_id"]) for row in profiles if row.get("user_id")]
    intent_rows = [
        {
            "user_id": str(intent["user_id"]),
            "expires_at": float(intent["expires_at"]),
            **concept,
        }
        for intent in intents
        for concept in intent["concepts"]
    ]
    tx.run(
        """
        UNWIND $profiles AS profile
        MERGE (user:User {id:profile.user_id})
        SET user = {id:profile.user_id}
        """,
        profiles=[{"user_id": user_id} for user_id in user_ids],
    ).consume()
    tx.run(
        """
        MATCH (user:User)
        WHERE NOT user.id IN $user_ids
        DETACH DELETE user
        """,
        user_ids=user_ids,
    ).consume()
    tx.run(
        """
        MATCH (:User)-[relation:PREFERS|AVOIDS|CURRENTLY_WANTS]->()
        DELETE relation
        """
    ).consume()
    tx.run(
        """
        UNWIND $facts AS fact
        MATCH (user:User {id:fact.user_id})
        MERGE (concept:Concept {key:fact.key})
        SET concept.label=fact.label
        FOREACH (_ IN CASE WHEN fact.relation='PREFERS' THEN [1] ELSE [] END |
            MERGE (user)-[:PREFERS]->(concept))
        FOREACH (_ IN CASE WHEN fact.relation='AVOIDS' THEN [1] ELSE [] END |
            MERGE (user)-[:AVOIDS]->(concept))
        """,
        facts=facts,
    ).consume()
    tx.run(
        """
        UNWIND $intents AS intent
        MATCH (user:User {id:intent.user_id})
        MERGE (concept:Concept {key:intent.key})
        SET concept.label=intent.label
        MERGE (user)-[relation:CURRENTLY_WANTS]->(concept)
        SET relation.expires_at=intent.expires_at
        """,
        intents=intent_rows,
    ).consume()
    tx.run("MATCH (memory:MemoryObservation) DETACH DELETE memory").consume()
    tx.run("MATCH (trait:Trait) WHERE NOT (trait)--() DELETE trait").consume()
    tx.run("MATCH (concept:Concept) WHERE NOT (concept)--() DELETE concept").consume()
    verification = tx.run(
        """
        MATCH (user:User)
        OPTIONAL MATCH (user)-[relation:PREFERS|AVOIDS|CURRENTLY_WANTS]->(:Concept)
        RETURN count(DISTINCT user) AS users, count(relation) AS relations
        """
    ).single()
    result = dict(verification) if verification else {}
    if int(result.get("users", -1)) != len(user_ids):
        raise RuntimeError("Neo4j User projection verification failed; transaction rolled back")
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    load_dotenv(ROOT / "social_demotest" / ".env", override=False)
    mongo = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=8_000)
    mongo.admin.command("ping")
    db = mongo["profiling_db"]
    profiles = list(db.profiles.find(
        {}, {"_id": 0, "user_id": 1, "recent_context_state": 1, "recent_context_expires_at": 1},
    ))
    facts = list(db.preference_facts.find(
        {"active": True},
        {"_id": 0, "user_id": 1, "concept_key": 1, "label": 1, "stance": 1},
    ))
    now = time.time()
    current_user_ids = {str(profile.get("user_id")) for profile in profiles if profile.get("user_id")}
    intents = [
        {
            "user_id": profile.get("user_id"),
            "expires_at": float(profile.get("recent_context_expires_at") or 0),
            "concepts": context_concepts(profile),
        }
        for profile in profiles
        if profile.get("user_id")
        and float(profile.get("recent_context_expires_at") or 0) > now
        and context_concepts(profile)
    ]
    clean_facts = [
        {
            "user_id": str(row.get("user_id") or ""),
            "key": concept_key(str(row.get("label") or ""), str(row.get("concept_key") or "")),
            "label": str(row.get("label") or "").strip()[:40],
            "relation": "AVOIDS" if str(row.get("stance") or "").lower() in {"avoid", "dislike"} else "PREFERS",
        }
        for row in facts
        if str(row.get("user_id") or "") in current_user_ids
        and str(row.get("label") or "").strip()
    ]
    print("mode", "apply" if args.apply else "dry-run")
    print("profiles", len(profiles))
    print("active_preference_facts_total", len(facts))
    print("active_preference_relations_current_users", len(clean_facts))
    print("stale_preference_facts_skipped", len(facts) - len(clean_facts))
    print("active_context_users", len(intents))
    print("active_context_relations", sum(len(item["concepts"]) for item in intents))
    if not args.apply:
        return
    if not current_user_ids:
        raise RuntimeError("Refusing to rebuild Neo4j projection from an empty Mongo profile set")

    graph_env = dotenv_values(ROOT / "matchmaker_agent" / ".env")
    with GraphDatabase.driver(
        graph_env.get("NEO4J_URI"),
        auth=(graph_env.get("NEO4J_USERNAME"), graph_env.get("NEO4J_PASSWORD")),
    ) as driver:
        driver.verify_connectivity()
        with driver.session(database=graph_env.get("NEO4J_DATABASE", "neo4j")) as session:
            session.run(
                "CREATE CONSTRAINT concept_key IF NOT EXISTS FOR (c:Concept) REQUIRE c.key IS UNIQUE"
            ).consume()
            verification = session.execute_write(
                rebuild_projection_transaction, profiles, clean_facts, intents,
            )
    print("verification", verification)


if __name__ == "__main__":
    main()
