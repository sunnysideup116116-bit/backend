"""Private signed worker endpoint. No user-controlled owner or provider config."""
import os
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, field_validator
from starlette.concurrency import run_in_threadpool
from neo4j import GraphDatabase

from .preference_bootstrap_contract import verify_internal, BootstrapError, require
from .preference_embedding_contract import enabled, frozen_contract, MODEL
from . import preference_embedding_jobs as jobs
from .related_interest_retrieval import index_metadata
from .related_interest_contract import unit_vector

router = APIRouter(prefix='/api/v2/preferences/embedding-jobs', tags=['Internal preference embeddings'])


class Empty(BaseModel):
    model_config = ConfigDict(extra='forbid')


class Lease(Empty):
    key: str = Field(pattern=r'^v2_[0-9a-f]{48}$')
    lease: str = Field(pattern=r'^[0-9a-f-]{36}$')


class Finish(Lease):
    source_hash: str = Field(pattern=r'^[0-9a-f]{64}$')
    vector: list[StrictFloat | StrictInt] = Field(min_length=768, max_length=768)

    @field_validator('vector')
    @classmethod
    def normalized_vector(cls, value):
        import math
        if unit_vector(value) is None or not math.isfinite(sum(x*x for x in value)) or abs(sum(x*x for x in value)-1)>1e-5:
            raise ValueError('embedding_vector_invalid')
        return value


async def execute(request, payload, action):
    try:
        verify_internal(request.url.path, await request.json(), request.headers.get('X-Preference-Bootstrap',''))
        require(enabled(), 'embedding_worker_disabled', 503)
        frozen=frozen_contract()
        def run():
            with GraphDatabase.driver(os.getenv('NEO4J_URI'), auth=(os.getenv('NEO4J_USERNAME'),os.getenv('NEO4J_PASSWORD')),
                    connection_timeout=3,connection_acquisition_timeout=3,max_transaction_retry_time=0) as driver:
                with driver.session(database=os.getenv('NEO4J_DATABASE','neo4j')) as session:
                    require(index_metadata(session)['ready'], 'embedding_index_unavailable', 503)
                    # No automatic transaction replay of claim/completion.
                    with session.begin_transaction(timeout=10) as tx:
                        result=action(tx,frozen);tx.commit();return result
        return await run_in_threadpool(run)
    except BootstrapError as exc:
        raise HTTPException(exc.status,detail={'code':exc.code}) from None
    except Exception:
        raise HTTPException(503,detail={'code':'embedding_job_unavailable'}) from None


@router.post('/claim')
async def claim(payload: Empty, request: Request):
    return await execute(request,payload,lambda tx,frozen: {'job':jobs.claim(tx,MODEL,frozen)})


@router.post('/finish')
async def finish(payload: Finish, request: Request):
    return await execute(request,payload,lambda tx,frozen: jobs.finish(tx,payload.model_dump(),MODEL,frozen))


@router.post('/fail')
async def fail(payload: Lease, request: Request):
    return await execute(request,payload,lambda tx,_frozen: jobs.fail(tx,payload.key,payload.lease))
