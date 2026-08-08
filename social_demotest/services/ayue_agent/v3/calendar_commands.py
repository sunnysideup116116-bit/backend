"""Typed Calendar commands and the deterministic V3 mutation preflight.

The Calendar sub-agent only produces :class:`CalendarCommand` values.  This
module is the authority boundary that turns those untrusted descriptions into
server-owned :class:`CalendarMutationPlan` values.  Plans are executor-only;
they must never be included in an agent context or observation.
"""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

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
    aliases = {
        "type": "action", "operation": "action", "summary": "title",
        "activity": "title", "name": "title", "event_hint": "target_hint",
        "event_hints": "target_hints", "start": "start_time", "end": "end_time",
    }
    action_aliases = {
        "add": "create", "new": "create", "edit": "update", "modify": "update",
        "delete": "cancel", "remove": "cancel",
    }
    normalized_commands: list[dict[str, Any]] = []
    for raw in raw_commands:
        if not isinstance(raw, dict):
            raise ValueError("each calendar command must be an object")
        command = dict(raw)
        for alias, canonical in aliases.items():
            if alias in command and canonical not in command:
                command[canonical] = command.pop(alias)
        if isinstance(command.get("action"), str):
            action = command["action"].strip().lower()
            command["action"] = action_aliases.get(action, action)
        normalized_commands.append(command)
    return {"commands": normalized_commands}


class CalendarCommand(BaseModel):
    """LLM-owned, authority-free description of one calendar mutation."""

    model_config = ConfigDict(extra="forbid")

    action: CalendarAction = Field(description="create、update、cancel、cancel_selected 或 cancel_all_upcoming")
    target_hint: str | None = Field(default=None, max_length=120, description="update/cancel 的自然語言行程描述，不要填 server 內部識別欄位")
    target_reference: Literal["recent_event", "candidate_1", "candidate_2", "candidate_3"] | None = Field(
        default=None,
        description="使用 context 中最近一次唯一選取的行程；只能填 recent_event",
    )
    target_hints: list[str] = Field(default_factory=list, max_length=10, description="cancel_selected 的 2–10 個自然語言行程描述")
    title: str | None = Field(default=None, max_length=120, description="活動名稱，例如「去日本」；不要包含日期或時間")
    date: str | None = Field(default=None, max_length=32, description="優先使用 YYYY-MM-DD；今天／明天／後天也可由 server 依 authoritative clock 轉換")
    start_time: str | None = Field(default=None, max_length=16, description="開始時間，使用 HH:MM")
    end_time: str | None = Field(default=None, max_length=16, description="結束時間，使用 HH:MM")
    timezone: str | None = Field(default=None, max_length=64, description="時區；未提供時由 server 使用 Asia/Taipei")
    location: str | None = Field(default=None, max_length=160, description="可選地點")
    notes: str | None = Field(default=None, max_length=500, description="可選備註")
    draft_mode: CalendarDraftMode = Field(
        default="none",
        description="補齊既有 Calendar 草稿用 continue；新請求用 replace；一般用 none",
    )

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


class CalendarPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "needs_clarification", "denied"]
    plans: list[CalendarMutationPlan] = Field(default_factory=list, max_length=10)
    clarification: NeedsClarification | None = None
    preview: str = ""
    denial_code: str | None = None


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
    for key in labels:
        value = str(getattr(command, key) or "").strip()
        if not value or (key == "title" and value in generic_titles):
            missing.append(key)
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
    for term, resolved in references.items():
        if raw.startswith(term) and len(raw) <= len(term) + 4:
            return resolved, None
    if raw in {"本週", "這週", "下週", "本月", "下個月"}:
        return raw, "「%s」不是單一日期，請補上明確的年月日。" % raw
    return raw, None


def _plan_for_event(
    ctx: Any, command: CalendarCommand, event: dict[str, Any], index: int,
    *, resolution_kind: str = "exact",
) -> tuple[CalendarMutationPlan | None, CalendarPreflightResult | None]:
    user_id = ctx.user_id
    action = "cancel" if command.action == "cancel" else "update"
    source_type = str(event.get("source_type") or "personal")
    if action == "update" and source_type == "date" and event.get("status") != "confirmed":
        return None, _clarification(
            "invalid_interval",
            "這筆共同約會正在等待重新確認，請先完成或取消目前的改期。",
            index,
        )

    changes = _command_changes(command)
    if "date" in changes:
        canonical_date, date_error = _canonicalize_date(ctx, changes["date"])
        if date_error:
            return None, _clarification("invalid_date", date_error, index)
        changes["date"] = canonical_date
    if action == "update" and not changes:
        return None, _clarification("missing_fields", f"你想把「{_event_label(event)}」改成什麼呢？", index)

    proposed_form: dict[str, Any] = {}
    if action == "update":
        current = _base_current_form(event)
        if source_type == "date" and "title" in changes:
            changes = {**changes, "activity": changes.pop("title")}
        proposed_form = normalize_form({**current, **changes})
        try:
            start_at, end_at, _ = _parse_local_interval(proposed_form)
        except HTTPException as exc:
            return None, _clarification("invalid_interval", str(exc.detail), index)
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
        canonical_date, date_error = _canonicalize_date(ctx, command.date)
        if date_error:
            return _clarification("invalid_date", date_error, index)
        form = normalize_form({
            "title": command.title, "date": canonical_date,
            "start_time": command.start_time, "end_time": command.end_time,
            "timezone": command.timezone or "Asia/Taipei",
            "location": command.location or "", "notes": command.notes or "",
        })
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
            "ambiguous", "", index, query=target_hint,
            searched_count=len(candidates), candidates=projections,
        )
    if resolution == "ambiguous":
        return _clarification("ambiguous", "我找到不只一筆符合的行程，請補上日期或完整名稱。", index)
    if not event and not use_recent_reference:
        return _clarification("not_found", "", index, query=target_hint, searched_count=0)
    if resolution == "stale_revision":
        return _clarification("stale_revision", "剛剛提到的那筆行程已經有變動，請重新告訴我想處理哪一筆。", index)
    if not event:
        return _clarification("not_found", "我找不到這筆自己的行程，請補上日期或名稱。", index)
    plan, result = _plan_for_event(ctx, command, event, index, resolution_kind=resolution_kind)
    if result and resolution_kind == "fuzzy_suggestion" and plan:
        result.preview = f"我找到名稱相近的「{plan.safe_label}」，你是要變更這筆嗎？\n{result.preview}"
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
