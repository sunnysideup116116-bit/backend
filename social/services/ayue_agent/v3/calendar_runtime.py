"""Bounded Calendar runtime owned by the Calendar domain.

The Calendar agent remains semantic/LLM-only.  This module owns the server-side
interpretation of its authority-free read proposals and typed mutation
commands, including references, drafts, preflight, and confirmation creation.
"""

from __future__ import annotations

from typing import Any, Callable

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
from .calendar_references import (
    DRAFT_TARGET_REFERENCE_KEY,
    clear_reference,
    get_reference,
    remember_candidates,
    remember_event,
    remember_resolved_target,
    public_projection,
)
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

    if agent_metrics:
        print_llm_metrics(f"{task.id}:{task.agent}", agent_metrics)
        if agent_metrics.error:
            print(f"  [{task.id}] error=sub_agent_failed")

    calendar_commands = list(getattr(proposals, "commands", []) or [])
    calendar_command_errors = list(getattr(proposals, "command_errors", []) or [])
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
        draft = get_draft(turn_ctx.user_id) if len(calendar_commands) == 1 else None
        if draft is not None:
            try:
                replacement = resolved_target_replaced(calendar_commands[0], draft)
                merged_command = merge_command(calendar_commands[0], draft)
                if replacement:
                    clear_draft(turn_ctx.user_id)
                    draft = None
                calendar_commands = [merged_command]
            except Exception:
                clear_draft(turn_ctx.user_id)

        canonical_commands = []
        for command in calendar_commands:
            canonical_command, _date_error = canonicalize_calendar_command(turn_ctx, command)
            canonical_commands.append(canonical_command)
        calendar_commands = canonical_commands

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
            preflight = preflight_calendar_commands(
                turn_ctx,
                calendar_commands,
                recent_references=reference_map,
            )
            if preflight.status != "ready":
                clarification = preflight.clarification
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
                print(f"  [{task.id}] result=OK (calendar {preflight.status})")
                results.append(SubTaskResult(
                    task_id=task.id,
                    status=SubTaskStatus.OK,
                    tool_name="calendar.submit_commands",
                    observation=safe_result,
                ))
            else:
                clear_draft(turn_ctx.user_id)
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
