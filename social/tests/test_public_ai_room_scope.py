from unittest.mock import patch

from services import public_ai_room_scope as scope


def test_legacy_room_is_accepted_without_a_storage_read():
    with patch.object(scope, "get_room", side_effect=RuntimeError("must not read")) as read:
        assert scope.is_owned_public_ai_room("owner", "ai_assistant_owner") is True
    read.assert_not_called()


def test_persisted_room_must_resolve_for_the_same_owner():
    with patch.object(scope, "get_room", return_value={"room_id": "ai_room::owner::one"}) as read:
        assert scope.is_owned_public_ai_room("owner", "ai_room::owner::one") is True
    read.assert_called_once_with("ai_room::owner::one", "owner")


def test_foreign_deleted_or_unavailable_room_fails_closed():
    with patch.object(scope, "get_room", return_value=None):
        assert scope.is_owned_public_ai_room("owner", "ai_room::other::one") is False
    with patch.object(scope, "get_room", side_effect=RuntimeError("storage unavailable")):
        assert scope.is_owned_public_ai_room("owner", "ai_room::owner::one") is False
