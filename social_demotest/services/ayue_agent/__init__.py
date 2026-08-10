"""Bounded, local agent runtime for the public Ayue assistant."""

from __future__ import annotations


def run_public_agent_turn_v3(*args, **kwargs):
    """Lazy import for the V3 sub-agent runtime."""
    from .v3.scheduler import run_public_agent_turn_v3 as _run_v3
    return _run_v3(*args, **kwargs)


__all__ = ["run_public_agent_turn_v3"]
