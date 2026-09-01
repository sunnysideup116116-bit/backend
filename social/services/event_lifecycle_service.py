"""Lifecycle owner for public Events and unresolved Event proposals."""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import requests

from services.match_decision_service import expire_event_proposals


AGENT_EVENT_CLEANUP_URL = "http://127.0.0.1:9001/api/events/lifecycle/cleanup"
DEFAULT_PROPOSAL_EXPIRY_INTERVAL_SECONDS = 300
DEFAULT_GRAPH_CLEANUP_INTERVAL_SECONDS = 86400

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_run_lock = threading.Lock()


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(parsed, maximum))


def run_event_lifecycle_once(
    *, now: float | None = None, include_graph_cleanup: bool = True,
) -> dict[str, Any]:
    """CAS-expire Mongo proposals and optionally run the daily Graph cleanup."""
    if not _run_lock.acquire(blocking=False):
        return {"status": "already_running", "expired_proposal_count": 0}
    try:
        current_time = float(now if now is not None else time.time())
        event_ids: list[str] = []
        deleted_event_count = 0
        cleanup_error = ""
        if include_graph_cleanup:
            try:
                response = requests.post(AGENT_EVENT_CLEANUP_URL, timeout=(3, 30))
                response.raise_for_status()
                payload = response.json()
                if payload.get("status") == "success":
                    event_ids = [
                        str(value)[:80] for value in list(payload.get("event_ids") or [])[:500]
                        if str(value or "").strip()
                    ]
                    deleted_event_count = int(payload.get("deleted_count", 0) or 0)
                else:
                    cleanup_error = "agent_cleanup_failed"
            except (requests.RequestException, ValueError) as exc:
                cleanup_error = type(exc).__name__

        lead_seconds = _bounded_int(
            os.getenv("EVENT_PROPOSAL_EXPIRY_LEAD_SECONDS", "0"),
            0, 0, 7 * 86400,
        )
        proposal_result = expire_event_proposals(
            now=current_time, event_ids=event_ids, lead_seconds=lead_seconds,
        )
        return {
            "status": "partial" if cleanup_error else "success",
            "deleted_event_count": deleted_event_count,
            "expired_proposal_count": int(proposal_result.get("expired_count", 0) or 0),
            "stale_proposal_count": int(proposal_result.get("stale_count", 0) or 0),
            "graph_cleanup_ran": include_graph_cleanup,
            "cleanup_error": cleanup_error or None,
        }
    finally:
        _run_lock.release()


def _run_worker_iteration(*, include_graph_cleanup: bool) -> dict[str, Any]:
    """Keep a transient database/provider failure from terminating the worker."""
    try:
        return run_event_lifecycle_once(include_graph_cleanup=include_graph_cleanup)
    except Exception as exc:
        print(f"[EVENT_LIFECYCLE] iteration_failed error={type(exc).__name__}")
        return {
            "status": "error",
            "deleted_event_count": 0,
            "expired_proposal_count": 0,
            "graph_cleanup_ran": include_graph_cleanup,
            "cleanup_error": type(exc).__name__,
        }


def _worker_loop() -> None:
    proposal_interval = _bounded_int(
        os.getenv(
            "EVENT_PROPOSAL_EXPIRY_INTERVAL_SECONDS",
            DEFAULT_PROPOSAL_EXPIRY_INTERVAL_SECONDS,
        ),
        DEFAULT_PROPOSAL_EXPIRY_INTERVAL_SECONDS, 30, 86400,
    )
    graph_interval = _bounded_int(
        os.getenv(
            "EVENT_GRAPH_CLEANUP_INTERVAL_SECONDS",
            DEFAULT_GRAPH_CLEANUP_INTERVAL_SECONDS,
        ),
        DEFAULT_GRAPH_CLEANUP_INTERVAL_SECONDS, proposal_interval, 7 * 86400,
    )
    last_graph_cleanup = 0.0
    while not _stop_event.wait(proposal_interval):
        monotonic_now = time.monotonic()
        include_graph_cleanup = monotonic_now - last_graph_cleanup >= graph_interval
        result = _run_worker_iteration(include_graph_cleanup=include_graph_cleanup)
        if include_graph_cleanup and result.get("status") != "already_running":
            last_graph_cleanup = monotonic_now
        print(
            f"[EVENT_LIFECYCLE] status={result.get('status')} "
            f"events={result.get('deleted_event_count', 0)} "
            f"proposals={result.get('expired_proposal_count', 0)} "
            f"graph_cleanup={result.get('graph_cleanup_ran', False)}"
        )


def start_event_lifecycle_worker() -> None:
    global _worker_thread
    if os.getenv("EVENT_LIFECYCLE_WORKER_ENABLED", "on").strip().lower() not in {
        "1", "true", "on",
    }:
        return
    if _worker_thread and _worker_thread.is_alive():
        return
    _stop_event.clear()
    _worker_thread = threading.Thread(
        target=_worker_loop, name="event-lifecycle-worker", daemon=True,
    )
    _worker_thread.start()


def stop_event_lifecycle_worker() -> None:
    global _worker_thread
    _stop_event.set()
    if _worker_thread and _worker_thread.is_alive():
        _worker_thread.join(timeout=2.0)
    _worker_thread = None
