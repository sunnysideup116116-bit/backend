"""Event aggregate observations using the existing 3-in-15m failure policy.

The automatic provider circuit (CLOSED/OPEN/HALF_OPEN) is now a separate
authority from the operator manual kill. A run of terminal systemic
unavailabilities opens the circuit; it never writes the manual kill file. The
manual kill stays absolute and is never auto-cleared.
"""
import logging
import os
import re
from pathlib import Path
import time

from pymongo import timeout as mongo_timeout

from matchmaker_agent.event_semantic_circuit import (
    cooldown_elapsed, open_circuit, probe_and_recover, snapshot as circuit_snapshot,
)
from matchmaker_agent.related_interest_contract import POLICY, bounded_counts
from matchmaker_agent.validator_diagnostics import breaker_transition
from services.related_interest_pilot_monitor import (
    UNAVAILABLE_CODES, WINDOW_SECONDS, CONSECUTIVE_FAILURE_LIMIT, systemic_failure,
)

LOG = logging.getLogger(__name__)
_PROBE_PATH = '/api/events/v2/circuit/probe'


def probe_provider_health(timeout=(3, 25)):
    """One synthetic provider round trip through the deployed Matchmaker.

    Returns True only when the validator completed a real call successfully.
    """
    try:
        from matchmaker_agent.preference_bootstrap_contract import internal_headers
        import requests
        body = {}
        response = requests.post('http://127.0.0.1:9001' + _PROBE_PATH, json=body,
            headers=internal_headers(_PROBE_PATH, body), timeout=timeout,
            allow_redirects=False)
        response.raise_for_status()
        result = response.json()
        return isinstance(result, dict) and result.get('healthy') is True
    except Exception:
        return False


def probe_if_due(now=time.time, path=None):
    """Recover the automatic circuit when its bounded cooldown has elapsed.

    Does nothing while CLOSED or before the cooldown. Never touches the manual
    kill file. Returns the resulting circuit state name.
    """
    state = circuit_snapshot(path)
    if state.get('state') == 'closed' or not cooldown_elapsed(now=now, path=path):
        return state.get('state')
    if state.get('state') != 'open':
        return state.get('state')
    return probe_and_recover(lambda: probe_provider_health(), now=now, path=path)


def on_result(status, code, telemetry, *, collection=None, now=time.time, engage=None,
        path=None):
    if not isinstance(telemetry, dict) or telemetry.get('semantic_triggered') is not True:
        return False
    stamp = now()
    row = {'completed_at': stamp, 'status': 'failed' if status in {'unavailable','error','failed'} else 'completed',
        'error_code': code if code in UNAVAILABLE_CODES else '',
        'related_interest_pilot': {'policy_version': POLICY, 'telemetry_version': 3, 'triggered': True},
        'event_policy': 'event_relevance_v2', 'counts': bounded_counts(telemetry.get('validator')),
        'ann_calls': max(0,min(telemetry.get('ann_calls') or 0,10000)) if type(telemetry.get('ann_calls')) is int else 0,
        'validator_calls': max(0,min(telemetry.get('validator_calls') or 0,10000)) if type(telemetry.get('validator_calls')) is int else 0}
    diagnostic_id = telemetry.get('diagnostic_id')
    if isinstance(diagnostic_id, str) and re.fullmatch('[a-f0-9]{32}', diagnostic_id):
        row['diagnostic_id'] = diagnostic_id
    try:
        if collection is None:
            from database import db
            collection = db['event_semantic_observations']
        with mongo_timeout(.5):
            collection.insert_one(row)
            # Bounded retention; raw source/proof/provider output never enters this store.
            collection.delete_many({'completed_at': {'$lt': stamp-7*86400}})
            latest = list(collection.find({'completed_at': {'$gte': stamp-WINDOW_SECONDS}}, {'_id': 0})
                .sort('completed_at', -1).limit(CONSECUTIVE_FAILURE_LIMIT))
        if systemic_failure(latest, now=stamp):
            # Open the automatic circuit (bounded cooldown + half-open probe).
            # The operator manual kill file is deliberately NOT touched here.
            if engage is None:
                engage = lambda: open_circuit(now=lambda: stamp, path=path)
            engage()
            breaker_transition(diagnostic_id, latest, now=stamp, engaged=True)
            LOG.error('event_semantic_circuit_open reason=consecutive_validator_unavailability')
            return True
        breaker_transition(diagnostic_id, latest, now=stamp, engaged=False)
    except Exception:
        LOG.warning('event_semantic_observation_unavailable')
    return False
