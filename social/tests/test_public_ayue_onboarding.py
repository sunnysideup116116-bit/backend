from types import SimpleNamespace
from unittest.mock import patch

from pymongo.errors import DuplicateKeyError

from services.ayue_agent import onboarding


def _record(room_id: str, user_id: str = "owner") -> dict:
    return {
        "_id": onboarding._onboarding_account_message_id(user_id),
        "room_id": room_id,
        "sender_id": "ai_assistant",
        "content": onboarding.PUBLIC_AYUE_ONBOARDING_MESSAGE,
        "message_type": "text",
        "metadata": {
            "event_type": "public_ayue_onboarding",
            "onboarding_version": 2,
            "notification_eligible": False,
        },
        "timestamp": 123.0,
    }


def test_ensure_onboarding_persists_one_legacy_room_message_and_reuses_it():
    room_id = onboarding.generate_room_id("owner", "ai_assistant")
    stored = _record(room_id)
    with patch.object(onboarding, "messages_coll") as messages, \
         patch.object(onboarding, "profiles_coll") as profiles, \
         patch.object(onboarding, "ai_rooms_coll") as ai_rooms, \
         patch.object(onboarding, "mirror_message_to_appwrite_async") as mirror:
        messages.find_one.side_effect = [None, stored, stored]
        messages.count_documents.return_value = 0
        ai_rooms.find.return_value = []
        messages.update_one.return_value = SimpleNamespace(upserted_id=stored["_id"])
        profiles.find_one.return_value = {}

        first = onboarding.ensure_public_ayue_onboarding("owner")
        second = onboarding.ensure_public_ayue_onboarding("owner")

    assert first["version"] == 2
    assert first["created"] is True
    assert first["message"] == {
        "message_id": stored["_id"],
        "content": onboarding.PUBLIC_AYUE_ONBOARDING_MESSAGE,
        "sender_id": "ai_assistant",
        "message_type": "text",
        "metadata": stored["metadata"],
        "timestamp": 123.0,
    }
    assert second["created"] is False
    assert second["message"]["message_id"] == first["message"]["message_id"]
    mirror.assert_called_once()
    assert profiles.update_one.call_count == 2


def test_ensure_onboarding_supports_empty_normal_room_but_skips_hub_and_proposal():
    normal_room = "ai_room::owner::normal"
    with patch.object(onboarding, "messages_coll") as messages, \
         patch.object(onboarding, "profiles_coll") as profiles, \
         patch.object(onboarding, "ai_rooms_coll") as ai_rooms, \
         patch.object(onboarding, "mirror_message_to_appwrite_async"), \
         patch("services.ai_room_service.get_room") as get_room:
        messages.find_one.side_effect = [None, _record(normal_room), None, None]
        messages.count_documents.return_value = 0
        messages.update_one.return_value = SimpleNamespace(upserted_id="new")
        profiles.find_one.return_value = {}
        ai_rooms.find.return_value = []
        get_room.return_value = {"room_kind": "conversation"}

        created = onboarding.ensure_public_ayue_onboarding(
            "owner", room_id=normal_room,
        )

        get_room.return_value = {"room_kind": "match_hub"}
        hub = onboarding.ensure_public_ayue_onboarding(
            "owner", room_id="ai_room::owner::match_hub",
        )
        get_room.return_value = {"room_kind": "legacy_proposal", "is_proposal_room": True}
        proposal = onboarding.ensure_public_ayue_onboarding(
            "owner", room_id="ai_room::owner::proposal::match-1",
        )

    assert created["created"] is True
    assert hub == {"version": 2, "created": False, "message": None}
    assert proposal == {"version": 2, "created": False, "message": None}
    assert messages.update_one.call_count == 1


def test_existing_legacy_history_is_marked_complete_without_inserting_greeting():
    with patch.object(onboarding, "messages_coll") as messages, \
         patch.object(onboarding, "profiles_coll") as profiles, \
         patch.object(onboarding, "ai_rooms_coll") as ai_rooms:
        messages.find_one.return_value = None
        messages.count_documents.return_value = 1
        profiles.find_one.return_value = {}
        ai_rooms.find.return_value = []

        result = onboarding.ensure_public_ayue_onboarding("owner")

    assert result == {"version": 2, "created": False, "message": None}
    messages.update_one.assert_not_called()
    profiles.update_one.assert_called_once()


def test_existing_content_in_another_normal_room_suppresses_selected_room_greeting():
    normal_room = "ai_room::owner::empty"
    with patch.object(onboarding, "messages_coll") as messages, \
         patch.object(onboarding, "profiles_coll") as profiles, \
         patch.object(onboarding, "ai_rooms_coll") as ai_rooms:
        messages.find_one.return_value = None
        messages.count_documents.return_value = 1
        profiles.find_one.return_value = {}
        ai_rooms.find.return_value = [
            {"room_id": "ai_room::owner::already-used", "room_kind": "conversation"},
            {"room_id": "ai_room::owner::match_hub", "room_kind": "match_hub"},
            {"room_id": "ai_room::owner::proposal::old", "room_kind": "legacy_proposal"},
        ]
        with patch("services.ai_room_service.get_room", return_value={"room_kind": "conversation"}):
            result = onboarding.ensure_public_ayue_onboarding(
                "owner", room_id=normal_room,
            )

    assert result == {"version": 2, "created": False, "message": None}
    messages.update_one.assert_not_called()
    profiles.update_one.assert_called_once()


def test_account_event_id_prevents_second_empty_room_from_getting_a_copy():
    room_a = "ai_room::owner::first"
    room_b = "ai_room::owner::second"
    stored = _record(room_a)
    with patch.object(onboarding, "messages_coll") as messages, \
         patch.object(onboarding, "profiles_coll") as profiles, \
         patch.object(onboarding, "ai_rooms_coll") as ai_rooms, \
         patch.object(onboarding, "mirror_message_to_appwrite_async"):
        messages.find_one.side_effect = [None, stored, stored]
        messages.count_documents.return_value = 0
        messages.update_one.return_value = SimpleNamespace(upserted_id=stored["_id"])
        profiles.find_one.return_value = {}
        ai_rooms.find.return_value = []
        with patch("services.ai_room_service.get_room", return_value={"room_kind": "conversation"}):
            first = onboarding.ensure_public_ayue_onboarding("owner", room_id=room_a)
            second = onboarding.ensure_public_ayue_onboarding("owner", room_id=room_b)

    assert first["created"] is True
    assert second == {"version": 2, "created": False, "message": None}
    assert messages.update_one.call_count == 1


def test_second_room_handles_account_event_duplicate_without_local_merge():
    room_b = "ai_room::owner::second"
    stored = _record("ai_room::owner::first")
    with patch.object(onboarding, "messages_coll") as messages, \
         patch.object(onboarding, "profiles_coll") as profiles, \
         patch.object(onboarding, "ai_rooms_coll") as ai_rooms:
        messages.find_one.side_effect = [None, stored]
        messages.count_documents.return_value = 0
        messages.update_one.side_effect = DuplicateKeyError("account event already exists")
        profiles.find_one.return_value = {}
        ai_rooms.find.return_value = []
        with patch("services.ai_room_service.get_room", return_value={"room_kind": "conversation"}):
            result = onboarding.ensure_public_ayue_onboarding("owner", room_id=room_b)

    assert result == {"version": 2, "created": False, "message": None}
