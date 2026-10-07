"""Automatic provider circuit separated from the operator manual kill.

Two independent authorities:

* MANUAL KILL -- the existing ``.related-interest-disabled`` file (or
  ``MATCH_RELATED_INTEREST_KILL_SWITCH_FILE``). Operator-only. Never written by
  this module and never auto-cleared. It remains absolute (checked by
  ``related_interest_contract.enabled``/``kill_engaged``).
* AUTO CIRCUIT -- this module. CLOSED -> OPEN after ``CONSECUTIVE_FAILURE_LIMIT``
  terminal systemic unavailabilities inside ``WINDOW_SECONDS`` -> bounded
  cooldown -> HALF_OPEN -> one synthetic, private-data-free provider probe ->
  healthy: CLOSED, resume; unhealthy: OPEN + next cooldown.

While OPEN, semantic work fails closed: the caller returns the existing typed
unavailable without calling providers. Exact-safe behavior is untouched.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

# Bounded cooldown: first open waits 5 min, then doubles to a 30 min cap.
COOLDOWN_BASE_SECONDS = 300
COOLDOWN_CAP_SECONDS = 1800


def state_path():
    override = os.getenv("EVENT_SEMANTIC_CIRCUIT_FILE")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[1] / ".event-semantic-circuit"


def _read(path=None):
    path = path or state_path()
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {"state": CLOSED, "opened_at": 0.0, "cooldown": COOLDOWN_BASE_SECONDS,
                "opens": 0}
    if not isinstance(value, dict) or value.get("state") not in {CLOSED, OPEN, HALF_OPEN}:
        return {"state": CLOSED, "opened_at": 0.0, "cooldown": COOLDOWN_BASE_SECONDS,
                "opens": 0}
    return value


def _write(value, path=None):
    path = Path(path or state_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(value, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, path)
    except Exception:
        # Persistence failure must not crash the request path; fail closed.
        pass


def snapshot(path=None):
    return _read(path)


def allow_semantic(now=time.time, path=None):
    """True when semantic calls may run right now.

    CLOSED -> True. OPEN -> False until the cooldown elapses (then the caller
    may run the half-open probe). HALF_OPEN -> False (probe pending).
    """
    state = _read(path)
    if state["state"] == CLOSED:
        return True
    if state["state"] == HALF_OPEN:
        return False
    return (now() - float(state.get("opened_at") or 0)) >= float(state.get("cooldown") or 0)


def cooldown_elapsed(now=time.time, path=None):
    state = _read(path)
    return (state["state"] == OPEN
            and (now() - float(state.get("opened_at") or 0)) >= float(state.get("cooldown") or 0))


def open_circuit(now=time.time, path=None):
    """Trip the automatic circuit (never the manual kill file)."""
    state = _read(path)
    opens = int(state.get("opens") or 0) + 1
    cooldown = min(COOLDOWN_CAP_SECONDS, COOLDOWN_BASE_SECONDS * (2 ** min(opens - 1, 5)))
    _write({"state": OPEN, "opened_at": float(now()), "cooldown": cooldown, "opens": opens},
        path)
    return snapshot(path)


def begin_half_open(path=None):
    state = _read(path)
    if state["state"] != OPEN:
        return state
    _write({**state, "state": HALF_OPEN}, path)
    return snapshot(path)


def close_circuit(path=None):
    _write({"state": CLOSED, "opened_at": 0.0, "cooldown": COOLDOWN_BASE_SECONDS,
            "opens": 0}, path)
    return snapshot(path)


def probe_and_recover(probe, now=time.time, path=None):
    """Run one synthetic probe when the cooldown elapsed.

    `probe` is a zero-argument callable returning True when the providers are
    healthy. Returns the resulting state name. A raising probe is treated as
    unhealthy. When the cooldown has not elapsed the state is left untouched.
    """
    if not cooldown_elapsed(now=now, path=path):
        return snapshot(path)["state"]
    begin_half_open(path=path)
    try:
        healthy = bool(probe())
    except Exception:
        healthy = False
    if healthy:
        close_circuit(path=path)
        return CLOSED
    state = _read(path)
    opens = max(1, int(state.get("opens") or 1))
    cooldown = min(COOLDOWN_CAP_SECONDS, COOLDOWN_BASE_SECONDS * (2 ** min(opens - 1, 5)))
    _write({"state": OPEN, "opened_at": float(now()), "cooldown": cooldown, "opens": opens},
        path)
    return OPEN
