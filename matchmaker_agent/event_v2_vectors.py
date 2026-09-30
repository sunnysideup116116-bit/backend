"""Signed bounded query encoding in Social; no SDK/dependency changes in 9001."""
import time

import requests
from urllib3.util import Timeout

from .event_v2_contract import EventUnavailable
from .preference_bootstrap_contract import internal_headers
from .preference_embedding_contract import frozen_contract, RUNTIME, PROVENANCE
from .related_interest_contract import unit_vector

PATH = '/api/internal/events/v2/query-vectors'


def query_vectors(signals, *, deadline, clock=time.monotonic, http=requests):
    frozen_contract()
    unique = {s.source_hash: s for s in signals}
    if not unique:
        return {}
    budget = min(25., deadline-clock()-.1)
    if budget <= 0:
        raise EventUnavailable('semantic_retrieval_timeout')
    body = {'signals': [{'text': s.text, 'kind': s.kind, 'namespace': s.namespace,
        'source_hash': s.source_hash} for s in unique.values()], 'request_budget_seconds': budget}
    try:
        response = http.post('http://127.0.0.1:8000'+PATH, json=body,
            headers=internal_headers(PATH, body), timeout=Timeout(total=budget,
                connect=min(1., budget), read=budget), allow_redirects=False)
        response.raise_for_status()
        result = response.json()
        if clock() >= deadline:
            raise EventUnavailable('semantic_retrieval_timeout')
        if (not isinstance(result, dict) or result.get('status') != 'success'
                or result.get('runtime') != RUNTIME or result.get('encoder_provenance') != PROVENANCE
                or not isinstance(result.get('vectors'), dict)):
            raise EventUnavailable('event_query_embedding_unavailable')
        vectors = result['vectors']
        if any(key not in unique or unit_vector(vector) is None
                or abs(sum(x*x for x in vector)-1)>1e-5 for key,vector in vectors.items()):
            raise EventUnavailable('event_query_embedding_invalid')
        return vectors
    except EventUnavailable:
        raise
    except Exception:
        raise EventUnavailable('event_query_embedding_unavailable') from None
