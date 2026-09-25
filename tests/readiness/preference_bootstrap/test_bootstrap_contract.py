"""Synthetic contract scenarios; no real consent, Graph, provider or account I/O."""
import ast
from copy import deepcopy
from pathlib import Path
import socket

import pytest

from bootstrap_contract_model import (
    ACTIVE, Association, ContractError, State, acknowledge_projection,
    compile_items, owner_snapshot, pilot_preference_ready, plan, receipt,
    simulate_apply, simulate_rollback,
)
from matchmaker_agent.concept_identity import (
    PreferenceTextError, canonicalize_fresh_concept, stored_concept_identity,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    for key in ("MATCH_PREFERENCE_SEMANTIC_MODE", "MATCH_PREFERENCE_SEMANTIC_EMBEDDING_SPACE_CONFIRMED", "MATCH_RELATED_INTEREST_ENABLED"):
        monkeypatch.setenv(key, "off")
    monkeypatch.setenv("DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE", "6")
    def forbidden(*args, **kwargs):
        raise AssertionError("contract tests must not open a network connection")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def item(text, relation="PREFERS"):
    return {"semantic_text": text, "relation": relation}


def sample():
    state = State()
    # Different unknown keys/stances with identical legacy display prefixes.
    for key in ("legacy_a", "legacy_b", "inactive", "recent"):
        state.concepts[key] = {"key": key, "label": "同一段舊標籤" * 8}
    for edge in (
        Association("a-like", "a", "legacy_a", "PREFERS", {"weight": 0.8, "original_source": "synthetic-old"}),
        Association("a-avoid", "a", "legacy_b", "AVOIDS", {"confidence": 0.9}),
        Association("b-shared", "b", "legacy_a", "PREFERS", {"weight": 0.7}),
        Association("a-disabled", "a", "inactive", "MEMORY_DISABLED", {"original_relation": "AVOIDS"}),
        Association("a-recent", "a", "recent", "CURRENTLY_WANTS", {"expires_at": 999999}),
    ):
        state.associations[edge.id] = edge
    return state


def preview(state, items=None, mode="full_set", **kwargs):
    return plan(state, actor="a", owner="a", mode=mode,
                items=items if items is not None else [item("Kpop"), item("安静的咖啡厅"), item("吸菸", "AVOIDS")], **kwargs)


def consent(p, **overrides):
    return receipt(p, authenticated_owner="a", confirm_items=True, **{
        "full_set": True, "reviewed_prefers": True, "reviewed_avoids": True,
        "confirm_retirement": True, **overrides,
    })


def test_dry_run_is_read_only_and_lists_four_change_groups():
    state = sample(); before = deepcopy(state)
    p = preview(state)
    assert state == before
    assert len(p["create_concepts"]) == 3 and len(p["create_associations"]) == 3
    assert {e["relation"] for e in p["create_associations"]} == ACTIVE
    assert {e["id"] for e in p["retire_associations"]} == {"a-like", "a-avoid"}
    assert set(p["keep_owner_association_ids"]) == {"a-disabled", "a-recent"}
    assert "other owners" in p["untouched"]
    assert all(e["owner"] == "a" for e in p["retire_associations"])


def test_add_only_never_supersedes_legacy_or_claims_v2_clean():
    state = sample(); p = preview(state, mode="add_only")
    assert p["retire_associations"] == []
    confirm = receipt(p, authenticated_owner="a", confirm_items=True)
    changed = acknowledge_projection(simulate_apply(state, p, confirm, actor="a", operation_id="op"), "op")
    assert changed.associations["a-avoid"] == state.associations["a-avoid"]
    assert not pilot_preference_ready(changed, "a")


@pytest.mark.parametrize("ack", ["full_set", "reviewed_prefers", "reviewed_avoids", "confirm_retirement"])
def test_full_set_needs_each_explicit_ack_including_empty_avoids(ack):
    state = sample(); p = preview(state, [item("K-pop")])
    with pytest.raises(ContractError, match="full_set_consent_required"):
        simulate_apply(state, p, consent(p, **{ack: False}), actor="a", operation_id="op")
    assert "a-avoid" in state.associations


def test_explicit_complete_positive_only_set_retires_avoid_without_inverting_it():
    state = sample(); p = preview(state, [item("K-pop")])
    changed = simulate_apply(state, p, consent(p), actor="a", operation_id="op")
    assert not any(e.owner == "a" and e.relation == "AVOIDS" for e in changed.associations.values())
    assert changed.concepts["legacy_b"] == state.concepts["legacy_b"]
    assert next(e for e in changed.retired["op"] if e["id"] == "a-avoid")["relation"] == "AVOIDS"
    assert {e.key for e in changed.associations.values() if e.owner == "a" and e.relation == "PREFERS"} == {canonicalize_fresh_concept("K-pop").key}


def test_complete_set_is_atomic_auditable_owner_scoped_and_reversible():
    state = sample(); before = deepcopy(state); p = preview(state)
    changed = simulate_apply(state, p, consent(p), actor="a", operation_id="op")
    assert state == before
    assert not pilot_preference_ready(changed, "a")  # Mongo projection not acknowledged.
    changed = acknowledge_projection(changed, "op")
    assert pilot_preference_ready(changed, "a")
    assert changed.associations["b-shared"] == before.associations["b-shared"]
    for key in before.concepts:
        assert changed.concepts[key] == before.concepts[key]
    restored = simulate_rollback(changed, "op", actor="a")
    assert restored.associations == before.associations and restored.concepts == before.concepts
    assert restored.journal["op"]["status"] == "rolled_back"
    assert restored.revisions["a"] == 2 and not restored.projection_ready["a"]
    assert simulate_rollback(restored, "op", actor="a") == restored
    with pytest.raises(ContractError, match="already_rolled_back"):
        simulate_apply(restored, p, consent(p), actor="a", operation_id="op")


@pytest.mark.parametrize("stage", ["concepts", "associations", "retirement", "journal"])
def test_injected_failure_has_no_partial_result(stage):
    state = sample(); before = deepcopy(state); p = preview(state)
    with pytest.raises(ContractError, match="injected_failure"):
        simulate_apply(state, p, consent(p), actor="a", operation_id="op", fail_at=stage)
    assert state == before


def test_response_loss_idempotency_does_not_reapply_or_retire_twice():
    state = sample(); p = preview(state)
    changed = simulate_apply(state, p, consent(p), actor="a", operation_id="op")
    assert simulate_apply(changed, p, consent(p), actor="a", operation_id="op", now=9999) == changed
    different = preview(state, [item("J-pop")])
    with pytest.raises(ContractError, match="idempotency_conflict"):
        simulate_apply(changed, different, consent(different), actor="a", operation_id="op")


@pytest.mark.parametrize("phase", ["plan", "confirm", "apply", "rollback"])
def test_cross_owner_permission_is_never_inferred(phase):
    state = sample(); p = preview(state)
    with pytest.raises(ContractError, match="owner_mismatch"):
        if phase == "plan":
            plan(state, actor="b", owner="a", mode="full_set", items=[item("K-pop")])
        elif phase == "confirm":
            receipt(p, authenticated_owner="b", confirm_items=True)
        elif phase == "apply":
            simulate_apply(state, p, consent(p), actor="b", operation_id="op")
        else:
            changed = simulate_apply(state, p, consent(p), actor="a", operation_id="op")
            simulate_rollback(changed, "op", actor="b")


@pytest.mark.parametrize("change", ["edge", "revision", "pending"])
def test_changes_between_preview_and_confirmation_require_new_preview(change):
    state = sample(); p = preview(state)
    if change == "edge": state.associations["a-avoid"].properties["new_fact"] = True
    if change == "revision": state.revisions["a"] = 1
    if change == "pending": state.pending_owners.add("a")
    before = deepcopy(state)
    with pytest.raises(ContractError, match="snapshot_changed|pending_writer"):
        simulate_apply(state, p, consent(p), actor="a", operation_id="op")
    assert state == before


def test_expired_or_tampered_confirmation_cannot_write():
    state = sample(); p = preview(state)
    with pytest.raises(ContractError, match="confirmation_expired"):
        simulate_apply(state, p, consent(p), actor="a", operation_id="op", now=601)
    forged = deepcopy(p); forged["mode"] = "add_only"
    with pytest.raises(ContractError, match="plan_tampered"):
        simulate_apply(state, forged, consent(p), actor="a", operation_id="op")


def test_complete_inventory_not_first_8_20_or_30_and_no_overflow_truncation():
    state = State(concepts={"legacy": {"key": "legacy", "label": "舊標籤"}})
    for n in range(60):
        state.associations[str(n)] = Association(str(n), "a", "legacy", "AVOIDS" if n == 59 else "PREFERS")
    p = preview(state)
    assert len(p["retire_associations"]) == 60
    assert any(e["relation"] == "AVOIDS" for e in p["retire_associations"])
    with pytest.raises(ContractError, match="incomplete_inventory"):
        preview(state, inventory_complete=False)
    for n in range(60, 101):
        state.associations[str(n)] = Association(str(n), "a", "legacy", "PREFERS")
    with pytest.raises(ContractError, match="inventory_overflow"):
        preview(state)


def test_existing_v2_must_be_listed_unchanged_or_edited_separately():
    state = sample(); identity = canonicalize_fresh_concept("Board Games")
    state.concepts[identity.key] = identity.as_dict()
    state.associations["existing-v2"] = Association("existing-v2", "a", identity.key, "PREFERS", {"existing": True})
    with pytest.raises(ContractError, match="full_set_would_omit_or_flip_v2"):
        preview(state)
    p = preview(state, [item("board-games")])
    assert p["create_concepts"] == [] and p["create_associations"] == []
    changed = simulate_apply(state, p, consent(p), actor="a", operation_id="op")
    assert changed.associations["existing-v2"] == state.associations["existing-v2"]
    assert changed.concepts[identity.key] == state.concepts[identity.key]


def test_invalid_v2_is_not_disguised_as_retirable_legacy():
    state = sample(); identity = canonicalize_fresh_concept("K-pop")
    state.concepts[identity.key] = {**identity.as_dict(), "semantic_text": "tampered"}
    state.associations["broken"] = Association("broken", "a", identity.key, "PREFERS")
    with pytest.raises(ContractError, match="malformed_v2"):
        preview(state)


@pytest.mark.parametrize("items", [
    [item("K-pop", "UNKNOWN")], [item("K-pop"), item("Kpop", "AVOIDS")],
    [item("K-pop"), item("Kpop")], [item("我喜歡咖啡但我討厭吸菸")],
    [item("K-pop、J-pop、西洋音樂")], [], [item(str(n)) for n in range(6)],
])
def test_ambiguous_duplicate_polarity_atomicity_and_bounds_fail_whole_plan(items):
    state = sample(); before = deepcopy(state)
    with pytest.raises((ContractError, PreferenceTextError)):
        preview(state, items)
    assert state == before


@pytest.mark.parametrize("text", ["Kpop", "K-pop", "K pop", "k-pop"])
def test_uses_real_alias_identity_pipeline(text):
    assert compile_items([item(text)])[0]["key"] == canonicalize_fresh_concept("K-pop").key


def test_traditional_and_simplified_follow_the_same_fixed_fresh_boundary():
    assert compile_items([item("安静的咖啡厅")])[0]["key"] == compile_items([item("安靜的咖啡廳")])[0]["key"]


@pytest.mark.parametrize("size", [41, 499, 500])
def test_semantic_unicode_length_and_same_prefix_qualifier_preserved(size):
    text = "藝" * (size - 1) + "😀"
    compiled = compile_items([item(text)])[0]
    assert len(compiled["semantic_text"]) == size and compiled["semantic_text"] == text
    pair = compile_items([item("紙" * 40 + "安靜觀賞"), item("紙" * 40 + "熱鬧創作")])
    assert pair[0]["key"] != pair[1]["key"]


def test_501_rejects_whole_operation_with_no_retirement():
    state = sample(); before = deepcopy(state)
    with pytest.raises(PreferenceTextError):
        preview(state, [item("K-pop"), item("藝" * 501)])
    assert state == before


def test_current_context_collision_is_not_deleted_by_durable_bootstrap():
    state = sample(); identity = canonicalize_fresh_concept("K-pop")
    state.concepts[identity.key] = identity.as_dict()
    state.associations["recent-v2"] = Association("recent-v2", "a", identity.key, "CURRENTLY_WANTS", {"expires_at": 999})
    with pytest.raises(ContractError, match="context_overlap"):
        preview(state)


def test_rollback_conflict_never_clobbers_a_later_owner_edit():
    state = sample(); p = preview(state)
    changed = simulate_apply(state, p, consent(p), actor="a", operation_id="op")
    changed.associations["op:new:0"].properties["later_explicit_owner_change"] = True
    before = deepcopy(changed)
    with pytest.raises(ContractError, match="rollback_conflict"):
        simulate_rollback(changed, "op", actor="a")
    assert changed == before


@pytest.mark.parametrize("later_use", ["other_owner", "derived_embedding"])
def test_rollback_keeps_new_concept_if_other_data_now_depends_on_it(later_use):
    state = sample(); p = preview(state)
    changed = simulate_apply(state, p, consent(p), actor="a", operation_id="op")
    key = p["create_concepts"][0]["key"]
    if later_use == "other_owner":
        changed.associations["b-new"] = Association("b-new", "b", key, "PREFERS")
    else:
        changed.concepts[key]["embedding"] = [1.0, 0.0]  # synthetic marker, not a real vector.
    restored = simulate_rollback(changed, "op", actor="a")
    assert key in restored.concepts and key in restored.journal["op"]["retained_concepts"]
    assert owner_snapshot(restored, "a")["rows"] == owner_snapshot(state, "a")["rows"]


def test_ten_synthetic_members_three_to_five_items_keep_independent_consent():
    state = State(concepts={"shared_legacy": {"key": "shared_legacy", "label": "unknown prefix"}})
    for n in range(10):
        owner = f"synthetic-member-{n}"
        state.associations[owner] = Association(owner, owner, "shared_legacy", "PREFERS")
    plans = []
    for n in range(10):
        owner = f"synthetic-member-{n}"
        items = [item(f"Synthetic activity {n} part {i}", "AVOIDS" if i == 2 else "PREFERS") for i in range(3 + n % 3)]
        p = plan(state, actor=owner, owner=owner, mode="full_set", items=items)
        r = receipt(p, authenticated_owner=owner, confirm_items=True, full_set=True,
                    reviewed_prefers=True, reviewed_avoids=True, confirm_retirement=True)
        state = acknowledge_projection(simulate_apply(state, p, r, actor=owner, operation_id=owner), owner)
        plans.append(p)
    assert len(state.journal) == 10 and sum(len(p["create_associations"]) for p in plans) == 39
    assert all(pilot_preference_ready(state, f"synthetic-member-{n}") for n in range(10))
    assert state.concepts["shared_legacy"] == {"key": "shared_legacy", "label": "unknown prefix"}


def test_real_writer_validation_and_existing_qualification_gates(monkeypatch):
    # Full modules are imported only inside the repository's network-disabled
    # offline runner. No startup, API client, Graph or provider calls are made.
    from services.memory_service import validate_memory_proposals
    from routers import match as router
    state = sample(); p = preview(state)
    proposals = [{**i, "stance": "like" if i["relation"] == "PREFERS" else "avoid"} for i in p["items"]]
    validated = validate_memory_proposals(proposals)
    assert [r["key"] for r in validated] == [r["key"] for r in p["items"]]
    identity = canonicalize_fresh_concept("K-pop")
    def memories(uid, _limit):
        if uid == "requester": return [{**identity.as_dict(), "stance": "like"}]
        return [{**state.concepts[e.key], "stance": "like" if e.relation == "PREFERS" else "avoid"}
                for e in state.associations.values() if e.owner == "a" and e.relation in ACTIVE]
    monkeypatch.setattr(router, "get_user_graph_memories", memories)
    args = ({"user_id": "requester"}, {"user_id": "a"})
    query = {"search_intent": "preference", "normalized_topic": "K-pop"}
    blocked = router.candidate_qualification(*args, search_context=query)
    assert not blocked["eligible"] and blocked["legacy_identity_status"] == "indeterminate_legacy_conflict"
    state = acknowledge_projection(simulate_apply(state, p, consent(p), actor="a", operation_id="op"), "op")
    accepted = router.candidate_qualification(*args, search_context=query)
    assert accepted["eligible"] and accepted["requested_preference_matched"]
    # A verified same-concept opposite stance must still lose after bootstrap.
    for edge in state.associations.values():
        if edge.owner == "a" and edge.key == identity.key: edge.relation = "AVOIDS"
    rejected = router.candidate_qualification(*args, search_context=query)
    assert not rejected["eligible"] and rejected["hard_conflict_keys"] == [identity.key]


def test_reference_model_has_no_io_or_runtime_registration():
    path = Path(__file__).with_name("bootstrap_contract_model.py")
    tree = ast.parse(path.read_text())
    imports = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imports |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert imports <= {"copy", "dataclasses", "hashlib", "json", "matchmaker_agent.concept_identity"}
    assert not any(isinstance(n, ast.Name) and n.id in {"open", "exec", "eval", "__import__"} for n in ast.walk(tree))
