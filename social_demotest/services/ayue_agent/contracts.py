"""Typed contracts for the bounded public-Ayue agent runtime."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    PROFILE = "profile"
    ASSESSMENT = "assessment"
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
    # Executor-only metadata.  Scheduler may consume this for a server-owned
    # reference, but it must never be copied into an observation/prompt.
    private_data: dict[str, Any] = Field(default_factory=dict, repr=False, exclude=True)


class AgentTurnContext(BaseModel):
    user_id: str
    room_id: str
    message: str
    # Server-owned control plane input.  Public V3 handles this before the
    # planner; it is never copied into prompt-safe context or trace payloads.
    assessment_action: Literal["cancel"] | None = None
    message_id: str | None = None
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


class PublicAgentTurnContext(BaseModel):
    """Prompt-safe Public V3 state, assembled once for each turn."""
    version: str = "public-v1"
    user_id: str
    room_id: str
    message: str
    recent_messages: list[dict[str, str]] = Field(default_factory=list)
    recent_context: str = ""
    user_location: str = ""
    relevant_memories: list[str] = Field(default_factory=list)
    active_proposal: dict[str, Any] | None = None
    latest_match_outcome: dict[str, Any] | None = None
    calendar_draft: dict[str, Any] | None = None
    calendar_recent_reference: dict[str, Any] | None = None
    calendar_recent_mutation: dict[str, Any] | None = None
    recent_context_draft: dict[str, Any] | None = None
    # Public references only. Their executor-side IDs remain on AgentTurnContext.
    mentioned_contacts: list[dict[str, str]] = Field(default_factory=list)
    mentioned_contact_overflow: bool = False
    capability_manifest_version: str = "v2"
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
    place_search_followup: Literal["none", "recommend"] = "none"


class PresentationBlock(BaseModel):
    """Bounded server/UI projection for Markdown fragments and place cards."""
    model_config = ConfigDict(extra="forbid")

    message_index: int = Field(ge=0, le=2)
    # Empty markdown is allowed only when a card is bound. Older persisted
    # blocks may still carry the deprecated card_description field.
    markdown: str = Field(default="", max_length=1400)
    place_card_indices: list[int] = Field(default_factory=list, max_length=1)
    # Deprecated compatibility input. New responses must keep explanations in
    # markdown and leave this field unset.
    card_description: str | None = Field(default=None, min_length=1, max_length=180)

    @model_validator(mode="after")
    def validate_projection(self):
        if self.card_description and not self.place_card_indices:
            raise ValueError("card_description requires one place_card_indices entry")
        if not self.markdown and not self.place_card_indices:
            raise ValueError("presentation block needs markdown or a place card")
        return self


class AgentResult(BaseModel):
    handled: bool
    reply: str | None = None
    messages: list[str] = Field(default_factory=list, max_length=3)
    presentation_class: Literal[
        "conversation", "social_opportunity", "product_info", "transaction",
        "capability", "fallback", "onboarding", "grounded_recommendation",
    ] = "conversation"
    # True only when a confirmed Calendar write committed in this turn.  The
    # public UI uses this as a cache-invalidation hint; it is not domain state.
    calendar_state_changed: bool = False
    conversation_intent: str = "casual_chat"
    mentioned_other_ids: list[str] = Field(default_factory=list)
    context_changed: bool = False
    context_confirmation_needed: bool = False
    agent_run_id: str | None = None
    agent_mode: str = "unknown"
    fallback_reason: str | None = None
    match_readiness_state: str | None = None
    match_guidance_shown: bool = False
    # Compatibility metadata. The Public HTTP layer excludes assessment turns
    # through profile_write_reason; ordinary saved owner messages still reach
    # the isolated extractor.
    profile_write_allowed: bool = True
    assessment_state: str | None = None
    assessment_kind: str | None = None
    assessment_revision: int | None = None
    profile_write_reason: str = "casual"
    sources: list[dict[str, str]] = Field(default_factory=list)
    place_cards: list[dict[str, str]] = Field(default_factory=list)
    presentation_blocks: list[PresentationBlock] = Field(default_factory=list, max_length=12)
    llm_call_metrics: list[dict[str, Any]] = Field(default_factory=list)

