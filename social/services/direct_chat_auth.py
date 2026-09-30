"""Bind direct-chat HTTP requests to the authenticated Appwrite sender.

The pair-chat quota decorator deliberately does not charge normal messages.
Authentication is a separate HTTP dependency so that exemption cannot become
permission to submit another participant's ``user_id``.
"""

from fastapi import HTTPException, Request
from starlette.concurrency import run_in_threadpool

from models import DirectChatRequest
from services.appwrite_identity_service import AppwriteIdentityError, authenticate_owner


MAX_DIRECT_CHAT_BODY_BYTES = 64 * 1024
MAX_DIRECT_CHAT_MESSAGE_CHARS = 8000


async def require_direct_chat_owner(req: DirectChatRequest, request: Request) -> str:
    if len(await request.body()) > MAX_DIRECT_CHAT_BODY_BYTES:
        raise HTTPException(status_code=413, detail={"code": "direct_chat_request_too_large"})
    if len(req.message) > MAX_DIRECT_CHAT_MESSAGE_CHARS:
        raise HTTPException(status_code=422, detail={"code": "direct_chat_input_too_long"})
    try:
        owner = await run_in_threadpool(authenticate_owner, request.headers.get("Authorization"))
    except AppwriteIdentityError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.code) from exc
    if owner != req.user_id:
        raise HTTPException(status_code=403, detail="appwrite_user_mismatch")
    request.state.trusted_owner_id = owner
    return owner
