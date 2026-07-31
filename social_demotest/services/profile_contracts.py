"""Provider-neutral contracts for owner-scoped profile extraction.

These models describe what a provider may *propose*.  They deliberately do
not contain database ids, prior conversation, match state, or assistant text.
The profile service validates every proposal against the saved owner message
before it can be persisted.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


RecentContextAction = Literal["update", "clear", "none"]
FieldOperation = Literal["set", "clear"]


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


class ProfileExtractionDecision(BaseModel):
    """The only provider-neutral extraction payload accepted by V2."""

    model_config = ConfigDict(extra="forbid")

    recent_context: RecentContextDecision
    memories: list[DurableMemoryCandidate] = Field(default_factory=list)
