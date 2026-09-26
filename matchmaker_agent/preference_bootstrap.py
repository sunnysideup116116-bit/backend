"""Owner-scoped Neo4j bootstrap transactions; no Mongo/distributed-ACID claim."""
import json
import time
import uuid

from neo4j import unit_of_work
from neo4j.exceptions import TransientError

from .concept_identity import stored_concept_identity, is_v2_preference_key, FRESH_PREFERENCE_NORMALIZATION_POLICY
from .preference_bootstrap_contract import (
    BootstrapError, POLICY, MAX_SNAPSHOT, MAX_BYTES, PREVIEW_TTL, digest, encoded,
    enabled, normalize_items, open_preview, seal_preview, require, legacy_compound_source,
    is_compound_item,
)
from .preference_write_fence import lock_preferences, bump_preferences
from .preference_mutations import write_preference_edges

RELATIONS = {"PREFERS", "AVOIDS", "MEMORY_DISABLED", "CURRENTLY_WANTS"}
CONCEPT_FIELDS = ("key", "label", "semantic_text", "display_label", "canonicalization_version",
                  "semantic_input_hash", "fidelity_status", "kind")


def require_schema(tx):
    rows = list(tx.run("SHOW CONSTRAINTS YIELD labelsOrTypes,properties,type RETURN labelsOrTypes,properties,type"))
    present = {(tuple(row["labelsOrTypes"]), tuple(row["properties"])) for row in rows
               if "UNIQUENESS" in str(row["type"]) or str(row["type"]) == "NODE_KEY"}
    require({(("User",), ("id",)), (("Concept",), ("key",)),
             (("PreferenceBootstrapOperation",), ("id",))} <= present,
            "bootstrap_schema_not_ready", 503)


def snapshot(tx, owner):
    users = list(tx.run("""MATCH (u:User {id:$owner})
        RETURN coalesce(u.preference_revision,0) AS revision,
               coalesce(u.preference_epoch,0) AS epoch,
               coalesce(u.preference_epoch_at,0) AS epoch_at,
               u.preference_projection_pending AS pending LIMIT 2""", owner=owner))
    require(len(users) == 1, "preference_owner_missing_or_duplicate")
    rows = list(tx.run("""MATCH (u:User {id:$owner})
        -[r:PREFERS|AVOIDS|MEMORY_DISABLED|CURRENTLY_WANTS]->(c:Concept)
        RETURN elementId(r) AS id,type(r) AS relation,properties(r) AS properties,
            c{.key,.label,.semantic_text,.display_label,.canonicalization_version,
              .semantic_input_hash,.fidelity_status} AS concept
        ORDER BY id LIMIT $limit""", owner=owner, limit=MAX_SNAPSHOT + 1))
    require(len(rows) <= MAX_SNAPSHOT, "bootstrap_inventory_overflow", 422)
    result = {"owner": owner, "revision": users[0]["revision"], "epoch": users[0]["epoch"], "epoch_at": users[0]["epoch_at"],
              "rows": [dict(row) for row in rows]}
    # Reject unsupported Neo4j property types rather than lossy stringify.
    require(len(encoded(result)) < MAX_BYTES // 5, "bootstrap_inventory_too_large", 413)
    return result, users[0]["pending"]


def concepts_for(tx, keys):
    rows = list(tx.run("""UNWIND $keys AS key MATCH (c:Concept {key:key})
        RETURN c{.key,.label,.semantic_text,.display_label,.canonicalization_version,
                 .semantic_input_hash,.fidelity_status,.kind} AS concept""", keys=keys))
    result = {}
    for row in rows:
        data = dict(row["concept"])
        require(data["key"] not in result, "duplicate_concept_key")
        require(stored_concept_identity(data), "preference_identity_conflict", 422)
        result[data["key"]] = data
    return result


def build_plan(tx, owner, mode, items):
    require(mode in {"add_only", "complete_set"}, "invalid_bootstrap_mode", 422)
    before, pending = snapshot(tx, owner)
    require(not pending, "preference_projection_pending")
    compound_sources = []
    for item in items:
        text = item["semantic_text"]
        compound = is_compound_item(text)
        if compound or "legacy_compound_source" in item:
            require(compound and stored_concept_identity(item), "legacy_compound_binding_invalid", 422)
            expected = legacy_compound_source(before, owner, mode, text, item["relation"])
            require(item.get("legacy_compound_source") == expected, "legacy_compound_binding_invalid", 422)
            compound_sources.append(expected)
    current = concepts_for(tx, [item["key"] for item in items])
    proposed = {(item["key"], item["relation"]) for item in items}
    retired, keep, new_edges = [], [], []
    for row in before["rows"]:
        concept = row["concept"]
        if row["relation"] not in {"PREFERS", "AVOIDS"}:
            keep.append(row["id"])
            continue
        identity = stored_concept_identity(concept)
        require(identity or not (concept.get("canonicalization_version") == "v2"
                                or is_v2_preference_key(concept.get("key"))), "invalid_v2_not_legacy", 422)
        if identity:
            require(mode != "complete_set" or (identity.key, row["relation"]) in proposed,
                    "complete_set_omits_or_reverses_v2", 422)
            keep.append(row["id"])
        elif mode == "complete_set":
            retired.append(row)
        else:
            keep.append(row["id"])
    for item in items:
        active = [r for r in before["rows"] if r["concept"]["key"] == item["key"]
                  and r["relation"] in {"PREFERS", "AVOIDS", "CURRENTLY_WANTS"}]
        require(all(r["relation"] == item["relation"] for r in active), "existing_polarity_or_context_conflict", 422)
        require(len(active) <= 1, "duplicate_owner_association", 422)
        if not active:
            new_edges.append({"key": item["key"], "relation": item["relation"]})
    require(len({s["relationship_id"] for s in compound_sources}) == len(compound_sources),
            "legacy_compound_binding_invalid", 422)
    for source in compound_sources:
        matches = [r for r in retired if r["id"] == source["relationship_id"]]
        require(len(matches) == 1 and digest(matches[0]) == source["association_hash"],
                "legacy_compound_retirement_mismatch", 422)
    plan = {"owner_revision": before["revision"], "snapshot_hash": digest(before), "items": items,
            "create_concepts": [i for i in items if i["key"] not in current],
            "reuse_concepts": list(current.values()), "create_edges": new_edges,
            "retire_edges": retired, "keep_edge_ids": keep,
            "concept_preconditions": {i["key"]: current.get(i["key"]) for i in items}}
    return plan, before


def preview_plan(tx, owner, mode, prefers, avoids):
    # Read and validate provenance in the same bounded preview transaction.
    # build_plan rechecks the exact locator/snapshot; a concurrent change cannot
    # transfer an exemption to a different relationship or preference set.
    before, pending = snapshot(tx, owner)
    require(not pending, "preference_projection_pending")
    items = normalize_items(prefers, avoids, mode=mode, legacy_snapshot=before, owner=owner)
    return build_plan(tx, owner, mode, items)


def check_compound_retirement(payload, plan):
    if any("legacy_compound_source" in item for item in plan["items"]):
        require(payload.get("retire_edges") == plan["retire_edges"],
                "legacy_compound_retirement_mismatch", 422)


def journal(tx, owner, op_id):
    row = tx.run("""MATCH (o:PreferenceBootstrapOperation {id:$key,owner_user_id:$owner})
        RETURN o.record AS record""", key=digest([owner, op_id]), owner=owner).single()
    if not row:
        return None
    data = json.loads(row["record"])
    require(data["owner"] == owner and data["operation_id"] == op_id, "invalid_operation_journal")
    return data


def save_journal(tx, record):
    tx.run("""MATCH (u:User {id:$owner})
        MERGE (o:PreferenceBootstrapOperation {id:$key})
        SET o.owner_user_id=$owner,o.operation_id=$op_id,o.record=$record
        MERGE (u)-[:PREFERENCE_BOOTSTRAP_OPERATION]->(o)
    """, owner=record["owner"], op_id=record["operation_id"], key=digest([record["owner"], record["operation_id"]]),
           record=encoded(record).decode()).consume()


def _consent(mode, consent):
    require(consent.get("confirm_items") is True, "preferences_not_confirmed", 422)
    if mode == "complete_set":
        require(all(consent.get(key) is True for key in (
            "complete_set", "reviewed_prefers", "reviewed_avoids", "retire_legacy")),
            "complete_set_consent_required", 422)


class BootstrapGraph:
    def __init__(self, driver, database="neo4j"):
        self.driver, self.database = driver, database

    def read(self, fn, *args):
        with self.driver.session(database=self.database, default_access_mode="READ") as session:
            return session.execute_read(unit_of_work(timeout=8)(fn), *args)

    def write(self, fn, *args):
        # Neo4j may abort a node-lock/relationship-delete deadlock victim.
        # Retry ONLY that known-rolled-back transaction, once, within the same
        # total budget. Never retry an ambiguous commit/transport outcome.
        deadline = time.monotonic() + 8
        for attempt in range(2):
            remaining = deadline - time.monotonic()
            require(remaining > 0, "bootstrap_transaction_deadline", 503)
            try:
                with self.driver.session(database=self.database) as session:
                    with session.begin_transaction(timeout=remaining) as tx:
                        result = fn(tx, *args)
                        tx.commit()
                        return result
            except TransientError as exc:
                if attempt or exc.code != "Neo.TransientError.Transaction.DeadlockDetected":
                    raise

    def preview(self, owner, mode, prefers, avoids, mongo_snapshot_hash=None):
        require(enabled(), "preference_bootstrap_disabled", 503)
        self.read(require_schema)
        plan, before = self.read(preview_plan, owner, mode, prefers, avoids)
        payload = {"policy": POLICY, "normalization_policy": FRESH_PREFERENCE_NORMALIZATION_POLICY,
                   "owner": owner, "mode": mode, "preview_id": str(uuid.uuid4()),
                   "expires_at": time.time() + PREVIEW_TTL, "plan_hash": digest(plan),
                   "mongo_snapshot_hash": mongo_snapshot_hash or digest([]), **plan}
        # Full edge before-images are inside the signed private receipt and
        # journal, not logs or any generative model input.
        return {**payload, "active_edges": [r for r in before["rows"] if r["relation"] in {"PREFERS", "AVOIDS"}],
                "preview_token": seal_preview(payload),
                "untouched": ["other owners", "legacy Concept identity", "inactive memories", "recent context",
                              "non-memory profile fields", "chat/history/proposals", "embeddings/indexes/flags"]}

    def check(self, owner, token):
        payload = open_preview(token, owner)
        def check(tx):
            current, _ = snapshot(tx, owner)
            require(current["revision"] == payload["owner_revision"]
                    and digest(current) == payload["snapshot_hash"], "stale_preview")
            plan, _ = build_plan(tx, owner, payload["mode"], payload["items"])
            require(digest(plan) == payload["plan_hash"], "stale_preview")
            check_compound_retirement(payload, plan)
        self.read(check)
        return payload

    def status(self, owner, op_id):
        return self.read(journal, owner, op_id)

    def commit(self, owner, token, consent):
        payload = open_preview(token, owner, allow_expired=True)
        _consent(payload["mode"], consent)
        prior = self.status(owner, payload["preview_id"])
        if prior:
            require(prior["plan_hash"] == payload["plan_hash"], "operation_payload_conflict")
            return prior
        self.read(require_schema)
        return self.write(self._commit, owner, payload, consent)

    def _commit(self, tx, owner, payload, consent):
        op_id = payload["preview_id"]
        # Owner lock first, before audit/Concept-index reads. Read-only
        # idempotency lookup is a separate transaction, avoiding lock upgrades.
        state = lock_preferences(tx, owner, bootstrap_operation=op_id, require_existing=True)
        prior = journal(tx, owner, op_id)  # Second concurrent identical request.
        if prior:
            require(prior["plan_hash"] == payload["plan_hash"], "operation_payload_conflict")
            return prior
        require(enabled(), "preference_bootstrap_disabled", 503)
        require(time.time() < payload["expires_at"], "preview_expired")
        require(state["revision"] == payload["owner_revision"], "stale_preview")
        plan, before = build_plan(tx, owner, payload["mode"], payload["items"])
        require(digest(plan) == payload["plan_hash"], "stale_preview")
        check_compound_retirement(payload, plan)
        created = []
        for item in plan["create_concepts"]:
            props = {k: item[k] for k in CONCEPT_FIELDS if k in item}
            props.update(kind="preference", bootstrap_created_by=op_id)
            row = tx.run("""MERGE (c:Concept {key:$key}) ON CREATE SET c += $props
                RETURN c.bootstrap_created_by AS creator""", key=item["key"], props=props).single()
            require(row and row["creator"] == op_id, "concept_creation_race")
            created.append(props)
        new_keys = {item["key"] for item in plan["create_edges"]}
        memories = [{**item, "stance": "like" if item["relation"] == "PREFERS" else "avoid"}
                    for item in payload["items"] if item["key"] in new_keys]
        if memories:
            write_preference_edges(tx, owner, memories)
        created_edges, retired = [], []
        for item in plan["create_edges"]:
            rows = list(tx.run(f"""MATCH (u:User {{id:$owner}})-[r:{item['relation']}]->(c:Concept {{key:$key}})
                SET r.bootstrap_operation=$op_id
                RETURN elementId(r) AS id,type(r) AS relation,c.key AS key,properties(r) AS properties""",
                owner=owner, key=item["key"], op_id=op_id))
            require(len(rows) == 1, "bootstrap_edge_count_mismatch")
            created_edges.append(dict(rows[0]))
        for edge in plan["retire_edges"]:
            row = tx.run("""MATCH (u:User {id:$owner})-[r:PREFERS|AVOIDS]->(c:Concept {key:$key})
                WHERE elementId(r)=$edge_id
                CREATE (u)-[archived:PREFERENCE_SUPERSEDED]->(c)
                SET archived.operation_id=$op_id,archived.retired_id=$edge_id,
                    archived.original_relation=$relation,archived.properties_json=$properties
                DELETE r RETURN elementId(archived) AS archive_id""", owner=owner,
                key=edge["concept"]["key"], edge_id=edge["id"], op_id=op_id,
                relation=edge["relation"], properties=encoded(edge["properties"]).decode()).single()
            require(row is not None, "retirement_snapshot_changed")
            retired.append({**edge, "archive_id": row["archive_id"]})
        bump_preferences(tx, owner)
        tx.run("""MATCH (u:User {id:$owner}) SET u.preference_projection_pending=$op_id
            FOREACH (_ IN CASE WHEN $complete THEN [1] ELSE [] END |
                SET u.preference_epoch=coalesce(u.preference_epoch,0)+1,u.preference_epoch_at=$now)
            """, owner=owner, now=time.time(), op_id=op_id, complete=payload["mode"] == "complete_set").consume()
        after, _ = snapshot(tx, owner)
        record = {"operation_id": op_id, "owner": owner, "mode": payload["mode"], "plan_hash": payload["plan_hash"],
                  "mongo_snapshot_hash": payload["mongo_snapshot_hash"],
                  "status": "committed", "projection_status": "pending", "confirmation": consent,
                  "before": before, "after": after, "effect": after,
                  "revision_before": state["revision"], "revision_after": after["revision"],
                  "created_concepts": created, "reused_concepts": plan["reuse_concepts"],
                  "created_edges": created_edges, "retired_edges": retired, "created_at": time.time()}
        sources = [item["legacy_compound_source"] for item in plan["items"] if "legacy_compound_source" in item]
        if sources:
            record["legacy_compound_sources"] = sources
        save_journal(tx, record)
        return record

    def resolve(self, owner, op_id, plan_hash, repair_projection=False):
        """Reconcile an ambiguous HTTP outcome, never replay the preference write.

        If original commit is waiting on this owner lock, an aborted tombstone
        prevents it from committing after a 'not found' response was observed.
        """
        def resolve(tx):
            existing = journal(tx, owner, op_id)
            if existing:
                require(existing["plan_hash"] == plan_hash, "operation_payload_conflict")
                if repair_projection and existing["projection_status"] == "synced":
                    lock_preferences(tx, owner, bootstrap_operation=op_id, require_existing=True)
                    existing = journal(tx, owner, op_id)
                    existing["effect"] = snapshot(tx, owner)[0]
                    existing["projection_status"] = "pending"
                    tx.run("MATCH (u:User {id:$owner}) SET u.preference_projection_pending=$op_id", owner=owner, op_id=op_id).consume()
                    save_journal(tx, existing)
                return existing
            lock_preferences(tx, owner, bootstrap_operation=op_id, require_existing=True)
            existing = journal(tx, owner, op_id)
            if existing:
                require(existing["plan_hash"] == plan_hash, "operation_payload_conflict")
                return existing
            current, _ = snapshot(tx, owner)
            record = {"operation_id": op_id, "owner": owner, "mode": "uncommitted",
                      "plan_hash": plan_hash, "status": "aborted", "projection_status": "pending",
                      "effect": current, "revision_before": current["revision"], "revision_after": current["revision"],
                      "created_concepts": [], "reused_concepts": [], "created_edges": [], "retired_edges": []}
            tx.run("MATCH (u:User {id:$owner}) SET u.preference_projection_pending=$op_id", owner=owner, op_id=op_id).consume()
            save_journal(tx, record)
            return record
        return self.write(resolve)

    def check_rollback(self, owner, op_id, revision):
        def check(tx):
            record = journal(tx, owner, op_id)
            require(record is not None, "operation_not_found", 404)
            if record["status"] == "rolled_back":
                return record
            current, pending = snapshot(tx, owner)
            require(record["status"] == "committed" and revision == record["revision_after"]
                    and current["revision"] == revision, "stale_rollback")
            require(not pending or pending == op_id, "preference_projection_pending")
            require(digest(current) == digest(record["after"]), "rollback_after_image_changed")
            return record
        return self.read(check)

    def acknowledge(self, owner, op_id, revision):
        def ack(tx):
            record = journal(tx, owner, op_id)
            require(record is not None, "operation_not_found", 404)
            if record["projection_status"] == "synced":
                return record
            lock_preferences(tx, owner, expected_revision=revision, bootstrap_operation=op_id, require_existing=True)
            require(record["effect"]["revision"] == revision, "projection_revision_mismatch")
            tx.run("MATCH (u:User {id:$owner}) WHERE u.preference_projection_pending=$op_id REMOVE u.preference_projection_pending",
                   owner=owner, op_id=op_id).consume()
            record["projection_status"] = "synced"
            save_journal(tx, record)
            return record
        return self.write(ack)

    def rollback(self, owner, op_id, expected_revision):
        def rollback(tx):
            record = journal(tx, owner, op_id)
            require(record is not None, "operation_not_found", 404)
            if record["status"] == "rolled_back":
                return record
            require(record["status"] == "committed", "operation_not_committed")
            require(expected_revision == record["revision_after"], "rollback_revision_mismatch")
            lock_preferences(tx, owner, expected_revision=record["revision_after"], bootstrap_operation=op_id, require_existing=True)
            current, _ = snapshot(tx, owner)
            require(digest(current) == digest(record["after"]), "rollback_after_image_changed")
            for edge in record["created_edges"]:
                row = tx.run("""MATCH (u:User {id:$owner})-[r:PREFERS|AVOIDS]->(c:Concept {key:$key})
                    WHERE elementId(r)=$id AND r.bootstrap_operation=$op_id DELETE r RETURN count(r) AS n""",
                    owner=owner, key=edge["key"], id=edge["id"], op_id=op_id).single()
                require(row and row["n"] == 1, "rollback_edge_changed")
            for edge in record["retired_edges"]:
                require(edge["relation"] in {"PREFERS", "AVOIDS"}, "invalid_retirement_journal")
                row = tx.run(f"""MATCH (u:User {{id:$owner}})-[a:PREFERENCE_SUPERSEDED]->(c:Concept {{key:$key}})
                    WHERE elementId(a)=$archive_id AND a.operation_id=$op_id AND a.retired_id=$old_id
                      AND a.original_relation=$relation AND a.properties_json=$properties_json
                    CREATE (u)-[r:{edge['relation']}]->(c) SET r=$properties DELETE a
                    RETURN elementId(r) AS restored_id""", owner=owner, key=edge["concept"]["key"],
                    archive_id=edge["archive_id"], old_id=edge["id"], op_id=op_id, properties=edge["properties"],
                    relation=edge["relation"], properties_json=encoded(edge["properties"]).decode()).single()
                require(row is not None, "rollback_archive_missing")
                edge["restored_id"] = row["restored_id"]
            retained = []
            for concept in record["created_concepts"]:
                row = tx.run("""MATCH (c:Concept {key:$key})
                    RETURN keys(c) AS names,COUNT { (c)--() } AS links""", key=concept["key"]).single()
                if not row:
                    continue
                if row["links"] or set(row["names"]) != set(concept):
                    retained.append(concept["key"])
                    continue
                deleted = tx.run("""MATCH (c:Concept {key:$key})
                    WHERE c.bootstrap_created_by=$op_id AND NOT (c)--()
                      AND all(k IN keys($props) WHERE c[k]=$props[k])
                    DELETE c RETURN count(c) AS n""", key=concept["key"], op_id=op_id, props=concept).single()
                if not deleted or not deleted["n"]:
                    retained.append(concept["key"])
            bump_preferences(tx, owner)
            tx.run("""MATCH (u:User {id:$owner}) SET u.preference_projection_pending=$op_id
                FOREACH (_ IN CASE WHEN $complete THEN [1] ELSE [] END |
                    SET u.preference_epoch=coalesce(u.preference_epoch,0)+1,u.preference_epoch_at=$now)
                """, owner=owner, now=time.time(), op_id=op_id, complete=record["mode"] == "complete_set").consume()
            record.update(status="rolled_back", projection_status="pending", retained_concepts=retained,
                          effect=snapshot(tx, owner)[0], rolled_back_at=time.time())
            save_journal(tx, record)
            return record
        return self.write(rollback)
