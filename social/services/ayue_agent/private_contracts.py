"""Typed contracts owned by the accepted-pair Private Ayue surface."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .contracts import AgentResult


class PrivateAgentDecision(BaseModel):
    """Provider output for one bounded Private planner step.

    The model may describe semantic intent, but it cannot supply authority
    fields.  Redirect is a first-class decision so the HTTP/UI layers do not
    infer scope from generated prose.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["final", "tool_call", "confirmation", "redirect"]
    intent: Literal[
        "advice",
        "pair_summary",
        "shared_history",
        "history_search",
        "availability",
        "date_coordination",
        "out_of_scope",
        "unclear",
    ] = "advice"
    tool_name: str | None = None
    arguments: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_span: str = ""
    strategy: Literal["warm", "playful", "calm", "direct"] = "warm"
    reply: str = ""
    redirect_target: Literal["public_ayue"] | None = None

    @model_validator(mode="after")
    def validate_kind_contract(self) -> "PrivateAgentDecision":
        if self.kind == "final":
            if self.intent == "out_of_scope":
                raise ValueError("final cannot use out_of_scope intent")
            if self.tool_name or self.arguments or self.redirect_target:
                raise ValueError("final cannot carry tool or redirect fields")
        elif self.kind == "tool_call":
            if self.intent == "out_of_scope":
                raise ValueError("tool_call cannot use out_of_scope intent")
            if not self.tool_name or self.reply or self.redirect_target:
                raise ValueError("tool_call requires only a tool intent")
        elif self.kind == "confirmation":
            if self.intent == "out_of_scope":
                raise ValueError("confirmation cannot use out_of_scope intent")
            if not self.tool_name or self.arguments or self.reply or self.redirect_target:
                raise ValueError("confirmation requires one write tool and no arguments")
        elif self.kind == "redirect":
            if (
                self.intent != "out_of_scope"
                or self.redirect_target != "public_ayue"
                or self.tool_name
                or self.arguments
                or self.reply
            ):
                raise ValueError("redirect requires public_ayue and no tool payload")
        return self


class PrivateSurfaceHandoff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: Literal["public_ayue"] = "public_ayue"
    mode: Literal["prefill"] = "prefill"
    original_message: str = Field(min_length=1)
    auto_send: Literal[False] = False


class PrivateClientAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["navigate_public_prefill"]
    label: str
    value: str


class PrivateRelationshipMemoryEntry(BaseModel):
    """Model-authored presentation for the owner-scoped memory page.

    Navigation authority stays on the client: this object contains copy and
    placement only, never a user id, relationship id, or destination URL.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["relationship_memory_entry"] = "relationship_memory_entry"
    title: str = Field(min_length=1, max_length=40)
    summary: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=32)
    placement: Literal["before_reply", "after_reply"] = "after_reply"


class PrivateAgentResult(AgentResult):
    """Private-only metadata; shared AgentResult remains Public-compatible."""

    handoff: PrivateSurfaceHandoff | None = None
    relationship_memory_entry: PrivateRelationshipMemoryEntry | None = None
    # These fields are server-owned telemetry for the Private runtime.  They
    # are intentionally excluded from the HTTP payload so the client cannot
    # mistake a candidate or receipt for a completed write.
    relationship_memory_candidate: dict[str, object] | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    post_date_feedback_recorded: bool = Field(
        default=False,
        exclude=True,
        repr=False,
    )
