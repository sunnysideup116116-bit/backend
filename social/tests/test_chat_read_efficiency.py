"""Real query semantics on an in-memory Mongo substitute; no external I/O."""
from unittest.mock import patch

import mongomock
import pytest
from bson import ObjectId
from fastapi import HTTPException

from routers import chat_messages as routes
from services.ayue_agent import public_relationship_projection as labels


class CountedCollection:
    def __init__(self, collection):
        self.collection = collection
        self.find_calls = 0
        self.find_one_calls = 0
        self.aggregate_rows = 0

    def find(self, *args, **kwargs):
        self.find_calls += 1
        return self.collection.find(*args, **kwargs)

    def find_one(self, *args, **kwargs):
        self.find_one_calls += 1
        return self.collection.find_one(*args, **kwargs)

    def aggregate(self, pipeline):
        for row in self.collection.aggregate(pipeline):
            self.aggregate_rows += 1
            yield row


@pytest.fixture
def mongo(monkeypatch):
    db = mongomock.MongoClient().db
    for name in ("messages", "profiles", "matches", "notification_threads"):
        monkeypatch.setattr(routes, name + "_coll", CountedCollection(db[name]))
    monkeypatch.setattr(routes.risk_block_service, "excluded_user_ids", lambda _: set())
    monkeypatch.setattr(routes, "notification_unread_map", lambda *_: {})
    return db


def accepted(db, other):
    db.matches.insert_one({
        "from_user": "owner", "to_user": other, "status": "accepted",
        "state_history": [{"from": "draft", "to": "pending", "action": "accept"},
                          {"from": "pending", "to": "accepted", "action": "accept"}],
    })


def test_contacts_return_only_one_preview_per_room_and_batch_profiles(mongo):
    for other in ("alice", "bob", "carol"):
        accepted(mongo, other)
        mongo.profiles.insert_one({"user_id": other, "name": "Name " + other, "current_context": "hi"})
        room = routes.generate_room_id("owner", other)
        mongo.messages.insert_many([
            {"room_id": room, "content": str(i), "timestamp": i}
            for i in range(100)
        ])
        mongo.messages.insert_one({"room_id": room, "content": "blocked", "timestamp": 101, "is_blocked": True})
    with patch.object(labels.profiles_coll, "find_one", side_effect=AssertionError("unexpected per-user read")):
        contacts = routes.get_contacts("owner")["contacts"][1:]
    assert {c["id"] for c in contacts} == {"alice", "bob", "carol"}
    assert all(c["latest_message"] == "99" for c in contacts)
    assert all(c["name"] == "Name " + c["id"] for c in contacts)
    assert routes.messages_coll.aggregate_rows == 3
    assert routes.messages_coll.find_calls == 0
    assert routes.profiles_coll.find_calls == 1
    assert routes.profiles_coll.find_one_calls == 1


def test_contact_previews_render_latest_image_and_preserve_text_blocked_and_empty(mongo):
    for other in ("text", "image", "blocked", "empty"):
        accepted(mongo, other)

    text_room = routes.generate_room_id("owner", "text")
    mongo.messages.insert_many([
        {"room_id": text_room, "content": "較早文字", "timestamp": 1},
        {"room_id": text_room, "content": "最新文字", "timestamp": 2},
    ])

    image_room = routes.generate_room_id("owner", "image")
    mongo.messages.insert_many([
        {"room_id": image_room, "content": "較早文字", "timestamp": 1},
        {
            "room_id": image_room,
            "content": "",
            "message_type": "image",
            "timestamp": 2,
        },
    ])

    blocked_room = routes.generate_room_id("owner", "blocked")
    mongo.messages.insert_one({
        "room_id": blocked_room,
        "content": "不應顯示",
        "timestamp": 1,
        "is_blocked": True,
    })

    contacts = {
        contact["id"]: contact
        for contact in routes.get_contacts("owner")["contacts"][1:]
    }

    assert contacts["text"]["latest_message"] == "最新文字"
    assert contacts["image"]["latest_message"] == "傳送了一張圖片"
    assert contacts["blocked"]["latest_message"] == ""
    assert contacts["empty"]["latest_message"] == ""


def test_contact_previews_follow_deletion_and_preserve_block_exclusions(mongo, monkeypatch):
    for other in ("alice", "bob"):
        accepted(mongo, other)
    room = routes.generate_room_id("owner", "alice")
    first = mongo.messages.insert_one({"room_id": room, "content": "older", "timestamp": 1}).inserted_id
    last = mongo.messages.insert_one({"room_id": room, "content": "newer", "timestamp": 2}).inserted_id
    monkeypatch.setattr(routes.risk_block_service, "excluded_user_ids", lambda _: {"bob"})
    assert routes.get_contacts("owner")["contacts"][1]["latest_message"] == "newer"
    mongo.messages.delete_one({"_id": last})
    contacts = routes.get_contacts("owner")["contacts"]
    assert [c["id"] for c in contacts] == ["ai_assistant", "alice"]
    assert contacts[1]["latest_message"] == "older"
    mongo.messages.update_one({"_id": first}, {"$set": {"is_blocked": True}})
    assert routes.get_contacts("owner")["contacts"][1]["latest_message"] == ""


def test_contact_without_profile_or_text_keeps_safe_defaults(mongo):
    accepted(mongo, "missing")
    mongo.messages.insert_one({"room_id": routes.generate_room_id("owner", "missing"), "timestamp": 1})
    item = routes.get_contacts("owner")["contacts"][1]
    assert item["name"] == "暱稱暫無法取得"
    assert item["name_available"] is False
    assert item["latest_message"] == ""
    assert item["context"] == "尚無近期情境"


def test_focused_unread_does_not_read_profiles_or_message_history(mongo):
    accepted(mongo, "alice")
    mongo.matches.update_one({"to_user": "alice"}, {"$set": {"private_unread": {"from": 2, "to": 99}}})
    mongo.notification_threads.insert_many([
        {"user_id": "owner", "surface": routes.MEDIATOR_PRIVATE,
         "conversation_id": routes.generate_mediator_private_room_id("owner", "alice"), "unread_count": 3},
        {"user_id": "someone_else", "surface": routes.MEDIATOR_PRIVATE,
         "conversation_id": routes.generate_mediator_private_room_id("owner", "alice"), "unread_count": 50},
    ])
    result = routes.get_contacts("owner", unread_for="alice")
    assert result == {"contacts": [{"id": "alice", "mediator_unread_count": 3}]}
    assert routes.profiles_coll.find_calls == routes.profiles_coll.find_one_calls == 0
    assert routes.messages_coll.find_calls == routes.messages_coll.aggregate_rows == 0
    assert routes.notification_threads_coll.find_one_calls == 1


def test_focused_unread_preserves_legacy_count_and_block_rules(mongo, monkeypatch):
    accepted(mongo, "alice")
    mongo.matches.update_one({"to_user": "alice"}, {"$set": {"private_unread": {"from": 2}}})
    assert routes.get_contacts("owner", unread_for="alice")["contacts"][0]["mediator_unread_count"] == 2
    assert routes.get_contacts("owner", unread_for="not-accepted") == {"contacts": []}
    monkeypatch.setattr(routes.risk_block_service, "excluded_user_ids", lambda _: {"alice"})
    assert routes.get_contacts("owner", unread_for="alice") == {"contacts": []}


@pytest.fixture
def history(mongo, monkeypatch):
    monkeypatch.setattr(routes, "generate_room_id", lambda *_: "room")
    return mongo.messages


def message(index, timestamp=100.1234567, **extra):
    return {"_id": ObjectId(f"{index:024x}"), "room_id": "room", "timestamp": timestamp,
            "message_id": f"public-{index}", "content": str(index), **extra}


def test_cursor_pagination_keeps_equal_timestamp_messages_and_precision(history):
    history.insert_many([message(i) for i in range(1, 8)])
    received = []
    kwargs = {"limit": 3}
    while True:
        page = routes.get_messages("other", "owner", **kwargs)
        received.extend(m["message_id"] for m in page["messages"])
        assert page["next_before"] == 100.1234567
        if not page["has_more"]:
            break
        kwargs = {"limit": 3, "before": page["next_before"], "before_id": page["next_before_id"]}
    assert len(received) == len(set(received)) == 7
    assert set(received) == {f"public-{i}" for i in range(1, 8)}


def test_loaded_window_refresh_includes_new_updates_and_removes_deleted(history):
    history.insert_many([message(i, timestamp=i) for i in range(1, 6)])
    initial = routes.get_messages("other", "owner", limit=3)
    history.delete_one({"message_id": "public-4"})
    history.update_one({"message_id": "public-3"}, {"$set": {"risk": {"level": "warning"}}})
    history.insert_many([message(i, timestamp=i) for i in range(6, 11)])
    refresh = routes.get_messages("other", "owner", through=initial["next_before"], through_id=initial["next_before_id"])
    assert [m["message_id"] for m in refresh["messages"]] == ["public-3", "public-5", *[f"public-{i}" for i in range(6, 11)]]
    assert refresh["messages"][0]["risk"]["level"] == "warning"
    assert refresh["has_more"] is True
    assert "date_coordination" in refresh


def test_cursor_crosses_legacy_object_ids_and_idempotent_string_ids(history):
    history.insert_many([
        message(1), message(2),
        {**message(3), "_id": "pair-first"}, {**message(4), "_id": "pair-second"},
    ])
    page = routes.get_messages("other", "owner", limit=2)
    older = routes.get_messages("other", "owner", limit=2, before=page["next_before"], before_id=page["next_before_id"])
    assert {m["message_id"] for m in [*page["messages"], *older["messages"]]} == {"public-1", "public-2", "public-3", "public-4"}
    sync = routes.get_messages("other", "owner", through=older["next_before"], through_id=older["next_before_id"])
    assert len(sync["messages"]) == 4


def test_loaded_window_can_be_empty_after_all_loaded_rows_disappear(history):
    history.insert_many([message(i, timestamp=i) for i in range(1, 4)])
    initial = routes.get_messages("other", "owner", limit=2)
    history.delete_many({"timestamp": {"$gte": 2}})
    page = routes.get_messages("other", "owner", through=initial["next_before"], through_id=initial["next_before_id"])
    assert page["messages"] == []
    assert page["has_more"] is True


@pytest.mark.parametrize("kwargs", [
    {"before_id": "invalid"}, {"through_id": "invalid"},
    {"before": 1, "before_id": "!!!"}, {"before": float("nan")},
    {"through": 1, "limit": 50}, {"through": 1, "before": 1},
])
def test_invalid_or_ambiguous_cursor_is_rejected(history, kwargs):
    with pytest.raises(HTTPException) as caught:
        routes.get_messages("other", "owner", **kwargs)
    assert caught.value.status_code == 400


def test_old_clients_still_receive_full_history(history):
    history.insert_many([message(i, timestamp=i) for i in range(1, 81)])
    response = routes.get_messages("other", "owner")
    assert len(response["messages"]) == 80
    assert response["messages"][0]["content"] == "1"
    assert response["messages"][-1]["content"] == "80"
