"""Canonical state transitions for the accepted-pair compatibility quiz."""

from __future__ import annotations

from copy import deepcopy
import time
from uuid import uuid4

from fastapi import HTTPException

from database import matches_coll
from services.ayue_agent.public_relationship_projection import display_name
from services.chat_service import generate_room_id, save_message
from services.mediator_event_service import queue_mediator_event


QUIZ_TTL_SECONDS = 7 * 86400
QUIZ_QUESTIONS = [
    {
        "id": "weekend",
        "text": "週末比較想怎麼安排？",
        "options": ["安靜休息", "出去走走", "找人一起活動"],
    },
    {
        "id": "first_meet",
        "text": "第一次見面你比較喜歡哪種節奏？",
        "options": ["輕鬆聊天", "一起吃飯", "做一件小活動"],
    },
    {
        "id": "chat_rhythm",
        "text": "你喜歡怎樣的聊天頻率？",
        "options": ["慢慢聊", "即時回覆", "看情況自然來"],
    },
]


def _quiz(match_doc: dict) -> dict:
    return ((match_doc.get("relationship_games", {}) or {}).get("compatibility_quiz", {}) or {})


def _revision_query(quiz: dict) -> dict:
    field = "relationship_games.compatibility_quiz.revision"
    if "revision" in quiz:
        return {field: int(quiz.get("revision", 0))}
    return {field: {"$exists": False}}


def _local_with_quiz(match_doc: dict, quiz: dict) -> dict:
    updated = deepcopy(match_doc)
    updated.setdefault("relationship_games", {})["compatibility_quiz"] = quiz
    return updated


def _modified(result) -> bool:
    return bool(getattr(result, "modified_count", 0))


def public_quiz_state(match_doc: dict, user_id: str) -> dict:
    games = match_doc.get("relationship_games", {}) or {}
    quiz = games.get("compatibility_quiz", {}) or {}
    now = time.time()
    if quiz.get("status") == "active" and quiz.get("expires_at", 0) < now:
        expired = matches_coll.update_one(
            {
                "_id": match_doc["_id"],
                "relationship_games.compatibility_quiz.status": "active",
                "relationship_games.compatibility_quiz.round_id": quiz.get("round_id"),
                "relationship_games.compatibility_quiz.expires_at": {"$lt": now},
                **_revision_query(quiz),
            },
            {
                "$set": {"relationship_games.compatibility_quiz.status": "expired"},
                "$inc": {"relationship_games.compatibility_quiz.revision": 1},
            },
        )
        if _modified(expired):
            quiz = {
                **quiz,
                "status": "expired",
                "revision": int(quiz.get("revision", 0)) + 1,
            }
        else:
            refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or match_doc
            games = refreshed.get("relationship_games", {}) or {}
            quiz = games.get("compatibility_quiz", {}) or {}
    answers = quiz.get("answers", {}) or {}
    return {
        "status": quiz.get("status", "idle"),
        "round_id": quiz.get("round_id"),
        "questions": quiz.get("questions", QUIZ_QUESTIONS),
        "my_answers": answers.get(user_id, {}),
        "my_completed": user_id in answers,
        "waiting_for_partner": quiz.get("status") == "active" and user_id in answers,
        "result": quiz.get("result") if quiz.get("status") == "completed" else None,
        "topic_box": games.get("topic_box", {}),
    }


def start_quiz(match_doc: dict, user_id: str, other_id: str) -> dict:
    current = _quiz(match_doc)
    now = time.time()
    if current.get("status") == "active" and current.get("expires_at", 0) >= now:
        return public_quiz_state(match_doc, user_id)

    quiz = {
        "round_id": f"{int(now * 1000)}-{uuid4().hex[:8]}",
        "status": "active",
        "started_by": user_id,
        "started_at": now,
        "expires_at": now + QUIZ_TTL_SECONDS,
        "revision": int(current.get("revision", 0)) + 1,
        "questions": QUIZ_QUESTIONS,
        "answers": {},
    }
    query = {"_id": match_doc["_id"], **_revision_query(current)}
    if current.get("round_id") is not None:
        query["relationship_games.compatibility_quiz.round_id"] = current.get("round_id")
        query["relationship_games.compatibility_quiz.status"] = current.get("status")
    else:
        query["relationship_games.compatibility_quiz.round_id"] = {"$exists": False}
    claimed = matches_coll.update_one(
        query,
        {"$set": {"relationship_games.compatibility_quiz": quiz}},
    )
    if _modified(claimed):
        inviter_label = display_name(user_id)
        queue_mediator_event(
            other_id, f"{inviter_label} 邀請你一起玩默契小測驗，看看你們有哪些地方合拍。",
            "compatibility_quiz_invite", match_id=str(match_doc["_id"]), other_id=user_id,
        )
        fallback = _local_with_quiz(match_doc, quiz)
    else:
        fallback = match_doc
    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or fallback
    return public_quiz_state(refreshed, user_id)


def answer_quiz(match_doc: dict, user_id: str, answers_from_user: dict[str, str]) -> dict:
    quiz = _quiz(match_doc)
    now = time.time()
    if quiz.get("status") != "active" or quiz.get("expires_at", 0) < now:
        raise HTTPException(status_code=409, detail="這輪測驗已經結束，請重新開始")

    valid_answers = {}
    for question in quiz.get("questions", QUIZ_QUESTIONS):
        answer = answers_from_user.get(question["id"])
        if answer not in question["options"]:
            raise HTTPException(status_code=422, detail=f"答案不在選項內：{question['text']}")
        valid_answers[question["id"]] = answer

    current_match = match_doc
    for _attempt in range(3):
        quiz = _quiz(current_match)
        now = time.time()
        if quiz.get("status") != "active" or quiz.get("expires_at", 0) < now:
            raise HTTPException(status_code=409, detail="這輪測驗已經結束，請重新開始")
        revision = int(quiz.get("revision", 0))
        query = {
            "_id": current_match["_id"],
            "relationship_games.compatibility_quiz.status": "active",
            "relationship_games.compatibility_quiz.round_id": quiz.get("round_id"),
            "relationship_games.compatibility_quiz.expires_at": {"$gte": now},
            **_revision_query(quiz),
        }
        written = matches_coll.update_one(
            query,
            {
                "$set": {f"relationship_games.compatibility_quiz.answers.{user_id}": valid_answers},
                "$inc": {"relationship_games.compatibility_quiz.revision": 1},
            },
        )
        if _modified(written):
            local_quiz = deepcopy(quiz)
            local_quiz.setdefault("answers", {})[user_id] = valid_answers
            local_quiz["revision"] = revision + 1
            current_match = matches_coll.find_one({"_id": current_match["_id"]}) or _local_with_quiz(current_match, local_quiz)
            break
        current_match = matches_coll.find_one({"_id": current_match["_id"]}) or current_match
    else:
        raise HTTPException(status_code=409, detail="這輪測驗剛被更新，請再送出一次")

    quiz = _quiz(current_match)
    answers = dict(quiz.get("answers", {}) or {})
    participants = {match_doc["from_user"], match_doc["to_user"]}
    if participants.issubset(answers.keys()):
        first, second = match_doc["from_user"], match_doc["to_user"]
        matches = []
        for question in quiz.get("questions", QUIZ_QUESTIONS):
            question_id = question["id"]
            if answers[first][question_id] == answers[second][question_id]:
                matches.append({
                    "question_id": question_id,
                    "question": question["text"],
                    "answer": answers[first][question_id],
                })
        completed_at = time.time()
        result = {"match_count": len(matches), "matches": matches, "total": len(QUIZ_QUESTIONS)}
        completed = matches_coll.update_one(
            {
                "_id": current_match["_id"],
                "relationship_games.compatibility_quiz.status": "active",
                "relationship_games.compatibility_quiz.round_id": quiz.get("round_id"),
                "relationship_games.compatibility_quiz.expires_at": {"$gte": completed_at},
                **_revision_query(quiz),
            },
            {
                "$set": {
                    "relationship_games.compatibility_quiz.status": "completed",
                    "relationship_games.compatibility_quiz.completed_at": completed_at,
                    "relationship_games.compatibility_quiz.result": result,
                },
                "$inc": {"relationship_games.compatibility_quiz.revision": 1},
            },
        )
        summary = (
            f"你們這輪默契測驗有 {len(matches)} 題答得一樣："
            + ("、".join(item["answer"] for item in matches) if matches else "這次沒有一樣的答案，但也可以當成新的聊天話題。")
        )
        if _modified(completed):
            save_message(
                generate_room_id(first, second), "ai_assistant", summary,
                message_type="mediator_card",
                metadata={"event_type": "compatibility_quiz_result", "result": result},
            )
            completed_quiz = {**quiz, "status": "completed", "completed_at": completed_at, "result": result, "revision": int(quiz.get("revision", 0)) + 1}
            current_match = _local_with_quiz(current_match, completed_quiz)

    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or current_match
    return public_quiz_state(refreshed, user_id)


def cancel_quiz(match_doc: dict, user_id: str) -> dict:
    quiz = _quiz(match_doc)
    if quiz.get("status") != "active":
        return public_quiz_state(match_doc, user_id)

    cancelled_at = time.time()
    cancelled = matches_coll.update_one(
        {
            "_id": match_doc["_id"],
            "relationship_games.compatibility_quiz.status": "active",
            "relationship_games.compatibility_quiz.round_id": quiz.get("round_id"),
            **_revision_query(quiz),
        },
        {
            "$set": {
                "relationship_games.compatibility_quiz.status": "cancelled",
                "relationship_games.compatibility_quiz.cancelled_by": user_id,
                "relationship_games.compatibility_quiz.cancelled_at": cancelled_at,
            },
            "$inc": {"relationship_games.compatibility_quiz.revision": 1},
        },
    )
    if _modified(cancelled):
        fallback = _local_with_quiz(match_doc, {
            **quiz, "status": "cancelled", "cancelled_by": user_id,
            "cancelled_at": cancelled_at,
            "revision": int(quiz.get("revision", 0)) + 1,
        })
    else:
        fallback = match_doc
    refreshed = matches_coll.find_one({"_id": match_doc["_id"]}) or fallback
    return public_quiz_state(refreshed, user_id)
