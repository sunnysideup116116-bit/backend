"""Indexes and status queries for shared public-agent runtime state."""

from __future__ import annotations

from .confirmation import ConfirmationManager
from .contact_selections import ContactSelectionManager
from .operation_batches import OperationBatchManager
from .runtime_collections import CONFIRMATIONS, CONTACT_SELECTIONS, OPERATION_BATCHES, RUNS


def ensure_indexes() -> None:
    try:
        RUNS.create_index("created_at", expireAfterSeconds=14 * 86400)
        RUNS.create_index([("user_id", 1), ("created_at", -1)])
        OperationBatchManager(OPERATION_BATCHES).ensure_indexes()
        ContactSelectionManager(CONTACT_SELECTIONS).ensure_indexes()
    except Exception as exc:
        print(f"Agent runtime index setup skipped: {type(exc).__name__}")


def has_active_public_confirmation(user_id: str) -> bool:
    try:
        return bool(ConfirmationManager(CONFIRMATIONS).list_active(
            user_id=user_id,
            surface="public_ayue",
        ))
    except Exception:
        return False
