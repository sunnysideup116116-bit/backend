"""Exercise real card projection/persistence with an in-memory Mongo store."""
from unittest.mock import MagicMock
import mongomock
from bson import ObjectId
from services import event_delivery_service as worker
from services import proactive_delivery_service as adapter
from services import chat_service as chat


def test_real_adapter_saves_directional_event_once_and_acknowledges_queue(monkeypatch):
    db = mongomock.MongoClient().db
    mid = ObjectId()
    db.matches.insert_one({
        "_id": mid, "from_user": "a", "to_user": "b", "status": "draft",
        "proposal_namespace": "event_invitation", "proposal_source": "event_opportunity",
        "proposal_revision": 0, "reason": "一起看音樂表演嗎？", "receiver_reason": "有位朋友想邀你聽音樂。",
        "event_snapshot": {"title": "音樂表演", "starts_at": 9999999999, "source_url": "https://example.com/event"},
    })
    for uid in ("a", "b"):
        db.profiles.insert_one({"user_id": uid, "mediator_inbox": [{"match_id": str(mid), "type": "match_proposal"}]})
    for module in (worker, adapter):
        monkeypatch.setattr(module, "matches_coll", db.matches)
        monkeypatch.setattr(module, "profiles_coll", db.profiles)
        monkeypatch.setattr(module, "messages_coll", db.messages)
        monkeypatch.setattr(module, "match_hub_v1_enabled", lambda: True)
    monkeypatch.setattr(chat, "messages_coll", db.messages)
    monkeypatch.setattr(chat, "mirror_message_to_appwrite_async", lambda _: None)
    push = MagicMock()
    monkeypatch.setattr(chat, "queue_push_notification", push)
    monkeypatch.setattr(adapter, "most_recent_ai_room", lambda uid, **_: f"ai_room::{uid}::match_hub")
    monkeypatch.setattr(adapter, "get_room", lambda room, uid: {
        "room_id": room, "user_id": uid, "room_kind": "match_hub", "title": "阿月牽線",
    } if room == f"ai_room::{uid}::match_hub" else None)
    assert worker.deliver_event_proposals_once()["delivered"] == 1
    card = db.messages.find_one({"room_id": "ai_room::a::match_hub"})
    assert card["metadata"]["matches"][0]["viewer_reason"] == "一起看音樂表演嗎?"
    assert card["metadata"]["matches"][0]["event"]["title"] == "音樂表演"
    assert db.messages.count_documents({"room_id": "ai_room::b::match_hub"}) == 0
    assert db.profiles.find_one({"user_id": "a"})["mediator_inbox"] == []
    # Simulate crash after saving card but before acknowledgement.
    db.matches.update_one({"_id": mid}, {"$unset": {"event_delivery": ""}})
    assert worker.deliver_event_proposals_once()["delivered"] == 1
    assert db.messages.count_documents({}) == 1
    assert push.call_count == 1
    db.matches.update_one({"_id": mid}, {"$set": {"status": "pending", "proposal_revision": 1}})
    assert worker.deliver_event_proposals_once()["delivered"] == 1
    second = db.messages.find_one({"room_id": "ai_room::b::match_hub"})
    assert second["metadata"]["matches"][0]["viewer_reason"] == "有位朋友想邀你聽音樂。"
    assert push.call_count == 2
