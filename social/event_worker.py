r"""Dedicated low-priority Event discovery worker.

Can be run standalone:
    ..\.project-venv\Scripts\python.exe event_worker.py

Or embedded in FastAPI lifecycle via start_event_discovery_worker() / stop_event_discovery_worker().
"""

from __future__ import annotations

import os
import sys
import threading
import time
import uuid

from pymongo.errors import PyMongoError

from services.event_discovery_job_service import (
    claim_event_discovery_job,
    enqueue_weekly_event_discovery_if_due,
    ensure_event_discovery_job_indexes,
    fail_event_discovery_job,
    finish_event_discovery_job,
    open_event_discovery_job_change_stream,
    renew_event_discovery_job_lease,
    update_event_discovery_job_stage,
)
from services.event_cycle_service import run_weekly_event_cycle
from services.event_discovery_service import discover_and_ingest_events

_worker_thread: threading.Thread | None = None
_stop_event = threading.Event()
_worker_lock = threading.Lock()


def _keep_job_lease_alive(
    job: dict, worker_id: str, stop_event: threading.Event,
) -> None:
    while not stop_event.wait(30.0):
        if not renew_event_discovery_job_lease(job, worker_id):
            print("[EVENT_WORKER] lease lost; current run will not be committed", flush=True)
            return


def _reconcile_seconds() -> float:
    try:
        value = float(os.getenv("EVENT_WORKER_RECONCILE_SECONDS", "60") or 60)
    except (TypeError, ValueError):
        value = 60.0
    return max(10.0, min(value, 300.0))


def _open_wakeup_stream(reconcile_seconds: float):
    try:
        stream = open_event_discovery_job_change_stream(
            max_await_time_ms=int(reconcile_seconds * 1000),
        )
        print("[EVENT_WORKER] MongoDB change-stream wake-up active", flush=True)
        return stream
    except PyMongoError as exc:
        print(
            f"[EVENT_WORKER] change stream unavailable error={type(exc).__name__}; "
            f"reconciling every {int(reconcile_seconds)}s",
            flush=True,
        )
        return None


def _wait_for_work(stream, reconcile_seconds: float, stop_event: threading.Event | None = None):
    if stop_event and stop_event.is_set():
        return None
    if stream is None:
        if stop_event:
            stop_event.wait(reconcile_seconds)
        else:
            time.sleep(reconcile_seconds)
        return None
    try:
        stream.try_next()
        return stream
    except PyMongoError as exc:
        print(
            f"[EVENT_WORKER] change stream interrupted error={type(exc).__name__}; "
            "falling back to reconciliation",
            flush=True,
        )
        try:
            stream.close()
        except Exception:
            pass
        if stop_event:
            stop_event.wait(reconcile_seconds)
        else:
            time.sleep(reconcile_seconds)
        return None


def _execute_job(job: dict, worker_id: str) -> dict:
    arguments = {
        "region": str(job.get("region") or "高雄"),
        "window_days": int(job.get("window_days") or 30),
        "categories": list(job.get("categories") or []),
    }
    if job.get("job_kind") == "weekly_cycle":
        return run_weekly_event_cycle(
            **arguments,
            stage_callback=lambda stage: update_event_discovery_job_stage(
                job, worker_id, stage,
            ),
        )
    update_event_discovery_job_stage(job, worker_id, "discovering")
    result = discover_and_ingest_events(**arguments)
    result["job_kind"] = "discovery"
    return result


def run_worker(stop_event: threading.Event | None = None) -> None:
    worker_id = f"event-worker-{uuid.uuid4().hex[:10]}"
    reconcile_seconds = _reconcile_seconds()
    ensure_event_discovery_job_indexes()
    print(f"[EVENT_WORKER] started worker={worker_id}", flush=True)
    wakeup_stream = _open_wakeup_stream(reconcile_seconds)
    try:
        while stop_event is None or not stop_event.is_set():
            if os.getenv("EVENT_WEEKLY_CYCLE_ENABLED", "off").strip().lower() in {
                "1", "true", "on",
            }:
                enqueue_weekly_event_discovery_if_due()
            job = claim_event_discovery_job(worker_id)
            if not job:
                wakeup_stream = _wait_for_work(wakeup_stream, reconcile_seconds, stop_event)
                if wakeup_stream is None and (stop_event is None or not stop_event.is_set()):
                    wakeup_stream = _open_wakeup_stream(reconcile_seconds)
                continue
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(
                target=_keep_job_lease_alive,
                args=(job, worker_id, heartbeat_stop),
                name="event-job-heartbeat",
                daemon=True,
            )
            heartbeat.start()
            try:
                result = _execute_job(job, worker_id)
                finish_event_discovery_job(job, result)
                print(
                    f"[EVENT_WORKER] completed outcome={result.get('status')} "
                    f"ingested={result.get('ingested_count', 0)}",
                    flush=True,
                )
            except Exception as exc:
                fail_event_discovery_job(job, exc)
                print(f"[EVENT_WORKER] failed error={type(exc).__name__}", flush=True)
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=2.0)
    finally:
        if wakeup_stream is not None:
            try:
                wakeup_stream.close()
            except Exception:
                pass
        print(f"[EVENT_WORKER] stopped worker={worker_id}", flush=True)


def start_event_discovery_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread and _worker_thread.is_alive():
            return
        _stop_event.clear()
        _worker_thread = threading.Thread(
            target=run_worker,
            args=(_stop_event,),
            name="event-discovery-worker",
            daemon=True,
        )
        _worker_thread.start()
        print("[EVENT_WORKER] background worker thread started", flush=True)


def stop_event_discovery_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if not _worker_thread or not _worker_thread.is_alive():
            return
        _stop_event.set()
        _worker_thread.join(timeout=3.0)
        _worker_thread = None
        print("[EVENT_WORKER] background worker thread stopped", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run_worker()
