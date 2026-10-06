"""Bounded Event adapter transport; no normal Preference API changes."""
import time
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from neo4j import GraphDatabase, Query

from .event_v2_adapter import Adapter, infrastructure, recheck
from .event_v2_contract import EventUnavailable
from .preference_bootstrap_contract import verify_internal, BootstrapError
from .validator_diagnostics import diagnostic_scope, emit, exception_metadata

router = APIRouter(prefix='/api/events/v2')
_profiles = None
# Single Event outer-deadline authority. 75 s is the bounded mitigation for the
# Run #28 outer-deadline terminal unavailables: under pessimistic replay of the
# observed workload all three historical terminations disappear at ~61 s, and
# the real-provider captures complete with headroom (worst ~51 s). 75 s is the
# smallest tested value that clears the deterministic minimum while keeping a
# >=15 s margin under the caller's 90 s HTTP timeout. 85 s+ leaves no caller
# margin and is deliberately not used. The validator shared deadline (18 s),
# per-call maximum (6 s) and attempt limit (2) are unchanged and remain
# independently enforced below.
EVENT_OUTER_DEADLINE_SECONDS = 75.0


def initialize_event_profiles():
    """Exact Event eligibility has no dependency on semantic rollout flags."""
    from database import profiles_coll
    global _profiles
    _profiles = profiles_coll


def profiles_for_events():
    return _profiles


class EventMatches(list):
    def __init__(self, matches, telemetry):
        super().__init__(matches)
        self.telemetry = telemetry


@contextmanager
def session_for(agent):
    uri, auth, database = agent._graph_config()
    with GraphDatabase.driver(uri, auth=auth, connection_timeout=3,
            connection_acquisition_timeout=3, max_transaction_retry_time=0) as driver:
        with driver.session(database=database, default_access_mode='READ') as session:
            yield session


def agent_instance():
    from agent_api import agent
    return agent


@router.get('/readiness')
def readiness_endpoint():
    try:
        with session_for(agent_instance()) as session:
            session.run(Query('RETURN 1 AS ok', timeout=3)).single(strict=True)
            return infrastructure(session)
    except Exception:
        return {'status': 'unavailable', 'ready': False, 'exact_ready': False,
            'semantic_ready': False, 'policy': 'event_relevance_v2',
            'error_code': 'event_graph_unavailable'}


def find_matches(agent, owner, excluded):
    with diagnostic_scope('event_request') as trace:
        adapter = None
        started = time.monotonic()
        try:
            with session_for(agent) as session:
                adapter = Adapter(session, agent.client, agent.model, deadline=time.monotonic()+EVENT_OUTER_DEADLINE_SECONDS,
                    fallback_model=getattr(agent, 'validator_fallback_model', None))
                matches = adapter.select(owner, excluded)
                emit('event_result', stage='event_request', category='success',
                    final_typed_outcome='accepted' if matches else 'normal_no_match', elapsed_ms=(time.monotonic()-started)*1000)
                return EventMatches(matches, adapter.telemetry())
        except EventUnavailable as exc:
            exc.telemetry = adapter.telemetry() if adapter else {'diagnostic_id': trace.correlation_id}
            emit('event_result', stage='event_request', typed_code=exc.code, final_typed_outcome='unavailable',
                elapsed_ms=(time.monotonic()-started)*1000)
            raise
        except TimeoutError as exc:
            error = EventUnavailable('semantic_retrieval_timeout', systemic=True)
            error.telemetry = adapter.telemetry() if adapter else {'diagnostic_id': trace.correlation_id}
            emit('event_result', **exception_metadata(exc, 'deadline'), typed_code=error.code,
                final_typed_outcome='unavailable', elapsed_ms=(time.monotonic()-started)*1000)
            raise error from None
        except Exception as exc:
            emit('event_result', **exception_metadata(exc, 'event_request'), typed_code='event_graph_unavailable',
                final_typed_outcome='unavailable', elapsed_ms=(time.monotonic()-started)*1000)
            raise EventUnavailable('event_graph_unavailable', systemic=True) from None


class RecheckRequest(BaseModel):
    model_config = {'extra': 'forbid'}
    owner: str = Field(min_length=1, max_length=128)
    candidate: str = Field(min_length=1, max_length=128)
    receipt: str = Field(min_length=40, max_length=64)


@router.post('/recheck')
def recheck_endpoint(req: RecheckRequest, request: Request):
    try:
        verify_internal(request.url.path, req.model_dump(), request.headers.get('X-Preference-Bootstrap', ''))
    except BootstrapError:
        raise HTTPException(403, detail='invalid_event_context') from None
    try:
        with session_for(agent_instance()) as session:
            valid = recheck(session, req.receipt, req.owner, req.candidate, deadline=time.monotonic()+5)
        return {'status': 'success', 'eligible': valid}
    except Exception:
        return {'status': 'unavailable', 'eligible': False}
