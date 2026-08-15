"""Central Guard for V3: pure deterministic checks, zero LLM."""

from __future__ import annotations

from services.ayue_agent.tool_registry import (
    TOOL_REGISTRY,
    ToolRisk,
    get_tool_spec,
    planner_arguments_allowed,
    tool_call_key,
)
from .contracts import GuardDecision, GuardResultCode, ToolProposal
from .calendar_commands import CalendarCommand


def guard_calendar_commands(commands: list[CalendarCommand]) -> GuardDecision:
    """Validate the typed Calendar intent before deterministic preflight.

    Commands are not executable tools, so this check intentionally does not
    resolve targets or decide whether a command is complete.  Missing business
    fields are normal preflight outcomes; authority fields are impossible to
    represent in the strict command model and are checked again here.
    """
    if not commands or len(commands) > 10:
        return GuardDecision(ok=False, code=GuardResultCode.SCHEMA_INVALID,
                             reason="calendar command batch must contain 1-10 commands")
    forbidden = {"user_id", "event_id", "revision", "expected_revision", "match_id", "coordination_id", "other_id"}
    for command in commands:
        if forbidden.intersection(command.model_dump().keys()):
            return GuardDecision(ok=False, code=GuardResultCode.FORBIDDEN_ARG_FIELD,
                                 reason="calendar command contains an authority field")
    return GuardDecision(ok=True, code=GuardResultCode.PASSED, reason="typed calendar commands accepted")


def guard_proposal(
    proposal: ToolProposal,
    *,
    agent_name: str,
    seen_keys: set[tuple[str, str]],
    step_count: int,
    max_reads: int,
) -> GuardDecision:
    """Validate a sub-agent's tool-call proposal before execution.

    Pure deterministic checks; never calls LLM.
    """
    spec = get_tool_spec(proposal.tool_name)
    if spec is None:
        return GuardDecision(ok=False, code=GuardResultCode.TOOL_NOT_REGISTERED,
                             reason=f"tool {proposal.tool_name} not registered")

    # Schema validation: planner args must match the tool's planner_arguments_model.
    if not planner_arguments_allowed(spec, proposal.arguments):
        return GuardDecision(ok=False, code=GuardResultCode.SCHEMA_INVALID,
                             reason="arguments do not match planner schema")

    # Duplicate check: same tool+args already run by this agent.
    key = tool_call_key(spec, proposal.arguments)
    if key in seen_keys:
        return GuardDecision(ok=False, code=GuardResultCode.DUPLICATE_CALL,
                             reason="duplicate tool+args already run")

    # Step limit: per-agent read cap.
    if spec.risk is ToolRisk.READ and step_count >= max_reads:
        return GuardDecision(ok=False, code=GuardResultCode.STEP_LIMIT_EXCEEDED,
                             reason=f"agent {agent_name} exceeded {max_reads} reads")

    # Write tools never execute directly; Scheduler must build confirmation.
    if spec.risk is ToolRisk.WRITE:
        return GuardDecision(ok=False, code=GuardResultCode.WRITE_REQUIRES_CONFIRMATION,
                             reason=f"write tool {proposal.tool_name} requires confirmation")

    return GuardDecision(ok=True, code=GuardResultCode.PASSED, reason="")
