from copy import deepcopy

from bson import ObjectId
import pytest

from scripts.repair_timeout_restorations import make_manifest, apply_manifest, eligible
from tests.match_flow_store import Collection


def restored():
    decision = {"from": "expired", "to": "draft", "actor": "system", "action": "proposal_timeout_reverted", "at": 50}
    return {"_id": ObjectId(), "from_user": "owner", "to_user": "other", "status": "draft",
            "created_at": 1, "proposal_namespace": "relationship_match", "proposal_revision": 2,
            "state_history": [decision], "last_decision": decision}


def test_dry_run_is_read_only_and_excludes_user_decisions():
    candidate = restored()
    accepted = {**restored(), "status": "accepted"}
    acted = restored()
    acted["state_history"].append({"actor": "owner", "action": "accept", "at": 51})
    declined = {**restored(), "status": "declined"}
    event = {**restored(), "proposal_namespace": "event_invitation"}
    rows = Collection([candidate, accepted, acted, declined, event])
    before = deepcopy(rows.rows)
    manifest = make_manifest(rows, "owner")
    assert [item["match_id"] for item in manifest["candidates"]] == [str(candidate["_id"])]
    assert rows.rows == before and rows.writes == 0


def test_apply_backs_up_cas_rolls_back_and_invalidates_bound_choice(tmp_path):
    row = restored()
    choices = Collection([{"_id": "choice", "user_id": "owner", "room_id": "room", "status": "pending", "expires_at": 100,
                          "payload": {"match_id": str(row["_id"]), "proposal_namespace": "relationship_match"}}])
    messages = Collection([{"room_id": "room", "metadata": {"choice_prompt": {"id": "choice", "state": "pending"}}}])
    rows = Collection([row])
    manifest = make_manifest(rows, "owner")
    result = apply_manifest(rows, choices, messages, manifest, tmp_path)
    assert result[0]["status"] == "repaired"
    current = rows.find_one({"_id": row["_id"]})
    assert current["status"] == "expired"
    assert current["proposal_revision"] == 3
    assert current["state_history"][:-1] == row["state_history"]
    assert len(list(tmp_path.glob("*.json"))) == 1
    assert choices.rows[0]["status"] == "superseded"
    assert messages.rows[0]["metadata"]["choice_prompt"]["state"] == "superseded"
    again = apply_manifest(rows, choices, messages, manifest, tmp_path)
    assert again[0]["status"] == "already_repaired"
    assert rows.find_one({"_id": row["_id"]})["proposal_revision"] == 3


def test_manifest_stale_revision_never_overwrites_new_decision(tmp_path):
    row = restored()
    rows = Collection([row])
    manifest = make_manifest(rows, "owner")
    rows.update_one({"_id": row["_id"]}, {"$inc": {"proposal_revision": 1}})
    assert apply_manifest(rows, Collection(), Collection(), manifest, tmp_path)[0]["status"] == "stale_or_not_eligible"
    assert not list(tmp_path.iterdir())


def test_cas_race_preserves_user_action_and_leaves_backup(tmp_path):
    row = restored()
    rows = Collection([row])
    manifest = make_manifest(rows, "owner")
    real_update = rows.update_one
    def race(query, update, **kwargs):
        real_update({"_id": row["_id"]}, {"$set": {"status": "accepted"}, "$inc": {"proposal_revision": 1}})
        return real_update(query, update, **kwargs)
    rows.update_one = race
    result = apply_manifest(rows, Collection(), Collection(), manifest, tmp_path)
    assert result[0]["status"] == "cas_conflict"
    assert rows.rows[0]["status"] == "accepted"
    assert len(list(tmp_path.glob("*.json"))) == 1
