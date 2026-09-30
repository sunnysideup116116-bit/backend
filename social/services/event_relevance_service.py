"""Event relevance is a current-source read, not a legacy-vector projection."""
import requests

READINESS_URL = 'http://127.0.0.1:9001/api/events/v2/readiness'


def _readiness():
    try:
        response = requests.get(READINESS_URL, timeout=(3, 15))
        response.raise_for_status()
        result = response.json()
        if result.get('policy') != 'event_relevance_v2':
            raise ValueError('event_contract_unavailable')
        return {'status': 'success' if result.get('semantic_ready') else 'deferred',
            'exact_ready': result.get('exact_ready') is True,
            'semantic_ready': result.get('semantic_ready') is True,
            'policy': 'event_relevance_v2', 'projection_mode': 'read_time', 'link_count': 0,
            'embedded_count': 0}
    except Exception:
        return {'status': 'unavailable', 'error_code': 'event_readiness_unavailable', 'link_count': 0}


def project_event_relevance(events):
    if not events:
        return {'status': 'empty', 'event_count': 0, 'link_count': 0}
    return {**_readiness(), 'event_count': len(events)}


def rebuild_all_event_relevance(limit=20):
    """Compatibility endpoint: inspect V2 infrastructure, never encode legacy."""
    return _readiness()
