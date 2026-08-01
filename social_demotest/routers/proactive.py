"""HTTP adapter for safe mediator/proactive delivery polling."""

from fastapi import APIRouter

from services.proactive_delivery_service import proactive_check as run_proactive_check


router = APIRouter()


@router.get("/proactive_check")
def proactive_check(user_id: str, conversation_active: bool = False):
    return run_proactive_check(user_id, conversation_active)
