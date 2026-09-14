"""Production Pi entrypoint for public Ayue."""
from __future__ import annotations

from typing import Any


def run_public_agent_turn(*args: Any, **kwargs: Any):
    ctx = args[0] if args else kwargs.get("ctx")
    if not bool(getattr(ctx, "external_calendar_authorized", False)):
        raise PermissionError("pi_authenticated_owner_required")
    from .pi.public_turn import run_pi_public_turn
    return run_pi_public_turn(*args, **kwargs)


def mark_public_interaction_presented(**kwargs: Any) -> bool:
    from .shared.runtime_collections import CONFIRMATIONS, CONTACT_SELECTIONS
    from .shared.confirmation import ConfirmationManager
    from .shared.contact_selections import ContactSelectionManager
    confirmation = ConfirmationManager(CONFIRMATIONS).mark_presented(**kwargs)
    selection = ContactSelectionManager(CONTACT_SELECTIONS).mark_presented(
        user_id=kwargs["user_id"], origin_run_id=kwargs["origin_run_id"],
        message_id=kwargs["message_id"], persisted_content=kwargs["persisted_content"],
        interaction_blocks_v1=kwargs.get("interaction_blocks_v1") or [],
    )
    return bool(confirmation or selection)
