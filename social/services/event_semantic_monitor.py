"""Event aggregate observations using the existing 3-in-15m failure policy."""
import logging
import os
from pathlib import Path
import time

from pymongo import timeout as mongo_timeout

from matchmaker_agent.related_interest_contract import POLICY, bounded_counts
from services.related_interest_pilot_monitor import (
    UNAVAILABLE_CODES, WINDOW_SECONDS, CONSECUTIVE_FAILURE_LIMIT, systemic_failure,
)

LOG = logging.getLogger(__name__)


def on_result(status, code, telemetry, *, collection=None, now=time.time, engage=None):
    if not isinstance(telemetry, dict) or telemetry.get('semantic_triggered') is not True:
        return False
    stamp = now()
    row = {'completed_at': stamp, 'status': 'failed' if status in {'unavailable','error','failed'} else 'completed',
        'error_code': code if code in UNAVAILABLE_CODES else '',
        'related_interest_pilot': {'policy_version': POLICY, 'telemetry_version': 3, 'triggered': True},
        'event_policy': 'event_relevance_v2', 'counts': bounded_counts(telemetry.get('validator')),
        'ann_calls': max(0,min(telemetry.get('ann_calls') or 0,10000)) if type(telemetry.get('ann_calls')) is int else 0,
        'validator_calls': max(0,min(telemetry.get('validator_calls') or 0,10000)) if type(telemetry.get('validator_calls')) is int else 0}
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
            if engage is None:
                path = Path(os.getenv('MATCH_RELATED_INTEREST_KILL_SWITCH_FILE') or
                    Path(__file__).resolve().parents[2]/'.related-interest-disabled')
                engage = lambda: path.touch(exist_ok=True)
            engage()
            LOG.error('event_semantic_stopped reason=consecutive_validator_unavailability')
            return True
    except Exception:
        LOG.warning('event_semantic_observation_unavailable')
    return False
