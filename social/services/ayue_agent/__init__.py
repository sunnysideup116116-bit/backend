"""Bounded, local agent runtime for the public Ayue assistant."""

from __future__ import annotations


def run_public_agent_turn(*args, **kwargs):
    """Pi public entrypoint for HTTP and shared voice callers."""
    from .public_runtime import run_public_agent_turn as _run_public
    return _run_public(*args, **kwargs)


def mark_public_interaction_presented(**kwargs):
    """Activate any prepared public interaction after durable publication."""
    from .public_runtime import mark_public_interaction_presented as _mark_interaction
    return _mark_interaction(**kwargs)


__all__ = [
    "run_public_agent_turn", "mark_public_interaction_presented",
]
