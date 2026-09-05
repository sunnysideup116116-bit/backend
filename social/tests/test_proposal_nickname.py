"""Public nicknames are viewer-bound UI data, never new mutation authority."""

from copy import deepcopy
from unittest.mock import MagicMock

from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from routers import chat_messages, match as routes
from services import match_card_projection as cards
from tests.match_flow_store import Collection


NAMES = {"alice": "小葵", "bob": "小晴"}
MATCH_ID = ObjectId("64f000000000000000000071")


def document(status="pending", namespace="relationship_match"):
    result = {
        "_id": MATCH_ID, "from_user": "alice", "to_user": "bob", "status": status,
        "proposal_namespace": namespace, "proposal_revision": 2,
        "reason_version": "v4_friend_intro", "friend_intro_v4": {},
        "match_context_snapshot": {
            "target": {"user_id": "alice", "current_context": "想看電影", "public_personality": "溫和"},
            "candidate": {"user_id": "bob", "current_context": "想爬山", "public_personality": "活潑"},
        },
    }
    if status == "accepted":
        result["last_decision"] = {"from": "pending", "to": "accepted", "action": "accept", "actor": "bob"}
    return result


@pytest.mark.parametrize("value", [
    None, {}, "", "對方", "seed_user_04", "seed_user_05", "USER_ID:abc",
    "4@gmail.com", "４＠gmail.com", "小晴 0912-345-678", "https://example.test/profile",
    "64f000000000000000000071", "小晴\u202e", "小晴\n另一行",
])
def test_missing_internal_or_contact_values_never_become_public_nicknames(value):
    assert cards.safe_proposal_nickname(value, "seed_user_04") == ""


def test_public_nickname_is_bounded_and_cannot_embed_the_known_account_id():
    assert cards.safe_proposal_nickname("  小晴 Sunny  ", "account-identifier") == "小晴 Sunny"
    assert len(cards.safe_proposal_nickname("晴" * 80, "account-identifier")) == 30
    assert cards.safe_proposal_nickname("暱稱 account-identifier", "account-identifier") == ""


@pytest.mark.parametrize("viewer,expected", [("alice", "小晴"), ("bob", "小葵")])
@pytest.mark.parametrize("status", ["pending", "accepted", "declined", "expired"])
@pytest.mark.parametrize("namespace", ["relationship_match", "event_invitation"])
def test_state_returns_only_the_actual_counterparty_nickname(monkeypatch, viewer, expected, status, namespace):
    matches = Collection([document(status, namespace)])
    monkeypatch.setattr(routes, "matches_coll", matches)
    monkeypatch.setattr(routes, "public_display_name", lambda uid: NAMES[uid])
    before = deepcopy(matches.rows)
    result = routes.get_single_match_state(viewer, str(MATCH_ID))
    assert result["counterparty_nickname"] == expected
    if status != "accepted":
        assert "other_id" not in result
    assert "other_name" not in result
    assert matches.rows == before
    assert matches.writes == 0


def test_unsent_draft_does_not_disclose_an_initiator_name_to_the_receiver(monkeypatch):
    monkeypatch.setattr(routes, "matches_coll", Collection([document("draft")]))
    lookup = MagicMock(side_effect=lambda uid: NAMES[uid])
    monkeypatch.setattr(routes, "public_display_name", lookup)
    assert routes.get_single_match_state("bob", str(MATCH_ID))["counterparty_nickname"] == ""
    lookup.assert_not_called()
    assert routes.get_single_match_state("alice", str(MATCH_ID))["counterparty_nickname"] == "小晴"


def test_name_lookup_failure_preserves_terminal_state_without_an_id_fallback(monkeypatch):
    monkeypatch.setattr(routes, "matches_coll", Collection([document("declined")]))
    monkeypatch.setattr(routes, "public_display_name", MagicMock(side_effect=RuntimeError("unavailable")))
    result = routes.get_single_match_state("alice", str(MATCH_ID))
    assert result["status"] == "declined"
    assert result["counterparty_nickname"] == ""
    assert "other_id" not in result


@pytest.mark.parametrize("viewer,expected", [("alice", "小晴"), ("bob", "小葵")])
def test_history_names_are_canonical_cached_per_peer_and_do_not_revive_actions(viewer, expected):
    current = document("declined")
    rows = [{"content": "原本的匿名理由", "metadata": {
        "event_type": "match_proposal", "match_id": str(MATCH_ID),
        "proposal_role": "not-trusted", "counterparty_nickname": "不可信的舊稱呼",
        "actions": ["accept", "decline"],
        "matches": [{"match_id": str(MATCH_ID), "matched_user_id": "not-trusted", "counterparty_nickname": "舊稱呼"}],
    }} for _ in range(3)]
    before = deepcopy(rows)
    collection = Collection([current])
    lookup = MagicMock(side_effect=lambda uid: NAMES[uid])
    projected = cards.project_match_card_history(rows, viewer, collection=collection, nickname_lookup=lookup)
    for row in projected:
        metadata = row["metadata"]
        assert metadata["counterparty_nickname"] == expected
        assert metadata["matches"][0]["counterparty_nickname"] == expected
        assert metadata["canonical_status"] == "declined"
        assert metadata["actions"] == []
        assert row["content"] == "原本的匿名理由"
    lookup.assert_called_once_with("bob" if viewer == "alice" else "alice")
    assert rows == before
    assert collection.writes == 0


def test_missing_or_foreign_history_drops_old_nicknames_without_reading_profiles():
    lookup = MagicMock()
    rows = [{"metadata": {
        "event_type": "match_proposal", "match_id": str(MATCH_ID), "counterparty_nickname": "不可保留",
        "matches": [{"match_id": str(MATCH_ID), "counterparty_nickname": "也不可保留"}],
    }}]
    projected = cards.project_match_card_history(rows, "stranger", collection=Collection([document()]), nickname_lookup=lookup)
    metadata = projected[0]["metadata"]
    assert metadata["counterparty_nickname"] == ""
    assert "counterparty_nickname" not in metadata["matches"][0]
    assert metadata["canonical_status"] == "unavailable"
    lookup.assert_not_called()


def test_history_http_route_includes_nickname_without_persisting_it(monkeypatch):
    room = "ai_room::alice::nickname-test"
    messages = Collection([{"room_id": room, "timestamp": 1, "sender_id": "ai_assistant", "content": "匿名理由", "metadata": {
        "event_type": "match_proposal", "match_id": str(MATCH_ID),
    }}])
    matches = Collection([document("declined")])
    monkeypatch.setattr(cards, "matches_coll", matches)
    monkeypatch.setattr(chat_messages, "messages_coll", messages)
    monkeypatch.setattr(chat_messages, "profiles_coll", Collection([{"user_id": "alice"}]))
    monkeypatch.setattr(chat_messages, "public_display_name", lambda uid: NAMES[uid])
    monkeypatch.setattr(chat_messages, "get_ai_room", lambda *_: {"room_id": room})
    monkeypatch.setattr(chat_messages, "maybe_backfill_title", lambda *_: None)
    monkeypatch.setattr(chat_messages, "mark_room_read", lambda *_: None)
    monkeypatch.setattr(chat_messages, "project_match_choice_history", lambda rows, **_: rows)
    app = FastAPI()
    app.include_router(chat_messages.router, prefix="/api/chat")
    with TestClient(app) as client:
        response = client.get("/api/chat/messages/ai_assistant", params={"user_id": "alice", "ai_room_id": room})
    assert response.status_code == 200
    assert response.json()["messages"][0]["metadata"]["counterparty_nickname"] == "小晴"
    assert "counterparty_nickname" not in messages.rows[0]["metadata"]
    assert messages.writes == matches.writes == 0
