"""Typed contracts for the bounded public-Ayue agent runtime."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class DecisionKind(str, Enum):
    TOOL_CALL = "tool_call"
    FINAL = "final"
    CONFIRMATION = "confirmation"


class AgentIntent(str, Enum):
    CHAT = "chat"
    MATCH_STATUS = "match_status"
    MATCH_ACTION = "match_action"
    CALENDAR = "calendar"
    CALENDAR_ACTION = "calendar_action"
    RELATIONSHIP = "relationship"
    MEMORY = "memory"
    TIME = "time"
    WEB = "web"
    PLACES = "places"
    UNCLEAR = "unclear"


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    ok: bool
    data: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    user_message: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class AgentTurnContext(BaseModel):
    user_id: str
    room_id: str
    message: str
    mentioned_ids: list[str] = Field(default_factory=list)
    mention_overflow: bool = False
    user_profile: dict[str, Any] = Field(default_factory=dict)
    recent_history: list[dict[str, Any]] = Field(default_factory=list)


class TurnClockV1(BaseModel):
    """Authoritative local clock, created once for a public-agent turn."""
    version: str = "v1"
    timezone: str
    utc_iso: str
    local_iso: str
    local_date: str
    local_time: str
    weekday_zh_tw: str
    temporal_references: dict[str, str] = Field(default_factory=dict)


class AgentTurnContextV2(BaseModel):
    """Prompt-safe public-agent state, assembled once for each turn."""
    version: str = "v2"
    user_id: str
    room_id: str
    message: str
    recent_messages: list[dict[str, str]] = Field(default_factory=list)
    recent_context: str = ""
    user_location: str = ""
    relevant_memories: list[str] = Field(default_factory=list)
    active_proposal: dict[str, Any] | None = None
    latest_match_outcome: dict[str, Any] | None = None
    pending_confirmation: dict[str, Any] | None = None
    action_draft: dict[str, Any] | None = None
    recent_context_draft: dict[str, Any] | None = None
    # Public references only. Their executor-side IDs remain on AgentTurnContext.
    mentioned_contacts: list[dict[str, str]] = Field(default_factory=list)
    mentioned_contact_overflow: bool = False
    capability_manifest_version: str = "v1"
    match_opportunity_state: str = "not_ready"
    guidance_directive: str = "none"
    clock: TurnClockV1 = Field(default_factory=lambda: TurnClockV1(
        timezone="Asia/Taipei", utc_iso="1970-01-01T00:00:00+00:00",
        local_iso="1970-01-01T08:00:00+08:00", local_date="1970-01-01",
        local_time="08:00", weekday_zh_tw="星期四",
    ))


class AgentDecision(BaseModel):
    """Provider-neutral planner contract. IDs and revisions are never model inputs."""
    model_config = ConfigDict(extra="forbid")

    kind: DecisionKind
    intent: AgentIntent = AgentIntent.CHAT
    tool_name: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    evidence_span: str | None = None
    reply: str | None = None
    opportunity_signal: Literal["none", "social_opening"] = "none"
    opportunity_confidence: float = 0.0
    opportunity_evidence_span: str | None = None
    clarification_goal: str | None = None
    missing_fields: list[str] = Field(default_factory=list)
    recent_context_followup: Literal["none", "ask_activity"] = "none"


class AgentResult(BaseModel):
    handled: bool
    reply: str | None = None
    conversation_intent: str = "casual_chat"
    mentioned_other_ids: list[str] = Field(default_factory=list)
    context_changed: bool = False
    context_confirmation_needed: bool = False
    agent_run_id: str | None = None
    agent_mode: str = "legacy"
    fallback_reason: str | None = None
    match_readiness_state: str | None = None
    match_guidance_shown: bool = False
    # Legacy/private compatibility metadata. Public V2 always lets the isolated
    # profile extractor inspect the saved owner message and ignores this gate.
    profile_write_allowed: bool = True
    profile_write_reason: str = "casual"
    sources: list[dict[str, str]] = Field(default_factory=list)

