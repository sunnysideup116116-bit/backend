from enum import Enum
from pydantic import BaseModel, ConfigDict

class ChatType(str, Enum):
    big_five = "big_five"
    deep_profile = "deep_profile"
    direct = "direct"

class ChatRequest(BaseModel):
    user_id: str
    message: str
    state: str # "big_five" or "deep_profile"
    initial_interest: str = None

class MatchRequest(BaseModel):
    user_id: str
    source: str = "manual"
    force_new: bool = False
    confirmed: bool = False

class ClearRequest(BaseModel):
    user_id: str

class BigFiveProfileInitRequest(BaseModel):
    user_id: str
    initial_interest: str | None = None

class AcceptRequest(BaseModel):
    user_id: str
    match_id: str
    explicit_reasons: list[str] = []

class DirectChatRequest(BaseModel):
    user_id: str
    contact_id: str
    message: str
    chat_type: str = "direct"  # "direct", "deep_profile"
    mentioned_other_id: str | None = None
    mentioned_other_ids: list[str] | None = None
    message_timestamp: str | None = None  # ISO 8601，建議帶時區；用於風險時段判定

class GuidanceActivityRequest(BaseModel):
    user_id: str
    contact_id: str

class GuidanceSuggestionRequest(GuidanceActivityRequest):
    input_text: str = ""

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
    proactive_frequency: str = "normal"  # "low", "normal", "high"

class MediatorToneRequest(BaseModel):
    user_id: str
    mediator_tone: str = "friend"  # "friend", "gentle", "enthusiastic"

class ProfileMemoryActionRequest(BaseModel):
    user_id: str
    key: str
    action: str
    value: str | None = None

class ResetRequest(BaseModel):
    user_id: str
    state: str  # "big_five" or "deep_profile"


class DateForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date: str = ""
    time: str = ""
    activity: str = ""
    budget: str = ""


class DateUpdateRequest(BaseModel):
    user_id: str
    other_id: str
    form: DateForm
    form_revision: int | None = None


class DateConfirmRequest(BaseModel):
    user_id: str
    other_id: str
    form_revision: int | None = None


class RiskFeedbackRequest(BaseModel):
    triggered_by_msg_id: str
    role: str
    feedback: str
