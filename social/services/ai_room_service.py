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
    generate_match_hub_ai_room_id,
    generate_proposal_ai_room_id,
    generate_room_id,
    is_ai_room,
)
from services.chat_service import save_system_message_once
from services.assessment_session_service import assessment_public_state_for_room
from services.notification_service import PUBLIC_AYUE, notification_unread_map


LEGACY_AI_ROOM_TITLE = "和阿月聊聊"
PROPOSAL_AI_ROOM_TITLE = "牽線提案"
MATCH_HUB_ROOM_TITLE = "阿月牽線"
MATCH_HUB_ROOM_SUBTITLE = "近期、指定主題與活動邀請都在這裡"
MATCH_HUB_ROOM_KIND = "match_hub"

# Title generation budget: each LLM call may take at most TITLE_CALL_TIMEOUT
# seconds; we retry up to TITLE_MAX_ATTEMPTS times so a transient empty reply
# (observed with the cloud model) does not leave the room untitled.
TITLE_CALL_TIMEOUT_SECONDS = 5
TITLE_MAX_ATTEMPTS = 3


def match_hub_v1_enabled() -> bool:
    """Return whether the fixed Match Hub surface is enabled.

    The flag gives deployments a safe rollback to the pre-hub room flow while
    keeping the room contract in one place.  It defaults on because the hub is
    the current product surface.
    """
    import os

    value = os.getenv("MATCH_HUB_V1", "on").strip().lower()
    return value not in {"0", "false", "off", "no", "disabled"}


def match_hub_room_id(user_id: str) -> str:
    """Return the canonical fixed hub id for one owner."""
    return generate_match_hub_ai_room_id(str(user_id))


def _match_hub_counts(user_id: str) -> tuple[int, int]:
    """Count viewer actions and proposals waiting on the other participant."""
    try:
        from database import matches_coll
        rows = matches_coll.find(
            {
                "status": {"$in": ["draft", "pending"]},
                "$or": [{"from_user": user_id}, {"to_user": user_id}],
                "proposal_suppressed": {"$ne": True},
            },
            {"from_user": 1, "to_user": 1, "status": 1},
        )
        pending = 0
        waiting = 0
        for row in rows:
            status = str(row.get("status") or "")
            if status == "draft" and row.get("from_user") == user_id:
                pending += 1
            elif status == "pending" and row.get("to_user") == user_id:
                pending += 1
            elif status == "pending" and row.get("from_user") == user_id:
                waiting += 1
        return pending, waiting
    except Exception:
        # Room listing is best effort; an unavailable count must not prevent a
        # user from opening the durable room or cached history.
        return 0, 0


def ensure_match_hub(user_id: str) -> dict | None:
    """Idempotently provision and project the user's server-owned hub."""
    if not match_hub_v1_enabled():
        return None
    owner = str(user_id or "").strip()
    if not owner:
        return None
    room_id = match_hub_room_id(owner)
    now = time.time()
    document = {
        "_id": room_id,
        "room_id": room_id,
        "user_id": owner,
        "title": MATCH_HUB_ROOM_TITLE,
        "subtitle": MATCH_HUB_ROOM_SUBTITLE,
        "room_kind": MATCH_HUB_ROOM_KIND,
        "is_pinned": True,
        "can_rename": False,
        "can_delete": False,
        "needs_title": False,
        "created_at": now,
        "updated_at": now,
        "is_legacy": False,
        "is_new": False,
    }
    try:
        ai_rooms_coll.update_one(
            {"room_id": room_id, "user_id": owner},
            {
                "$setOnInsert": document,
                "$set": {
                    "title": MATCH_HUB_ROOM_TITLE,
                    "subtitle": MATCH_HUB_ROOM_SUBTITLE,
                    "room_kind": MATCH_HUB_ROOM_KIND,
                    "is_pinned": True,
                    "can_rename": False,
                    "can_delete": False,
                    "updated_at": now,
                },
            },
            upsert=True,
        )
        stored = ai_rooms_coll.find_one({"room_id": room_id, "user_id": owner}, {"_id": 0})
    except Exception as exc:
        # A temporary storage outage should preserve the legacy room fallback.
        print(f"[ai_room_service] match hub provision skipped: {type(exc).__name__}")
        stored = None
    return _project(stored or document)


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
        "room_kind": "conversation",
        "is_pinned": False,
        "can_rename": True,
        "can_delete": True,
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
                "is_legacy_proposal": True,
                "room_kind": "legacy_proposal",
                "is_pinned": False,
                "can_rename": False,
                "can_delete": False,
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
    if room_id == match_hub_room_id(user_id):
        return ensure_match_hub(user_id)
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
    unread_by_room = notification_unread_map(user_id, PUBLIC_AYUE)

    hub = ensure_match_hub(user_id)
    if hub is not None:
        pending_count, waiting_count = _match_hub_counts(user_id)
        hub.update({
            "pending_action_count": pending_count,
            "waiting_other_count": waiting_count,
            "pending_count": pending_count,
            "unread_count": int(unread_by_room.get(hub["room_id"], 0)),
        })
        hub["latest_message"] = _latest_message(hub["room_id"])
        rooms.append(hub)

    # Legacy room (synthesized).
    legacy_id = legacy_ai_room_id(user_id)
    legacy_latest = _latest_message(legacy_id)
    rooms.append({
        "room_id": legacy_id,
        "title": LEGACY_AI_ROOM_TITLE,
        "needs_title": False,
        "is_legacy": True,
        "is_new": False,
        "room_kind": "legacy",
        "is_pinned": False,
        "can_rename": False,
        "can_delete": False,
        "subtitle": "性格探索與一般對話",
        "created_at": 0.0,
        "updated_at": legacy_latest or now,
        "latest_message": legacy_latest,
        "unread_count": int(unread_by_room.get(legacy_id, 0)),
    })

    hub_id = match_hub_room_id(user_id)
    for doc in ai_rooms_coll.find({"user_id": user_id}, {"_id": 0}):
        if str(doc.get("room_id") or doc.get("_id") or "") == hub_id:
            continue
        proj = _project(doc)
        latest = _latest_message(doc["room_id"])
        proj["updated_at"] = latest or doc.get("updated_at") or doc.get("created_at") or now
        proj["latest_message"] = latest
        proj["unread_count"] = int(unread_by_room.get(str(doc.get("room_id") or ""), 0))
        rooms.append(proj)

    if assessment_profile is not None:
        for room in rooms:
            room.update(assessment_public_state_for_room(
                assessment_profile,
                str(room.get("room_id") or ""),
                include_unscoped=bool(room.get("is_legacy")),
            ))

    rooms.sort(
        key=lambda r: (not bool(r.get("is_pinned")), -(float(r.get("updated_at") or 0))),
    )
    return rooms


def rename_room(room_id: str, user_id: str, title: str) -> dict | None:
    """Rename a non-legacy AI room. Returns the updated projection or None."""
    if room_id in {legacy_ai_room_id(user_id), match_hub_room_id(user_id)}:
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
    if room_id in {legacy_ai_room_id(user_id), match_hub_room_id(user_id)}:
        return False
    if ai_room_owner(room_id) != user_id:
        return False
    result = ai_rooms_coll.delete_one({"room_id": room_id, "user_id": user_id})
    return bool(getattr(result, "deleted_count", 0))


def most_recent_ai_room(user_id: str, *, include_proposal_rooms: bool = True) -> str:
    """The room id where proactive care / system messages should land.

    Falls back to the legacy room when the user has no extra rooms or no room
    has any messages yet.
    """
    rooms = list_rooms(user_id)
    for room in rooms:
        if not include_proposal_rooms and (
            "::proposal::" in str(room.get("room_id") or "")
            or room.get("room_kind") == "legacy_proposal"
        ):
            continue
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
    room_id = str(doc.get("room_id") or doc.get("_id") or "")
    room_kind = str(doc.get("room_kind") or "conversation")
    is_hub = room_kind == MATCH_HUB_ROOM_KIND or room_id.endswith("::match_hub")
    # Older proposal rooms were written with only ``is_proposal_room`` (or
    # only their deterministic id). Infer the historical kind at read time so
    # clients can group and lock them without a data migration.
    is_legacy_proposal = bool(
        not is_hub
        and (
            doc.get("is_legacy_proposal")
            or doc.get("is_proposal_room")
            or "::proposal::" in room_id
        )
    )
    if is_legacy_proposal:
        room_kind = "legacy_proposal"
    is_legacy = bool(doc.get("is_legacy", False))
    return {
        "room_id": doc.get("room_id") or doc.get("_id"),
        "title": MATCH_HUB_ROOM_TITLE if is_hub else (LEGACY_AI_ROOM_TITLE if is_legacy else doc.get("title")),
        "subtitle": MATCH_HUB_ROOM_SUBTITLE if is_hub else doc.get("subtitle"),
        "room_kind": MATCH_HUB_ROOM_KIND if is_hub else ("legacy" if is_legacy else room_kind),
        "is_pinned": True if is_hub else bool(doc.get("is_pinned", False)),
        "can_rename": False if is_hub or is_legacy or is_legacy_proposal else bool(doc.get("can_rename", True)),
        "can_delete": False if is_hub or is_legacy or is_legacy_proposal else bool(doc.get("can_delete", True)),
        "needs_title": bool(doc.get("needs_title")),
        "is_legacy": is_legacy,
        "is_legacy_proposal": is_legacy_proposal,
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
        "subtitle": "性格探索與一般對話",
        "room_kind": "legacy",
        "is_pinned": False,
        "can_rename": False,
        "can_delete": False,
        "needs_title": False,
        "is_legacy": True,
        "created_at": 0.0,
        "updated_at": latest or time.time(),
        "latest_message": latest,
    }
