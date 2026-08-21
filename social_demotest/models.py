from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

class ChatType(str, Enum):
    big_five = "big_five"
    deep_profile = "deep_profile"
    direct = "direct"

class ChatRequest(BaseModel):
    user_id: str
    message: str
    state: str # "big_five" or "deep_profile"
    initial_interest: str = None
    initialize: bool = False

class MatchRequest(BaseModel):
    user_id: str
    source: str = "manual"
    force_new: bool = False
    confirmed: bool = False

class ProactiveEventRequest(BaseModel):
    user_id: str

class EventOpportunityScanRequest(BaseModel):
    max_proposals: int = Field(default=3, ge=1, le=10)

class EventDiscoveryRequest(BaseModel):
    region: str = "高雄"
    window_days: int = Field(default=30, ge=1, le=60)
    categories: list[str] | None = None

class ClearRequest(BaseModel):
    user_id: str

class AcceptRequest(BaseModel):
    user_id: str
    match_id: str
    explicit_reasons: list[str] = []

class MatchDecisionRequest(AcceptRequest):
    """A guarded, compare-and-set decision made from a visible match card."""
    action: str
    expected_status: str
    expected_revision: int | None = None
    proposal_namespace: str | None = None

class DirectChatRequest(BaseModel):
    user_id: str
    contact_id: str
    message: str
    chat_type: str = "direct"  # "direct", "deep_profile"
    mentioned_other_id: str | None = None
    mentioned_other_ids: list[str] | None = None
    mentions_inline: bool = False
    # A server-typed assessment action.  It is intentionally narrow so the
    # public chat adapter cannot turn arbitrary client values into commands.
    assessment_action: Literal["cancel"] | None = None

    @model_validator(mode="after")
    def _validate_assessment_action_scope(self):
        if self.assessment_action is not None and self.contact_id != "ai_assistant":
            raise ValueError("assessment_action is only available for ai_assistant")
        return self

class MediatorPrivateRequest(BaseModel):
    user_id: str
    other_id: str
    message: str

class RelationshipGameRequest(BaseModel):
    user_id: str
    other_id: str

class RelationshipQuizAnswerRequest(RelationshipGameRequest):
    answers: dict[str, str]

class SettingsRequest(BaseModel):
    user_id: str
    proactive_frequency: str = "3600"  # "none", "60", "3600", "86400"

class MediatorToneRequest(BaseModel):
    user_id: str
    mediator_tone: str = "friend"  # "friend", "gentle", "enthusiastic"
    probe_mode: str | None = None

class MediatorProbeRequest(BaseModel):
    user_id: str
    other_id: str
    force: bool = False
    kind: str | None = None

class ProfileMemoryActionRequest(BaseModel):
    user_id: str
    key: str
    action: str
    value: str | None = None

class ProfileLocationRequest(BaseModel):
    user_id: str
    city: str = Field(default="", max_length=20)
    district: str = Field(default="", max_length=20)

class ModelSettingsRequest(BaseModel):
    model: str | None = None
    thinking_level: str | None = None  # "off", "low", "medium", "high", "max"

class ResetRequest(BaseModel):
    user_id: str
    state: str  # "big_five" or "deep_profile"

class DateUpdateRequest(BaseModel):
    user_id: str
    other_id: str
    coordination_id: str
    revision: int
    form: dict

class DateConfirmRequest(BaseModel):
    user_id: str
    other_id: str
    coordination_id: str
    revision: int

class DateInviteResponseRequest(BaseModel):
    user_id: str
    other_id: str
    coordination_id: str
    accepted: bool

class CalendarEventCreateRequest(BaseModel):
    user_id: str
    title: str = Field(min_length=1, max_length=120)
    date: str
    start_time: str
    end_time: str
    timezone: str = "Asia/Taipei"
    location: str = ""
    notes: str = ""

class CalendarEventUpdateRequest(BaseModel):
    user_id: str
    title: str | None = Field(default=None, min_length=1, max_length=120)
    date: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    timezone: str | None = None
    location: str | None = None
    notes: str | None = None
    expected_revision: int | None = None

class CalendarRescheduleRequest(BaseModel):
    user_id: str
    date: str
    start_time: str
    end_time: str
    timezone: str = "Asia/Taipei"
    activity: str = ""
    location: str = ""
    budget: str = ""
    notes: str = ""

class CalendarActionRequest(BaseModel):
    user_id: str
    expected_revision: int | None = None

class CalendarSettingsRequest(BaseModel):
    user_id: str
    mediator_calendar_access: bool
