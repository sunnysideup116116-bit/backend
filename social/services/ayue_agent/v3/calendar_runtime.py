"""Bounded Calendar runtime owned by the Calendar domain.

The Calendar agent remains semantic/LLM-only.  This module owns the server-side
interpretation of its authority-free read proposals and typed mutation
commands, including references, drafts, preflight, and confirmation creation.
"""

from __future__ import annotations

import re
from typing import Any, Callable

from services.ayue_agent.contracts import ToolCall
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.tool_registry import TOOL_REGISTRY, ToolRisk

from .calendar_commands import canonicalize_calendar_command, preflight_calendar_commands
from .calendar_drafts import (
    candidate_reference_allowed,
    clear_draft,
    get_draft,
    merge_command,
    resolved_target_replaced,
    save_draft,
)
from .place_followups import (
    PlaceFollowupPersistenceError,
    clear_followup as clear_place_followup,
    get_followup as get_place_followup,
    merge_command as merge_place_followup,
    save_followup as save_place_followup,
)
from .calendar_references import (
    DRAFT_TARGET_REFERENCE_KEY,
    clear_reference,
    get_reference,
    remember_candidates,
    remember_event,
    remember_resolved_target,
    public_projection,
)
from .place_references import get_candidate as get_place_candidate
from .contracts import SubTask, SubTaskResult, SubTaskStatus
from .debug_trace import append_event as append_debug_event
from .guard import guard_calendar_commands
from .guarded_execution import GuardedReadExecutor
from .runtime_registry import TaskRunnerResult
from .sub_agents.base import SubAgentMetrics
from .sub_agents.calendar_agent import run as run_calendar
from services.calendar_service import resolve_owned_event_with_candidates


_CALENDAR_READ_TOOLS = frozenset(
    name for name, spec in TOOL_REGISTRY.items()
    if name.startswith("calendar.") and spec.risk is ToolRisk.READ
)

ConfirmationCreator = Callable[..., Any]
SupersedeConfirmation = Callable[..., dict[str, Any]]
RunnerInvoker = Callable[..., Any]
MetricsPrinter = Callable[[str, Any], Any]


def _availability_protocol_failure(task_id: str, *, reason: str) -> SubTaskResult:
    return SubTaskResult(
        task_id=task_id,
        status=SubTaskStatus.FAILED,
        error_code="calendar_availability_protocol_invalid",
        observation={"failure": {"code": reason}},
    )


def _attach_availability_outcome(
    task: SubTask, results: list[SubTaskResult],
) -> list[SubTaskResult]:
    """Attach the Calendar-owned closed outcome to one successful list read."""
    if task.outcome_contract != "calendar.availability.v1":
        return results
    if len(results) != 1:
        return [_availability_protocol_failure(task.id, reason="expected_one_read_result")]
    result = results[0]
    if result.status is not SubTaskStatus.OK:
        # Technical failure is deliberately not converted into a business
        # outcome; hard-gated downstream tasks fail closed.
        return results
    observation = result.observation or {}
    events = observation.get("events")
    if not isinstance(events, list):
        return [_availability_protocol_failure(task.id, reason="events_projection_missing")]
    event_count = len(events)
    code = "calendar.no_scheduled_events" if event_count == 0 else "calendar.has_scheduled_events"
    result.observation = {
        **observation,
        "availability": {
            "schema_version": "calendar.availability.v1",
            "state": "no_scheduled_events" if event_count == 0 else "has_scheduled_events",
            "event_count": event_count,
        },
    }
    result.outcome_codes = [code]
    return results


def run(
    context_slice: Any,
    *,
    task: SubTask,
    services: GuardedReadExecutor,
) -> tuple[TaskRunnerResult, SubAgentMetrics | None]:
    """Run Calendar semantics and server-owned orchestration as one runtime.

    The Scheduler supplies a task-bound guarded adapter carrying only the
    current turn services.  Calendar's semantic agent remains the injected
    ``run_calendar`` seam; all command, draft, reference, preflight, and
    confirmation interpretation stays in this module.
    """
    semantic_runner = getattr(services, "semantic_runner_override", None) or run_calendar
    def invoke_semantic_runner(_runner: Any, _context_slice: Any, _task: SubTask, _services: Any) -> Any:
        del _runner, _services
        return semantic_runner(_context_slice, task_brief=_task.task_brief)

    if (
        task.outcome_contract is None
        and getattr(services.turn_ctx, "_place_selection_requires_calendar_create", False)
    ):
        context_slice = _copy_context_with_place_create_hint(context_slice)
    if task.outcome_contract == "calendar.availability.v1":
        # This marker is an internal context hint only.  The runtime still
        # validates the returned proposal and never trusts the model to choose
        # an outcome or a mutation capability.
        if hasattr(context_slice, "model_copy"):
            context_slice = context_slice.model_copy(deep=True)
        context_slice.payload = dict(getattr(context_slice, "payload", {}) or {})
        context_slice.payload["_calendar_availability_only"] = True
    results, metrics = run_task(
        context_slice,
        task=task,
        turn_ctx=services.turn_ctx,
        prior_observations=list(getattr(services, "prior_observations", []) or []),
        runner=semantic_runner,
        services=services,
        invoke_runner=invoke_semantic_runner,
        max_reads=int(getattr(services, "max_reads", 3) or 3),
        run_id=services.run_id,
        trace=services.trace,
        debug_enabled=services.debug_enabled,
        # Scheduler owns the centralized metrics envelope for all registered
        # runtimes; avoid printing Calendar metrics twice at this boundary.
        print_llm_metrics=lambda _label, _metrics: None,
        create_confirmation=getattr(services, "create_confirmation", None) or (lambda **_kwargs: None),
        supersede_confirmation=(
            getattr(services, "supersede_confirmation", None)
            if callable(getattr(services, "supersede_confirmation", None))
            else None
        ),
    )
    return TaskRunnerResult.from_completed(results), metrics


def direct_chat_block_reason(turn: Any) -> str | None:
    """Return the Calendar-owned fast-path blocker, if one is active."""
    if getattr(turn, "calendar_draft", None):
        return "calendar_draft"
    if getattr(turn, "calendar_recent_mutation", None):
        return "recent_calendar_mutation"
    return None


def confirmed_state_changed(results: list[dict[str, Any]]) -> bool:
    """Project Calendar mutation success into the public response hint."""
    return any(
        bool(item.get("ok")) and str(item.get("tool_name") or "").startswith("calendar.")
        for item in results if isinstance(item, dict)
    )


def confirmed_result_projection(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the allowlisted Calendar update for the public AgentResult."""
    return {"calendar_state_changed": confirmed_state_changed(results)}


def _calendar_reference_for_command(
    user_id: str,
    command: Any,
    draft: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Load one server reference after validating its opaque draft token."""
    if str(getattr(command, "action", "") or "") not in {"update", "cancel"}:
        return None
    if (
        draft
        and (draft.get("resolved_target") or {}).get("bound")
        and str(getattr(command, "draft_mode", "") or "") == "continue"
        and not getattr(command, "target_reference", None)
    ):
        reference = get_reference(user_id, reference_key=DRAFT_TARGET_REFERENCE_KEY)
        if reference:
            reference["_force"] = True
            reference["_draft_bound"] = True
            return reference
    reference_key = str(getattr(command, "target_reference", "") or "recent_event")
    if reference_key.startswith("candidate_") and not candidate_reference_allowed(draft, reference_key):
        return None
    return get_reference(user_id, reference_key=reference_key)


def _invalid_command_result(task_id: str, clarification: Any) -> SubTaskResult:
    return SubTaskResult(
        task_id=task_id,
        status=SubTaskStatus.OK,
        tool_name="calendar.submit_commands",
        observation={
            "calendar_command_result": {
                "status": "needs_clarification",
                "clarification": {
                    "code": str(clarification.get("code") or "invalid_command"),
                    "message": str(
                        clarification.get("message")
                        or "這次行程指令格式無法驗證，請重新描述需求。"
                    ),
                    "command_index": 0,
                    "missing_fields": [],
                },
            }
        },
    )


_PLACE_RECHECK_MARKERS = re.compile(
    r"店|餐廳|飲料|奶茶|咖啡|酒吧|公園|地點|那間|那家|這間|這家|候選|推薦地點",
)
_PLACE_ACTION_RECHECK_MARKERS = re.compile(r"加到|加入|記進|記到|放進|排入|安排|排一下")
_LEGACY_NON_PLACE_MARKERS = re.compile(
    r"提醒|通知|訊息|問題|公司|學校|部門|工作|郵件|信件|聯絡人|朋友|會議|任務|人事",
)
_GENERIC_PLACE_TITLES = frozenset({
    "地點", "餐廳", "店家", "這間店", "那間店", "行程", "一筆行程", "行事曆", "日曆",
})
_LEGACY_ORDINAL_RE = re.compile(
    r"(?<![0-9零〇一二三四五六七八九十百])(?:第\s*)?"
    r"([0-9]+|[零〇一二三四五六七八九十百]+)\s*"
    r"(?:個(?!\s*(?:星期|禮拜|週|周|月|年|天))|家|間)"
)
_LEGACY_LAST_RE = re.compile(r"最後(?:一)?(?:個|家|間)")
_LEGACY_NUMBERED_LINE_RE = re.compile(
    r"^\s*(?:[-*•]\s*)?(?:([0-9]+|[零〇一二三四五六七八九十百]+)\s*[.、)]\s*)(.+)$"
)
_LEGACY_BULLET_LINE_RE = re.compile(r"^\s*[-*•]\s+(.+)$")
_CHINESE_PLACE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _parse_legacy_ordinal(value: str) -> int:
    token = str(value or "").strip()
    if token.isdigit():
        try:
            return int(token)
        except ValueError:
            return 0
    if not token or any(char not in _CHINESE_PLACE_DIGITS and char != "十" and char != "百" for char in token):
        return 0
    if "百" in token:
        hundreds, _, remainder = token.partition("百")
        if not hundreds:
            hundreds = "一"
        result = _CHINESE_PLACE_DIGITS.get(hundreds[-1], 0) * 100
        if remainder:
            result += _parse_legacy_ordinal(remainder)
        return result
    if "十" in token:
        tens, _, ones = token.partition("十")
        tens_value = _CHINESE_PLACE_DIGITS.get(tens[-1], 1) if tens else 1
        ones_value = _CHINESE_PLACE_DIGITS.get(ones, 0) if ones else 0
        return tens_value * 10 + ones_value
    if len(token) == 1:
        return _CHINESE_PLACE_DIGITS.get(token, 0)
    return 0


def _requested_legacy_ordinal(message: str) -> int | None:
    """Return one complete place ordinal, or ``None`` for ambiguous input."""
    last_matches = list(_LEGACY_LAST_RE.finditer(message))
    ordinal_matches = [
        match for match in _LEGACY_ORDINAL_RE.finditer(message)
        if not any(last.start() <= match.start() < last.end() for last in last_matches)
    ]
    if len(ordinal_matches) > 1 or (ordinal_matches and last_matches):
        return 0
    if last_matches:
        return -1
    if not ordinal_matches:
        return None
    return _parse_legacy_ordinal(ordinal_matches[0].group(1))


def _clean_legacy_place_label(value: str) -> str:
    label = str(value or "").strip()
    label = re.sub(r"\*\*(.+?)\*\*", r"\1", label)
    label = re.sub(r"[（(][^）)]*[）)]", "", label)
    label = re.split(r"\s+[—–-]\s+", label, maxsplit=1)[0]
    return label.strip(" -—:：")[:160]


def _legacy_place_list(content: str) -> tuple[dict[int, str], list[str]]:
    """Extract visible numbered and bullet rows from one old assistant reply."""
    numbered: dict[int, str] = {}
    bullets: list[str] = []
    saw_numbered = False
    for line in str(content or "").splitlines():
        numbered_match = _LEGACY_NUMBERED_LINE_RE.match(line)
        if numbered_match:
            saw_numbered = True
            ordinal = _parse_legacy_ordinal(numbered_match.group(1))
            label = _clean_legacy_place_label(numbered_match.group(2))
            if ordinal > 0 and label:
                numbered.setdefault(ordinal, label)
            continue
        bullet_match = _LEGACY_BULLET_LINE_RE.match(line)
        if bullet_match:
            label = _clean_legacy_place_label(bullet_match.group(1))
            if label:
                bullets.append(label)
    return numbered if saw_numbered else {}, bullets


def _has_legacy_place_list(turn_ctx: Any) -> bool:
    for item in getattr(turn_ctx, "recent_messages", []) or []:
        if not isinstance(item, dict) or str(item.get("role") or "") != "assistant":
            continue
        content = str(item.get("content") or "")
        numbered, bullets = _legacy_place_list(content)
        labels = list(numbered.values()) or bullets
        if labels and (
            "推薦地點" in content
            or any(_PLACE_RECHECK_MARKERS.search(label) for label in labels)
        ):
            return True
    return False


def _has_legacy_place_evidence(turn_ctx: Any) -> bool:
    """Decide whether a text-only create must prove its place before use."""
    message = str(getattr(turn_ctx, "message", "") or "")
    has_place_word = bool(_PLACE_RECHECK_MARKERS.search(message))
    if _LEGACY_NON_PLACE_MARKERS.search(message) and not has_place_word:
        return False
    ordinal = _requested_legacy_ordinal(message)
    if ordinal is not None:
        return _has_legacy_place_list(turn_ctx)
    return bool(
        _has_legacy_place_list(turn_ctx)
        and (_PLACE_ACTION_RECHECK_MARKERS.search(message) or has_place_word)
    )


def _clear_unverified_place_fields(command: Any) -> Any:
    """Return the same typed command without an unverified place identity."""
    values = command.model_dump(exclude_none=True)
    values.pop("title", None)
    values.pop("location", None)
    return command.__class__.model_validate(values)


def _place_recheck_query(turn_ctx: Any, command: Any) -> str:
    """Find an explicit old place clue that is safe to send to Places resolve."""
    if str(getattr(command, "action", "")) != "create":
        return ""
    message = str(getattr(turn_ctx, "message", "") or "")
    requested_ordinal = _requested_legacy_ordinal(message)

    # A pre-rollout assistant reply may still be the only source of the place
    # name.  Resolve the user's ordinal against the nearest visible list, then
    # ask Places to establish identity; never create a provider ref from text.
    message_key = _compact_place_text(message)
    for item in reversed(getattr(turn_ctx, "recent_messages", []) or []):
        if not isinstance(item, dict) or str(item.get("role") or "") != "assistant":
            continue
        content = str(item.get("content") or "")
        numbered, bullets = _legacy_place_list(content)
        labels = list(numbered.values()) or bullets
        if not labels:
            continue
        if requested_ordinal is not None:
            if requested_ordinal == 0:
                return ""
            if requested_ordinal == -1:
                return labels[-1][:160]
            if numbered:
                return numbered.get(requested_ordinal, "")[:160]
            if 1 <= requested_ordinal <= len(bullets):
                return bullets[requested_ordinal - 1][:160]
            return ""
        for clue in labels:
            clue_key = _compact_place_text(clue)
            if len(clue_key) >= 2 and clue_key in message_key:
                return clue[:160]

    title = str(getattr(command, "title", "") or "").strip()
    location = str(getattr(command, "location", "") or "").strip()
    query = location or title
    if len(query) < 2 or query in _GENERIC_PLACE_TITLES:
        return ""
    if _PLACE_RECHECK_MARKERS.search(message):
        return query[:160]
    if (
        getattr(turn_ctx, "place_followup", None)
        and _PLACE_ACTION_RECHECK_MARKERS.search(message)
        and query in message
    ):
        return query[:160]
    # A legacy assistant recommendation may be the only place evidence left
    # after a restart. Use its public text as a clue, but still require the
    # live Places resolver to establish the provider identity.
    for item in reversed(getattr(turn_ctx, "recent_messages", []) or []):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "")
        if role == "assistant" and query in content and _PLACE_RECHECK_MARKERS.search(content):
            return query[:160]
    return ""


def _compact_place_text(value: Any) -> str:
    text = re.sub(r"[\s\W_]+", "", str(value or ""), flags=re.UNICODE)
    return text.casefold()


def _recheck_old_place_for_create(
    turn_ctx: Any,
    command: Any,
    *,
    require_verification: bool = False,
) -> tuple[Any | None, dict[str, Any] | None]:
    """Revalidate a legacy text-only place before allowing Calendar create."""
    query = _place_recheck_query(turn_ctx, command)
    if not query:
        if not require_verification:
            return command, None
        try:
            return _clear_unverified_place_fields(command), None
        except Exception:
            return None, {
                "code": "invalid_command",
                "message": "這次地點行程的格式無法安全驗證，請重新描述。",
            }
    try:
        result = execute_tool(
            ToolCall(
                name="places.resolve_place",
                arguments={"query": query},
            ),
            turn_ctx,
            clock=getattr(turn_ctx, "clock", None),
        )
    except Exception:
        result = None
    data = getattr(result, "data", None) if result is not None else None
    place = data.get("place") if isinstance(data, dict) else None
    label = str(place.get("name") or "").strip() if isinstance(place, dict) else ""
    if getattr(result, "ok", False) and data and data.get("found") and label:
        values = command.model_dump(exclude_none=True)
        values["title"] = label[:80]
        values["location"] = label[:80]
        try:
            return command.__class__.model_validate(values), None
        except Exception:
            pass
    # The text may still be a valid Calendar request, but its place identity
    # was not verified. Let preflight save the date/time and ask for a place.
    try:
        return _clear_unverified_place_fields(command), None
    except Exception:
        return None, {
            "code": "invalid_command",
            "message": "這次地點行程的格式無法安全驗證，請重新描述。",
        }


def _bind_resolved_place_to_create(
    turn_ctx: Any,
    command: Any,
) -> tuple[Any | None, dict[str, Any] | None]:
    """Bind a verified place or preserve a typed draft while it is unresolved."""
    resolution = getattr(turn_ctx, "place_reference_resolution", None)
    if str(getattr(command, "action", "")) != "create":
        return command, None
    if not isinstance(resolution, dict) or not str(resolution.get("status") or ""):
        followup = getattr(turn_ctx, "place_followup", None)
        resolved_place = followup.get("resolved_place") if isinstance(followup, dict) else None
        trusted_reference = (
            str(resolved_place.get("reference") or "").strip()
            if isinstance(resolved_place, dict) else ""
        )
        if trusted_reference:
            candidate = get_place_candidate(
                turn_ctx.user_id,
                turn_ctx.room_id,
                trusted_reference,
            )
            if candidate:
                return _bind_place_candidate(command, candidate)
            # A previously trusted ref is not permission to replace the venue
            # with a fresh Places search. Keep the typed date/time and ask for
            # a new place if the immutable snapshot is inaccessible.
            try:
                return _clear_unverified_place_fields(command), None
            except Exception:
                return None, {
                    "code": "invalid_command",
                    "message": "這次地點行程的格式無法安全驗證，請重新描述。",
                }
        return _recheck_old_place_for_create(
            turn_ctx,
            command,
            require_verification=_has_legacy_place_evidence(turn_ctx),
        )
    resolution_status = str(resolution.get("status") or "")
    if resolution_status in {"missing_snapshot", "source_unavailable", "unavailable"}:
        return _recheck_old_place_for_create(
            turn_ctx, command, require_verification=True,
        )
    if resolution_status != "resolved":
        # An ordinal/name ambiguity is not permission to use the Calendar
        # model's guessed title. Clear only the unverified place fields; the
        # Calendar preflight will retain date/time and save a draft asking for
        # the missing place.
        if resolution_status in {
            "ambiguous", "invalid_ordinal", "invalid_selection",
            "unavailable", "missing_snapshot", "source_unavailable",
            "storage_unavailable",
        }:
            try:
                return _clear_unverified_place_fields(command), None
            except Exception:
                return None, {
                    "code": "invalid_command",
                    "message": "這次地點行程的格式無法安全驗證，請重新描述。",
                }
        return command, None
    reference = str(resolution.get("reference") or "")
    candidate = get_place_candidate(
        turn_ctx.user_id,
        turn_ctx.room_id,
        reference,
    )
    if not candidate:
        # Historical snapshots are durable, but a deleted or inaccessible
        # record must still leave the user's typed time in a Calendar draft.
        try:
            return _clear_unverified_place_fields(command), None
        except Exception:
            return None, {
                "code": "invalid_command",
                "message": "這次地點行程的格式無法安全驗證，請重新描述。",
            }

    label = str(candidate.get("label") or "").strip()[:80]
    if not label:
        return None, {
            "code": "not_found",
            "message": "剛才選定的地點目前不可用，請重新選擇。",
        }
    return _bind_place_candidate(command, candidate)


def _bind_place_candidate(command: Any, candidate: dict[str, Any]) -> tuple[Any | None, dict[str, Any] | None]:
    """Copy a server-owned candidate label into a Calendar create form."""
    label = str(candidate.get("label") or "").strip()[:80]
    if not label:
        return None, {
            "code": "not_found",
            "message": "剛才選定的地點目前不可用，請重新選擇。",
        }
    values = command.model_dump(exclude_none=True)
    # A resolved place is the selected activity for this create. Always use
    # the server-owned label so the Calendar preview cannot retain a model
    # guess (for example, the first item) after the user selected item two.
    values["title"] = label
    values["location"] = label
    try:
        return command.__class__.model_validate(values), None
    except Exception:
        return None, {
            "code": "invalid_command",
            "message": "這次地點行程的格式無法安全驗證，請重新描述。",
        }


def _place_resolution_for_followup(
    turn_ctx: Any,
    resolution: dict[str, Any] | None,
    prior_record: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """Build the private, allowlisted place binding saved with a draft."""
    current: dict[str, Any] | None = None
    if isinstance(resolution, dict) and str(resolution.get("status") or "") == "resolved":
        reference = str(resolution.get("reference") or "").strip()
        label = str(resolution.get("label") or "").strip()
        if reference and label:
            current = {
                "status": "resolved",
                "reference": reference,
                "label": label,
            }
            if resolution.get("ordinal") not in (None, ""):
                current["ordinal"] = resolution.get("ordinal")

    prior = prior_record.get("resolved_place") if isinstance(prior_record, dict) else None
    if current is None and isinstance(prior, dict) and prior.get("reference") and prior.get("label"):
        current = {
            "status": "resolved",
            "reference": str(prior.get("reference") or "").strip(),
            "label": str(prior.get("label") or "").strip(),
        }
        if prior.get("ordinal") not in (None, ""):
            current["ordinal"] = prior.get("ordinal")

    if current is None:
        return None
    raw_origin = getattr(turn_ctx, "_place_resolution_origin_run_id", "")
    origin = raw_origin.strip() if isinstance(raw_origin, str) else ""
    if not origin and isinstance(prior, dict):
        prior_origin = prior.get("origin_run_id")
        origin = prior_origin.strip() if isinstance(prior_origin, str) else ""
    if origin:
        current["origin_run_id"] = origin
    return current


def _copy_context_with_place_create_hint(context_slice: Any) -> Any:
    """Copy a prompt slice and add only the internal create protocol bit."""
    copied = context_slice.model_copy(deep=True) if hasattr(context_slice, "model_copy") else context_slice
    copied.payload = {
        **dict(getattr(copied, "payload", {}) or {}),
        "_place_selection_requires_create": True,
    }
    return copied


def _aggregate_agent_metrics(
    primary: SubAgentMetrics | None,
    secondary: SubAgentMetrics | None,
) -> SubAgentMetrics | None:
    """Aggregate two semantic calls while retaining the latest raw payload."""
    if primary is None:
        return secondary
    if secondary is None:
        return primary
    primary.input_tokens += int(getattr(secondary, "input_tokens", 0) or 0)
    primary.output_tokens += int(getattr(secondary, "output_tokens", 0) or 0)
    primary.duration_ms += int(getattr(secondary, "duration_ms", 0) or 0)
    primary_count = int(getattr(primary, "llm_call_count", 0) or 0) or 1
    secondary_count = int(getattr(secondary, "llm_call_count", 0) or 0) or 1
    primary.llm_call_count = primary_count + secondary_count
    primary.tool_calls_raw.extend(list(getattr(secondary, "tool_calls_raw", []) or []))
    primary.rejected_calls.extend(list(getattr(secondary, "rejected_calls", []) or []))
    primary.llm_requests.extend(list(getattr(secondary, "llm_requests", []) or []))
    primary.prompt_raw = str(getattr(secondary, "prompt_raw", "") or primary.prompt_raw)
    primary.content_raw = str(getattr(secondary, "content_raw", "") or primary.content_raw)
    primary.tools_raw = list(getattr(secondary, "tools_raw", []) or primary.tools_raw)
    primary.input_payload = dict(
        getattr(secondary, "input_payload", {}) or primary.input_payload
    )
    primary.error = str(getattr(secondary, "error", "") or "")
    return primary


_PLACE_CREATE_RETRY_MESSAGE = (
    "這次已選定地點，但沒有收到可確認的新增行程指令；行事曆沒有變更。"
    "請再說一次要安排的日期與時間。"
)


def _run_calendar_reads(
    *,
    task: SubTask,
    turn_ctx: Any,
    proposals: list[Any],
    prior_observations: list[dict[str, Any]],
    services: GuardedReadExecutor,
    max_reads: int,
) -> list[SubTaskResult]:
    results: list[SubTaskResult] = []
    read_step_count = 0
    for index, proposal in enumerate(proposals):
        print(f"  [{task.id}#{index}] proposal: tool={proposal.tool_name}")
        outcome = services.execute(
            proposal,
            allowed_tools=_CALENDAR_READ_TOOLS,
            step_count=read_step_count,
            max_reads=max_reads,
            prior_observations=prior_observations,
            call_index=index,
        )
        if outcome.attempted:
            read_step_count += 1
        result = outcome.result
        if result.status is not SubTaskStatus.OK:
            if result.error_code:
                print(f"  [{task.id}#{index}] result=FAILED  error_code={result.error_code}")
            results.append(result)
            continue

        private_data = outcome.private_data or {}
        reference_payload = (
            private_data.get("calendar_event_reference")
            if isinstance(private_data, dict) else None
        )
        if isinstance(reference_payload, dict):
            event = reference_payload.get("event")
            if isinstance(event, dict):
                remember_event(
                    turn_ctx.user_id,
                    event,
                    safe_label=str(reference_payload.get("safe_label") or ""),
                )
        if result.tool_name in {"calendar.find_my_event", "calendar.list_my_events"}:
            if not reference_payload:
                # A new ambiguous/not-found/list-of-many read must not leave a
                # previous referent armed for a later terse mutation.
                clear_reference(turn_ctx.user_id)
        print(f"  [{task.id}#{index}] result=OK")
        results.append(result)
    return results


def run_task(
    context_slice: Any,
    *,
    task: SubTask,
    turn_ctx: Any,
    prior_observations: list[dict[str, Any]],
    runner: Callable[..., Any],
    services: GuardedReadExecutor,
    invoke_runner: RunnerInvoker,
    max_reads: int,
    run_id: str,
    trace: dict[str, Any],
    debug_enabled: bool,
    print_llm_metrics: MetricsPrinter,
    create_confirmation: ConfirmationCreator,
    supersede_confirmation: SupersedeConfirmation | None = None,
) -> tuple[list[SubTaskResult], SubAgentMetrics | None]:
    """Run one Calendar task while keeping authority in server code."""
    try:
        proposals, agent_metrics = invoke_runner(
            runner, context_slice, task, services,
        )
    except Exception as exc:
        agent_metrics = SubAgentMetrics(error=str(exc))
        print(f"  [{task.id}] sub_agent EXCEPTION: {type(exc).__name__}")
        return [
            SubTaskResult(
                task_id=task.id,
                status=SubTaskStatus.FAILED,
                error_code="sub_agent_exception",
            )
        ], agent_metrics

    calendar_commands = list(getattr(proposals, "commands", []) or [])
    calendar_command_errors = list(getattr(proposals, "command_errors", []) or [])
    place_create_required = bool(
        task.outcome_contract is None
        and (
            bool((getattr(context_slice, "payload", {}) or {}).get(
                "_place_selection_requires_create"
            ))
            or getattr(turn_ctx, "_place_selection_requires_calendar_create", False)
        )
    )
    if place_create_required and not calendar_commands:
        # A model may answer the resolved-place create task with a read-only
        # Calendar proposal. Retry once with a private protocol hint; neither
        # the first read nor a second read is allowed to become a fake preview.
        retry_context = _copy_context_with_place_create_hint(context_slice)
        try:
            retry_proposals, retry_metrics = invoke_runner(
                runner, retry_context, task, services,
            )
        except Exception as exc:
            retry_proposals = None
            retry_metrics = SubAgentMetrics(error=str(exc))
        agent_metrics = _aggregate_agent_metrics(agent_metrics, retry_metrics)
        retry_commands = list(getattr(retry_proposals, "commands", []) or [])
        if not retry_commands:
            proposals = []
            calendar_commands = []
            calendar_command_errors = [{
                "code": "invalid_command",
                "message": _PLACE_CREATE_RETRY_MESSAGE,
            }]
            if agent_metrics:
                print_llm_metrics(f"{task.id}:{task.agent}", agent_metrics)
            print(f"  [{task.id}] result=FAILED  reason=place_create_protocol_invalid")
            return [_invalid_command_result(task.id, calendar_command_errors[0])], agent_metrics
        # The retry is only authoritative for the typed mutation. Discard all
        # read proposals from the first response and from the retry response.
        proposals = []
        calendar_commands = retry_commands
        calendar_command_errors = list(getattr(retry_proposals, "command_errors", []) or [])

    if agent_metrics:
        print_llm_metrics(f"{task.id}:{task.agent}", agent_metrics)
        if agent_metrics.error:
            print(f"  [{task.id}] error=sub_agent_failed")

    if task.outcome_contract == "calendar.availability.v1":
        if calendar_commands or len(list(proposals or [])) != 1:
            return [_availability_protocol_failure(task.id, reason="expected_one_calendar_list")], agent_metrics
        only_proposal = list(proposals or [])[0]
        if getattr(only_proposal, "tool_name", "") != "calendar.list_my_events":
            return [_availability_protocol_failure(task.id, reason="calendar_list_only")], agent_metrics
    if not proposals and not calendar_commands:
        if calendar_command_errors:
            return [_invalid_command_result(task.id, calendar_command_errors[0])], agent_metrics

        # A read miss may return bounded candidates, but a mutation is never
        # chained from that read into an unconfirmed write.
        not_found_queries = [
            str((obs.get("result") or {}).get("query") or "")
            for obs in prior_observations
            if obs.get("tool") == "calendar.find_my_event"
            and (obs.get("result") or {}).get("status") == "not_found"
            and str((obs.get("result") or {}).get("query") or "").strip()
        ]
        if not_found_queries:
            suggestions: list[dict[str, Any]] = []
            for query in not_found_queries[:3]:
                try:
                    event, resolution, candidates = resolve_owned_event_with_candidates(
                        turn_ctx.user_id, query, limit=3,
                    )
                except Exception:
                    event, resolution, candidates = None, "not_found", []
                if not candidates and event is not None:
                    candidates = [event]
                if not candidates:
                    continue
                records = remember_candidates(turn_ctx.user_id, candidates)
                projections = [
                    public_projection(record)
                    for record in records
                    if public_projection(record)
                ]
                if projections:
                    suggestions.append({
                        "query": query,
                        "resolution": resolution,
                        "candidates": projections,
                    })
            if suggestions:
                print(f"  [{task.id}] result=OK (calendar candidate suggestions)")
                return [SubTaskResult(
                    task_id=task.id,
                    status=SubTaskStatus.OK,
                    tool_name="calendar.find_my_event",
                    observation={"calendar_candidate_suggestions": suggestions},
                )], agent_metrics
            print(f"  [{task.id}] result=OK (no_write_proposed)")
            return [SubTaskResult(
                task_id=task.id,
                status=SubTaskStatus.OK,
                tool_name="calendar.find_my_event",
                observation={
                    "no_write_proposed": True,
                    "not_found_queries": not_found_queries,
                },
            )], agent_metrics
        error_code = (
            "sub_agent_invalid_proposal"
            if agent_metrics and agent_metrics.rejected_calls
            else "sub_agent_no_proposal"
        )
        print(f"  [{task.id}] result=FAILED  reason={error_code}")
        return [SubTaskResult(
            task_id=task.id,
            status=SubTaskStatus.FAILED,
            error_code=error_code,
        )], agent_metrics

    results: list[SubTaskResult] = []
    if calendar_command_errors:
        results.append(_invalid_command_result(task.id, calendar_command_errors[0]))

    if proposals:
        results.extend(_run_calendar_reads(
            task=task,
            turn_ctx=turn_ctx,
            proposals=list(proposals),
            prior_observations=prior_observations,
            services=services,
            max_reads=max_reads,
        ))

    if task.outcome_contract == "calendar.availability.v1":
        return _attach_availability_outcome(task, results), agent_metrics

    if calendar_commands:
        print(f"  [{task.id}] typed calendar commands: {len(calendar_commands)}")
        place_followup_record = None
        place_followup_projection = getattr(turn_ctx, "place_followup", None)
        if any(
            str(getattr(command, "action", "")) in {"cancel", "cancel_all_upcoming"}
            for command in calendar_commands
        ):
            # An explicit Calendar cancellation also abandons an unfinished
            # place-to-calendar request in this room.
            clear_place_followup(turn_ctx.user_id, turn_ctx.room_id)
            place_followup_projection = None
        if (
            len(calendar_commands) == 1
            and str(getattr(calendar_commands[0], "action", "")) == "create"
            and place_followup_projection
        ):
            if str(getattr(calendar_commands[0], "draft_mode", "none") or "none") == "replace":
                # An explicit new create must not inherit the previous room's
                # place or time fields.
                clear_place_followup(turn_ctx.user_id, turn_ctx.room_id)
                place_followup_projection = None
            else:
                try:
                    place_followup_record = get_place_followup(
                        turn_ctx.user_id, turn_ctx.room_id,
                    )
                except PlaceFollowupPersistenceError as exc:
                    # The context projection is already safe to show, but the
                    # authoritative command fields cannot be merged without the
                    # room-scoped record. Keep the request bounded and ask the user
                    # to retry instead of falling back to another room's draft.
                    print(f"  [{task.id}] place follow-up load failed: {exc}")
                    place_followup_record = None
                if place_followup_record:
                    try:
                        calendar_commands = [merge_place_followup(
                            calendar_commands[0], place_followup_record,
                        )]
                    except Exception:
                        place_followup_record = None

        # A room-scoped place follow-up is the authoritative draft for this
        # branch. Do not merge the legacy user-scoped Calendar draft into it.
        draft = (
            get_draft(turn_ctx.user_id)
            if len(calendar_commands) == 1 and place_followup_record is None
            else None
        )
        if draft is not None:
            try:
                replacement = resolved_target_replaced(calendar_commands[0], draft)
                merged_command = merge_command(calendar_commands[0], draft)
                if replacement:
                    clear_draft(turn_ctx.user_id)
                    clear_place_followup(turn_ctx.user_id, turn_ctx.room_id)
                    draft = None
                calendar_commands = [merged_command]
            except Exception:
                clear_draft(turn_ctx.user_id)

        place_resolution_context = getattr(turn_ctx, "place_reference_resolution", None)
        place_message = str(getattr(turn_ctx, "message", "") or "")
        place_flow_hint = any(
            str(getattr(command, "action", "")) == "create"
            and bool(_PLACE_RECHECK_MARKERS.search(place_message))
            and any(
                clue and str(clue) in place_message
                for clue in (
                    getattr(command, "title", None),
                    getattr(command, "location", None),
                )
            )
            for command in calendar_commands
        )
        place_bound_commands = []
        for command in calendar_commands:
            if (
                place_followup_record
                and "title" in (place_followup_record.get("missing_fields") or [])
                and not isinstance(place_followup_record.get("resolved_place"), dict)
                and str((place_resolution_context or {}).get("status") or "") != "resolved"
            ):
                message_text = str(getattr(turn_ctx, "message", "") or "")
                explicit_clue = any(
                    clue and str(clue) in message_text
                    for clue in (
                        getattr(command, "title", None),
                        getattr(command, "location", None),
                    )
                ) and bool(
                    _PLACE_RECHECK_MARKERS.search(message_text)
                    or _PLACE_ACTION_RECHECK_MARKERS.search(message_text)
                )
                if not explicit_clue:
                    try:
                        command = _clear_unverified_place_fields(command)
                    except Exception:
                        continue
            bound_command, binding_error = _bind_resolved_place_to_create(
                turn_ctx, command,
            )
            if binding_error:
                results.append(_invalid_command_result(task.id, binding_error))
                continue
            place_bound_commands.append(bound_command)
        calendar_commands = place_bound_commands
        if not calendar_commands:
            return results, agent_metrics

        canonical_commands = []
        for command in calendar_commands:
            canonical_command, date_error = canonicalize_calendar_command(turn_ctx, command)
            if date_error:
                error_code = (
                    "invalid_interval"
                    if str(date_error).startswith("target_selector_time:")
                    else "invalid_date"
                )
                results.append(_invalid_command_result(task.id, {
                    "code": error_code,
                    "message": date_error,
                }))
                continue
            canonical_commands.append(canonical_command)
        calendar_commands = canonical_commands
        if not calendar_commands:
            return results, agent_metrics

        recent_reference = None
        if len(calendar_commands) == 1:
            recent_reference = _calendar_reference_for_command(
                turn_ctx.user_id, calendar_commands[0], draft,
            )
        reference_map: dict[int, dict[str, Any]] = {}
        if recent_reference and str(calendar_commands[0].action) in {"update", "cancel"}:
            same_turn_selected = any(
                obs.get("tool") == "calendar.find_my_event"
                and (obs.get("result") or {}).get("status") == "found"
                for obs in prior_observations
            )
            reference_map[0] = {
                **recent_reference,
                "_force": bool(recent_reference.get("_force") or same_turn_selected),
            }

        command_guard = guard_calendar_commands(calendar_commands)
        trace["guard_results"].append(command_guard.code.value)
        if debug_enabled:
            append_debug_event(
                run_id,
                "function_call",
                task_id=task.id,
                agent=task.agent,
                call_index=len(results),
                call_id=f"{task.id}:calendar.submit_commands",
                function="calendar.submit_commands",
                planner_arguments={
                    "commands": [
                        command.model_dump(exclude_none=True)
                        for command in calendar_commands
                    ]
                },
                guard={
                    "ok": command_guard.ok,
                    "code": command_guard.code.value,
                    "reason": command_guard.reason,
                },
            )
        if not command_guard.ok:
            results.append(SubTaskResult(
                task_id=task.id,
                status=SubTaskStatus.FAILED,
                tool_name="calendar.submit_commands",
                guard_code=command_guard.code,
            ))
        else:
            if supersede_confirmation is not None:
                supersession = supersede_confirmation(
                    user_id=turn_ctx.user_id,
                    tool_name="calendar.submit_commands",
                    reason="calendar_replacement_attempt",
                )
                supersession_status = str(supersession.get("status") or "none")
                if supersession_status in {"already_executing", "already_completed", "storage_unavailable"}:
                    message = {
                        "already_executing": "上一筆行事曆操作已開始執行，目前不能回溯取消；請稍後確認目前狀態。",
                        "already_completed": "上一筆行事曆操作已經完成，無法回溯取消；請先確認目前狀態，再重新提出安排。",
                        "storage_unavailable": "目前無法安全更新待確認操作，行事曆尚未變更；請稍後再試。",
                    }[supersession_status]
                    results.append(_invalid_command_result(task.id, {
                        "code": f"confirmation_{supersession_status}",
                        "message": message,
                    }))
                    return results, agent_metrics
            preflight = preflight_calendar_commands(
                turn_ctx,
                calendar_commands,
                recent_references=reference_map,
            )
            if preflight.status != "ready":
                clarification = preflight.clarification
                place_followup_save_error = None
                if (
                    clarification is not None
                    and clarification.code != "invalid_date"
                    and len(calendar_commands) == 1
                ):
                    if preflight.resolved_target is not None:
                        remember_resolved_target(
                            turn_ctx.user_id,
                            preflight.resolved_target,
                        )
                    place_resolution = place_resolution_context
                    followup_resolution = _place_resolution_for_followup(
                        turn_ctx,
                        place_resolution,
                        place_followup_record,
                    )
                    place_flow = bool(
                        place_followup_record
                        or place_followup_projection
                        or isinstance(place_resolution, dict)
                        or place_flow_hint
                    )
                    if place_flow:
                        try:
                            raw_ctx = getattr(turn_ctx, "_raw_ctx", None)
                            save_place_followup(
                                turn_ctx.user_id,
                                turn_ctx.room_id,
                                calendar_commands[0],
                                missing_fields=clarification.missing_fields,
                                candidate_options=clarification.candidates,
                                resolution=followup_resolution,
                                source_message_id=(
                                    getattr(raw_ctx, "message_id", None)
                                    or getattr(turn_ctx, "message_id", None)
                                ),
                            )
                        except PlaceFollowupPersistenceError as exc:
                            place_followup_save_error = str(exc)
                            _LOGGER.warning(
                                "Unable to save room-scoped place follow-up user=%s room=%s reason=%s",
                                str(turn_ctx.user_id)[:80], str(turn_ctx.room_id)[:80], str(exc),
                            )
                    else:
                        save_draft(
                            turn_ctx.user_id,
                            calendar_commands[0],
                            missing_fields=clarification.missing_fields,
                            candidates=clarification.candidates,
                            resolved_target=preflight.resolved_target,
                        )
                safe_result: dict[str, Any] = {
                    "calendar_command_result": {
                        "status": preflight.status,
                        "denial_code": preflight.denial_code,
                        "preview": preflight.preview,
                    }
                }
                if clarification is not None:
                    safe_result["calendar_command_result"]["clarification"] = clarification.model_dump()
                if place_followup_save_error:
                    safe_result["place_followup"] = {
                        "status": "storage_unavailable",
                    }
                print(f"  [{task.id}] result=OK (calendar {preflight.status})")
                results.append(SubTaskResult(
                    task_id=task.id,
                    status=SubTaskStatus.OK,
                    tool_name="calendar.submit_commands",
                    observation=safe_result,
                ))
            else:
                clear_draft(turn_ctx.user_id)
                clear_place_followup(turn_ctx.user_id, turn_ctx.room_id)
                payload: dict[str, Any] = {
                    "calendar_plan_version": 1,
                    "plans": [plan.model_dump(exclude_none=True) for plan in preflight.plans],
                }
                create_confirmation(
                    user_id=turn_ctx.user_id,
                    agent_name=task.agent,
                    tool_name="calendar.submit_commands",
                    arguments={},
                    payload=payload,
                    origin_run_id=run_id,
                    preview=preflight.preview or "",
                )
                results.append(SubTaskResult(
                    task_id=task.id,
                    status=SubTaskStatus.OK,
                    tool_name="calendar.submit_commands",
                    observation={
                        "pending_confirmation": True,
                        "tool_name": "calendar.submit_commands",
                        "preview": preflight.preview or "",
                    },
                ))

    if any(result.status is SubTaskStatus.OK for result in results):
        print(f"  [{task.id}] result=OK ({len(results)} call(s))")
    elif results:
        print(f"  [{task.id}] result=FAILED (all {len(results)} call(s) failed)")
    return results, agent_metrics
