"""Private Social→9001 bootstrap boundary with a distinct signed scope."""
import os

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool
from neo4j import GraphDatabase
from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from .preference_bootstrap import BootstrapGraph
from .preference_bootstrap_contract import (
    BootstrapError, operation_id, owner_id, verify_internal,
    COMPLETE_SET_MAX_ITEMS, validate_bootstrap_item_count,
)
from .preference_write_fence import PreferenceFenceError
from .concept_identity import PreferenceTextError

router = APIRouter(prefix="/api/v2/preferences/bootstrap", tags=["Internal preference bootstrap"])


class InternalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(min_length=1, max_length=128)


class PreviewRequest(InternalRequest):
    mode: str
    prefers: list[str] = Field(max_length=COMPLETE_SET_MAX_ITEMS)
    avoids: list[str] = Field(max_length=COMPLETE_SET_MAX_ITEMS)
    mongo_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_item_count(self):
        validate_bootstrap_item_count(self.prefers, self.avoids, mode=self.mode)
        return self


class TokenRequest(InternalRequest):
    preview_token: str = Field(min_length=1, max_length=524288)


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirm_items: StrictBool
    complete_set: StrictBool = False
    reviewed_prefers: StrictBool = False
    reviewed_avoids: StrictBool = False
    retire_legacy: StrictBool = False


class CommitRequest(TokenRequest):
    consent: Consent


class OperationRequest(InternalRequest):
    operation_id: str


class ResolveRequest(OperationRequest):
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    repair_projection: StrictBool = False


class RevisionRequest(OperationRequest):
    revision: int = Field(ge=0)


class ActionProjectionScan(BaseModel):
    model_config = ConfigDict(extra='forbid')
    limit: int = Field(default=16, ge=1, le=32)


class ActionProjectionAck(InternalRequest):
    key: str = Field(min_length=1, max_length=512)
    revision: int = Field(ge=0, strict=True)


async def execute(request, payload, action, *, require_owner=True):
    try:
        verify_internal(request.url.path, await request.json(), request.headers.get("X-Preference-Bootstrap", ""))
        if require_owner:
            owner_id(payload.owner)
        if isinstance(payload, OperationRequest):
            operation_id(payload.operation_id)
        def run():
            with GraphDatabase.driver(os.getenv("NEO4J_URI"), auth=(os.getenv("NEO4J_USERNAME"), os.getenv("NEO4J_PASSWORD")),
                connection_timeout=3, connection_acquisition_timeout=3, max_transaction_retry_time=0) as driver:
                graph = BootstrapGraph(driver, os.getenv("NEO4J_DATABASE", "neo4j"))
                return action(graph)
        return await run_in_threadpool(run)
    except BootstrapError as exc:
        raise HTTPException(exc.status, detail={"code": exc.code}) from None
    except PreferenceTextError as exc:
        raise HTTPException(422, detail={"code": exc.code}) from None
    except PreferenceFenceError as exc:
        raise HTTPException(409, detail={"code": exc.code}) from None
    except Exception:
        raise HTTPException(503, detail={"code": "bootstrap_graph_unavailable"}) from None


@router.post("/preview")
async def preview(payload: PreviewRequest, request: Request):
    return await execute(request, payload, lambda g: g.preview(payload.owner, payload.mode, payload.prefers, payload.avoids, payload.mongo_snapshot_hash))


@router.post('/source')
async def source(payload: InternalRequest, request: Request):
    return await execute(request, payload, lambda g: g.source(payload.owner))


@router.post("/check")
async def check(payload: TokenRequest, request: Request):
    return await execute(request, payload, lambda g: g.check(payload.owner, payload.preview_token))


@router.post("/commit")
async def commit(payload: CommitRequest, request: Request):
    return await execute(request, payload, lambda g: g.commit(payload.owner, payload.preview_token, payload.consent.model_dump()))


@router.post("/status")
async def status(payload: OperationRequest, request: Request):
    return await execute(request, payload, lambda g: g.status(payload.owner, payload.operation_id))


@router.post("/resolve")
async def resolve(payload: ResolveRequest, request: Request):
    return await execute(request, payload, lambda g: g.resolve(payload.owner, payload.operation_id, payload.plan_hash, payload.repair_projection))


@router.post("/ack")
async def acknowledge(payload: RevisionRequest, request: Request):
    return await execute(request, payload, lambda g: g.acknowledge(payload.owner, payload.operation_id, payload.revision))


@router.post("/rollback")
async def rollback(payload: RevisionRequest, request: Request):
    return await execute(request, payload, lambda g: g.rollback(payload.owner, payload.operation_id, payload.revision))


@router.post("/rollback-check")
async def rollback_check(payload: RevisionRequest, request: Request):
    return await execute(request, payload, lambda g: g.check_rollback(payload.owner, payload.operation_id, payload.revision))


@router.post('/action-projection-pending')
async def action_projection_pending(payload: ActionProjectionScan, request: Request):
    # Service-signed recovery scan, never a public owner endpoint.
    from .preference_action_reference import pending_projections
    return await execute(request, payload, lambda g: {'items': g.read(pending_projections, payload.limit)}, require_owner=False)


@router.post('/action-projection-ack')
async def action_projection_ack(payload: ActionProjectionAck, request: Request):
    from .preference_action_reference import acknowledge_projection
    return await execute(request, payload, lambda g: g.write(acknowledge_projection, payload.owner, payload.key, payload.revision))
