"""Shared, bounded context helpers for legacy public and private mediator chat."""

from __future__ import annotations

import re

from database import messages_coll, profiles_coll
from services.chat_service import generate_room_id
from services.memory_service import get_user_graph_memories


MEDIATOR_PERSONA = """

你是阿月，一個會聊天、會觀察契機的校園媒人。你不是冷冰冰的配對按鈕，而是會邊聊邊理解使用者近況、偏好、地雷與當下需求的人。

你的風格是自然、短句、有一點熟朋友的吐槽，但不能冒犯；目標是在對話裡找到適合牽線的時機，並在資訊足夠時主動幫使用者留意人選。

"""

MEDIATOR_TONES = {
    "friend": "像熟朋友一樣自然、有點嘴但不冒犯，回覆要短、有人味。",
    "gentle": "溫柔、穩定、少一點玩笑，多一點照顧感。",
    "enthusiastic": "活潑、主動、帶一點興奮感，但不要吵。",
}


def mediator_style(user_id: str) -> str:
    doc = profiles_coll.find_one({"user_id": user_id}, {"mediator_tone": 1}) or {}
    tone = doc.get("mediator_tone", "friend")
    return MEDIATOR_TONES.get(tone, MEDIATOR_TONES["friend"])


def latest_shared_chat(match_doc: dict, limit: int = 16) -> list[dict]:
    if not match_doc:
        return []
    room_id = generate_room_id(match_doc["from_user"], match_doc["to_user"])
    history = list(messages_coll.find(
        {"room_id": room_id}, {"_id": 0, "sender_id": 1, "content": 1},
    ).sort("timestamp", -1).limit(limit))[::-1]
    return [{"sender_id": item.get("sender_id"), "content": item.get("content", "")} for item in history]


def relevant_graph_memories(user_id: str, message: str, limit: int = 9) -> list[dict]:
    memories = get_user_graph_memories(user_id, 20)
    compact = re.sub(r"\s+", "", message.lower())
    grams = {compact[index:index + 2] for index in range(max(0, len(compact) - 1))}

    def relevance(item: dict) -> tuple[int, float, float]:
        haystack = " ".join(str(item.get(key, "")).lower() for key in ("key", "label", "category"))
        overlap = sum(1 for gram in grams if gram and gram in haystack)
        return overlap, float(item.get("confidence", 0)), float(item.get("last_seen_at", 0))

    related = sorted(memories, key=relevance, reverse=True)
    selected = [item for item in related if relevance(item)[0] > 0][:6]
    for item in sorted(
        memories,
        key=lambda value: (float(value.get("confidence", 0)), float(value.get("last_seen_at", 0))),
        reverse=True,
    ):
        if item not in selected and len(selected) < limit:
            selected.append(item)
    return selected


def mediator_profile_context(user_id: str, message: str) -> dict:
    doc = profiles_coll.find_one({"user_id": user_id}, {
        "_id": 0, "user_id": 1, "initial_interest": 1, "current_context": 1,
        "big_five": 1, "deep_profile": 1,
    }) or {}
    return {
        "owner_user_id": user_id,
        "initial_interest": doc.get("initial_interest"),
        "current_context": doc.get("current_context"),
        "big_five": doc.get("big_five", {}),
        "deep_profile": doc.get("deep_profile", {}),
        "graph_memories": relevant_graph_memories(user_id, message),
    }
