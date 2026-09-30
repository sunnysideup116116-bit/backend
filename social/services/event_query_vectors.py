"""Event query encoding in Social's existing pinned Gemini environment."""
from collections import OrderedDict
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from matchmaker_agent.event_v2_contract import EventUnavailable, query_signal
from matchmaker_agent.preference_embedding_contract import frozen_contract, MODEL, RUNTIME, PROVENANCE
from matchmaker_agent.preference_bootstrap_contract import verify_internal, BootstrapError
from matchmaker_agent.related_interest_contract import unit_vector

router = APIRouter(prefix='/api/internal/events/v2')
_cache = OrderedDict()
_lock = threading.Lock()


def produce(signals, *, deadline, clock=time.monotonic, factory=None):
    """Cache encoding artifacts only. No preference/Graph/provider side effects at import."""
    frozen = frozen_contract()
    unique = {s.source_hash: s for s in signals}
    result, missing = {}, []
    with _lock:
        for key, signal in unique.items():
            cached = _cache.get(key)
            if cached and cached['until'] > clock() and cached['runtime'] == RUNTIME and cached['encoder'] == PROVENANCE:
                result[key] = cached['vector']
            else:
                missing.append(signal)
    if not missing or deadline-clock() < 21:
        return result
    from scripts.semantic_embedding_pipeline import SafeGeminiEmbedding
    from scripts.prepare_related_interest_embeddings import prepare_batch
    from config import GOOGLE_API_KEYS
    provider = None
    batch = missing[:20]
    try:
        provider = (factory or SafeGeminiEmbedding)(MODEL, frozen, prepare_batch, keys=GOOGLE_API_KEYS[:1])
        remaining = deadline-clock()
        if remaining <= .25:
            raise EventUnavailable('event_query_embedding_unavailable')
        vectors = provider.embed([s.text for s in batch], task_type='semantic_similarity',
            output_dimensionality=768, request_timeout_seconds=min(10, remaining))
        if clock() >= deadline or not isinstance(vectors, list) or len(vectors) != len(batch):
            raise EventUnavailable('event_query_embedding_unavailable')
        normalized = [unit_vector(v) for v in vectors]
        if not all(v is not None for v in normalized):
            raise EventUnavailable('event_query_embedding_invalid')
        with _lock:
            for signal, vector in zip(batch, normalized):
                result[signal.source_hash] = vector
                _cache[signal.source_hash] = {'vector': vector, 'until': clock()+300,
                    'runtime': RUNTIME, 'encoder': PROVENANCE}
                _cache.move_to_end(signal.source_hash)
            while len(_cache) > 1024:
                _cache.popitem(last=False)
        return result
    except EventUnavailable:
        raise
    except Exception:
        raise EventUnavailable('event_query_embedding_unavailable') from None
    finally:
        if provider:
            provider.close()


class QuerySignal(BaseModel):
    model_config = {'extra': 'forbid'}
    text: str = Field(min_length=1, max_length=500)
    kind: str = Field(pattern=r'^(tag|vibe|activity|interest)$')
    namespace: str = Field(pattern=r'^(event-signal-v1|event-recent-v1)$')
    source_hash: str = Field(pattern=r'^[0-9a-f]{64}$')


class QueryBatch(BaseModel):
    model_config = {'extra': 'forbid'}
    signals: list[QuerySignal] = Field(min_length=1, max_length=1020)
    request_budget_seconds: float = Field(gt=0, le=25, allow_inf_nan=False)


@router.post('/query-vectors')
def query_vectors_endpoint(req: QueryBatch, request: Request):
    try:
        verify_internal(request.url.path, req.model_dump(), request.headers.get('X-Preference-Bootstrap', ''))
    except BootstrapError:
        raise HTTPException(403, detail='invalid_event_context') from None
    try:
        from matchmaker_agent.related_interest_contract import enabled
        from matchmaker_agent.semantic_rollout_policy import rollout_mode
        import os
        if (not enabled() or rollout_mode() != 'enabled_accounts'
                or os.getenv('MATCH_PREFERENCE_SEMANTIC_MODE', 'off').strip().lower() != 'active'):
            raise EventUnavailable('event_semantic_disabled')
        signals = [query_signal(s.text, s.kind, '', namespace=s.namespace) for s in req.signals]
        if any(s.source_hash!=raw.source_hash or s.text!=raw.text for s,raw in zip(signals,req.signals)):
            raise EventUnavailable('event_query_source_invalid')
        result = produce(signals, deadline=time.monotonic()+req.request_budget_seconds)
        return {'status': 'success', 'runtime': RUNTIME, 'encoder_provenance': PROVENANCE,
            'vectors': result}
    except Exception as exc:
        code = exc.code if isinstance(exc, EventUnavailable) else 'event_query_embedding_unavailable'
        return {'status': 'unavailable', 'error_code': code, 'vectors': {}}
