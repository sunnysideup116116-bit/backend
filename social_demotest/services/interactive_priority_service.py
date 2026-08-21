"""Cross-process priority lease for interactive Public Ayue turns."""

from __future__ import annotations

import os
import time
import uuid
from contextlib import contextmanager

from database import db


_leases = db["runtime_priority_leases"]


def ensure_interactive_priority_indexes() -> None:
    try:
        _leases.create_index("expires_at", name="interactive_lease_expiry")
    except Exception:
        pass


@contextmanager
def interactive_chat_lease(ttl_seconds: int = 300):
    """No-op compatibility placeholder; Event worker runs independently."""
    yield


def interactive_chat_active() -> bool:
    """Always False; Event discovery does not yield to interactive chat."""
    return False

