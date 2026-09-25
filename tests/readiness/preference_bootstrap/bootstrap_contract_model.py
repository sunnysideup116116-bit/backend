"""Executable DESIGN model, not a production writer, API, migration or CLI.

All persistence is copied in-memory synthetic state. No I/O or credential loader.
Physical Neo4j relationship IDs are NOT modelled/restorable; association IDs here
are logical audit identities. Real adapters/fencing require separate approval.
"""
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import json

from matchmaker_agent.concept_identity import (
    FRESH_PREFERENCE_NORMALIZATION_POLICY,
    canonicalize_fresh_concept,
    durable_memory_limit,
    has_mixed_preference_polarity,
    is_v2_preference_key,
    normalize_fresh_preference_text,
    split_compound_concept_label,
    split_explicit_preference_enumeration,
    stored_concept_identity,
)

POLICY = "owner-preference-bootstrap-design-v1"
ACTIVE = {"PREFERS", "AVOIDS"}
INVENTORY_MAX = 100  # Proposed owner-only preview bound; not a DB limit.
PREVIEW_TTL = 600  # Proposed consent freshness bound, not memory/context TTL.


class ContractError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise ContractError(code)


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":"), allow_nan=False).encode()).hexdigest()


@dataclass
class Association:
    id: str
    owner: str
    key: str
    relation: str
    properties: dict = field(default_factory=dict)


@dataclass
class State:
    concepts: dict = field(default_factory=dict)
    associations: dict = field(default_factory=dict)
    revisions: dict = field(default_factory=dict)
    pending_owners: set = field(default_factory=set)
    projection_ready: dict = field(default_factory=dict)
    retired: dict = field(default_factory=dict)
    journal: dict = field(default_factory=dict)


def identity_fields(concept):
    return {key: concept.get(key) for key in (
        "key", "canonical_key", "label", "display_label", "semantic_text",
        "canonicalization_version", "semantic_input_hash", "fidelity_status",
    )}


def owner_snapshot(state, owner):
    rows = []
    for edge in sorted(state.associations.values(), key=lambda edge: edge.id):
        if edge.owner == owner:
            require(edge.key in state.concepts, "missing_concept")
            rows.append({"association": deepcopy(vars(edge)),
                         "concept": identity_fields(state.concepts[edge.key])})
    return {"revision": state.revisions.get(owner, 0), "rows": rows}


def compile_items(items):
    # Keep the pilot <=5, within the authoritative configured per-message cap.
    require(isinstance(items, list) and 1 <= len(items) <= min(5, durable_memory_limit()), "item_count")
    result = {}
    for item in items:
        require(isinstance(item, dict) and set(item) == {"semantic_text", "relation"}, "item_schema")
        require(item["relation"] in ACTIVE, "explicit_polarity_required")
        text = normalize_fresh_preference_text(item["semantic_text"])
        require(not has_mixed_preference_polarity(text), "mixed_polarity")
        require(not split_explicit_preference_enumeration(text)
                and not split_compound_concept_label(text), "atomic_item_required")
        identity = canonicalize_fresh_concept(text)
        require(identity is not None, "invalid_identity")
        if identity.key in result:
            require(result[identity.key]["relation"] == item["relation"], "contradictory_polarity")
            # No silently discarding a user-confirmed row, even for an alias.
            raise ContractError("duplicate_identity_reconfirm")
        result[identity.key] = {**identity.as_dict(), "relation": item["relation"]}
    return sorted(result.values(), key=lambda item: (item["key"], item["relation"]))


def plan(state, *, actor, owner, mode, items, now=0, inventory_complete=True):
    require(actor == owner and bool(owner), "owner_mismatch")
    require(mode in {"add_only", "full_set"}, "invalid_mode")
    require(inventory_complete, "incomplete_inventory")
    require(owner not in state.pending_owners, "pending_writer")
    snapshot = owner_snapshot(state, owner)
    require(len(snapshot["rows"]) <= INVENTORY_MAX, "inventory_overflow")
    prepared = compile_items(items)
    selected = {(item["key"], item["relation"]) for item in prepared}
    active, legacy, existing_v2 = [], [], []
    for row in snapshot["rows"]:
        edge, concept = row["association"], row["concept"]
        if edge["relation"] not in ACTIVE:
            continue
        active.append(edge)
        identity = stored_concept_identity(concept)
        claims_v2 = concept["canonicalization_version"] == "v2" or is_v2_preference_key(edge["key"])
        require(not claims_v2 or identity is not None, "malformed_v2_requires_investigation")
        (existing_v2 if identity else legacy).append(edge)
    if mode == "full_set":
        require(all((e["key"], e["relation"]) in selected for e in existing_v2), "full_set_would_omit_or_flip_v2")
    creates, reuses, edges = [], [], []
    for item in prepared:
        current = state.concepts.get(item["key"])
        if current is not None:
            require(stored_concept_identity(current) is not None, "existing_identity_conflict")
            reuses.append(item["key"])
        else:
            creates.append({k: v for k, v in item.items() if k != "relation"})
        same_key = [e for e in active if e["key"] == item["key"]]
        require(all(e["relation"] == item["relation"] for e in same_key), "existing_polarity_change_requires_separate_action")
        require(len(same_key) <= 1, "duplicate_v2_association")
        # Current writer deletes CURRENTLY_WANTS for the same key. Bootstrap
        # must not silently touch short-term context, even with full-set consent.
        require(not any(r["association"]["key"] == item["key"]
                        and r["association"]["relation"] == "CURRENTLY_WANTS"
                        for r in snapshot["rows"]), "context_overlap_requires_separate_action")
        if not same_key:
            edges.append({"owner": owner, "key": item["key"], "relation": item["relation"]})
    payload = {
        "policy": POLICY, "normalization_policy": FRESH_PREFERENCE_NORMALIZATION_POLICY,
        "owner": owner, "mode": mode, "expires_at": now + PREVIEW_TTL,
        "snapshot_hash": fingerprint(snapshot), "items": prepared,
        "create_concepts": creates, "reuse_concepts": reuses, "create_associations": edges,
        "retire_associations": legacy if mode == "full_set" else [],
        "keep_owner_association_ids": [r["association"]["id"] for r in snapshot["rows"]
            if mode != "full_set" or r["association"] not in legacy],
        "concept_preconditions": {item["key"]: identity_fields(state.concepts[item["key"]])
                                  if item["key"] in state.concepts else None for item in prepared},
        "untouched": ["other owners", "legacy Concept identity/properties", "inactive memories",
                      "recent context", "non-memory profile fields/chat/history/proposals", "embeddings/indexes/flags"],
    }
    return {**payload, "plan_hash": fingerprint(payload)}


def receipt(preview, *, authenticated_owner, confirm_items, full_set=False,
            reviewed_prefers=False, reviewed_avoids=False, confirm_retirement=False):
    """Test stand-in for a SERVER-issued, owner-authenticated confirmation.

    NOT a client-trusted authorization object or production signature scheme.
    """
    require(authenticated_owner == preview["owner"], "owner_mismatch")
    require(confirm_items is True, "items_not_confirmed")
    return {"owner": authenticated_owner, "plan_hash": preview["plan_hash"],
            "mode": preview["mode"], "confirm_items": confirm_items,
            "full_set": full_set, "reviewed_prefers": reviewed_prefers,
            "reviewed_avoids": reviewed_avoids, "confirm_retirement": confirm_retirement}


def simulate_apply(state, preview, confirmation, *, actor, operation_id, now=0, fail_at=None):
    """Copy-on-write failure model only: no HTTP/Cypher/DB persistence."""
    owner = preview["owner"]
    require(actor == owner == confirmation["owner"], "owner_mismatch")
    require(fingerprint({k: v for k, v in preview.items() if k != "plan_hash"}) == preview["plan_hash"], "plan_tampered")
    require(confirmation["plan_hash"] == preview["plan_hash"]
            and confirmation["mode"] == preview["mode"] and confirmation["confirm_items"] is True, "confirmation_mismatch")
    if operation_id in state.journal:
        record = state.journal[operation_id]
        require(record["owner"] == owner and record["plan_hash"] == preview["plan_hash"], "idempotency_conflict")
        require(record["status"] != "rolled_back", "already_rolled_back")
        return deepcopy(state)  # Response-loss retry reads the original receipt.
    require(now <= preview["expires_at"], "confirmation_expired")
    require(owner not in state.pending_owners, "pending_writer")
    require(fingerprint(owner_snapshot(state, owner)) == preview["snapshot_hash"], "snapshot_changed")
    for key, before in preview["concept_preconditions"].items():
        current = identity_fields(state.concepts[key]) if key in state.concepts else None
        require(current == before, "concept_snapshot_changed")
    if preview["mode"] == "full_set":
        require(all(confirmation[k] is True for k in (
            "full_set", "reviewed_prefers", "reviewed_avoids", "confirm_retirement")), "full_set_consent_required")
    else:
        require(not preview["retire_associations"], "add_only_cannot_retire")
    result = deepcopy(state)
    created_edges = []
    for concept in preview["create_concepts"]:
        result.concepts[concept["key"]] = deepcopy(concept)
    require(fail_at != "concepts", "injected_failure")
    for index, data in enumerate(preview["create_associations"]):
        edge_id = operation_id + ":new:" + str(index)
        require(edge_id not in result.associations, "association_id_collision")
        result.associations[edge_id] = Association(edge_id, **data, properties={"bootstrap_operation": operation_id})
        created_edges.append(edge_id)
    require(fail_at != "associations", "injected_failure")
    for edge in preview["retire_associations"]:
        del result.associations[edge["id"]]
    result.retired[operation_id] = deepcopy(preview["retire_associations"])
    require(fail_at != "retirement", "injected_failure")
    result.revisions[owner] = result.revisions.get(owner, 0) + 1
    result.projection_ready[owner] = False
    result.journal[operation_id] = {
        "owner": owner, "plan_hash": preview["plan_hash"], "status": "graph_committed",
        "confirmation": deepcopy(confirmation), "before_owner": owner_snapshot(state, owner),
        "created_edges": created_edges, "retired": deepcopy(preview["retire_associations"]),
        "created_concepts": deepcopy(preview["create_concepts"]),
        "after_hash": fingerprint(owner_snapshot(result, owner)),
    }
    require(fail_at != "journal", "injected_failure")
    return result


def acknowledge_projection(state, operation_id):
    result = deepcopy(state)
    record = result.journal[operation_id]
    require(record["status"] in {"graph_committed", "ready"}, "invalid_operation_state")
    require(fingerprint(owner_snapshot(result, record["owner"])) == record["after_hash"], "snapshot_changed")
    result.projection_ready[record["owner"]] = True
    record["status"] = "ready"
    return result


def pilot_preference_ready(state, owner):
    """Preference-state readiness only, NOT matching/semantic eligibility."""
    active = [r for r in owner_snapshot(state, owner)["rows"] if r["association"]["relation"] in ACTIVE]
    return bool(active and state.projection_ready.get(owner) and owner not in state.pending_owners
                and all(stored_concept_identity(r["concept"]) for r in active)
                and any(r["association"]["relation"] == "PREFERS" for r in active))


def simulate_rollback(state, operation_id, *, actor):
    record = state.journal[operation_id]
    owner = record["owner"]
    require(actor == owner, "owner_mismatch")
    if record["status"] == "rolled_back":
        return deepcopy(state)
    require(owner not in state.pending_owners, "pending_writer")
    require(fingerprint(owner_snapshot(state, owner)) == record["after_hash"], "rollback_conflict")
    require(state.retired.get(operation_id) == record["retired"], "retirement_journal_mismatch")
    result = deepcopy(state)
    for edge_id in record["created_edges"]:
        del result.associations[edge_id]
    for edge in record["retired"]:
        require(edge["id"] not in result.associations, "rollback_id_collision")
        result.associations[edge["id"]] = Association(**deepcopy(edge))
    retained = []
    for concept in record["created_concepts"]:
        key = concept["key"]
        if any(e.key == key for e in result.associations.values()) or result.concepts[key] != concept:
            retained.append(key)  # Never erase shared or subsequently enriched data.
        else:
            del result.concepts[key]
    del result.retired[operation_id]
    result.revisions[owner] += 1  # Audit concurrency clock never rewinds.
    result.projection_ready[owner] = False
    result.journal[operation_id].update(status="rolled_back", retained_concepts=retained)
    return result
