"""Single source of truth for public-Ayue tools and their safety contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from services.ayue_agent.v3.calendar_commands import CalendarCommandBatch


class ToolRisk(str, Enum):
    READ = "read"
    WRITE = "write"


class ToolArgumentSource(str, Enum):
    """Where executor arguments originate; never from an untrusted planner."""

    NONE = "none"
    MENTIONED_RELATIONSHIP = "mentioned_relationship"
    MENTIONED_CONTACTS = "mentioned_contacts"
    PLANNER_GROUNDED = "planner_grounded"


PlaceEnrichment = Literal["rating", "hours", "walking"]
PlaceDetailsEnrichment = Literal["rating", "hours"]


class _NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _RelationshipEvidenceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    other_id: str | None = None


class _MentionedContactSummaryArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    other_ids: list[str] = Field(min_length=1, max_length=3)


class _ProposalDecisionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["interested", "declined"]


class _AssessmentStartArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["basic", "deep"]


class _CalendarFindArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_hint: str = Field(min_length=1, max_length=120)
    date_hint: str | None = Field(default=None, max_length=32)
    companion_hint: str | None = Field(default=None, max_length=30)
    limit: int | None = Field(default=None, ge=1, le=30)


class _CalendarListArguments(BaseModel):
    """Date range filter for listing the owner's calendar.

    Primary path: `start_date` / `end_date` (ISO YYYY-MM-DD, both optional).
    The sub-agent resolves relative terms (這個月/本週/上週…) against the
    turn clock and fills explicit dates. When both are omitted the range
    defaults to the next 90 days.

    Legacy fallback: `date` (single day) and `range_label`
    (今天/明天/後天/本週/下週/本月/下個月) are still accepted and resolved
    server-side through the turn clock.
    """

    model_config = ConfigDict(extra="forbid")
    start_date: str | None = Field(default=None, min_length=10, max_length=10)
    end_date: str | None = Field(default=None, min_length=10, max_length=10)
    date: str | None = Field(default=None, min_length=10, max_length=10)
    range_label: str | None = Field(default=None, max_length=32)


class _WebSearchArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=300)
    recency: Literal["none", "day", "week", "month", "year"] = "none"
    use_saved_location: bool = False


class _WebExtractArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[str] = Field(min_length=1, max_length=2)
    query: str = Field(default="", max_length=300)


class _PlacesNearbyArguments(BaseModel):
    """Human place names only; coordinates and arbitrary map queries are never planner input.

    cuisine is a bounded free-text food-type hint (e.g. 火鍋、日式、素食). It is
    only ever folded into the Google Places textQuery JSON body; it never
    reaches the Overpass QL builder, which stays on the allowlisted categories.
    """

    model_config = ConfigDict(extra="forbid")
    anchor: str = Field(default="", max_length=160)
    categories: list[Literal["restaurant", "cafe", "bar", "attraction", "park"]] = Field(min_length=1, max_length=3)
    cuisine: str = Field(default="", max_length=30)
    radius_m: int = Field(default=1500, ge=300, le=5000)
    limit: int = Field(default=3, ge=1, le=8)
    ordering: Literal["distance", "balanced"] = "distance"
    use_saved_location: bool = False
    enrichments: list[PlaceEnrichment] = Field(default_factory=list, max_length=3)

    @field_validator("enrichments")
    @classmethod
    def _deduplicate_enrichments(cls, value: list[PlaceEnrichment]) -> list[PlaceEnrichment]:
        return list(dict.fromkeys(value))


class _PlacesDistanceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(default="", max_length=160)
    destination: str = Field(min_length=2, max_length=160)
    use_saved_origin: bool = False
    travel_mode: Literal["DRIVE", "WALK"] = "DRIVE"


class _PlacesResolveArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=2, max_length=160)
    enrichments: list[PlaceDetailsEnrichment] = Field(default_factory=list, max_length=2)

    @field_validator("enrichments")
    @classmethod
    def _deduplicate_enrichments(cls, value: list[PlaceDetailsEnrichment]) -> list[PlaceDetailsEnrichment]:
        return list(dict.fromkeys(value))


class _CalendarEventOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str
    start_time: str
    end_time: str
    activity: str
    status: str
    location: str = ""
    notes: str = ""
    event_kind: Literal["personal", "shared_date", ""] = ""


class _CalendarOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[_CalendarEventOutput]
    range: str


class _CalendarNextOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["found", "not_found"]
    event: _CalendarEventOutput | None = None


class _CalendarEventCandidateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    activity: str
    date: str
    start_time: str
    end_time: str
    location: str = ""
    notes: str = ""
    event_kind: Literal["personal", "shared_date", ""] = ""


class _CalendarFindOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["found", "not_found", "ambiguous"]
    reason_code: Literal[
        "", "event_not_found", "event_ambiguous", "companion_not_found", "companion_ambiguous",
    ] = ""
    activity: str = ""
    date: str = ""
    start_time: str = ""
    end_time: str = ""
    location: str = ""
    notes: str = ""
    event_kind: Literal["personal", "shared_date", ""] = ""
    companion_known: bool = False
    companion_display_name: str = "對方"
    companion_safe_summary: str = ""
    query: str = ""
    candidates: list[_CalendarEventCandidateOutput] = Field(default_factory=list, max_length=10)


class _CalendarMutationVerificationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal[
        "verified_success", "still_active", "failed", "partial",
        "verification_failed", "not_available",
    ]
    action: str = ""
    label: str = ""
    outcome: str = ""


class _CalendarMutationVerificationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calendar_mutation_verification: _CalendarMutationVerificationStatus


class _MatchStatusOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: str
    scope: str | None = None
    is_terminal: bool | None = None
    chat_opened: bool
    counterparty: str = "對方"
    revision: int | None = None
    updated_at: Any = None
    reason_code: str | None = None


class _CounterpartySummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    found: bool
    match_state: str | None = None
    display_name: str = "對方"
    safe_summary: str = ""
    recent_context: str = ""
    initial_interest: str = ""
    personality_summary: str = ""
    distinctive_tags: list[str] = Field(default_factory=list)
    verified_common_ground: list[str] = Field(default_factory=list)
    recommendation_tier: Literal["grounded", "exploratory", ""] = ""
    chat_opened: bool = False


class _RecentContextOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    current_context: str = ""
    revision: int = 0
    exists: bool


class _SelfProfileOutput(BaseModel):
    """Owner-only, typed profile projection for an on-demand self summary."""

    model_config = ConfigDict(extra="forbid")
    display_name: str = ""
    initial_interest: str = ""
    personality_summary: str = ""
    openness: float | None = None
    conscientiousness: float | None = None
    extraversion: float | None = None
    agreeableness: float | None = None
    neuroticism: float | None = None
    values: list[str] = Field(default_factory=list)
    life_goals: list[str] = Field(default_factory=list)
    relationship_needs: list[str] = Field(default_factory=list)
    stress_coping: str = ""
    ideal_future: str = ""
    deep_profile_summary: str = ""
    recent_context: str = ""
    location: str = ""
    preferences: list[str] = Field(default_factory=list, max_length=8)
    missing_sections: list[str] = Field(default_factory=list)


class _RelationshipOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relationships: list[dict[str, Any]]


class _MentionedContactOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str = "對方"
    recent_context: str = ""
    initial_interest: str = ""
    personality_summary: str = ""
    safe_match_reason: str = ""
    verified_common_ground: list[str] = Field(default_factory=list)
    distinctive_tags: list[str] = Field(default_factory=list)


class _MentionedContactSummaryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contacts: list[_MentionedContactOutput] = Field(default_factory=list)


class _AcceptedContactListOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contacts: list[_MentionedContactOutput] = Field(default_factory=list, max_length=8)
    truncated: bool = False
    total_count: int | None = Field(default=None, ge=0)


class _MemoryOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = ""
    current_context: str = ""
    preferences: list[Any] = Field(default_factory=list)


class _ClockOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["v1"] = "v1"
    timezone: str
    utc_iso: str
    local_iso: str
    local_date: str
    local_time: str
    weekday_zh_tw: str
    temporal_references: dict[str, str] = Field(default_factory=dict)


class _WebSearchResultOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str
    url: str
    snippet: str = ""
    published_date: str = ""


class _WebSearchOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    results: list[_WebSearchResultOutput] = Field(default_factory=list)


class _WebExtractPageOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str
    content: str = ""
    truncated: bool = False


class _WebExtractOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pages: list[_WebExtractPageOutput] = Field(default_factory=list)


class _PlaceOpeningHoursOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    open_now: bool | None = None
    next_open_time: str | None = None
    next_close_time: str | None = None
    weekday_descriptions: list[str] = Field(default_factory=list, max_length=7)


class _PlaceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    category: Literal["restaurant", "cafe", "bar", "attraction", "park"]
    distance_m: int = Field(ge=0)
    address_summary: str = ""
    map_url: str
    provider: Literal["openstreetmap", "google"] = "openstreetmap"
    place_id: str = ""
    # Optional photo from the Text Search response (places.photos is a Pro-tier
    # field; the media bytes bill under Place Details Photos).
    photo_url: str = ""
    rating: float | None = Field(default=None, ge=0, le=5)
    user_rating_count: int | None = Field(default=None, ge=0)
    opening_hours: _PlaceOpeningHoursOutput | None = None
    walking_distance_m: int | None = Field(default=None, ge=0)
    walking_duration_seconds: int | None = Field(default=None, ge=0)

class _PlacesNearbyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor_label: str
    origin_kind: Literal["explicit", "saved_profile"]
    distance_basis: Literal["straight_line"]
    attribution: str
    attribution_url: str
    requested_categories: list[str] = Field(default_factory=list, max_length=3)
    requested_cuisine: str = ""
    radius_m: int = Field(default=1500, ge=300, le=5000)
    requested_limit: int = Field(default=3, ge=1, le=8)
    ordering: Literal["distance", "balanced"] = "distance"
    places: list[_PlaceOutput] = Field(default_factory=list)


class _PlacesResolveOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    found: bool
    place: _PlaceOutput | None = None
    attribution: str = "Google Maps"
    attribution_url: str = "https://www.google.com/maps"


class _PlacesDistanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_label: str
    destination_label: str
    origin_kind: Literal["explicit", "saved_profile"]
    distance_m: int = Field(ge=0)
    distance_basis: Literal["straight_line", "driving", "walking"]
    duration_text: str = ""
    duration_seconds: int | None = Field(default=None, ge=0)
    travel_mode: Literal["DRIVE", "WALK"] = "DRIVE"
    attribution: str
    attribution_url: str


@dataclass(frozen=True)
class ToolSpec:
    name: str
    risk: ToolRisk
    executor_key: str
    description: str
    progress_text: str
    requires_confirmation: bool = False
    planner_arguments_model: type[BaseModel] = _NoArguments
    executor_arguments_model: type[BaseModel] = _NoArguments
    output_model: type[BaseModel] = _NoArguments
    argument_source: ToolArgumentSource = ToolArgumentSource.NONE
    # Some reads answer one bounded fact for the whole turn.  If the planner
    # asks for that fact again with paraphrased arguments, Runtime reuses the
    # verified observation instead of spending a second read.
    reuse_success_within_turn: bool = False


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "system.get_current_time": ToolSpec(
        "system.get_current_time", ToolRisk.READ, "current_time",
        "讀取本回合的目前台北時間與訊息中相對日期的實際日期。",
        "我確認一下現在的時間…",
        output_model=_ClockOutput,
    ),
    "calendar.list_my_events": ToolSpec(
        "calendar.list_my_events", ToolRisk.READ, "calendar_events",
        "讀取本人的行事曆與忙碌時段，不讀取對方行事曆。建議用 start_date 與 end_date（YYYY-MM-DD）指定查詢區間，可包含今天之前與之後；不指定時預設未來 90 天。",
        "我看一下你的行事曆…",
        planner_arguments_model=_CalendarListArguments,
        executor_arguments_model=_CalendarListArguments,
        output_model=_CalendarOutput,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "calendar.get_next_my_event": ToolSpec(
        "calendar.get_next_my_event", ToolRisk.READ, "calendar_next_event",
        "讀取本人接下來 90 天內最近的一筆有效行程；適用於『最近一筆』『下一個行程』『最近有什麼行程』，會回傳唯一可供後續這筆／它／他／她指涉的行程。",
        "我看一下你最近的一筆行程…",
        output_model=_CalendarNextOutput,
    ),
    "calendar.verify_recent_mutation": ToolSpec(
        "calendar.verify_recent_mutation", ToolRisk.READ, "calendar_mutation_verification",
        "確認最近一次行事曆變更是否已套用；只適用於使用者追問剛才的新增、修改或取消結果，不會再次執行寫入。",
        "驗證最近一次行事曆變更是否成功。",
        output_model=_CalendarMutationVerificationOutput,
    ),
    "calendar.find_my_event": ToolSpec(
        "calendar.find_my_event", ToolRisk.READ, "calendar_event_find",
        "查詢本人一筆指定行程及其是否為已確認共同約會；適用於問某個行程的日期、內容或跟誰去。可用已接受聯絡人的公開名稱縮小共同約會，但只會從該筆行程回答，不能用目前配對對象猜測。",
        "我確認一下這筆行程…",
        planner_arguments_model=_CalendarFindArguments,
        executor_arguments_model=_CalendarFindArguments,
        output_model=_CalendarFindOutput,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "match.get_status": ToolSpec(
        "match.get_status", ToolRisk.READ, "match_status",
        "讀取本人唯一正式配對狀態；適用於配對成功、接受、回覆與目前進度。",
        "我看一下目前的配對進度…",
        output_model=_MatchStatusOutput,
    ),
    "match.get_counterparty_summary": ToolSpec(
        "match.get_counterparty_summary", ToolRisk.READ, "counterparty_summary",
        "讀取唯一目前有效或已接受配對的公開對象摘要；適用於問對方是誰、共同點或聊天室是否已開啟。",
        "我確認一下這位對象的公開資訊…",
        output_model=_CounterpartySummaryOutput,
    ),
    "profile.get_recent_context": ToolSpec(
        "profile.get_recent_context", ToolRisk.READ, "recent_context",
        "讀取本人已儲存的近期情境；適用於問我是否記得近期計畫，不會寫入或重新分析資料。",
        "我確認一下你最近提過的計畫…",
        output_model=_RecentContextOutput,
    ),
    "profile.get_self_summary": ToolSpec(
        "profile.get_self_summary", ToolRisk.READ, "self_profile",
        "讀取本人的已完成基礎資料、深度資料、偏好、近期情境與粗略地區；適用於問『我是誰』、『你了解我多少』或自己的個性與興趣。只讀取本人資料，不會寫入。",
        "我整理一下我目前認識的你…",
        output_model=_SelfProfileOutput,
    ),
    "relationship.get_verified_evidence": ToolSpec(
        "relationship.get_verified_evidence", ToolRisk.READ, "relationship_evidence",
        "讀取已接受配對的可驗證互動摘要。",
        "我確認一下這段互動的資訊…",
        executor_arguments_model=_RelationshipEvidenceArguments,
        output_model=_RelationshipOutput,
        argument_source=ToolArgumentSource.MENTIONED_RELATIONSHIP,
    ),
    "relationship.get_mentioned_contact_summary": ToolSpec(
        "relationship.get_mentioned_contact_summary", ToolRisk.READ, "mentioned_contact_summary",
        "讀取本回合 @ 的已接受聯絡人公開摘要；適用於詢問對方近況、特質或比較對象。",
        "我看一下這位對象的公開資訊…",
        executor_arguments_model=_MentionedContactSummaryArguments,
        output_model=_MentionedContactSummaryOutput,
        argument_source=ToolArgumentSource.MENTIONED_CONTACTS,
    ),
    "relationship.list_accepted_contacts": ToolSpec(
        "relationship.list_accepted_contacts", ToolRisk.READ, "accepted_contact_list",
        "讀取本人已接受聯絡人的最小公開摘要與總數；適用於詢問目前認識的人中誰適合某個活動或地點，不能用來推測對方行程，也不包含尚未接受的 proposal。",
        "我整理一下目前已建立聯絡的對象…",
        output_model=_AcceptedContactListOutput,
    ),
    "memory.search_my_profile": ToolSpec(
        "memory.search_my_profile", ToolRisk.READ, "memory_profile",
        "讀取本人已儲存的偏好與近期情境。",
        "我確認一下我替你記住的事情…",
        output_model=_MemoryOutput,
    ),
    "web.search": ToolSpec(
        "web.search", ToolRisk.READ, "web_search",
        "查詢最新公開資訊；適用於活動、店家、景點、新聞或使用者明確要求上網查詢。",
        "我查一下最近的公開資訊…",
        planner_arguments_model=_WebSearchArguments,
        executor_arguments_model=_WebSearchArguments,
        output_model=_WebSearchOutput,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "web.extract": ToolSpec(
        "web.extract", ToolRisk.READ, "web_extract",
        "讀取本回合公開搜尋結果或使用者提供網址的重點內容，以確認細節。",
        "我打開來源確認一下細節…",
        planner_arguments_model=_WebExtractArguments,
        executor_arguments_model=_WebExtractArguments,
        output_model=_WebExtractOutput,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "places.search_nearby": ToolSpec(
        "places.search_nearby", ToolRisk.READ, "places_nearby",
        "查詢指定地點或本人儲存地區附近的餐廳、咖啡廳、小酌地點、景點或公園；距離為直線距離。",
        "我找一下附近的地點…",
        planner_arguments_model=_PlacesNearbyArguments,
        executor_arguments_model=_PlacesNearbyArguments,
        output_model=_PlacesNearbyOutput,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "places.measure_distance": ToolSpec(
        "places.measure_distance", ToolRisk.READ, "places_distance",
        "查詢兩個指定地點，或本人儲存地區到指定地點的距離；Google Routes 可用時回傳駕車距離與時間，否則回傳直線距離。不提供即時路況或步行時間。",
        "我估一下兩地的距離…",
        planner_arguments_model=_PlacesDistanceArguments,
        executor_arguments_model=_PlacesDistanceArguments,
        output_model=_PlacesDistanceOutput,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
        reuse_success_within_turn=True,
    ),
    "places.resolve_place": ToolSpec(
        "places.resolve_place", ToolRisk.READ, "places_resolve",
        "將使用者明確提到、或本回合已確認名稱的店家或景點解析成公開地點卡；不能自行捏造店名。",
        "我確認一下這家店的公開地點資訊…",
        planner_arguments_model=_PlacesResolveArguments,
        executor_arguments_model=_PlacesResolveArguments,
        output_model=_PlacesResolveOutput,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "match.start_search": ToolSpec(
        "match.start_search", ToolRisk.WRITE, "start_search",
        "開始找新對象；只能先建立 confirmation，確認後由 Runtime 執行。",
        "好，我開始幫你找合適的人…",
        requires_confirmation=True,
    ),
    "match.decide_active_proposal": ToolSpec(
        "match.decide_active_proposal", ToolRisk.WRITE, "decide_active_proposal",
        "對唯一可操作提案表達有興趣或婉拒；Runtime 注入 proposal revision。",
        "我正在更新這張牽線提案…",
        planner_arguments_model=_ProposalDecisionArguments,
        executor_arguments_model=_ProposalDecisionArguments,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "profile.start_assessment": ToolSpec(
        "profile.start_assessment", ToolRisk.WRITE, "assessment_start",
        "開始或重新開始本人的基本／深層探索；kind=basic 或 deep，必須先取得確認，完成後仍需再次確認才會覆寫對應正式資料。",
        "我準備開始這段探索…",
        requires_confirmation=True,
        planner_arguments_model=_AssessmentStartArguments,
        executor_arguments_model=_AssessmentStartArguments,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "calendar.submit_commands": ToolSpec(
        "calendar.submit_commands", ToolRisk.WRITE, "calendar_commands",
        "提交一個或多個行事曆 mutation command；欄位必須使用 canonical action、target_reference 或 target_hint，"
        "不要使用 type 或 target；target_reference 只能是 server context 提供的 recent_event/candidate_1..candidate_3，"
        "target_hint 只放自然語言行程 identity clue，不含操作詞；不含 event_id、revision 或其他 authority fields。",
        "我整理一下要變更的行程…",
        requires_confirmation=True,
        planner_arguments_model=CalendarCommandBatch,
        executor_arguments_model=CalendarCommandBatch,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
}

READ_ONLY_TOOLS = frozenset(
    # Mention summary has no safe target without a validated entity binding, so
    # it is intentionally not part of the always-visible read surface.
    name for name, spec in TOOL_REGISTRY.items()
    if spec.risk is ToolRisk.READ
    and spec.argument_source is not ToolArgumentSource.MENTIONED_CONTACTS
    and spec.executor_key not in {"web_search", "web_extract", "places_nearby", "places_distance", "places_resolve"}
)
WEB_TOOLS = frozenset({"web.search", "web.extract"})
PLACES_TOOLS = frozenset({"places.search_nearby", "places.measure_distance", "places.resolve_place"})
ASSESSMENT_TOOLS = frozenset({"profile.start_assessment"})
SIDE_EFFECT_TOOLS = frozenset(
    name for name, spec in TOOL_REGISTRY.items() if spec.risk is ToolRisk.WRITE
)


def get_tool_spec(name: str | None) -> ToolSpec | None:
    return TOOL_REGISTRY.get(name or "")


def planner_arguments_allowed(spec: ToolSpec, arguments: dict[str, Any]) -> bool:
    """Validate the public planner payload without accepting IDs or revisions."""
    try:
        spec.planner_arguments_model.model_validate(arguments)
        return True
    except Exception:
        return False


def planner_arguments_schema(spec: ToolSpec) -> dict[str, Any]:
    return spec.planner_arguments_model.model_json_schema()


def executor_arguments_for_turn(
    spec: ToolSpec, mentioned_ids: list[str], planner_arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the sole canonical argument payload owned by the executor."""
    payload: dict[str, Any] = {}
    if spec.argument_source is ToolArgumentSource.MENTIONED_RELATIONSHIP and mentioned_ids:
        payload["other_id"] = mentioned_ids[0]
    elif spec.argument_source is ToolArgumentSource.MENTIONED_CONTACTS and mentioned_ids:
        payload["other_ids"] = mentioned_ids[:3]
    elif spec.argument_source is ToolArgumentSource.PLANNER_GROUNDED:
        payload = dict(planner_arguments or {})
    return spec.executor_arguments_model.model_validate(payload).model_dump()


def validate_executor_arguments(spec: ToolSpec, arguments: dict[str, Any]) -> dict[str, Any] | None:
    try:
        return spec.executor_arguments_model.model_validate(arguments).model_dump()
    except Exception:
        return None


def tool_call_key(spec: ToolSpec, arguments: dict[str, Any]) -> tuple[str, str]:
    """Stable duplicate key over executor-owned, validated arguments only."""
    return spec.name, json.dumps(arguments, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def planner_tool_names(
    *, can_start_search: bool, can_decide_active_proposal: bool,
    can_read_mentioned_contacts: bool = False, can_use_web: bool = False, can_use_places: bool = False,
    can_start_assessments: bool = True,
) -> frozenset[str]:
    """Return V2's complete safe read surface plus guarded write intents."""
    names = set(READ_ONLY_TOOLS)
    # This tool has no safe target unless the server validated an accepted @ mention.
    names.discard("relationship.get_mentioned_contact_summary")
    if can_read_mentioned_contacts:
        names.add("relationship.get_mentioned_contact_summary")
    if can_use_web:
        names.update(WEB_TOOLS)
    if can_use_places:
        names.update(PLACES_TOOLS)
    if can_start_search:
        names.add("match.start_search")
    if can_decide_active_proposal:
        names.add("match.decide_active_proposal")
    if can_start_assessments:
        names.update(ASSESSMENT_TOOLS)
    return frozenset(names)
