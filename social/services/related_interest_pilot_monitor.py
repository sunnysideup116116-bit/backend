"""Stop repeated typed validator unavailability; never retry or alter matching."""
import logging
import os
import time
from pathlib import Path

from matchmaker_agent.semantic_rollout_policy import legacy_owner_scope, requester_route_allowed
from matchmaker_agent.related_interest_contract import POLICY

UNAVAILABLE_CODES = frozenset({"semantic_validator_unavailable", "semantic_retrieval_timeout"})
WINDOW_SECONDS = 15 * 60
CONSECUTIVE_FAILURE_LIMIT = 3
LOG = logging.getLogger(__name__)


def systemic_failure(rows, *, now):
    terminal = [r for r in rows if type(r.get("completed_at")) in (int, float)
        and now-WINDOW_SECONDS <= r["completed_at"] <= now
        and (r.get("related_interest_pilot") or {}).get("policy_version") == POLICY
        and (r.get("related_interest_pilot") or {}).get("telemetry_version") in {2, 3}
        and (r.get("related_interest_pilot") or {}).get("triggered") is True]
    terminal.sort(key=lambda r: r["completed_at"], reverse=True)
    latest = terminal[:CONSECUTIVE_FAILURE_LIMIT]
    return len(latest) == CONSECUTIVE_FAILURE_LIMIT and all(
        r.get("status") == "failed" and r.get("error_code") in UNAVAILABLE_CODES for r in latest)


def on_job_finished(owner_id, status, error_code):
    if status != "failed" or error_code not in UNAVAILABLE_CODES or not requester_route_allowed(owner_id):
        return False
    try:
        from database import db
        from pymongo import timeout
        now = time.time()
        with timeout(0.5):
            legacy_scope = legacy_owner_scope()
            scope = {"user_id": {"$in": legacy_scope}} if legacy_scope is not None else {
                "related_interest_pilot.rollout_mode": "enabled_accounts"}
            rows = list(db["match_search_jobs"].find({
                **scope,
                "related_interest_pilot.policy_version": POLICY,
                "related_interest_pilot.telemetry_version": {"$in": [2, 3]},
                "related_interest_pilot.triggered": True,
                "completed_at": {"$gte": now-WINDOW_SECONDS},
            }, {"_id": 0, "status": 1, "error_code": 1, "completed_at": 1,
                "related_interest_pilot": 1}).sort("completed_at", -1).limit(CONSECUTIVE_FAILURE_LIMIT))
        if not systemic_failure(rows, now=now):
            return False
        path = Path(os.getenv("MATCH_RELATED_INTEREST_KILL_SWITCH_FILE") or
                    Path(__file__).resolve().parents[2]/".related-interest-disabled")
        path.touch(exist_ok=True)
        LOG.error("semantic_pilot_stopped reason=consecutive_validator_unavailability")
        return True
    except Exception as exc:
        # The failed request remains fail-closed. Never expose credentials,
        # owner IDs, text, or an exception body; the operator monitor backs up
        # this bounded check and verifies filesystem readiness before enable.
        LOG.warning("semantic_pilot_stop_check_unavailable error_type=%s", type(exc).__name__)
        return False
