"""Engine-neutral Calendar mutation preparation and confirmation binding.

This module owns deterministic Calendar preparation and confirmation binding
for public Pi without exposing authority fields to the model.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from services.ayue_agent.shared.calendar_commands import (
    CalendarCommand,
    CalendarPreflightResult,
    canonicalize_calendar_command,
    preflight_calendar_commands,
)
from services.ayue_agent.shared.guard import guard_calendar_commands


PI_CALENDAR_PROTOCOL = "pi_calendar.v1"


@dataclass(frozen=True)
class CalendarPreparation:
    status: str
    observation: dict[str, Any]
    error_code: str | None = None


def _clarification_observation(preflight: CalendarPreflightResult) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": preflight.status,
        "denial_code": preflight.denial_code,
        "preview": preflight.preview,
    }
    if preflight.clarification is not None:
        result["clarification"] = preflight.clarification.model_dump()
    return {"calendar_command_result": result}


def prepare_pi_calendar_confirmation(
    turn: Any,
    commands: list[CalendarCommand],
    *,
    run_id: str,
    create_confirmation: Any,
    supersede_confirmation: Any = None,
) -> CalendarPreparation:
    """Validate and bind one Pi-owned Calendar confirmation without DAG state."""
    canonical: list[CalendarCommand] = []
    for command in commands:
        value, error = canonicalize_calendar_command(turn, command)
        if error:
            return CalendarPreparation(
                "needs_clarification",
                {"calendar_command_result": {
                    "status": "needs_clarification",
                    "clarification": {
                        "code": "invalid_date",
                        "message": error,
                        "command_index": len(canonical),
                        "missing_fields": [],
                    },
                }},
            )
        canonical.append(value)

    guard = guard_calendar_commands(canonical)
    if not guard.ok:
        return CalendarPreparation(
            "failed",
            {"failure": {"code": guard.code.value, "operation_prepared": False}},
            guard.code.value,
        )

    if supersede_confirmation is not None:
        supersession = supersede_confirmation(
            user_id=turn.user_id,
            tool_name="calendar.submit_commands",
            reason="pi_calendar_replacement_attempt",
        )
        supersession_status = str((supersession or {}).get("status") or "none")
        if supersession_status in {"already_executing", "already_completed", "storage_unavailable"}:
            return CalendarPreparation(
                "failed",
                {"failure": {
                    "code": f"confirmation_{supersession_status}",
                    "operation_prepared": False,
                }},
                f"confirmation_{supersession_status}",
            )

    preflight = preflight_calendar_commands(turn, canonical)
    if preflight.status != "ready":
        return CalendarPreparation(preflight.status, _clarification_observation(preflight))

    payload = {
        "calendar_plan_version": 1,
        "source_engine": "pi",
        "tool_protocol_version": PI_CALENDAR_PROTOCOL,
        "plans": [plan.model_dump(exclude_none=True) for plan in preflight.plans],
    }
    create_confirmation(
        user_id=turn.user_id,
        agent_name="calendar",
        tool_name="calendar.submit_commands",
        arguments={},
        payload=payload,
        origin_run_id=run_id,
        preview=preflight.preview or "",
    )
    return CalendarPreparation(
        "pending_confirmation",
        {
            "pending_confirmation": True,
            "tool_name": "calendar.submit_commands",
            "preview": preflight.preview or "",
            "source_engine": "pi",
            "tool_protocol_version": PI_CALENDAR_PROTOCOL,
        },
    )
