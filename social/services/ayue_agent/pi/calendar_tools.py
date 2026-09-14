"""Pi-owned Calendar tool schemas and deterministic runtime.

This module intentionally does not import the DAG Calendar runtime or Calendar
subagent.  The Pi model describes intent; shared server code validates dates,
resolves owned events, and binds the existing confirmation executor payload.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from services.ayue_agent.shared.calendar_mutations import (
    CalendarCommand,
    prepare_pi_calendar_confirmation,
)


READ_TOOLS = frozenset({
    "calendar.list_my_events",
    "calendar.get_next_my_event",
    "calendar.verify_recent_mutation",
    "calendar.find_my_event",
})
WRITE_TOOLS = frozenset({
    "calendar.prepare_create",
    "calendar.prepare_update",
    "calendar.prepare_cancel",
    "calendar.prepare_cancel_many",
})
TOOLS = READ_TOOLS | WRITE_TOOLS

PROMPT = """【行事曆】
從本房間可見前後文整合活動、日期、時間、地點與更正；只補時間時不可丟掉前文活動與日期。
新增、修改、取消分別使用 prepare_create、prepare_update、prepare_cancel；明確取消多筆或全部未來行程才用 prepare_cancel_many。
新增日期用 start_date；修改後日期用 new_date；target_selector.date 只表示既有行程日期。
欄位不齊也先送已知欄位，依 server 的 clarification 只問真正缺少的內容；不得猜目標或時間。
「一小時」傳 duration_minutes=60；只有使用者明確說全天才傳 all_day=true。
修改／取消用具體名稱、日期與時間查本人行程；多筆相符時不可默選。詢問上一筆是否成功用 verify_recent_mutation。
使用者已給「既有行程描述＋要改的新欄位」時直接呼叫 prepare_update；已給取消目標時直接 prepare_cancel。
prepare 的 server preflight 會查本人行程並綁定正式 ID；不要先重複 list/find 來猜 ID。
解出前文場所後直接把可見的具體名稱與地區交給 Calendar；不要傳 place reference 或候選序號。
只有使用者明確要求安排、記入、變更或取消才準備寫入。"""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CalendarTargetSelector(_StrictModel):
    date: str | None = Field(default=None, max_length=32)
    start_time: str | None = Field(default=None, max_length=16)
    end_time: str | None = Field(default=None, max_length=16)


class CalendarCreateItem(_StrictModel):
    title: str | None = Field(default=None, max_length=120)
    activity: str | None = Field(default=None, max_length=120, description="title 的相容別名；兩者不可衝突")
    event_name: str | None = Field(default=None, max_length=120, description="title 的相容別名；兩者不可衝突")
    summary: str | None = Field(default=None, max_length=120, description="title 的相容別名；兩者不可衝突")
    start_date: str | None = Field(default=None, max_length=32)
    end_date: str | None = Field(default=None, max_length=32)
    all_day: bool | None = None
    start_time: str | None = Field(default=None, max_length=16)
    end_time: str | None = Field(default=None, max_length=16)
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    timezone: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=500)


class CalendarCreateInput(_StrictModel):
    events: list[CalendarCreateItem] = Field(min_length=1, max_length=10)


class CalendarUpdateItem(_StrictModel):
    target_hint: str | None = Field(default=None, max_length=120)
    target_selector: CalendarTargetSelector | None = None
    title: str | None = Field(default=None, max_length=120)
    new_date: str | None = Field(default=None, max_length=32)
    start_date: str | None = Field(default=None, max_length=32, description="new_date 的相容別名")
    end_date: str | None = Field(default=None, max_length=32)
    all_day: bool | None = None
    start_time: str | None = Field(default=None, max_length=16)
    new_start_time: str | None = Field(default=None, max_length=16, description="start_time 的相容別名")
    end_time: str | None = Field(default=None, max_length=16)
    new_end_time: str | None = Field(default=None, max_length=16, description="end_time 的相容別名")
    duration_minutes: int | None = Field(default=None, ge=1, le=1440)
    time_shift_minutes: int | None = Field(default=None, ge=-1440, le=1440)
    timezone: str | None = Field(default=None, max_length=64)
    location: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=500)


class CalendarUpdateInput(_StrictModel):
    changes: list[CalendarUpdateItem] = Field(min_length=1, max_length=10)


class CalendarCancelItem(_StrictModel):
    target_hint: str | None = Field(default=None, max_length=120)
    event_hint: str | None = Field(default=None, max_length=120, description="target_hint 的相容別名")
    target_selector: CalendarTargetSelector | None = None
    date: str | None = Field(default=None, max_length=32, description="既有行程日期的簡寫")
    start_time: str | None = Field(default=None, max_length=16, description="既有行程開始時間的簡寫")
    end_time: str | None = Field(default=None, max_length=16, description="既有行程結束時間的簡寫")


class CalendarCancelInput(_StrictModel):
    targets: list[CalendarCancelItem | str] = Field(
        min_length=1,
        max_length=10,
        description="每項可用完整 selector，或只用自然語言行程名稱",
    )


class CalendarCancelManyInput(_StrictModel):
    target_hints: list[str] = Field(default_factory=list, max_length=10)
    all_upcoming: bool = False


_INPUT_MODELS: dict[str, type[BaseModel]] = {
    "calendar.prepare_create": CalendarCreateInput,
    "calendar.prepare_update": CalendarUpdateInput,
    "calendar.prepare_cancel": CalendarCancelInput,
    "calendar.prepare_cancel_many": CalendarCancelManyInput,
}


_DESCRIPTIONS = {
    "calendar.prepare_create": (
        "準備新增一個或多個本人行程。缺少欄位時仍要提交已知內容，讓 server 精確回覆缺少欄位。"
        "一小時請傳 duration_minutes=60；不要自行猜時間。"
    ),
    "calendar.prepare_update": (
        "準備修改本人既有行程。target_hint 只放既有行程名稱／地點；新日期時間放其他欄位。"
    ),
    "calendar.prepare_cancel": "準備取消一個或多個本人既有行程，以名稱／地點和可選日期時間定位。",
    "calendar.prepare_cancel_many": (
        "準備取消多個指定行程，或在使用者明確要求全部取消時設 all_upcoming=true。"
    ),
}


def _inline_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    definitions = schema.get("$defs", {})

    def expand(node: Any, resolving: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [expand(item, resolving) for item in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if reference:
            name = str(reference).removeprefix("#/$defs/")
            if name not in definitions or name in resolving:
                raise ValueError("invalid_schema_reference")
            merged = {**definitions[name], **{key: value for key, value in node.items() if key != "$ref"}}
            return expand(merged, (*resolving, name))
        return {key: expand(value, resolving) for key, value in node.items() if key != "$defs"}

    return expand(schema)


def write_tool_schemas() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "description": _DESCRIPTIONS[name],
            "parameters": _inline_schema(model),
        }
        for name, model in _INPUT_MODELS.items()
    ]


def _commands(tool_name: str, arguments: dict[str, Any]) -> list[CalendarCommand]:
    model = _INPUT_MODELS[tool_name].model_validate(arguments)
    if tool_name == "calendar.prepare_create":
        commands = []
        for item in model.events:
            values = item.model_dump(exclude_none=True)
            title_values = [str(values[key]).strip() for key in ("title", "activity", "event_name", "summary") if values.get(key)]
            if len(set(title_values)) > 1:
                raise ValueError("conflicting create title aliases")
            for alias in ("activity", "event_name", "summary"):
                values.pop(alias, None)
            if title_values:
                values["title"] = title_values[0]
            if "start_date" in values:
                values["date"] = values.pop("start_date")
            commands.append(CalendarCommand(action="create", **values))
        return commands
    if tool_name == "calendar.prepare_update":
        commands = []
        for item in model.changes:
            values = item.model_dump(exclude_none=True)
            date_values = [str(values[key]).strip() for key in ("new_date", "start_date") if values.get(key)]
            if len(set(date_values)) > 1:
                raise ValueError("conflicting update date aliases")
            values.pop("new_date", None)
            values.pop("start_date", None)
            if date_values:
                values["date"] = date_values[0]
            for canonical_name, alias_name in (
                ("start_time", "new_start_time"),
                ("end_time", "new_end_time"),
            ):
                time_values = [
                    str(values[key]).strip()
                    for key in (canonical_name, alias_name)
                    if values.get(key)
                ]
                if len(set(time_values)) > 1:
                    raise ValueError(f"conflicting update {canonical_name} aliases")
                values.pop(alias_name, None)
                if time_values:
                    values[canonical_name] = time_values[0]
            commands.append(CalendarCommand(action="update", **values))
        return commands
    if tool_name == "calendar.prepare_cancel":
        commands = []
        for item in model.targets:
            if isinstance(item, str):
                commands.append(CalendarCommand(action="cancel", target_hint=item))
                continue
            values = item.model_dump(exclude_none=True)
            hint_values = [str(values[key]).strip() for key in ("target_hint", "event_hint") if values.get(key)]
            if len(set(hint_values)) > 1:
                raise ValueError("conflicting cancel target aliases")
            values.pop("event_hint", None)
            if hint_values:
                values["target_hint"] = hint_values[0]
            direct_selector = {
                key: values.pop(key)
                for key in ("date", "start_time", "end_time")
                if key in values
            }
            if direct_selector:
                if "target_selector" in values:
                    raise ValueError("conflicting cancel target selectors")
                values["target_selector"] = direct_selector
            commands.append(CalendarCommand(action="cancel", **values))
        return commands
    if model.all_upcoming:
        if model.target_hints:
            raise ValueError("all_upcoming cannot be combined with target_hints")
        return [CalendarCommand(action="cancel_all_upcoming")]
    if len(model.target_hints) < 2:
        raise ValueError("cancel_many requires two target_hints or all_upcoming")
    return [CalendarCommand(action="cancel_selected", target_hints=model.target_hints)]


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Execute a read or prepare one Pi-owned Calendar confirmation."""
    if name in READ_TOOLS:
        return runtime.read(name, arguments)
    commands = _commands(name, arguments)

    def create_confirmation(**kwargs: Any) -> str:
        return runtime.create_confirmation(
            agent_name=str(kwargs.get("agent_name") or "calendar"),
            tool_name=str(kwargs.get("tool_name") or "calendar.submit_commands"),
            arguments=dict(kwargs.get("arguments") or {}),
            payload=dict(kwargs.get("payload") or {}),
            preview=str(kwargs.get("preview") or ""),
            idempotency_key=kwargs.get("idempotency_key"),
        )

    preparation = prepare_pi_calendar_confirmation(
        runtime.turn, commands, run_id=runtime.run_id,
        create_confirmation=create_confirmation,
        supersede_confirmation=lambda **kwargs: runtime.confirmations.supersede_active(**kwargs),
    )
    return runtime.project(
        name="calendar.submit_commands",
        status="failed" if preparation.status == "failed" else "ok",
        result=preparation.observation,
        error_code=preparation.error_code,
    )
