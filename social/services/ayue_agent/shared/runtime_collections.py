"""Engine-neutral storage bindings for public Ayue runtime state."""
from __future__ import annotations

import os
from typing import Any

from database import db


_TEST_MODE = os.getenv("AYUE_TEST_MODE", "").strip().lower() in {"1", "true", "on"}
if _TEST_MODE:
    from services.ayue_agent.shared.test_store import MemoryCollection
    _COLLECTIONS: dict[str, Any] = {
        name: MemoryCollection()
        for name in (
            "agent_runs", "v3_pending_confirmations",
            "v3_operation_batches", "v3_contact_selections",
        )
    }


def runtime_collection(name: str) -> Any:
    return _COLLECTIONS[name] if _TEST_MODE else db[name]


RUNS = runtime_collection("agent_runs")
CONFIRMATIONS = runtime_collection("v3_pending_confirmations")
OPERATION_BATCHES = runtime_collection("v3_operation_batches")
CONTACT_SELECTIONS = runtime_collection("v3_contact_selections")
