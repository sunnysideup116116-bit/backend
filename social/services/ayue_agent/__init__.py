"""Bounded, local agent runtime for the public Ayue assistant."""

from __future__ import annotations


def run_public_agent_turn_v3(*args, **kwargs):
    """Lazy import for the V3 sub-agent runtime."""
    from .v3.scheduler import run_public_agent_turn_v3 as _run_v3
    return _run_v3(*args, **kwargs)


def mark_public_confirmation_presented(**kwargs):
    """Lazy facade for the persisted-public-preview confirmation boundary."""
    from .v3.scheduler import mark_public_confirmation_presented as _mark_presented
    return _mark_presented(**kwargs)


__all__ = ["run_public_agent_turn_v3", "mark_public_confirmation_presented"]
