"""Provider-neutral contracts for owner-scoped profile extraction.

These models describe what a provider may *propose*.  They deliberately do
not contain database ids, prior conversation, match state, or assistant text.
The profile service validates every proposal against the saved owner message
before it can be persisted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from matchmaker_agent.concept_identity import HARD_DURABLE_MEMORY_LIMIT


RecentContextAction = Literal["update", "clear", "none"]
RecentContextEpisodeRelation = Literal["continue", "new", "unrelated"]
FieldOperation = Literal["set", "clear"]
FollowUpAction = Literal["create", "update", "close", "none"]
FollowUpTiming = Literal["after_days", "after_activity", "none"]


class EvidenceField(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: FieldOperation
    value: str | None
    evidence_span: str
    confidence: float = Field(ge=0.0, le=1.0)
    subject: Literal["owner"]


class RecentContextDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: RecentContextAction
    confidence: float = Field(ge=0.0, le=1.0)
    message_kind: Literal[
        "real_world_update", "match_operation", "durable_preference", "other",
    ]
    fields: dict[str, EvidenceField] = Field(default_factory=dict)
    # The provider may relate the current owner message to a bounded typed
    # episode.  It never receives raw conversation history or prior evidence.
    # ``new`` is the backwards-compatible, fail-safe default for older
    # providers and fixtures.
    episode_relation: RecentContextEpisodeRelation = "new"
    reason_code: str = ""


class DurableMemoryCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    label_zh_tw: str
    stance: Literal["like", "dislike", "require", "avoid"]
    category: Literal["lifestyle", "habit", "personality", "relationship", "activity"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_span: str
    subject: Literal["owner"]
    reason_code: str = ""


class FollowUpDecision(BaseModel):
    """A bounded proposal for a later, owner-scoped conversational check-in."""

    model_config = ConfigDict(extra="forbid")

    action: FollowUpAction = "none"
    existing_slot: int | None = Field(default=None, ge=1, le=3)
    topic: str = Field(default="", max_length=80)
    question_goal: str = Field(default="", max_length=160)
    timing: FollowUpTiming = "none"
    wait_days: int = Field(default=3, ge=1, le=7)
    evidence_span: str = Field(default="", max_length=240)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    subject: Literal["owner"] = "owner"
    reason_code: str = Field(default="", max_length=60)


class ProfileExtractionDecision(BaseModel):
    """The only provider-neutral extraction payload accepted by V2."""

    model_config = ConfigDict(extra="forbid")

    recent_context: RecentContextDecision
    memories: list[DurableMemoryCandidate] = Field(
        default_factory=list, max_length=HARD_DURABLE_MEMORY_LIMIT,
    )
    follow_up: FollowUpDecision | None = None
