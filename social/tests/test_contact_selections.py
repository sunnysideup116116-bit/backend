"""Public Pi contact-selection lifecycle tests."""

import time

import mongomock

from services.ayue_agent.shared.contact_selections import ContactSelectionManager


def _candidate(name="小安", other_id="contact-1"):
    return {"display_name": name, "other_id": other_id, "kind": "similar"}


def test_pi_selection_is_prepared_until_bound_to_a_persisted_message():
    collection = mongomock.MongoClient().test.selections
    manager = ContactSelectionManager(collection)
    record = manager.create(
        user_id="owner", room_id="room", origin_run_id="run",
        name_hint="小安", candidates=[_candidate()],
    )
    assert record["source_engine"] == "pi"
    assert record["status"] == "prepared"
    assert manager.resolve(
        user_id="owner", room_id="room",
        action_id=record["candidates"][0]["action_id"], action="confirm",
    )[1] is None


def test_retired_dag_selection_expires_without_selecting_contact():
    collection = mongomock.MongoClient().test.selections
    collection.insert_one({
        "_id": "old", "user_id": "owner", "room_id": "room",
        "surface": "public_ayue", "source_engine": "dag", "status": "pending",
        "expires_at": time.time() + 900,
        "candidates": [{"action_id": "choice", **_candidate()}],
    })
    record, selected = ContactSelectionManager(collection).resolve(
        user_id="owner", room_id="room", action_id="choice", action="confirm",
    )
    assert selected is None
    assert record["status"] == "expired"
    assert collection.find_one({"_id": "old"})["resolution_reason"] == "dag_retired"
