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
import traceback
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
_supervisor_thread: threading.Thread | None = None
_stop_event = threading.Event()
_worker_lock = threading.Lock()

# The supervisor restarts a worker that exits unexpectedly with bounded
# exponential backoff. A persistently broken worker must make the Social
# process fail so systemd's Restart=on-failure policy can recover the full
# service stack instead of leaving HTTP healthy with no Event consumer.
_SUPERVISOR_MAX_RESTARTS = 3
_SUPERVISOR_RESTART_BASE_SECONDS = 2.0
_SUPERVISOR_RESTART_MAX_SECONDS = 30.0
_SUPERVISOR_POLL_SECONDS = 1.0
_SUPERVISOR_STABLE_SECONDS = 300.0
_SUPERVISOR_SHUTDOWN_JOIN_SECONDS = 3.0
_SUPERVISOR_EXIT_CODE = 70


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
        from services.event_discovery_job_service import event_job_is_current
        return run_weekly_event_cycle(
            **arguments,
            run_id=str(job.get("job_token") or ""),
            is_current=lambda: not _stop_event.is_set() and event_job_is_current(job, worker_id),
            stage_callback=lambda stage: update_event_discovery_job_stage(
                job, worker_id, stage,
            ),
        )
    update_event_discovery_job_stage(job, worker_id, "discovering")
    # Manual jobs never opt into the legacy same-process invitation hook.
    result = discover_and_ingest_events(**arguments, request_invitation_scan=False)
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
            # Bounded automatic circuit recovery: once the OPEN cooldown has
            # elapsed, one synthetic probe may close the circuit and let
            # checkpointed semantic work resume. Never touches the manual kill.
            if os.getenv("EVENT_SEMANTIC_CIRCUIT_RECOVERY", "on").strip().lower() in {
                "1", "true", "on",
            }:
                try:
                    from services.event_semantic_monitor import probe_if_due
                    probe_if_due()
                except Exception:
                    pass
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


def _run_worker_attempt(
    stop_event: threading.Event,
    result: dict[str, BaseException],
    worker_runner,
) -> None:
    try:
        worker_runner(stop_event)
    except BaseException as exc:
        result["error"] = exc
        try:
            print(
                f"[EVENT_WORKER] worker attempt raised error={type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            traceback.print_tb(exc.__traceback__, file=sys.stderr)
        except Exception:
            print(
                f"[EVENT_WORKER] worker attempt raised error={type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )


def _exit_social_after_worker_failure(
    consecutive_failures: int, error: BaseException | None,
) -> None:
    error_name = type(error).__name__ if error is not None else "unexpected_return"
    print(
        "[EVENT_WORKER] fatal: bounded recovery exhausted "
        f"failures={consecutive_failures} last_error={error_name} "
        f"process_exit={_SUPERVISOR_EXIT_CODE}",
        file=sys.stderr,
        flush=True,
    )
    os._exit(_SUPERVISOR_EXIT_CODE)


def _supervise_worker(
    stop_event: threading.Event,
    *,
    worker_runner=None,
    fatal_handler=None,
    max_restarts: int | None = None,
    restart_base_seconds: float | None = None,
    restart_max_seconds: float | None = None,
    poll_seconds: float | None = None,
    stable_seconds: float | None = None,
) -> None:
    """Run exactly one worker attempt at a time and recover bounded failures.

    There is intentionally no work-duration timeout. A live worker may be idle,
    waiting on MongoDB, performing Discovery, or awaiting an LLM/provider call.
    Only an attempt thread that has actually exited is considered failed.
    """
    global _worker_thread
    runner = worker_runner or run_worker
    fatal = fatal_handler or _exit_social_after_worker_failure
    restart_limit = _SUPERVISOR_MAX_RESTARTS if max_restarts is None else max_restarts
    base_delay = (
        _SUPERVISOR_RESTART_BASE_SECONDS
        if restart_base_seconds is None else restart_base_seconds
    )
    max_delay = (
        _SUPERVISOR_RESTART_MAX_SECONDS
        if restart_max_seconds is None else restart_max_seconds
    )
    poll_interval = _SUPERVISOR_POLL_SECONDS if poll_seconds is None else poll_seconds
    stable_uptime = _SUPERVISOR_STABLE_SECONDS if stable_seconds is None else stable_seconds
    consecutive_failures = 0

    while not stop_event.is_set():
        result: dict[str, BaseException] = {}
        worker = threading.Thread(
            target=_run_worker_attempt,
            args=(stop_event, result, runner),
            name="event-discovery-worker",
            daemon=True,
        )
        with _worker_lock:
            _worker_thread = worker
        started_at = time.monotonic()
        worker.start()
        while worker.is_alive():
            worker.join(timeout=poll_interval)
        with _worker_lock:
            if _worker_thread is worker:
                _worker_thread = None

        if stop_event.is_set():
            return

        if time.monotonic() - started_at >= stable_uptime:
            consecutive_failures = 0
        consecutive_failures += 1
        error = result.get("error")
        error_name = type(error).__name__ if error is not None else "unexpected_return"
        print(
            f"[EVENT_WORKER] worker exited unexpectedly; "
            f"consecutive_failures={consecutive_failures} error={error_name}",
            file=sys.stderr,
            flush=True,
        )
        if consecutive_failures > restart_limit:
            fatal(consecutive_failures, error)
            # Test handlers may return; production's handler exits the process.
            stop_event.set()
            return

        delay = min(base_delay * (2 ** (consecutive_failures - 1)), max_delay)
        print(
            f"[EVENT_WORKER] restarting after delay={delay:g}s "
            f"retry={consecutive_failures}/{restart_limit}",
            flush=True,
        )
        if stop_event.wait(delay):
            return


def _event_worker_supervisor_entry(stop_event: threading.Event) -> None:
    try:
        _supervise_worker(stop_event)
    except BaseException as exc:
        if stop_event.is_set():
            print(
                f"[EVENT_WORKER] supervisor stopped during shutdown "
                f"error={type(exc).__name__}",
                file=sys.stderr,
                flush=True,
            )
            return
        _exit_social_after_worker_failure(1, exc)


def start_event_discovery_worker() -> None:
    global _supervisor_thread
    with _worker_lock:
        if _supervisor_thread and _supervisor_thread.is_alive():
            return
        if _worker_thread and _worker_thread.is_alive():
            # A live attempt without its supervisor cannot be safely replaced.
            # Fail the Social process rather than allowing a second claimant.
            _exit_social_after_worker_failure(
                1, RuntimeError("worker_alive_without_supervisor"),
            )
        _stop_event.clear()
        _supervisor_thread = threading.Thread(
            target=_event_worker_supervisor_entry,
            args=(_stop_event,),
            name="event-discovery-supervisor",
            daemon=True,
        )
        _supervisor_thread.start()
        print("[EVENT_WORKER] background supervisor thread started", flush=True)


def stop_event_discovery_worker() -> None:
    global _worker_thread, _supervisor_thread
    with _worker_lock:
        supervisor = _supervisor_thread
        worker = _worker_thread
        if not supervisor or not supervisor.is_alive():
            _stop_event.set()
            if worker and worker.is_alive():
                print(
                    "[EVENT_WORKER] supervisor unavailable while worker remains alive; "
                    "worker state retained",
                    file=sys.stderr,
                    flush=True,
                )
                return
            _worker_thread = None
            _supervisor_thread = None
            return
        _stop_event.set()
    supervisor.join(timeout=_SUPERVISOR_SHUTDOWN_JOIN_SECONDS)
    with _worker_lock:
        if _supervisor_thread is supervisor and not supervisor.is_alive():
            _supervisor_thread = None
        worker = _worker_thread
        if worker and not worker.is_alive():
            _worker_thread = None
        still_alive = bool(supervisor.is_alive() or (worker and worker.is_alive()))
    if still_alive:
        print(
            "[EVENT_WORKER] shutdown pending; active worker retained and "
            "duplicate start refused until it exits",
            file=sys.stderr,
            flush=True,
        )
    else:
        print("[EVENT_WORKER] background supervisor stopped", flush=True)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    run_worker()
