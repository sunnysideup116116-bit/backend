"""Typed, bounded contracts for public-Ayue conversation continuity.

The summary is a projection only.  It is never a source of truth for profile,
relationship, calendar, or durable memory state.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SUMMARY_FIELDS = (
    "active_topics", "owner_goals", "known_continuity", "unresolved_questions",
    "ayue_commitments", "recent_decisions",
)
PRIVATE_SUMMARY_FIELDS = (
    "active_topics", "owner_goals", "known_continuity", "unresolved_questions",
)
SUMMARY_ITEM_LIMITS = {
    "active_topics": 5, "owner_goals": 5, "known_continuity": 6,
    "unresolved_questions": 5, "ayue_commitments": 5, "recent_decisions": 5,
}
MAX_SUMMARY_ITEM_CHARS = 120
MAX_SUMMARY_CHARS = 3000
COMPACTION_VERSION = "conversation-compaction-v2"
POLICY_VERSION = "conversation_compaction_policy_v4"


def _clean_items(value: Any, field: str) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in value[: SUMMARY_ITEM_LIMITS[field]]:
        text = " ".join(str(raw or "").split()).strip()[:MAX_SUMMARY_ITEM_CHARS]
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


class ConversationSummaryV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    active_topics: list[str] = Field(default_factory=list)
    owner_goals: list[str] = Field(default_factory=list)
    known_continuity: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    ayue_commitments: list[str] = Field(default_factory=list)
    recent_decisions: list[str] = Field(default_factory=list)

    @field_validator(*SUMMARY_FIELDS, mode="before")
    @classmethod
    def normalize_items(cls, value: Any, info):
        return _clean_items(value, info.field_name)

    @model_validator(mode="after")
    def enforce_total_budget(self):
        remaining = MAX_SUMMARY_CHARS
        for name in SUMMARY_FIELDS:
            kept: list[str] = []
            for item in getattr(self, name):
                if len(item) > remaining:
                    break
                kept.append(item)
                remaining -= len(item)
            setattr(self, name, kept)
        return self

    def as_private_projection(self) -> dict[str, list[str]]:
        return {name: list(getattr(self, name)) for name in PRIVATE_SUMMARY_FIELDS}

    def char_count(self) -> int:
        return sum(len(item) for name in SUMMARY_FIELDS for item in getattr(self, name))


class ConversationEvaluationV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "unavailable"
    confidence: float = 0.0
    issues: list[str] = Field(default_factory=list, max_length=12)
    retention: dict[str, bool] = Field(default_factory=dict)


class ConversationCompactionRecordV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = COMPACTION_VERSION
    mode: str = "shadow"
    owner_user_id: str
    room_id: str
    revision: int = 0
    covered_message_count: int = 0
    covered_through_message_id: str = ""
    covered_through_timestamp: float = 0.0
    source_hashes: list[str] = Field(default_factory=list, max_length=24)
    summary: ConversationSummaryV1
    evaluation: ConversationEvaluationV1 = Field(default_factory=ConversationEvaluationV1)
    observability: dict[str, Any] = Field(default_factory=dict)
    created_at: float
    updated_at: float


# Compatibility alias for callers that imported the first implementation
# before rolling summaries were introduced. Persisted records are separated by
# COMPACTION_VERSION, so this does not make V1 documents look like V2 data.
ConversationCompactionRecordV1 = ConversationCompactionRecordV2


def summary_from_payload(payload: Any) -> ConversationSummaryV1:
    if not isinstance(payload, dict):
        return ConversationSummaryV1()
    return ConversationSummaryV1(**{name: payload.get(name, []) for name in SUMMARY_FIELDS})
