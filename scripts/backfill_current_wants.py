"""Backfill active Mongo recent-context signals into Neo4j CURRENTLY_WANTS edges."""

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


def concept_key(label: str) -> str:
    normalized = re.sub(r"\s+", "", label.strip().lower())
    return "concept_" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def concepts_from_profile(profile: dict) -> list[dict[str, str]]:
    fields = ((profile.get("recent_context_state") or {}).get("fields") or {})
    concepts, seen = [], set()
    for field_name in ("activity", "destination"):
        label = re.sub(r"\s+", " ", str((fields.get(field_name) or {}).get("value") or "").strip())[:40]
        normalized = re.sub(r"\s+", "", label.lower())
        if not label or normalized in seen:
            continue
        seen.add(normalized)
        concepts.append({"key": concept_key(label), "label": label})
    return concepts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    load_dotenv(ROOT / "social" / ".env", override=False)
    mongo = MongoClient(os.environ["MONGO_URI"], serverSelectionTimeoutMS=8_000)
    mongo.admin.command("ping")
    profiles = list(mongo["profiling_db"].profiles.find(
        {},
        {"_id": 0, "user_id": 1, "recent_context_state": 1, "recent_context_expires_at": 1},
    ))
    now = time.time()
    projections = [
        {
            "user_id": profile.get("user_id"),
            "expires_at": float(profile.get("recent_context_expires_at") or 0),
            "concepts": concepts_from_profile(profile),
        }
        for profile in profiles
        if profile.get("user_id")
    ]
    active = [row for row in projections if row["expires_at"] > now and row["concepts"]]
    print("mode", "apply" if args.apply else "dry-run")
    print("profiles_scanned", len(projections))
    print("active_profiles_with_concepts", len(active))
    print("intent_edges_planned", sum(len(row["concepts"]) for row in active))
    if not args.apply:
        return

    graph_env = dotenv_values(ROOT / "matchmaker_agent" / ".env")
    with GraphDatabase.driver(
        graph_env.get("NEO4J_URI"),
        auth=(graph_env.get("NEO4J_USERNAME"), graph_env.get("NEO4J_PASSWORD")),
    ) as driver:
        with driver.session(database=graph_env.get("NEO4J_DATABASE", "neo4j")) as session:
            session.run("""
                MATCH ()-[intent:CURRENTLY_WANTS]->()
                DELETE intent
            """).consume()
            for row in active:
                session.run("MERGE (:User {id:$user_id})", user_id=row["user_id"]).consume()
                for item in row["concepts"]:
                    existing = session.run("""
                        MATCH (c:Concept)
                        WHERE toLower(c.label)=toLower($label)
                        RETURN c.key AS key LIMIT 1
                    """, label=item["label"]).single()
                    key = str(existing["key"] if existing else item["key"])
                    session.run("""
                        MATCH (u:User {id:$user_id})
                        MERGE (c:Concept {key:$key}) SET c.label=$label
                        MERGE (u)-[r:CURRENTLY_WANTS]->(c)
                        SET r.expires_at=$expires_at
                    """, user_id=row["user_id"], key=key, label=item["label"],
                         expires_at=row["expires_at"]).consume()
            verification = session.run("""
                MATCH ()-[r:CURRENTLY_WANTS]->(:Concept)
                RETURN count(r) AS edges,
                       count(CASE WHEN keys(r)=['expires_at'] THEN 1 END) AS minimal_edges
            """).single()
    print("graph_verification", dict(verification) if verification else {})


if __name__ == "__main__":
    main()
