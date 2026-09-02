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


class PushPreferenceRequest(BaseModel):
    user_id: str
    scope: Literal["global", "peer", "mediator_private", "public_ayue"]
    enabled: bool
    target_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_target_scope(self):
        if self.scope in {"peer", "mediator_private"} and not self.target_id:
            raise ValueError("target_id is required for peer notification settings")
        return self


class PushPresenceRequest(BaseModel):
    user_id: str
    session_id: str = Field(min_length=1, max_length=128)
    visible: bool = True
    surface: Literal["none", "pair", "mediator_private", "public_ayue"] = "none"
    conversation_id: str = Field(default="", max_length=256)
    other_user_id: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def _validate_visible_context(self):
        if self.visible and self.surface != "none" and not self.conversation_id:
            raise ValueError("conversation_id is required for a visible chat surface")
        return self


class PushReadRequest(BaseModel):
    user_id: str
    surface: Literal["pair", "mediator_private", "public_ayue"]
    conversation_id: str = Field(min_length=1, max_length=256)

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
    message: str = ""
    chat_type: str = "direct"  # "direct", "deep_profile"
    mentioned_other_id: str | None = None
    mentioned_other_ids: list[str] | None = None
    mentions_inline: bool = False
    # A server-typed assessment action.  It is intentionally narrow so the
    # public chat adapter cannot turn arbitrary client values into commands.
    assessment_action: Literal["cancel"] | None = None
    # Structured confirmation controls used only by the Public Ayue bubble UI.
    # They are mutually exclusive with a conversational message so a button
    # tap can never be reinterpreted as model-authored natural language.
    choice_id: str | None = Field(default=None, min_length=1, max_length=128)
    choice_action: Literal["confirm", "cancel"] | None = None
    client_message_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Appwrite storage file id for image messages. When present, the message
    # is treated as an image and the text risk gate is skipped.
    file_id: str | None = Field(default=None, min_length=1, max_length=128)
    # Optional AI room id for the multi-room AI chat surface. When provided
    # with contact_id == "ai_assistant", the message is routed to that room
    # (which must be owned by user_id) instead of the legacy single AI room.
    # Omit it to preserve the original single-room behavior.
    ai_room_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_assessment_action_scope(self):
        if self.assessment_action is not None and self.contact_id != "ai_assistant":
            raise ValueError("assessment_action is only available for ai_assistant")
        has_choice = self.choice_id is not None or self.choice_action is not None
        if has_choice:
            if self.contact_id != "ai_assistant":
                raise ValueError("choice actions are only available for ai_assistant")
            if self.choice_id is None or self.choice_action is None:
                raise ValueError("choice_id and choice_action must be provided together")
            if self.message.strip():
                raise ValueError("choice actions cannot include a conversational message")
            if self.assessment_action is not None:
                raise ValueError("choice actions cannot include assessment_action")
            if self.mentioned_other_id or self.mentioned_other_ids:
                raise ValueError("choice actions cannot include mentions")
        elif not self.message.strip() and self.file_id is None and self.assessment_action is None:
            raise ValueError("message is required when no typed action is provided")
        return self

class MediatorPrivateRequest(BaseModel):
    user_id: str
    other_id: str
    message: str = ""
    choice_id: str | None = Field(default=None, min_length=1, max_length=128)
    choice_action: Literal["confirm", "cancel"] | None = None

    @model_validator(mode="after")
    def _validate_choice_shape(self):
        has_choice = self.choice_id is not None or self.choice_action is not None
        if has_choice:
            if self.choice_id is None or self.choice_action is None:
                raise ValueError("choice_id and choice_action must be provided together")
            if self.message.strip():
                raise ValueError("choice actions cannot include a conversational message")
        elif not self.message.strip():
            raise ValueError("message is required when no choice action is provided")
        return self

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
    end_date: str | None = None
    all_day: bool = False
    start_time: str | None = None
    end_time: str | None = None
    timezone: str = "Asia/Taipei"
    location: str = ""
    notes: str = ""

    @model_validator(mode="after")
    def validate_interval_shape(self):
        if self.all_day:
            if self.start_time is not None or self.end_time is not None:
                raise ValueError("all-day event cannot include start_time or end_time")
        elif not self.start_time or not self.end_time:
            raise ValueError("timed event requires start_time and end_time")
        return self

class CalendarEventUpdateRequest(BaseModel):
    user_id: str
    title: str | None = Field(default=None, min_length=1, max_length=120)
    date: str | None = None
    end_date: str | None = None
    all_day: bool | None = None
    start_time: str | None = None
    end_time: str | None = None
    timezone: str | None = None
    location: str | None = None
    notes: str | None = None
    expected_revision: int | None = None

    @model_validator(mode="after")
    def validate_interval_shape(self):
        if self.all_day is True and (
            self.start_time is not None or self.end_time is not None
        ):
            raise ValueError("all-day event cannot include start_time or end_time")
        return self

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
