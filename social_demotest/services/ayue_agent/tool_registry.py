"""Single source of truth for public-Ayue tools and their safety contract."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ToolRisk(str, Enum):
    READ = "read"
    WRITE = "write"


class ToolArgumentSource(str, Enum):
    """Where executor arguments originate; never from an untrusted planner."""

    NONE = "none"
    MENTIONED_RELATIONSHIP = "mentioned_relationship"
    MENTIONED_CONTACTS = "mentioned_contacts"
    PLANNER_GROUNDED = "planner_grounded"


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


class _CalendarCreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    date: str = Field(min_length=10, max_length=10)
    start_time: str = Field(min_length=4, max_length=5)
    end_time: str = Field(min_length=4, max_length=5)
    timezone: str = "Asia/Taipei"
    location: str = Field(default="", max_length=160)
    notes: str = Field(default="", max_length=500)


class _CalendarUpdateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_hint: str = Field(min_length=1, max_length=120)
    title: str | None = Field(default=None, min_length=1, max_length=120)
    date: str | None = Field(default=None, min_length=10, max_length=10)
    start_time: str | None = Field(default=None, min_length=4, max_length=5)
    end_time: str | None = Field(default=None, min_length=4, max_length=5)
    timezone: str | None = None
    location: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=500)


class _CalendarCancelArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_hint: str = Field(min_length=1, max_length=120)


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
    """Human place names only; coordinates and arbitrary map queries are never planner input."""

    model_config = ConfigDict(extra="forbid")
    anchor: str = Field(default="", max_length=160)
    categories: list[Literal["restaurant", "cafe", "bar", "attraction", "park"]] = Field(min_length=1, max_length=3)
    radius_m: int = Field(default=1500, ge=300, le=5000)
    limit: int = Field(default=8, ge=1, le=10)
    use_saved_location: bool = False


class _PlacesDistanceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin: str = Field(default="", max_length=160)
    destination: str = Field(min_length=2, max_length=160)
    use_saved_origin: bool = False


class _CalendarEventOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    date: str
    start_time: str
    end_time: str
    activity: str
    status: str


class _CalendarOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[_CalendarEventOutput]
    range: str


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


class _PlaceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    category: Literal["restaurant", "cafe", "bar", "attraction", "park"]
    distance_m: int = Field(ge=0)
    address_summary: str = ""
    map_url: str


class _PlacesNearbyOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    anchor_label: str
    origin_kind: Literal["explicit", "saved_profile"]
    distance_basis: Literal["straight_line"]
    attribution: str
    attribution_url: str
    places: list[_PlaceOutput] = Field(default_factory=list)


class _PlacesDistanceOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_label: str
    destination_label: str
    origin_kind: Literal["explicit", "saved_profile"]
    distance_m: int = Field(ge=0)
    distance_basis: Literal["straight_line"]
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


TOOL_REGISTRY: dict[str, ToolSpec] = {
    "system.get_current_time": ToolSpec(
        "system.get_current_time", ToolRisk.READ, "current_time",
        "讀取本回合的目前台北時間與訊息中相對日期的實際日期。",
        "我確認一下現在的時間…",
        output_model=_ClockOutput,
    ),
    "calendar.list_my_events": ToolSpec(
        "calendar.list_my_events", ToolRisk.READ, "calendar_events",
        "讀取本人的行事曆與忙碌時段，不讀取對方行事曆。",
        "我看一下你的行事曆…",
        output_model=_CalendarOutput,
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
        "估算兩個指定地點，或本人儲存地區到指定地點的直線距離；不提供車程或步行時間。",
        "我估一下兩地的距離…",
        planner_arguments_model=_PlacesDistanceArguments,
        executor_arguments_model=_PlacesDistanceArguments,
        output_model=_PlacesDistanceOutput,
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
    "calendar.create_my_event": ToolSpec(
        "calendar.create_my_event", ToolRisk.WRITE, "calendar_create",
        "新增本人的私人行程；必須先向使用者確認日期、時間與內容。",
        "我確認一下要新增的行程…",
        requires_confirmation=True,
        planner_arguments_model=_CalendarCreateArguments,
        executor_arguments_model=_CalendarCreateArguments,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "calendar.update_my_event": ToolSpec(
        "calendar.update_my_event", ToolRisk.WRITE, "calendar_update",
        "修改本人行事曆中的唯一一筆行程；私人行程直接修改，共同約會會提出改期並通知對方重新確認。",
        "我確認一下要修改的行程…",
        requires_confirmation=True,
        planner_arguments_model=_CalendarUpdateArguments,
        executor_arguments_model=_CalendarUpdateArguments,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
    "calendar.cancel_my_event": ToolSpec(
        "calendar.cancel_my_event", ToolRisk.WRITE, "calendar_cancel",
        "取消本人行事曆中的唯一一筆行程；共同約會會同步取消並通知對方。",
        "我確認一下要取消的行程…",
        requires_confirmation=True,
        planner_arguments_model=_CalendarCancelArguments,
        executor_arguments_model=_CalendarCancelArguments,
        argument_source=ToolArgumentSource.PLANNER_GROUNDED,
    ),
}

READ_ONLY_TOOLS = frozenset(
    # Mention summary has no safe target without a validated entity binding, so
    # it is intentionally not part of the always-visible read surface.
    name for name, spec in TOOL_REGISTRY.items()
    if spec.risk is ToolRisk.READ
    and spec.argument_source is not ToolArgumentSource.MENTIONED_CONTACTS
    and spec.executor_key not in {"web_search", "web_extract", "places_nearby", "places_distance"}
)
WEB_TOOLS = frozenset({"web.search", "web.extract"})
PLACES_TOOLS = frozenset({"places.search_nearby", "places.measure_distance"})
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
    *, can_start_search: bool, can_decide_active_proposal: bool, can_edit_calendar: bool = True,
    can_read_mentioned_contacts: bool = False, can_use_web: bool = False, can_use_places: bool = False,
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
    if can_edit_calendar:
        names.update({"calendar.create_my_event", "calendar.update_my_event", "calendar.cancel_my_event"})
    return frozenset(names)
