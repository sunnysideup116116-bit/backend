from types import SimpleNamespace

from fastapi import BackgroundTasks

from models import AcceptRequest
from routers import match


class MatchCollection:
    def __init__(self, match_doc):
        self.match_doc = match_doc
        self.updates = []

    def find_one(self, *_args, **_kwargs):
        return self.match_doc

    def update_one(self, *args, **kwargs):
        self.updates.append((args, kwargs))
        return SimpleNamespace(modified_count=1)


class ProfileCollection:
    def find_one(self, query, *_args, **_kwargs):
        return {"user_id": query["user_id"], "big_five": {}}

    def update_many(self, *_args, **_kwargs):
        return SimpleNamespace(modified_count=2)


def test_receiver_decline_gently_notifies_invitation_sender(monkeypatch):
    queued = []
    monkeypatch.setattr(match, "ObjectId", lambda value: value)
    monkeypatch.setattr(
        match,
        "matches_coll",
        MatchCollection(
            {
                "_id": "match-1",
                "status": "pending",
                "from_user": "sender",
                "to_user": "receiver",
            }
        ),
    )
    monkeypatch.setattr(match, "profiles_coll", ProfileCollection())
    monkeypatch.setattr(
        match,
        "queue_mediator_event",
        lambda user_id, message, event_type, **extra: queued.append(
            (user_id, message, event_type, extra)
        ),
    )

    response = match.decline_match(
        AcceptRequest(
            user_id="receiver",
            match_id="match-1",
            explicit_reasons=["目前沒有想認識新的人"],
        ),
        BackgroundTasks(),
    )

    assert response["context"] == "receiver_declined_pending"
    assert len(queued) == 1
    user_id, message, event_type, metadata = queued[0]
    assert user_id == "sender"
    assert event_type == "match_declined"
    assert metadata["match_id"] == "match-1"
    assert metadata["other_id"] == "receiver"
    assert "沒有接下" in message
    assert "目前沒有想認識新的人" not in message
