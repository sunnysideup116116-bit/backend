"""Weekly Event inventory refresh owned by the dedicated Event worker."""

from __future__ import annotations

import os
import time
from typing import Any

import requests

from database import matches_coll, profiles_coll
from services.event_discovery_service import discover_and_ingest_events
from services.event_opportunity_service import scan_event_opportunities
from services.match_decision_service import expire_event_proposals
from services.proposal_namespace import (
    EVENT_INVITATION_NAMESPACE,
    LIVE_PROPOSAL_STATUSES,
    namespace_clause,
)


AGENT_EVENT_RESET_URL = "http://127.0.0.1:9001/api/events/reset"
AGENT_PENDING_CONCEPTS_URL = "http://127.0.0.1:9001/api/concepts/missing-embeddings"


class EventCycleError(RuntimeError):
    """Stable internal failure for one weekly-cycle stage."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = str(code or "event_cycle_failed")[:80]


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def reset_event_inventory() -> dict[str, Any]:
    """Reset only Event-owned Graph state and expire unresolved invitations.

    Declined and accepted Event invitation documents remain in Mongo so pair
    cooldown, audit history and accepted-room history survive weekly refreshes.
    """
    live_rows = list(matches_coll.find(
        {
            "$and": [
                {"status": {"$in": sorted(LIVE_PROPOSAL_STATUSES)}},
                namespace_clause(EVENT_INVITATION_NAMESPACE),
            ],
        },
        {"_id": 1},
    ).limit(1000))
    live_match_ids = [str(row["_id"]) for row in live_rows if row.get("_id")]
    try:
        response = requests.post(
            AGENT_EVENT_RESET_URL,
            params={"confirm": "true"},
            timeout=(3, 30),
        )
        response.raise_for_status()
        graph_result = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise EventCycleError("event_graph_reset_unavailable") from exc
    if graph_result.get("status") != "success":
        raise EventCycleError("event_graph_reset_failed")

    event_ids = [
        str(value)[:80]
        for value in list(graph_result.get("event_ids") or [])[:500]
        if str(value or "").strip()
    ]
    expiry = expire_event_proposals(
        now=time.time(), event_ids=event_ids, limit=1000, expire_all=True,
    )
    inbox_cleanup = 0
    if live_match_ids:
        result = profiles_coll.update_many(
            {},
            {"$pull": {"mediator_inbox": {
                "$or": [
                    {"match_id": {"$in": live_match_ids}},
                    {"event_key": {"$regex": "^event-opportunity:"}},
                ],
            }}},
        )
        inbox_cleanup = int(result.modified_count)
    return {
        "status": "success",
        "graph": {
            "status": "success",
            "events_deleted": int(graph_result.get("events_deleted", 0) or 0),
            "orphan_concepts_deleted": int(
                graph_result.get("orphan_concepts_deleted", 0) or 0
            ),
        },
        "mongo": {
            "expired_proposal_count": int(expiry.get("expired_count", 0) or 0),
            "stale_proposal_count": int(expiry.get("stale_count", 0) or 0),
            "profiles_with_inbox_cleanup": inbox_cleanup,
            "terminal_history_preserved": True,
        },
    }


def wait_for_event_relevance() -> dict[str, Any]:
    """Wait for the existing 8000 embedding worker to finish Graph projection."""
    wait_seconds = _bounded_int(
        os.getenv("EVENT_WEEKLY_RELEVANCE_WAIT_SECONDS", "900"),
        900, 0, 3600,
    )
    poll_seconds = _bounded_int(
        os.getenv("EVENT_WEEKLY_RELEVANCE_POLL_SECONDS", "10"),
        10, 2, 60,
    )
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            response = requests.get(
                AGENT_PENDING_CONCEPTS_URL,
                params={"limit": 1},
                timeout=(3, 15),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            return {
                "status": "unavailable", "ready": False,
                "error_code": type(exc).__name__,
            }
        if payload.get("status") != "success":
            return {"status": "unavailable", "ready": False}
        pending = int(payload.get("count", 0) or 0)
        if pending <= 0:
            return {"status": "ready", "ready": True, "pending_count": 0}
        if time.monotonic() >= deadline:
            return {
                "status": "timeout", "ready": False,
                "pending_count": pending,
            }
        time.sleep(poll_seconds)


def run_weekly_event_cycle(
    *, region: str, window_days: int, categories: list[str],
    stage_callback=None,
) -> dict[str, Any]:
    """Run reset -> discovery -> opportunity scan as one worker-owned job."""
    notify = stage_callback or (lambda _stage: None)
    notify("resetting")
    reset_result = reset_event_inventory()

    notify("discovering")
    discovery = discover_and_ingest_events(
        region=region, window_days=window_days, categories=categories,
        request_invitation_scan=False,
    )
    active_count = sum(
        int(value or 0)
        for value in dict(discovery.get("active_category_counts") or {}).values()
    )
    if active_count <= 0:
        relevance_readiness = {
            "status": "skipped_no_events", "ready": False,
        }
        invitation_scan = {
            "status": "skipped_no_events", "created_count": 0,
            "scanned_count": 0,
        }
    else:
        notify("waiting_relevance")
        relevance_readiness = wait_for_event_relevance()
        if relevance_readiness.get("ready"):
            notify("scanning_invitations")
            invitation_scan = scan_event_opportunities(
                max_proposals=_bounded_int(
                    os.getenv("EVENT_OPPORTUNITY_MAX_PROPOSALS_PER_SCAN", "3"),
                    3, 1, 10,
                ),
            )
        else:
            invitation_scan = {
                "status": "skipped_relevance_not_ready",
                "created_count": 0, "scanned_count": 0,
            }
    notify("completed")
    outcome = str(discovery.get("status") or "unknown")
    if invitation_scan.get("status") not in {"success", "skipped_no_events"}:
        outcome = "partial"
    return {
        **discovery,
        "status": outcome,
        "job_kind": "weekly_cycle",
        "reset": reset_result,
        "relevance_readiness": relevance_readiness,
        "invitation_scan": invitation_scan,
    }
