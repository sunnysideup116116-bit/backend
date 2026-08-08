"""Typed Calendar commands and the deterministic V3 mutation preflight.

The Calendar sub-agent only produces :class:`CalendarCommand` values.  This
module is the authority boundary that turns those untrusted descriptions into
server-owned :class:`CalendarMutationPlan` values.  Plans are executor-only;
they must never be included in an agent context or observation.
"""

from __future__ import annotations

from datetime import date as date_value, datetime, timedelta
import re
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from services.calendar_service import (
    _parse_local_interval,
    as_utc,
    calendar_access_enabled,
    conflicts_for_viewer,
    get_timezone,
    get_owned_event_resolution_candidates,
    get_owned_event_resolution_kind,
    normalize_form,
    resolve_owned_event,
    resolve_owned_events_for_cancel,
)


CalendarAction = Literal["create", "update", "cancel", "cancel_selected", "cancel_all_upcoming"]
CalendarDraftMode = Literal["none", "continue", "replace"]

_OPAQUE_TARGET_REFERENCES = frozenset({
    "recent_event", "candidate_1", "candidate_2", "candidate_3",
})

# ``fields`` is a provider-compatibility wrapper, not a second Calendar
# schema.  Keep this allowlist explicit so authority fields can never become
# accepted merely because a provider nested them.
_CALENDAR_COMMAND_FIELDS = frozenset({
    "action", "target_hint", "target_reference", "target_hints", "title", "date",
    "start_time", "end_time", "duration_minutes", "time_shift_minutes", "timezone",
    "location", "notes", "draft_mode",
})
_CALENDAR_FIELD_ALIASES = {
    "type": "action", "operation": "action", "summary": "title",
    "activity": "title", "name": "title", "event_hint": "target_hint",
    "event_hints": "target_hints", "start": "start_time", "end": "end_time",
    "duration": "duration_minutes",
}
_CALENDAR_ACTION_ALIASES = {
    "add": "create", "new": "create", "edit": "update", "modify": "update",
    "delete": "cancel", "remove": "cancel",
}


def _normalize_calendar_command_aliases(command: dict[str, Any]) -> None:
    """Apply only the existing closed provider aliases to one command map."""
    target = command.get("target")
    if (
        "target_reference" not in command
        and isinstance(target, str)
        and target.strip() in _OPAQUE_TARGET_REFERENCES
    ):
        command["target_reference"] = target.strip()
        command.pop("target", None)
    for alias, canonical in _CALENDAR_FIELD_ALIASES.items():
        if alias in command and canonical not in command:
            command[canonical] = command.pop(alias)
    if isinstance(command.get("action"), str):
        action = command["action"].strip().lower()
        command["action"] = _CALENDAR_ACTION_ALIASES.get(action, action)


def normalize_calendar_batch_payload(arguments: Any) -> dict[str, Any]:
    """Normalize legacy/provider spellings before strict command validation.

    The public schema has one canonical vocabulary, but some compatible model
    providers still emit the old calendar names (``type``, ``summary`` and
    ``event_hint``).  This is a closed protocol adapter, not an intent router:
    unknown fields are retained and rejected by Pydantic below.
    """
    if not isinstance(arguments, dict):
        raise ValueError("calendar command arguments must be an object")
    raw_commands = arguments.get("commands")
    if raw_commands is None and ("action" in arguments or "type" in arguments):
        raw_commands = [arguments]
    if not isinstance(raw_commands, list):
        raise ValueError("calendar command arguments must contain commands")
    normalized_commands: list[dict[str, Any]] = []
    for raw in raw_commands:
        if not isinstance(raw, dict):
            raise ValueError("each calendar command must be an object")
        command = dict(raw)
        _normalize_calendar_command_aliases(command)

        # A few providers wrap semantic command fields in ``fields``.  Flatten
        # only the canonical CalendarCommand vocabulary.  Keep the wrapper in
        # place when it contains anything else so strict validation rejects the
        # whole command instead of silently dropping unknown/authority data.
        nested_fields = command.get("fields")
        if isinstance(nested_fields, dict):
            nested = dict(nested_fields)
            _normalize_calendar_command_aliases(nested)
            unknown_nested_fields = set(nested) - _CALENDAR_COMMAND_FIELDS
            for key, value in nested.items():
                if key not in _CALENDAR_COMMAND_FIELDS:
                    continue
                if key in command:
                    if command[key] != value:
                        raise ValueError(f"conflicting calendar field: {key}")
                else:
                    command[key] = value
            if not unknown_nested_fields:
                command.pop("fields", None)
        normalized_commands.append(command)
    return {"commands": normalized_commands}


class CalendarCommand(BaseModel):
    """LLM-owned, authority-free description of one calendar mutation."""

    model_config = ConfigDict(extra="forbid")

    action: CalendarAction = Field(description="create、update、cancel、cancel_selected 或 cancel_all_upcoming")
    target_hint: str | None = Field(
        default=None,
        max_length=120,
        description=(
            "update/cancel 的行程 identity clue，例如牙醫、睡覺或雞排；"
            "只填活動／地點等辨識線索，不要填取消／修改等操作詞、禮貌語、情緒、"
            "完整對話句或 server 內部識別欄位。"
        ),
    )
    target_reference: Literal["recent_event", "candidate_1", "candidate_2", "candidate_3"] | None = Field(
        default=None,
        description=(
            "只能原樣回傳 server context 提供的 opaque reference："
            "recent_event 表示最近唯一行程；candidate_1、candidate_2、candidate_3 "
            "只能表示目前 calendar_draft.candidates 中相同 reference 的候選。"
        ),
    )
    target_hints: list[str] = Field(default_factory=list, max_length=10, description="cancel_selected 的 2–10 個自然語言行程描述")
    title: str | None = Field(default=None, max_length=120, description="活動名稱，例如「去日本」；不要包含日期或時間")
    date: str | None = Field(default=None, max_length=32, description="優先使用 YYYY-MM-DD；今天／明天／後天也可由 server 依 authoritative clock 轉換")
    start_time: str | None = Field(default=None, max_length=16, description="開始時間，使用 HH:MM")
    end_time: str | None = Field(default=None, max_length=16, description="結束時間，使用 HH:MM；若使用 duration_minutes 可省略")
    duration_minutes: int | None = Field(
        default=None, ge=1, le=24 * 60,
        description="使用者明確說出的持續分鐘數，例如半小時=30、一小時=60；不要自行猜測或做權威時鐘計算",
    )
    time_shift_minutes: int | None = Field(
        default=None,
        ge=-24 * 60,
        le=24 * 60,
        description=(
            "update 專用：將既有行程的開始與結束時間整體平移的分鐘數；"
            "正數代表延後、負數代表提前。這不是 duration_minutes，"
            "不可與新的 date/start_time/end_time/duration_minutes 同時提供。"
        ),
    )
    timezone: str | None = Field(default=None, max_length=64, description="時區；未提供時由 server 使用 Asia/Taipei")
    location: str | None = Field(default=None, max_length=160, description="可選地點")
    notes: str | None = Field(default=None, max_length=500, description="可選備註")
    draft_mode: CalendarDraftMode = Field(
        default="none",
        description="補齊既有 Calendar 草稿用 continue；新請求用 replace；一般用 none",
    )

    @field_validator("duration_minutes", mode="before")
    @classmethod
    def _coerce_explicit_duration(cls, value: Any) -> Any:
        """Accept a small closed vocabulary from compatible providers.

        The canonical tool schema remains an integer.  This adapter only
        handles literal duration phrases; it does not infer duration from an
        event title or from a pair of clock values.
        """
        if value is None or isinstance(value, int):
            return value
        text = str(value).strip().replace("大約", "").replace("大概", "").replace("約", "")
        aliases = {
            "半小時": 30, "一小時": 60, "1小時": 60,
            "一個半小時": 90, "1個半小時": 90,
            "兩小時": 120, "二小時": 120, "2小時": 120,
        }
        if text in aliases:
            return aliases[text]
        return value

    @model_validator(mode="after")
    def _validate_target_shape(self) -> "CalendarCommand":
        if self.action in {"create", "update", "cancel"} and self.target_hints:
            raise ValueError("target_hints is only valid for cancel_selected")
        if self.action in {"create", "cancel_all_upcoming"} and (self.target_hint or self.target_reference):
            raise ValueError("target selector is not valid for this action")
        if self.action == "cancel_selected" and (self.target_hint or self.target_reference):
            raise ValueError("target selector is only valid for update/cancel")
        if self.action != "cancel_selected" and self.target_hints:
            raise ValueError("target_hints is only valid for cancel_selected")
        if self.action in {"update", "cancel"} and self.target_hint and self.target_reference:
            raise ValueError("target_hint and target_reference are mutually exclusive")
        if self.time_shift_minutes is not None:
            if self.action != "update":
                raise ValueError("time_shift_minutes is only valid for update")
            if self.time_shift_minutes == 0:
                raise ValueError("time_shift_minutes cannot be zero")
            if any(value is not None for value in (
                self.date, self.start_time, self.end_time, self.duration_minutes,
            )):
                raise ValueError(
                    "time_shift_minutes cannot be combined with date, start_time, end_time or duration_minutes"
                )
        return self


class CalendarCommandBatch(BaseModel):
    """Bounded command list emitted by the Calendar Agent."""

    model_config = ConfigDict(extra="forbid")

    commands: list[CalendarCommand] = Field(min_length=1, max_length=10)


class NeedsClarification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: Literal[
        "missing_fields", "ambiguous", "not_found", "too_many", "invalid_date",
        "invalid_interval", "invalid_command", "stale_revision",
    ]
    message: str
    command_index: int = Field(ge=0)
    missing_fields: list[str] = Field(default_factory=list, max_length=8)
    query: str | None = Field(default=None, max_length=120)
    searched_count: int | None = Field(default=None, ge=0, le=1000)
    candidates: list[dict[str, str]] = Field(default_factory=list, max_length=3)


class CalendarMutationPlan(BaseModel):
    """Server-owned executor plan.  Never expose this model to an LLM."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["create", "update", "cancel"]
    source_type: Literal["personal", "date"] = "personal"
    event_id: str | None = None
    expected_revision: int | None = None
    other_id: str | None = None
    coordination_id: str | None = None
    form: dict[str, Any] = Field(default_factory=dict)
    changes: dict[str, Any] = Field(default_factory=dict)
    resolution_kind: Literal["server_created", "exact", "recent_reference", "fuzzy_suggestion"] = "server_created"
    safe_label: str = "這筆行程"


class CalendarResolvedTarget(BaseModel):
    """Server-only target binding carried across Calendar clarification turns."""

    model_config = ConfigDict(extra="forbid")

    event_id: str
    expected_revision: int
    source_type: Literal["personal", "date"]
    other_id: str | None = None
    coordination_id: str | None = None
    safe_label: str = "這筆行程"


class CalendarPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "needs_clarification", "denied"]
    plans: list[CalendarMutationPlan] = Field(default_factory=list, max_length=10)
    clarification: NeedsClarification | None = None
    preview: str = ""
    denial_code: str | None = None
    # This is consumed only by Scheduler to persist a server-owned draft
    # binding.  It is excluded from all model projections sent to an LLM.
    resolved_target: CalendarResolvedTarget | None = Field(default=None, exclude=True)


def _event_label(event: dict[str, Any]) -> str:
    value_start = event.get("start_at")
    value_end = event.get("end_at")
    if isinstance(value_start, str):
        value_start = datetime.fromisoformat(value_start.replace("Z", "+00:00"))
    if isinstance(value_end, str):
        value_end = datetime.fromisoformat(value_end.replace("Z", "+00:00"))
    zone = get_timezone(event.get("timezone") or "Asia/Taipei")
    start = as_utc(value_start).astimezone(zone)
    end = as_utc(value_end).astimezone(zone)
    title = str(event.get("activity") or event.get("title") or "這筆行程").strip()
    return f"{start.month}/{start.day} {start:%H:%M}–{end:%H:%M} {title}"


def _resolved_target(event: dict[str, Any], user_id: str = "") -> CalendarResolvedTarget:
    """Project an already-resolved event into a server-only binding."""
    source_type = "date" if str(event.get("source_type") or "") == "date" else "personal"
    other_id = _other_id(event, user_id) if source_type == "date" else None
    return CalendarResolvedTarget(
        event_id=str(event.get("event_id") or ""),
        expected_revision=int(event.get("revision", 1) or 1),
        source_type=source_type,
        other_id=other_id,
        coordination_id=event.get("coordination_id"),
        safe_label=_event_label(event),
    )


def _with_resolved_target(
    result: CalendarPreflightResult,
    event: dict[str, Any],
    user_id: str = "",
) -> CalendarPreflightResult:
    """Attach a private binding without exposing authority in observations."""
    result.resolved_target = _resolved_target(event, user_id)
    return result


def _other_id(event: dict[str, Any], user_id: str) -> str | None:
    if event.get("source_type") != "date":
        return None
    return next((item for item in event.get("participants", []) if item != user_id), None)


def _clarification(
    code: str,
    message: str,
    index: int,
    *,
    missing_fields: list[str] | None = None,
    query: str | None = None,
    searched_count: int | None = None,
    candidates: list[dict[str, str]] | None = None,
) -> CalendarPreflightResult:
    return CalendarPreflightResult(
        status="needs_clarification",
        clarification=NeedsClarification(
            code=code, message=message, command_index=index,
            missing_fields=missing_fields or [],
            query=query,
            searched_count=searched_count,
            candidates=candidates or [],
        ),
    )


def _required_create_fields(command: CalendarCommand) -> list[str]:
    labels = {
        "title": "行程名稱", "date": "日期",
        "start_time": "開始時間", "end_time": "結束時間",
    }
    generic_titles = {"行事曆", "日曆", "行程", "這筆行程", "一筆行程"}
    missing: list[str] = []
    for key in ("title", "date", "start_time"):
        value = str(getattr(command, key) or "").strip()
        if not value or (key == "title" and value in generic_titles):
            missing.append(key)
    if not str(command.end_time or "").strip() and command.duration_minutes is None:
        missing.append("end_time")
    return missing


def _base_current_form(event: dict[str, Any]) -> dict[str, str]:
    zone = get_timezone(event.get("timezone") or "Asia/Taipei")
    start = as_utc(event["start_at"]).astimezone(zone)
    end = as_utc(event["end_at"]).astimezone(zone)
    return {
        "title": str(event.get("activity") if event.get("source_type") == "date" else event.get("title") or event.get("activity") or "行程"),
        "activity": str(event.get("activity") or event.get("title") or "共同約會"),
        "date": start.date().isoformat(),
        "start_time": start.strftime("%H:%M"),
        "end_time": end.strftime("%H:%M"),
        "timezone": event.get("timezone") or "Asia/Taipei",
        "location": str(event.get("location") or ""),
        "notes": str(event.get("notes") or ""),
        "budget": str(event.get("budget") or ""),
    }


def _command_changes(command: CalendarCommand) -> dict[str, Any]:
    return {
        key: value for key, value in {
            "title": command.title,
            "date": command.date,
            "start_time": command.start_time,
            "end_time": command.end_time,
            "timezone": command.timezone,
            "location": command.location,
            "notes": command.notes,
        }.items() if value is not None
    }


def _apply_duration_to_form(command: CalendarCommand, form: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    """Derive an end time from an explicit duration at the authority boundary.

    A model may extract ``duration_minutes``, but it must not perform the clock
    arithmetic.  If both duration and end_time are present they must agree;
    silently choosing one would make a confirmation preview misleading.
    """
    duration = command.duration_minutes
    if duration is None:
        return form, None
    date_text = str(form.get("date") or "").strip()
    start_text = str(form.get("start_time") or "").strip()
    if not date_text or not start_text:
        # The normal required-field validation will report the missing input.
        return form, None
    try:
        zone = get_timezone(str(form.get("timezone") or "Asia/Taipei"))
        start = datetime.fromisoformat(f"{date_text}T{start_text}").replace(tzinfo=zone)
        derived_end = start + timedelta(minutes=int(duration))
    except (TypeError, ValueError, HTTPException):
        return form, "日期、開始時間或持續時間格式不正確。"
    if derived_end.date() != start.date():
        return form, "行程持續時間不能跨日，請補上明確的結束時間。"
    derived_end_text = derived_end.strftime("%H:%M")
    # For an update, ``form`` contains the event's current end_time even when
    # the user supplied only a duration.  Only the command's own end_time is
    # an explicit competing value; an inherited value must be replaced.
    explicit_end = str(form.get("end_time") or "").strip() if command.end_time is not None else ""
    if explicit_end and explicit_end != derived_end_text:
        return form, "結束時間與持續時間不一致，請確認其中一個即可。"
    updated = dict(form)
    updated["end_time"] = derived_end_text
    return updated, None


def _apply_time_shift_to_form(
    command: CalendarCommand,
    form: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Shift an existing interval at the server authority boundary."""
    shift = command.time_shift_minutes
    if shift is None:
        return form, None
    date_text = str(form.get("date") or "").strip()
    start_text = str(form.get("start_time") or "").strip()
    end_text = str(form.get("end_time") or "").strip()
    if not date_text or not start_text or not end_text:
        return form, "目前無法平移這筆行程，請補上明確的日期與時間。"
    try:
        zone = get_timezone(str(form.get("timezone") or "Asia/Taipei"))
        start = datetime.fromisoformat(f"{date_text}T{start_text}").replace(tzinfo=zone)
        end = datetime.fromisoformat(f"{date_text}T{end_text}").replace(tzinfo=zone)
        shifted_start = start + timedelta(minutes=int(shift))
        shifted_end = end + timedelta(minutes=int(shift))
    except (TypeError, ValueError, HTTPException):
        return form, "原行程的日期或時間格式不正確，暫時無法平移。"
    if shifted_start.date() != start.date() or shifted_end.date() != start.date():
        return form, "平移後會跨日，請直接提供新的日期與完整時間。"
    updated = dict(form)
    updated["start_time"] = shifted_start.strftime("%H:%M")
    updated["end_time"] = shifted_end.strftime("%H:%M")
    return updated, None


def _is_generic_target_hint(value: str) -> bool:
    """Recognize referential phrases that should use a server-side ref."""
    compact = re.sub(r"\s+", "", str(value or "")).strip("，。！？!?、")
    return compact in {
        "這筆", "這個", "這個行程", "這筆行程", "那筆", "那個", "那筆行程",
        "剛剛那筆", "剛剛這筆", "剛才那筆", "上面那筆", "上一筆",
        "它", "他", "她", "這個它", "那個它", "剛剛提到的它",
    }


def _clock_temporal_references(ctx: Any) -> dict[str, str]:
    clock = getattr(ctx, "clock", None)
    references = getattr(clock, "temporal_references", None)
    if isinstance(references, dict):
        return {str(key): str(value) for key, value in references.items() if str(value).strip()}
    if hasattr(clock, "model_dump"):
        try:
            dumped = clock.model_dump()
            references = dumped.get("temporal_references") if isinstance(dumped, dict) else None
            if isinstance(references, dict):
                return {str(key): str(value) for key, value in references.items() if str(value).strip()}
        except Exception:
            pass
    return {}


_FULL_NUMERIC_DATE_RE = re.compile(
    r"(?P<year>\d{4})(?P<separator>[-/])(?P<month>\d{1,2})(?P=separator)(?P<day>\d{1,2})"
)
_YEARLESS_NUMERIC_DATE_RE = re.compile(
    r"(?P<month>\d{1,2})(?P<separator>[-/])(?P<day>\d{1,2})"
)


def _clock_local_date(ctx: Any) -> date_value | None:
    """Read the authoritative local date without consulting wall-clock time."""
    clock = getattr(ctx, "clock", None)
    local_date = getattr(clock, "local_date", None)
    if local_date is None and isinstance(clock, dict):
        local_date = clock.get("local_date")
    if local_date is None and hasattr(clock, "model_dump"):
        try:
            dumped = clock.model_dump()
            local_date = dumped.get("local_date") if isinstance(dumped, dict) else None
        except Exception:
            local_date = None
    try:
        return date_value.fromisoformat(str(local_date or "").strip())
    except ValueError:
        return None


def _canonicalize_numeric_date(ctx: Any, raw: str) -> tuple[str, str | None] | None:
    """Canonicalize the small closed numeric Calendar date grammar."""
    match = _FULL_NUMERIC_DATE_RE.fullmatch(raw)
    if match:
        year = int(match.group("year"))
        month = int(match.group("month"))
        day = int(match.group("day"))
    else:
        match = _YEARLESS_NUMERIC_DATE_RE.fullmatch(raw)
        if not match:
            return None
        today = _clock_local_date(ctx)
        if today is None:
            return raw, "目前無法依 authoritative clock 確認這個日期，請補上四位數年份。"
        year = today.year
        month = int(match.group("month"))
        day = int(match.group("day"))
    try:
        candidate = date_value(year, month, day)
    except ValueError:
        return raw, "日期無效，請提供有效的年月日。"
    if not match.groupdict().get("year"):
        today = _clock_local_date(ctx)
        if today is not None and candidate < today:
            try:
                candidate = date_value(today.year + 1, candidate.month, candidate.day)
            except ValueError:
                return raw, "日期無效，請提供有效的年月日。"
    return candidate.isoformat(), None


def _canonicalize_date(ctx: Any, value: Any) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    if not raw:
        return raw, None
    raw = re.sub(r"(?:的|那天)$", "", raw).strip()
    if raw in {"今天", "明天", "後天"}:
        resolved = _clock_temporal_references(ctx).get(raw, "")
        if resolved:
            return resolved, None
        return raw, "目前無法確認「%s」是哪一天，請補上明確日期。" % raw
    references = _clock_temporal_references(ctx)
    # Match the complete relative expression first.  Prefix matching would
    # turn ``下週三`` into the Monday represented by ``下週``.
    if raw in {
        "本週", "這週", "下週", "本周", "這周", "下周",
        "下下週", "下下星期", "下下禮拜", "下下周",
        "本月", "下個月",
    }:
        return raw, "「%s」不是單一日期，請補上明確的年月日。" % raw
    resolved = references.get(raw)
    if resolved:
        return resolved, None
    numeric = _canonicalize_numeric_date(ctx, raw)
    if numeric is not None:
        return numeric
    return raw, None


def canonicalize_calendar_command(
    ctx: Any, command: CalendarCommand,
) -> tuple[CalendarCommand, str | None]:
    """Return a command with server-owned relative dates canonicalized.

    This helper is shared by Scheduler draft persistence and preflight so a
    resolvable date never remains as authority-free natural language state.
    """
    if not command.date:
        return command, None
    canonical_date, date_error = _canonicalize_date(ctx, command.date)
    if date_error:
        return command, date_error
    if canonical_date == command.date:
        return command, None
    return command.model_copy(update={"date": canonical_date}), None


def _plan_for_event(
    ctx: Any, command: CalendarCommand, event: dict[str, Any], index: int,
    *, resolution_kind: str = "exact",
) -> tuple[CalendarMutationPlan | None, CalendarPreflightResult | None]:
    user_id = ctx.user_id
    action = "cancel" if command.action == "cancel" else "update"
    source_type = str(event.get("source_type") or "personal")
    if action == "update" and source_type == "date" and event.get("status") != "confirmed":
        return None, _with_resolved_target(_clarification(
            "invalid_interval",
            "這筆共同約會正在等待重新確認，請先完成或取消目前的改期。",
            index,
        ), event, user_id)

    changes = _command_changes(command)
    if action == "update" and not changes and command.duration_minutes is None and command.time_shift_minutes is None:
        return None, _with_resolved_target(
            _clarification("missing_fields", f"你想把「{_event_label(event)}」改成什麼呢？", index),
            event, user_id,
        )

    proposed_form: dict[str, Any] = {}
    if action == "update":
        current = _base_current_form(event)
        if source_type == "date" and "title" in changes:
            changes = {**changes, "activity": changes.pop("title")}
        proposed_form = normalize_form({**current, **changes})
        proposed_form, duration_error = _apply_duration_to_form(command, proposed_form)
        if duration_error:
            return None, _with_resolved_target(
                _clarification("invalid_interval", duration_error, index), event, user_id,
            )
        if command.duration_minutes is not None:
            # The executor receives the canonical derived end_time, never a
            # free-form duration that it would have to interpret again.
            changes["end_time"] = proposed_form["end_time"]
        proposed_form, shift_error = _apply_time_shift_to_form(command, proposed_form)
        if shift_error:
            return None, _with_resolved_target(
                _clarification("invalid_interval", shift_error, index), event, user_id,
            )
        if command.time_shift_minutes is not None:
            changes["start_time"] = proposed_form["start_time"]
            changes["end_time"] = proposed_form["end_time"]
        try:
            start_at, end_at, _ = _parse_local_interval(proposed_form)
        except HTTPException as exc:
            return None, _with_resolved_target(
                _clarification("invalid_interval", str(exc.detail), index), event, user_id,
            )
        participants = list(event.get("participants") or [user_id]) if source_type == "date" else [user_id]
        conflicts = conflicts_for_viewer(user_id, participants, start_at, end_at, event.get("event_id"))
    else:
        conflicts = []

    plan = CalendarMutationPlan(
        action=action,
        source_type="date" if source_type == "date" else "personal",
        event_id=str(event.get("event_id") or ""),
        expected_revision=int(event.get("revision", 1) or 1),
        other_id=_other_id(event, user_id),
        coordination_id=event.get("coordination_id"),
        form=proposed_form,
        changes=changes,
        safe_label=_event_label(event),
        resolution_kind=resolution_kind if resolution_kind in {"exact", "recent_reference", "fuzzy_suggestion"} else "exact",
    )
    preview = ""
    if action == "cancel":
        preview = f"要取消「{plan.safe_label}」嗎？"
        if plan.source_type == "date":
            preview += " 這是共同約會，取消後會同步雙方行事曆並通知對方。"
    else:
        title = proposed_form.get("activity") if plan.source_type == "date" else proposed_form.get("title")
        preview = (
            f"要把「{plan.safe_label}」改成 {proposed_form['date'][5:].replace('-', '/')} "
            f"{proposed_form['start_time']}–{proposed_form['end_time']}「{title or '行程'}」嗎？"
        )
        if plan.source_type == "date":
            preview += " 對方會收到改期通知，重新確認後才會正式變更。"
    if conflicts:
        preview += f" 這會和你現有的 {len(conflicts)} 筆行程重疊；仍要這樣安排嗎？"
    return plan, CalendarPreflightResult(status="ready", plans=[plan], preview=preview)


def _remember_candidate_projection(user_id: str, events: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Store authority server-side and return only opaque refs + safe labels."""
    from .calendar_references import remember_candidates, public_projection

    records = remember_candidates(user_id, events)
    return [public_projection(record) for record in records if public_projection(record)]


def _preflight_one(
    ctx: Any,
    command: CalendarCommand,
    index: int,
    *,
    recent_reference: dict[str, Any] | None = None,
) -> CalendarPreflightResult:
    command, date_error = canonicalize_calendar_command(ctx, command)
    if date_error:
        return _clarification("invalid_date", date_error, index)
    if command.action == "create":
        missing = _required_create_fields(command)
        if missing:
            labels = {"title": "行程名稱", "date": "日期", "start_time": "開始時間", "end_time": "結束時間"}
            return _clarification(
                "missing_fields",
                "請補上：" + "、".join(labels[key] for key in missing) + "。",
                index,
                missing_fields=missing,
            )
        form = normalize_form({
            "title": command.title, "date": command.date,
            "start_time": command.start_time, "end_time": command.end_time,
            "timezone": command.timezone or "Asia/Taipei",
            "location": command.location or "", "notes": command.notes or "",
        })
        form, duration_error = _apply_duration_to_form(command, form)
        if duration_error:
            return _clarification("invalid_interval", duration_error, index)
        try:
            start_at, end_at, _ = _parse_local_interval(form)
        except HTTPException as exc:
            return _clarification("invalid_interval", str(exc.detail), index)
        conflicts = conflicts_for_viewer(ctx.user_id, [ctx.user_id], start_at, end_at)
        plan = CalendarMutationPlan(action="create", form=form, safe_label=(
            f"{form['date'][5:].replace('-', '/')} {form['start_time']}–{form['end_time']} {form['title']}"
        ))
        preview = f"要新增 {form['date'][5:].replace('-', '/')} {form['start_time']}–{form['end_time']}「{form['title']}」嗎？"
        if conflicts:
            preview += f" 這會和你現有的 {len(conflicts)} 筆行程重疊；仍要這樣安排嗎？"
        return CalendarPreflightResult(status="ready", plans=[plan], preview=preview)

    if command.action == "cancel_all_upcoming":
        events, resolution = resolve_owned_events_for_cancel(ctx.user_id, mode="all_upcoming", event_hints=[])
        if resolution == "too_many":
            return _clarification("too_many", "接下來的行程超過 10 筆；請先指定想取消哪些日期。", index)
        if resolution or not events:
            return _clarification("not_found", "目前沒有可取消的未來行程。", index)
        plans = [
            CalendarMutationPlan(
                action="cancel", source_type="date" if event.get("source_type") == "date" else "personal",
                event_id=str(event.get("event_id") or ""), expected_revision=int(event.get("revision", 1) or 1),
                other_id=_other_id(event, ctx.user_id), coordination_id=event.get("coordination_id"),
                safe_label=_event_label(event),
            ) for event in events
        ]
        return CalendarPreflightResult(
            status="ready", plans=plans,
            preview="要取消這些未來行程嗎：" + "、".join(f"「{plan.safe_label}」" for plan in plans) + "？",
        )

    if command.action == "cancel_selected":
        hints = [str(item or "").strip() for item in command.target_hints if str(item or "").strip()]
        if not 2 <= len(hints) <= 10:
            return _clarification("missing_fields", "請提供至少兩筆、最多十筆要取消的行程名稱或日期。", index)
        events, resolution = resolve_owned_events_for_cancel(
            ctx.user_id, mode="selected", event_hints=hints,
        )
        if resolution == "ambiguous":
            return _clarification("ambiguous", "有一筆行程對應到不只一個結果，請補上日期或完整名稱。", index)
        if resolution in {"not_found", "invalid_selection"} or not events:
            return _clarification("not_found", "我找不到其中一筆自己的行程，請補上日期或名稱。", index)
        plans = [
            CalendarMutationPlan(
                action="cancel", source_type="date" if event.get("source_type") == "date" else "personal",
                event_id=str(event.get("event_id") or ""), expected_revision=int(event.get("revision", 1) or 1),
                other_id=_other_id(event, ctx.user_id), coordination_id=event.get("coordination_id"),
                safe_label=_event_label(event),
            ) for event in events
        ]
        return CalendarPreflightResult(
            status="ready", plans=plans,
            preview="要取消這些行程嗎：" + "、".join(f"「{plan.safe_label}」" for plan in plans) + "？",
        )

    target_hint = str(command.target_hint or "").strip()
    target_reference = str(command.target_reference or "").strip()
    use_recent_reference = bool(recent_reference) and (
        bool(recent_reference.get("_force"))
        or bool(target_reference)
        or not target_hint
        or _is_generic_target_hint(target_hint)
    )
    if target_reference and not recent_reference:
        return _clarification("not_found", "我找不到剛剛提到的那筆行程，請補上日期或名稱。", index)
    if not target_hint and not target_reference and not use_recent_reference:
        return _clarification("missing_fields", "請補上要處理的行程名稱、日期或時間。", index, missing_fields=["target_hint"])
    resolution_kind = "recent_reference" if use_recent_reference else "exact"
    candidates: list[dict[str, Any]] = []
    if use_recent_reference:
        from services.calendar_service import resolve_owned_event_reference
        event, resolution = resolve_owned_event_reference(ctx.user_id, recent_reference or {})
    else:
        references = _clock_temporal_references(ctx)
        if references:
            event, resolution = resolve_owned_event(
                ctx.user_id, target_hint, temporal_references=references,
            )
        else:
            event, resolution = resolve_owned_event(ctx.user_id, target_hint)
        candidates = get_owned_event_resolution_candidates(ctx.user_id, target_hint)
        if resolution == "fuzzy_suggestion" or get_owned_event_resolution_kind(ctx.user_id, target_hint) == "fuzzy_suggestion":
            resolution_kind = "fuzzy_suggestion"
    if resolution == "ambiguous" and candidates:
        projections = _remember_candidate_projection(ctx.user_id, candidates)
        return _clarification(
            "ambiguous", "我找到幾筆相近的行程，請告訴我你要處理哪一筆。", index, query=target_hint,
            searched_count=len(candidates), candidates=projections,
        )
    if resolution == "ambiguous":
        return _clarification("ambiguous", "我找到不只一筆符合的行程，請補上日期或完整名稱。", index)
    if not event and not use_recent_reference:
        return _clarification(
            "not_found", "我沒有找到相近的行程，大概是哪一天或是在做什麼？", index,
            query=target_hint, searched_count=0,
        )
    if resolution == "stale_revision":
        return _clarification("stale_revision", "剛剛提到的那筆行程已經有變動，請重新告訴我想處理哪一筆。", index)
    if not event:
        return _clarification("not_found", "我沒有找到相近的行程，大概是哪一天或是在做什麼？", index)
    if resolution_kind == "fuzzy_suggestion":
        # Fuzzy retrieval is useful for typo tolerance, but it is never enough
        # authority for a destructive mutation.  Store the bounded candidate as
        # an opaque server reference and require the user to select it.
        fuzzy_candidates = candidates or [event]
        projections = _remember_candidate_projection(ctx.user_id, fuzzy_candidates[:1])
        label = _event_label(event)
        return _clarification(
            "ambiguous", f"你是指 {label} 嗎？", index,
            query=target_hint, searched_count=len(fuzzy_candidates), candidates=projections,
        )
    plan, result = _plan_for_event(ctx, command, event, index, resolution_kind=resolution_kind)
    return result or CalendarPreflightResult(status="denied")


def preflight_calendar_commands(
    ctx: Any,
    commands: list[CalendarCommand],
    *,
    recent_references: dict[int, dict[str, Any]] | None = None,
) -> CalendarPreflightResult:
    """Resolve and validate a command batch exactly once before confirmation."""
    if not calendar_access_enabled(ctx.user_id):
        return CalendarPreflightResult(
            status="denied", denial_code="calendar_access_denied",
            preview="你目前沒有授權我存取行事曆。",
        )
    if not commands:
        return _clarification("missing_fields", "我還不確定你想怎麼安排行程。", 0)

    all_plans: list[CalendarMutationPlan] = []
    previews: list[str] = []
    for index, command in enumerate(commands):
        result = _preflight_one(
            ctx,
            command,
            index,
            recent_reference=(recent_references or {}).get(index),
        )
        if result.status != "ready":
            return result
        all_plans.extend(result.plans)
        if result.preview:
            previews.append(result.preview)
    return CalendarPreflightResult(
        status="ready", plans=all_plans, preview="\n".join(previews) + " 回覆「確認」才會真的變更。",
    )
