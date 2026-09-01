"""Ephemeral localhost-only V3 diagnostics.

This store is deliberately separate from the durable privacy-safe agent trace.
It may contain prompts and typed observations, so it is disabled by default,
kept in process memory only, bounded, and exposed only through a loopback-only
HTTP adapter.
"""

from __future__ import annotations

import copy
import os
import threading
import time
from collections import OrderedDict
from typing import Any


_LOCK = threading.RLock()
_RUNS: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
_MAX_RUNS = 16
_MAX_EVENTS = 96
_TTL_SECONDS = 30 * 60
_SENSITIVE_KEYS = {"api_key", "apikey", "authorization", "password", "secret", "token", "access_token"}


def local_debug_enabled() -> bool:
    return os.getenv("AYUE_LOCAL_DEBUG_TRACE", "off").strip().lower() in {"1", "true", "on"}


def _bounded(value: Any, *, depth: int = 0) -> Any:
    if depth >= 7:
        return "[DEPTH_LIMIT]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:24000] + ("…[TRUNCATED]" if len(value) > 24000 else "")
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for index, (raw_key, item) in enumerate(value.items()):
            if index >= 80:
                output["__truncated__"] = True
                break
            key = str(raw_key)[:120]
            normalized_key = key.lower()
            if normalized_key in _SENSITIVE_KEYS or normalized_key.endswith(("_api_key", "_secret", "_password", "_token")):
                output[key] = "[REDACTED]"
            else:
                output[key] = _bounded(item, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        result = [_bounded(item, depth=depth + 1) for item in items[:50]]
        if len(items) > 50:
            result.append("[ITEM_LIMIT]")
        return result
    if hasattr(value, "model_dump"):
        return _bounded(value.model_dump(mode="json"), depth=depth + 1)
    return _bounded(str(value), depth=depth + 1)


def _cleanup(now: float) -> None:
    expired = [run_id for run_id, run in _RUNS.items() if now - float(run.get("updated_at", now)) > _TTL_SECONDS]
    for run_id in expired:
        _RUNS.pop(run_id, None)
    while len(_RUNS) > _MAX_RUNS:
        _RUNS.popitem(last=False)


def begin_run(run_id: str, user_id: str) -> None:
    if not local_debug_enabled():
        return
    now = time.time()
    with _LOCK:
        _cleanup(now)
        _RUNS[run_id] = {
            "run_id": run_id,
            "user_id": user_id,
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "events": [],
        }
        _RUNS.move_to_end(run_id)


def append_event(run_id: str, event_type: str, **payload: Any) -> None:
    if not local_debug_enabled():
        return
    now = time.time()
    with _LOCK:
        run = _RUNS.get(run_id)
        if run is None:
            return
        events = run["events"]
        if len(events) < _MAX_EVENTS:
            events.append(_bounded({"type": event_type, "at_ms": round((now - run["started_at"]) * 1000), **payload}))
        run["updated_at"] = now


def finish_run(run_id: str, *, status: str, response: dict[str, Any] | None = None) -> None:
    if not local_debug_enabled():
        return
    append_event(run_id, "final" if status == "completed" else "error", response=response or {})
    now = time.time()
    with _LOCK:
        run = _RUNS.get(run_id)
        if run is not None:
            run["status"] = status
            run["finished_at"] = now
            run["updated_at"] = now


def get_run(run_id: str, user_id: str) -> dict[str, Any] | None:
    if not local_debug_enabled():
        return None
    now = time.time()
    with _LOCK:
        _cleanup(now)
        run = _RUNS.get(run_id)
        if run is None or run.get("user_id") != user_id:
            return None
        return copy.deepcopy(run)
