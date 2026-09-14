"""Minimal deterministic guard contracts shared by public-agent runtimes."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


FORBIDDEN_ARG_FIELDS = frozenset({
    "user_id", "match_id", "event_id", "coordination_id", "calendar_event_id",
    "other_id", "revision", "expected_revision", "expected_status",
    "expected_coordination_revision", "expected_event_revision",
})


class GuardResultCode(str, Enum):
    PASSED = "passed"
    SCHEMA_INVALID = "schema_invalid"
    FORBIDDEN_ARG_FIELD = "forbidden_arg_field"
    DUPLICATE_CALL = "duplicate_call"
    STEP_LIMIT_EXCEEDED = "step_limit_exceeded"
    WRITE_REQUIRES_CONFIRMATION = "write_requires_confirmation"
    TOOL_NOT_REGISTERED = "tool_not_registered"


class ToolProposal(BaseModel):
    """A model-proposed tool call that contains no authority fields."""

    model_config = ConfigDict(extra="forbid")
    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_forbidden_fields(self) -> "ToolProposal":
        present = FORBIDDEN_ARG_FIELDS & set(self.arguments)
        if present:
            raise ValueError(f"forbidden arg fields: {sorted(present)}")
        return self


class GuardDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ok: bool
    code: GuardResultCode
    reason: str = ""

