"""Named bridge to mature deterministic public write executors.

It exposes confirmed domain writes without a planner or orchestration dependency.
"""
from services.ayue_agent.shared.write_executors import (
    _prepare_date_coordination_for_contact_id as prepare_date_coordination_for_contact_id,
    execute_write,
    prepare_write_confirmation,
)

__all__ = [
    "execute_write", "prepare_date_coordination_for_contact_id",
    "prepare_write_confirmation",
]
