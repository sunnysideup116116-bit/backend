"""Owner-authenticated bootstrap. No client-selected owner or Pi write tool."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from services.appwrite_identity_service import authenticate_owner, AppwriteIdentityError
from services import preference_bootstrap_service as service
from matchmaker_agent.preference_bootstrap_contract import BootstrapError
from matchmaker_agent.preference_bootstrap_api import Consent

router = APIRouter(prefix="/api/profile/preferences/bootstrap", tags=["Preference bootstrap"])


def authenticated(request: Request):
    try:
        return authenticate_owner(request.headers.get("authorization"))
    except AppwriteIdentityError as exc:
        raise HTTPException(exc.status_code, detail={"code": exc.code}) from None


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PreviewInput(Input):
    mode: Literal["add_only", "complete_set"]
    prefers: list[str] = Field(max_length=5)
    avoids: list[str] = Field(max_length=5)  # Required even when explicitly empty.


class CommitInput(Input):
    preview_token: str = Field(min_length=1, max_length=524288)
    consent: Consent


class RollbackInput(Input):
    operation_id: str
    revision: int = Field(ge=0)
    confirmed: StrictBool


def invoke(fn, *args):
    try:
        return fn(*args)
    except BootstrapError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    except Exception:
        raise HTTPException(503, detail={"code": "bootstrap_unavailable"}) from None


@router.post("/preview")
def preview(payload: PreviewInput, owner: str = Depends(authenticated)):
    return invoke(service.preview, owner, payload.mode, payload.prefers, payload.avoids)


@router.post("/commit")
def commit(payload: CommitInput, owner: str = Depends(authenticated)):
    return invoke(service.commit, owner, payload.preview_token, payload.consent.model_dump())


@router.get("/operations/{operation_id}")
def status(operation_id: str, owner: str = Depends(authenticated)):
    # Reading operation status is not permission to mutate or reconcile.
    def read():
        service.operation_id(operation_id)
        intent = service.OPERATIONS.find_one({"_id": service._key(owner, operation_id), "owner": owner})
        service.require(intent is not None, "operation_not_found", 404)
        return {**service._public(intent.get("graph_record")), "operation_id": operation_id,
                "reconciliation_state": intent["status"]}
    return invoke(read)


@router.post("/operations/{operation_id}/reconcile")
def reconcile(operation_id: str, owner: str = Depends(authenticated)):
    return invoke(service.reconcile, owner, operation_id)


@router.post("/rollback")
def rollback(payload: RollbackInput, owner: str = Depends(authenticated)):
    return invoke(service.rollback, owner, payload.operation_id, payload.revision, payload.confirmed)
