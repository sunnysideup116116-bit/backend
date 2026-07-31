"""Bounded, local agent runtime for the public Ayue assistant."""


def run_public_agent_turn(*args, **kwargs):
    """Lazy import keeps pure router tests independent of MongoDB dependencies."""
    from .runtime import run_public_agent_turn as _run_public_agent_turn
    return _run_public_agent_turn(*args, **kwargs)


__all__ = ["run_public_agent_turn"]