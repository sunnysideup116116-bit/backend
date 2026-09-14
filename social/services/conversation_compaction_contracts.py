"""Typed, bounded contracts for owner-scoped Public Ayue conversation compaction."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.profile_projection import contains_internal_identifier, contains_protected_content


SUMMARY_FIELDS = (
    "active_topics", "owner_goals", "known_continuity", "unresolved_questions",
    "ayue_commitments", "recent_decisions",
)
SUMMARY_ITEM_LIMITS = {
    "active_topics": 5,
    "owner_goals": 5,
    "known_continuity": 6,
    "unresolved_questions": 5,
    "ayue_commitments": 5,
    "recent_decisions": 5,
}
CompactionIssueCode = Literal[
    "omitted_active_topics", "omitted_owner_goals", "omitted_known_continuity",
    "omitted_unresolved_questions", "omitted_ayue_commitments",
    "omitted_recent_decisions", "unsupported_content", "role_confusion",
    "canonical_state_leak", "low_confidence", "evaluation_unavailable",
    "generation_unavailable",
]


class ConversationSummaryV1(BaseModel):
    """Provider output used only for conversational continuity, never as profile evidence."""

    model_config = ConfigDict(extra="forbid")

    active_topics: list[str] = Field(default_factory=list)
    owner_goals: list[str] = Field(default_factory=list)
    known_continuity: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    ayue_commitments: list[str] = Field(default_factory=list)
    recent_decisions: list[str] = Field(default_factory=list)

    @field_validator(*SUMMARY_FIELDS, mode="before")
    @classmethod
    def _bounded_safe_items(cls, value, info):
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("summary field must be a list")
        clean: list[str] = []
        for item in value[:SUMMARY_ITEM_LIMITS[info.field_name]]:
            text = " ".join(str(item or "").split())[:120]
            if (
                not text
                or contains_internal_identifier(text)
                or contains_protected_content(text)
            ):
                continue
            if text not in clean:
                clean.append(text)
        return clean


class ContinuityRetentionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_topics: bool
    owner_goals: bool
    known_continuity: bool
    unresolved_questions: bool
    ayue_commitments: bool
    recent_decisions: bool


class ConversationCompactionEvaluationDecisionV1(BaseModel):
    """Provider judgment with no free-text explanation or copied evidence."""

    model_config = ConfigDict(extra="forbid")

    retention: ContinuityRetentionV1
    unsupported_content: bool
    role_confusion: bool
    canonical_state_leak: bool
    confidence: float = Field(ge=0.0, le=1.0)


class ConversationCompactionEvaluationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["conversation-compaction-evaluation-v1"] = "conversation-compaction-evaluation-v1"
    status: Literal["pass", "review", "unavailable"]
    retention: ContinuityRetentionV1 | None = None
    unsupported_content: bool | None = None
    role_confusion: bool | None = None
    canonical_state_leak: bool | None = None
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    issue_codes: list[CompactionIssueCode] = Field(default_factory=list, max_length=11)


class ConversationCompactionObservabilityV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: Literal["conversation-compaction-observability-v1"] = "conversation-compaction-observability-v1"
    policy_version: str = Field(default="legacy", pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    input_message_count: int = Field(ge=1, le=16)
    input_char_count: int = Field(ge=0, le=9000)
    summary_item_count: int = Field(ge=0, le=31)
    summary_char_count: int = Field(ge=0, le=3720)
    generation_latency_ms: int = Field(ge=0, le=300000)
    evaluation_latency_ms: int = Field(ge=0, le=300000)
    generation_attempt_count: int = Field(default=1, ge=0, le=2)
    evaluation_attempt_count: int = Field(default=1, ge=0, le=2)
    generation_result_code: str = Field(default="legacy_unknown", pattern=r"^[a-z][a-z0-9_]{0,39}$")
    evaluation_result_code: str = Field(default="legacy_unknown", pattern=r"^[a-z][a-z0-9_]{0,39}$")
    profile_coverage_status: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    profile_requeued_count: int = Field(ge=0, le=64)


class ConversationCompactionV1(BaseModel):
    """Server-owned persisted shadow record; never expose its internal bindings."""

    model_config = ConfigDict(extra="forbid")

    version: Literal["conversation-compaction-v1"] = "conversation-compaction-v1"
    mode: Literal["shadow"] = "shadow"
    owner_user_id: str
    room_id: str
    revision: int = Field(ge=1)
    covered_message_count: int = Field(ge=1)
    covered_through_message_id: str
    covered_through_timestamp: float = Field(ge=0)
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_source_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    summary: ConversationSummaryV1
    evaluation: ConversationCompactionEvaluationV1
    observability: ConversationCompactionObservabilityV1
    created_at: float = Field(ge=0)
    updated_at: float = Field(ge=0)
