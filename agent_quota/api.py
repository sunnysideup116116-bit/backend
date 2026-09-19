"""Authenticated quota endpoints and existing text endpoint integration."""
import inspect
from functools import wraps
from typing import get_type_hints

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from services.appwrite_identity_service import authenticate_owner, AppwriteIdentityError
from .service import service, QuotaError, task_scope

router = APIRouter(prefix='/api/agent-quota', tags=['Agent quota'])


def owner_from_request(request):
    try:
        return authenticate_owner(request.headers.get('authorization') if request else None)
    except AppwriteIdentityError as exc:
        raise HTTPException(exc.status_code, detail=exc.code) from exc


def require_quota(owner):
    try:
        service.check(owner)
    except QuotaError as exc:
        raise HTTPException(403 if exc.code == 'agent_quota_exhausted' else 503,
                            detail={'code': exc.code, 'message': exc.message}) from exc


@router.get('')
def quota_status(request: Request):
    try:
        return JSONResponse(service.status(owner_from_request(request)),
                            headers={'Cache-Control': 'private, no-store'})
    except QuotaError as exc:
        raise HTTPException(503, detail={'code': exc.code, 'message': exc.message}) from exc


class RedeemRequest(BaseModel):
    code: str = Field(min_length=1, max_length=128)


@router.post('/redeem')
def redeem_quota(req: RedeemRequest, request: Request):
    owner = owner_from_request(request)
    try:
        result = service.redeem(owner, req.code)
        return {'status': result, 'quota': service.status(owner)}
    except QuotaError as exc:
        raise HTTPException(503, detail={'code': exc.code, 'message': exc.message}) from exc


def budgeted(feature):
    def decorate(function):
        signature = inspect.signature(function)
        @wraps(function)
        def wrapped(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            req = bound.arguments['req']
            if feature == 'matching' and getattr(req, 'contact_id', 'ai_assistant') != 'ai_assistant':
                return function(*args, **kwargs)
            owner = owner_from_request(bound.arguments.get('request'))
            if owner != req.user_id:
                raise HTTPException(403, detail='appwrite_user_mismatch')
            require_quota(owner)
            with task_scope(owner, feature):
                return function(*args, **kwargs)
        # FastAPI must resolve postponed annotations in the original module.
        hints = get_type_hints(function)
        wrapped.__signature__ = signature.replace(parameters=[
            p.replace(annotation=hints.get(n, p.annotation)) for n, p in signature.parameters.items()
        ], return_annotation=hints.get('return', signature.return_annotation))
        return wrapped
    return decorate
