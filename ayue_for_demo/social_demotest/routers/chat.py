"""Aggregate router for the chat API surface.

Leaf routers own their HTTP adapters. This module keeps the one `/api` prefix
and `Chat` OpenAPI tag required by the existing application contract.
"""

from fastapi import APIRouter

from routers.chat_messages import router as chat_messages_router
from routers.chat_onboarding import router as chat_onboarding_router
from routers.demo import router as demo_router
from routers.private_mediator import router as private_mediator_router
from routers.proactive import router as proactive_router
from routers.public_chat import router as public_chat_router
from routers.relationship_dates import router as relationship_dates_router
from routers.relationship_quiz import router as relationship_quiz_router


router = APIRouter(prefix="/api", tags=["Chat"])
router.include_router(chat_onboarding_router)
router.include_router(chat_messages_router)
router.include_router(relationship_dates_router)
router.include_router(demo_router)
router.include_router(proactive_router)
router.include_router(private_mediator_router)
router.include_router(relationship_quiz_router)
router.include_router(public_chat_router)
