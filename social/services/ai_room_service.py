"""Multi-room AI chat surface for Public Ayue.

A user can open many independent conversations with 阿月. Each room keeps its
own message history (keyed by ``room_id`` in ``messages_coll``) while the
agent context (memories, match state, profile) stays user-global.

Room ids are namespaced and self-validating (``ai_room::{user_id}::{nonce}``)
so ownership can be checked without a lookup. The legacy single room
(``generate_room_id(user_id, "ai_assistant")``) is preserved permanently: it
keeps onboarding, proactive care, old unscoped assessment drafts, and old
clients working, and it always appears in the room list.
"""

from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError

from database import ai_rooms_coll, messages_coll
from services.chat_service import (
    ai_room_owner,
    generate_ai_room_id,
    generate_proposal_ai_room_id,
    generate_room_id,
    is_ai_room,
)
from services.chat_service import save_system_message_once
from services.assessment_session_service import assessment_public_state_for_room


LEGACY_AI_ROOM_TITLE = "找阿月配對"
PROPOSAL_AI_ROOM_TITLE = "牽線提案"

# Title generation budget: each LLM call may take at most TITLE_CALL_TIMEOUT
# seconds; we retry up to TITLE_MAX_ATTEMPTS times so a transient empty reply
# (observed with the cloud model) does not leave the room untitled.
TITLE_CALL_TIMEOUT_SECONDS = 5
TITLE_MAX_ATTEMPTS = 3


def legacy_ai_room_id(user_id: str) -> str:
    """The original permanent AI room id for a user."""
    return generate_room_id(user_id, "ai_assistant")


def create_room(user_id: str) -> dict:
    """Create a new empty AI room owned by ``user_id`` and return its projection."""
    room_id = generate_ai_room_id(user_id)
    now = time.time()
    doc = {
        "_id": room_id,
        "room_id": room_id,
        "user_id": user_id,
        "title": None,
        "needs_title": False,
        "created_at": now,
        "updated_at": now,
        "is_legacy": False,
    }
    ai_rooms_coll.insert_one(doc)
    return _project(doc)


def create_proposal_room(
    user_id: str,
    match_id: str,
    message: str,
    *,
    metadata: dict | None = None,
    event_key: str = "",
) -> dict:
    """Create (or reuse) the deterministic AI room for one match proposal.

    The room id is derived from ``user_id`` and ``match_id`` so follow-up
    events for the same match land in the same room. The proposal card is
    persisted with :func:`save_system_message_once`, so redelivering the same
    event never duplicates the card. The room starts titled ``牽線提案`` and
    flagged ``is_new`` until the user opens it.
    """
    room_id = generate_proposal_ai_room_id(user_id, match_id)
    now = time.time()
    ai_rooms_coll.update_one(
        {"room_id": room_id, "user_id": user_id},
        {
            "$setOnInsert": {
                "_id": room_id,
                "room_id": room_id,
                "user_id": user_id,
                "title": PROPOSAL_AI_ROOM_TITLE,
                "needs_title": False,
                "created_at": now,
                "is_legacy": False,
                "is_proposal_room": True,
                "match_id": match_id,
            },
            "$set": {"updated_at": now, "is_new": True},
        },
        upsert=True,
    )
    save_system_message_once(
        room_id,
        message,
        message_type="mediator_card",
        metadata=metadata or {},
        event_key=event_key or f"proposal:{match_id}",
    )
    return get_room(room_id, user_id) or _project(
        {
            "room_id": room_id,
            "user_id": user_id,
            "title": PROPOSAL_AI_ROOM_TITLE,
            "needs_title": False,
            "is_legacy": False,
            "created_at": now,
            "updated_at": now,
        }
    )


def mark_room_read(room_id: str, user_id: str) -> bool:
    """Clear the NEW flag on a non-legacy AI room when the user opens it."""
    if room_id == legacy_ai_room_id(user_id) or ai_room_owner(room_id) != user_id:
        return False
    result = ai_rooms_coll.update_one(
        {"room_id": room_id, "user_id": user_id},
        {"$unset": {"is_new": 1}},
    )
    return bool(getattr(result, "modified_count", 0))


def get_room(room_id: str, user_id: str) -> dict | None:
    """Return a room projection if ``room_id`` belongs to ``user_id``, else None.

    The legacy room has no document in ``ai_rooms_coll``; it is synthesized so
    callers can treat it uniformly.
    """
    if room_id == legacy_ai_room_id(user_id):
        return _legacy_projection(user_id)
    if ai_room_owner(room_id) != user_id:
        return None
    doc = ai_rooms_coll.find_one({"room_id": room_id}, {"_id": 0})
    if not doc:
        return None
    return _project(doc)


def list_rooms(
    user_id: str,
    *,
    assessment_profile: dict | None = None,
) -> list[dict]:
    """All AI rooms for ``user_id`` ordered by most recent activity.

    Activity is derived from the room's latest message timestamp, falling back
    to the room document's ``updated_at``. The legacy room is always included
    and ordered by activity like any other room (not pinned).
    """
    now = time.time()
    rooms: list[dict] = []

    # Legacy room (synthesized).
    legacy_id = legacy_ai_room_id(user_id)
    legacy_latest = _latest_message(legacy_id)
    rooms.append({
        "room_id": legacy_id,
        "title": LEGACY_AI_ROOM_TITLE,
        "needs_title": False,
        "is_legacy": True,
        "is_new": False,
        "created_at": 0.0,
        "updated_at": legacy_latest or now,
        "latest_message": legacy_latest,
    })

    for doc in ai_rooms_coll.find({"user_id": user_id}, {"_id": 0}):
        proj = _project(doc)
        latest = _latest_message(doc["room_id"])
        proj["updated_at"] = latest or doc.get("updated_at") or doc.get("created_at") or now
        proj["latest_message"] = latest
        rooms.append(proj)

    if assessment_profile is not None:
        for room in rooms:
            room.update(assessment_public_state_for_room(
                assessment_profile,
                str(room.get("room_id") or ""),
                include_unscoped=bool(room.get("is_legacy")),
            ))

    rooms.sort(key=lambda r: r.get("updated_at") or 0, reverse=True)
    return rooms


def rename_room(room_id: str, user_id: str, title: str) -> dict | None:
    """Rename a non-legacy AI room. Returns the updated projection or None."""
    if room_id == legacy_ai_room_id(user_id):
        return None  # legacy room is not renameable
    if ai_room_owner(room_id) != user_id:
        return None
    cleaned = (title or "").strip()
    if not cleaned:
        return None
    ai_rooms_coll.update_one(
        {"room_id": room_id, "user_id": user_id},
        {"$set": {"title": cleaned[:60], "needs_title": False, "updated_at": time.time()}},
    )
    return get_room(room_id, user_id)


def delete_room(room_id: str, user_id: str) -> bool:
    """Delete a non-legacy AI room document only.

    Per the product decision, message history stays in ``messages_coll`` as a
    backup; only the room-list entry is removed.
    """
    if room_id == legacy_ai_room_id(user_id):
        return False
    if ai_room_owner(room_id) != user_id:
        return False
    result = ai_rooms_coll.delete_one({"room_id": room_id, "user_id": user_id})
    return bool(getattr(result, "deleted_count", 0))


def most_recent_ai_room(user_id: str) -> str:
    """The room id where proactive care / system messages should land.

    Falls back to the legacy room when the user has no extra rooms or no room
    has any messages yet.
    """
    rooms = list_rooms(user_id)
    for room in rooms:
        if room.get("latest_message"):
            return room["room_id"]
    return legacy_ai_room_id(user_id)


def ensure_room_title(room_id: str, user_id: str, first_message: str) -> None:
    """Generate a title from the first user message via one extra LLM call.

    Called synchronously after the first message in a new room is persisted.
    Each LLM attempt is bounded by :data:`TITLE_CALL_TIMEOUT_SECONDS` and the
    call retries up to :data:`TITLE_MAX_ATTEMPTS` times. On any failure the
    room stays ``needs_title=True`` so the next time the user opens the room
    :func:`maybe_backfill_title` can retry.
    """
    if room_id == legacy_ai_room_id(user_id):
        return
    if ai_room_owner(room_id) != user_id:
        return
    doc = ai_rooms_coll.find_one({"room_id": room_id}, {"_id": 0, "needs_title": 1})
    if not doc or doc.get("needs_title") is False:
        return
    text = (first_message or "").strip()
    if not text:
        return
    try:
        from services.ai_service import generate_chat_completion

        prompt = (
            "請從下面這句使用者開場白，提煉一個 4 到 8 個字的中文聊天標題。"
            "規則：必須客觀描述話題本身，禁止使用「你、我、他、她、它、妳」等代名詞，"
            "可以出現具體人名或事物名稱；只輸出標題本身，不要引號、不要標點、不要任何說明。\n\n"
            f"使用者開場白：{text[:200]}"
        )
        title = _generate_title_with_retry(prompt)
        if title:
            ai_rooms_coll.update_one(
                {"room_id": room_id},
                {"$set": {"title": title, "needs_title": False, "updated_at": time.time()}},
            )
            return
    except Exception as exc:  # noqa: BLE001 - best-effort title
        print(f"[ai_room_service] title generation failed: {type(exc).__name__}: {exc}")
    # Mark as still needing a title so the next room open can retry.
    ai_rooms_coll.update_one(
        {"room_id": room_id},
        {"$set": {"needs_title": True, "updated_at": time.time()}},
    )


def _generate_title_with_retry(prompt: str) -> str:
    """Run the title LLM call with a per-attempt timeout and retries.

    The cloud model occasionally returns an empty completion; a short timeout
    plus a few retries makes title generation reliable without blocking the
    chat turn for long.
    """
    executor = ThreadPoolExecutor(max_workers=1)

    def _attempt() -> str:
        from services.ai_service import generate_chat_completion

        result = generate_chat_completion(prompt, temperature=0.3, max_tokens=120)
        return (result.content or "").strip().splitlines()[0].strip()[:60]

    try:
        for _attempt_index in range(TITLE_MAX_ATTEMPTS):
            try:
                future = executor.submit(_attempt)
                title = future.result(timeout=TITLE_CALL_TIMEOUT_SECONDS)
            except TimeoutError:
                print("[ai_room_service] title call timed out; retrying")
                continue
            except Exception as exc:  # noqa: BLE001
                print(f"[ai_room_service] title call error: {type(exc).__name__}: {exc}")
                continue
            if title:
                return title
            print("[ai_room_service] title call returned empty; retrying")
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return ""


def maybe_backfill_title(room_id: str, user_id: str) -> None:
    """Retry title generation when opening a room whose title is still pending."""
    if room_id == legacy_ai_room_id(user_id):
        return
    if ai_room_owner(room_id) != user_id:
        return
    doc = ai_rooms_coll.find_one({"room_id": room_id}, {"_id": 0})
    if not doc or not doc.get("needs_title"):
        return
    first_user_msg = messages_coll.find_one(
        {"room_id": room_id, "sender_id": user_id},
        {"_id": 0, "content": 1},
        sort=[("timestamp", 1)],
    )
    first_message = str((first_user_msg or {}).get("content") or "")
    if first_message:
        ensure_room_title(room_id, user_id, first_message)


def mark_first_message_for_title(room_id: str, user_id: str) -> bool:
    """Flag a newly created room as awaiting a title from its first message.

    Returns True when the flag was set (i.e. this is the first user message in
    the room). Caller uses this to decide whether to run the extra LLM call.
    """
    if room_id == legacy_ai_room_id(user_id) or ai_room_owner(room_id) != user_id:
        return False
    existing = messages_coll.count_documents({"room_id": room_id, "sender_id": user_id})
    if existing > 1:
        return False
    ai_rooms_coll.update_one(
        {"room_id": room_id},
        {"$set": {"needs_title": True, "updated_at": time.time()}},
        upsert=False,
    )
    return True


# --- helpers ---

def _latest_message(room_id: str) -> float:
    msg = messages_coll.find_one(
        {"room_id": room_id}, {"_id": 0, "timestamp": 1}, sort=[("timestamp", -1)]
    )
    return float((msg or {}).get("timestamp") or 0)


def _project(doc: dict) -> dict:
    return {
        "room_id": doc.get("room_id") or doc.get("_id"),
        "title": doc.get("title"),
        "needs_title": bool(doc.get("needs_title")),
        "is_legacy": bool(doc.get("is_legacy", False)),
        "is_new": bool(doc.get("is_new", False)),
        "created_at": float(doc.get("created_at") or 0),
        "updated_at": float(doc.get("updated_at") or 0),
        "latest_message": 0.0,
    }


def _legacy_projection(user_id: str) -> dict:
    room_id = legacy_ai_room_id(user_id)
    latest = _latest_message(room_id)
    return {
        "room_id": room_id,
        "title": LEGACY_AI_ROOM_TITLE,
        "needs_title": False,
        "is_legacy": True,
        "created_at": 0.0,
        "updated_at": latest or time.time(),
        "latest_message": latest,
    }
