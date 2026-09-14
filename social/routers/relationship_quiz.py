"""Thin HTTP adapters for accepted-pair compatibility quiz actions."""

from fastapi import APIRouter, HTTPException

from models import RelationshipGameRequest, RelationshipQuizAnswerRequest
from services.relationship_engagement_service import find_accepted_match
from services.relationship_quiz_service import answer_quiz, cancel_quiz, public_quiz_state, start_quiz


router = APIRouter()


def _accepted_match_or_403(
    user_id: str, other_id: str, detail: str = "只能在已接受配對中使用互動小遊戲",
) -> dict:
    match_doc = find_accepted_match(user_id, other_id)
    if not match_doc:
        raise HTTPException(status_code=403, detail=detail)
    return match_doc


@router.get("/relationship/fun/{other_id}")
def relationship_fun_state(other_id: str, user_id: str):
    return public_quiz_state(_accepted_match_or_403(user_id, other_id), user_id)


@router.post("/relationship/quiz/start")
def start_relationship_quiz(req: RelationshipGameRequest):
    return start_quiz(_accepted_match_or_403(req.user_id, req.other_id), req.user_id, req.other_id)


@router.post("/relationship/quiz/answer")
def answer_relationship_quiz(req: RelationshipQuizAnswerRequest):
    return answer_quiz(
        _accepted_match_or_403(req.user_id, req.other_id), req.user_id, req.answers,
    )


@router.post("/relationship/quiz/cancel")
def cancel_relationship_quiz(req: RelationshipGameRequest):
    return cancel_quiz(
        _accepted_match_or_403(
            req.user_id, req.other_id, "只能在已接受配對中取消測驗",
        ),
        req.user_id,
    )
