"""Typed contracts for the V3 sub-agent runtime."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Forbidden ID/revision fields a sub-agent must never fill.
FORBIDDEN_ARG_FIELDS = frozenset({"user_id", "match_id", "event_id", "revision", "expected_status"})

VALID_AGENTS = frozenset({
    "calendar", "places", "match", "relationship", "profile", "synthesizer",
})


class SubTaskStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class GuardResultCode(str, Enum):
    PASSED = "passed"
    SCHEMA_INVALID = "schema_invalid"
    FORBIDDEN_ARG_FIELD = "forbidden_arg_field"
    DUPLICATE_CALL = "duplicate_call"
    STEP_LIMIT_EXCEEDED = "step_limit_exceeded"
    WRITE_REQUIRES_CONFIRMATION = "write_requires_confirmation"
    TOOL_NOT_REGISTERED = "tool_not_registered"


class OpportunitySignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal: Literal["none", "social_opening"] = "none"
    evidence_span: str = ""
    confidence: float = 0.0


class SubTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    agent: Literal["calendar", "places", "match", "relationship", "profile", "synthesizer"]
    depends_on: list[str] = Field(default_factory=list, max_length=3)
    task_brief: str = Field(min_length=1, max_length=500)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # At most three domain specialists plus one terminal synthesizer.
    tasks: list[SubTask] = Field(min_length=1, max_length=4)
    opportunity: OpportunitySignal | None = None

    @model_validator(mode="after")
    def _validate_dag(self) -> "Plan":
        ids = {t.id for t in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("duplicate SubTask id")
        for t in self.tasks:
            if len(set(t.depends_on)) != len(t.depends_on):
                raise ValueError(f"SubTask {t.id} has duplicate dependencies")
            if t.id in t.depends_on:
                raise ValueError(f"SubTask {t.id} cannot depend on itself")
            for dep in t.depends_on:
                if dep not in ids:
                    raise ValueError(f"SubTask {t.id} depends on unknown {dep}")
        synthesizers = [t for t in self.tasks if t.agent == "synthesizer"]
        if len(synthesizers) != 1:
            raise ValueError("plan must contain exactly one synthesizer")
        synthesizer = synthesizers[0]
        domain_ids = ids - {synthesizer.id}
        referenced_domains = {
            dep
            for task in self.tasks
            if task.agent != "synthesizer"
            for dep in task.depends_on
        }
        terminal_domain_ids = domain_ids - referenced_domains
        if not terminal_domain_ids <= set(synthesizer.depends_on):
            raise ValueError("synthesizer must depend on every terminal domain task")
        for other in self.tasks:
            if other.id != synthesizer.id and synthesizer.id in other.depends_on:
                raise ValueError(f"synthesizer {synthesizer.id} must be terminal")

        # A valid reference set can still contain a cycle.  Walk the whole DAG
        # so the Scheduler never receives an unbounded/unexecutable plan.
        completed: set[str] = set()
        remaining = list(self.tasks)
        while remaining:
            ready = [t for t in remaining if set(t.depends_on) <= completed]
            if not ready:
                raise ValueError("plan contains a dependency cycle")
            completed.update(t.id for t in ready)
            remaining = [t for t in remaining if t not in ready]
        return self


class AgentContextSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["calendar", "places", "match", "relationship", "profile", "synthesizer"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolProposal(BaseModel):
    """A sub-agent's tool-call proposal. Execution stays with the Scheduler/Guard."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_forbidden_fields(self) -> "ToolProposal":
        present = FORBIDDEN_ARG_FIELDS & set(self.arguments.keys())
        if present:
            raise ValueError(f"forbidden arg fields: {sorted(present)}")
        return self


class GuardDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    code: GuardResultCode
    reason: str = ""


class SubTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: SubTaskStatus
    tool_name: str | None = None
    observation: dict[str, Any] | None = None
    error_code: str | None = None
    skip_reason: str | None = None
    guard_code: GuardResultCode | None = None
