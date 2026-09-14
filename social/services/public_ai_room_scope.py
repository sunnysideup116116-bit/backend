"""Owner validation shared by Public Ayue room-scoped background services."""

from __future__ import annotations

from services.ai_room_service import get_room, legacy_ai_room_id


def is_owned_public_ai_room(user_id: str, room_id: str) -> bool:
    """Accept the permanent legacy room or a persisted room owned by the user.

    ``get_room`` also validates the self-describing room id before reading the
    room document. Storage failures and deleted rooms fail closed.
    """
    if not str(user_id or "").strip() or not str(room_id or "").strip():
        return False
    if str(room_id) == legacy_ai_room_id(str(user_id)):
        return True
    try:
        return get_room(str(room_id), str(user_id)) is not None
    except Exception:
        return False
