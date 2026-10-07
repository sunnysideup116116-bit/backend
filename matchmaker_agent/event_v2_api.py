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
# Single Event outer-deadline authority. 120 s is the bounded mitigation for the
# Run #29 heavy-tail outer-deadline unavailables: the ten terminal requests
# needed up to ~99 s of their observed bounded semantic flow under strict
# pessimistic replay, and requests with up to 16 sequential validator
# invocations were observed in production, so 75 s was structurally
# insufficient. 120 s keeps ~21 s of headroom over that deterministic maximum
# while staying well below the 150 s safety ceiling; 90 s (3/10 shapes still
# terminate) and 105 s (~6 s headroom) were rejected as too tight for the
# observed invocation-count tail. The caller HTTP budget was raised in the same
# change to keep a 30 s margin (caller 150 s). The validator shared deadline
# (18 s), per-call maximum (6 s) and attempt limit (2) are unchanged and remain
# independently enforced below.
EVENT_OUTER_DEADLINE_SECONDS = 120.0


def initialize_event_profiles():
    """Exact Event eligibility has no dependency on semantic rollout flags."""
    from database import profiles_coll
    global _profiles
    _profiles = profiles_coll


def profiles_for_events():
    return _profiles


class EventMatches(list):
    def __init__(self, matches, telemetry, checkpoint=None, resumed=False):
        super().__init__(matches)
        self.telemetry = telemetry
        self.checkpoint = checkpoint
        self.resumed = resumed


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


def find_matches(agent, owner, excluded, checkpoint=None):
    with diagnostic_scope('event_request') as trace:
        adapter = None
        started = time.monotonic()
        try:
            with session_for(agent) as session:
                adapter = Adapter(session, agent.client, agent.model, deadline=time.monotonic()+EVENT_OUTER_DEADLINE_SECONDS,
                    fallback_model=getattr(agent, 'validator_fallback_model', None), checkpoint=checkpoint)
                matches = adapter.select(owner, excluded)
                emit('event_result', stage='event_request', category='success',
                    final_typed_outcome='accepted' if matches else 'normal_no_match',
                    resumed=adapter.resumed, elapsed_ms=(time.monotonic()-started)*1000)
                return EventMatches(matches, adapter.telemetry(),
                    checkpoint=adapter.checkpoint_value(), resumed=adapter.resumed)
        except EventUnavailable as exc:
            exc.telemetry = adapter.telemetry() if adapter else {'diagnostic_id': trace.correlation_id}
            exc.checkpoint = adapter.checkpoint_value() if adapter else None
            exc.resumed = adapter.resumed if adapter else False
            # Producer contract: resumable work must carry (or already have) a
            # valid resume authority. A resumable stop without a serialized
            # checkpoint is an internal inconsistency: never fabricate one, but
            # emit an explicit diagnostic so the consumer can fail closed and
            # keep the work non-terminal instead of silently terminalizing it.
            exc.resume_contract_violation = bool(exc.resumable) and exc.checkpoint is None
            if exc.resume_contract_violation:
                emit('event_stage', stage='event_relevance', category='other',
                    typed_code=exc.code, final_typed_outcome='unavailable',
                    resumable=True, resume_contract_violation=True)
            emit('event_result', stage='event_request', typed_code=exc.code, final_typed_outcome='unavailable',
                resumable=bool(exc.resumable), resumed=exc.resumed,
                resume_contract_violation=bool(exc.resume_contract_violation),
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


class CircuitProbeRequest(BaseModel):
    model_config = {'extra': 'forbid'}


_PROBE_QUERY = '週末運動'
_PROBE_CONCEPT = '週末球類活動'


@router.post('/circuit/probe')
def circuit_probe_endpoint(request: Request):
    """One synthetic, private-data-free provider probe for the HALF_OPEN state.

    It reuses the exact validator contract (18 s shared, 6 s per call, 2
    attempts, failover) with fixed synthetic text only. No user data, no
    checkpoint, no proposal. Returns healthy only when a real provider round
    trip completes.
    """
    try:
        verify_internal(request.url.path, {}, request.headers.get('X-Preference-Bootstrap', ''))
    except BootstrapError:
        raise HTTPException(403, detail='invalid_event_context') from None
    from .related_interest_validator import validate_concepts
    agent = agent_instance()
    try:
        accepted, counts = validate_concepts(_PROBE_QUERY,
            [{'concept_key': 'circuit-probe', 'semantic_text': _PROBE_CONCEPT}],
            agent.client, agent.model, deadline=time.monotonic()+18,
            fallback_model=getattr(agent, 'validator_fallback_model', None))
    except Exception:
        return {'status': 'success', 'healthy': False}
    healthy = counts.get('error') == 0 and counts.get('attempts', 0) >= 1 and not counts.get('job_unavailable')
    return {'status': 'success', 'healthy': bool(healthy)}
