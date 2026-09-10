# services/ayue_agent/v3/scheduler.py
"""V3 Scheduler / Orchestrator: pure-code orchestration of the sub-agent runtime."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from services.ayue_agent.contracts import AgentResult, AgentTurnContext, PresentationBlock
from services.ayue_agent.context import build_public_agent_turn_context
from services.ayue_agent.public_relationship_projection import validated_mentioned_contact_ids
from services.ayue_agent.router import confirmation_choice
from services.ayue_agent.time_context import build_turn_clock
from services.language_service import normalize_public_reply
from services.ayue_agent.tool_registry import (
    TOOL_REGISTRY, ToolArgumentSource, ToolRisk, executor_arguments_for_turn, tool_call_key,
)
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.web_tools import is_safe_public_url
from services.ayue_agent.match_opportunity import (
    accept_guidance_offer,
    active_guidance_offer,
    assess_match_opportunity,
    claim_guidance_offer,
    decline_guidance_offer,
)
from services.ayue_agent.product_identity import PUBLIC_PENDING_CANCEL_REPLY
from services.assessment_session_service import (
    active_assessment_session, advance_assessment_session,
    assessment_cancel_choice, assessment_commit_choice,
    awaiting_assessment_commit, cancel_assessment_session,
    commit_assessment_session, expire_assessment_session,
)
from database import db, messages_coll
from services.ai_service import get_effective_chat_model

from .contracts import (
    DATE_COORDINATION_CANCEL_WRITE_INTENT, DATE_INVITATION_WRITE_INTENT,
    AgentContextSlice, GuardDecision, GuardResultCode, Plan, SubTask,
    SubTaskResult, SubTaskStatus, ToolProposal, normalize_plan_for_execution,
)
from .context_slicer import slice_for_agent
from .guard import guard_proposal
from .guarded_execution import GuardedReadExecutor, web_extract_urls_allowed as _web_extract_urls_allowed
from .planner import plan_turn, PlannerMetrics, _synthesizer_only_plan
from . import synthesizer
from .synthesizer import SynthesizerMetrics
from .public_reply import (
    build_presentation,
    public_place_cards_enabled,
    validate_public_reply,
)
from .sub_agents.base import SubAgentMetrics
from .confirmation import (
    ASSESSMENT_COMMIT_ACTION,
    INTERACTION_BUBBLE,
    INTERACTION_LEGACY,
    SURFACE_PUBLIC,
    ConfirmationManager,
    match_choice_cancel_reply,
    sync_choice_message_projection,
)
from . import calendar_runtime
from .runtime_registry import (
    RuntimeRegistration,
    TaskRunnerResult,
    normalize_runner_output,
    proposal_runner,
    registration_for,
)
from .sub_agents.places_agent import run as run_places
from .sub_agents.match_agent import run as run_match
from .sub_agents.profile_agent import run as run_profile
from .sub_agents.product_info_agent import (
    after_run as product_info_after_run,
    before_run as product_info_before_run,
    run as run_product_info,
)
from .place_references import (
    clarification_message as place_reference_clarification,
    commit_resolved_selection,
    PlaceReferencePersistenceError,
    get_candidate_set as get_place_candidate_set,
    get_candidate as get_place_candidate,
    public_projection as place_candidate_projection,
    public_resolution as public_place_resolution,
    replace_presented_candidates,
    resolve_message_reference,
)
from .place_followups import (
    abandonment_requested as place_followup_abandonment_requested,
    clear_followup as clear_place_followup,
)
from .place_projection import (
    MAX_PLACE_CARDS, MAX_PLACE_CARDS_PER_CATEGORY, _PLACE_CATEGORIES, _distance_label,
    _google_embed_url, _osm_embed_url, _place_candidate_ref,
    public_place_cards as _public_place_cards,
)
from . import web_runtime
from . import relationship_runtime
from .write_executors import execute_write, prepare_write_confirmation
from .debug_trace import (
    append_event as append_debug_event,
    begin_run as begin_debug_run,
    finish_run as finish_debug_run,
)
from . import match_runtime


ProgressCallback = Callable[[dict[str, Any]], Any]
_LOGGER = logging.getLogger(__name__)
MAX_READS = max(1, min(int(os.getenv("AYUE_SUBAGENT_MAX_READS", "3") or "3"), 3))
MAX_PARALLEL = max(1, min(int(os.getenv("AYUE_SUBAGENT_MAX_PARALLEL", "2") or "2"), 2))
# Four bounded domain tasks may now follow a Calendar precheck.  Preserve the
# old worst-case ceiling of three reads per domain task while making the
# ceiling explicit across the whole Public run.
MAX_TOTAL_READS = 9
_ASSESSMENT_START_CONFIRMATIONS = frozenset({"開始", "開始吧", "開始啊", "開始阿"})


def _assessment_start_confirmation_requested(message: str, pending: list[dict[str, Any]]) -> bool:
    """Accept bounded start wording only for an assessment confirmation."""
    compact = re.sub(r"\s+", "", str(message or "")).lower()
    if compact not in _ASSESSMENT_START_CONFIRMATIONS or len(pending) != 1:
        return False
    return str(pending[0].get("tool_name") or "") == "profile.start_assessment"


def _direct_chat_fast_path_enabled() -> bool:
    return os.getenv("AYUE_V3_SIMPLE_CHAT_FAST_PATH", "on").strip().lower() in {
        "1", "true", "on",
    }


def _direct_chat_block_reason(
    plan: Plan,
    turn: Any,
    pending_records: list[dict[str, Any]],
    active_offer: dict[str, Any] | None,
) -> str | None:
    """Validate protocol/state safety without reclassifying natural language."""
    if not _direct_chat_fast_path_enabled():
        return "feature_disabled"
    if pending_records:
        return "pending_confirmation"
    for registered in _iter_runtime_registrations():
        if registered.direct_chat_blocker is None:
            continue
        reason = registered.direct_chat_blocker(turn)
        if reason:
            return reason
    if getattr(turn, "recent_context_draft", None):
        return "active_draft"
    if active_offer:
        return "active_match_guidance"
    if getattr(turn, "mentioned_contact_overflow", False):
        return "mentioned_contact_overflow"
    if (
        getattr(turn, "place_reference_resolution", None)
        and getattr(plan, "place_selection", None) is not None
    ):
        return "place_reference_resolution"
    if getattr(turn, "place_followup", None):
        return "place_followup"
    if plan.opportunity is not None and plan.opportunity.signal != "none":
        return "opportunity"
    return None


def _planner_failure_reply(turn: Any) -> str:
    """Return neutral copy when the Planner protocol truly fails."""
    return "這次規劃沒有完成，所以我沒有執行任何操作。請稍後再試一次。"


def _abandon_place_followup_for_turn(turn: Any) -> Any:
    """Discard an explicitly abandoned draft while retaining its safe referent."""
    followup = getattr(turn, "place_followup", None)
    if (
        not isinstance(followup, dict)
        or not place_followup_abandonment_requested(getattr(turn, "message", ""))
    ):
        return turn
    clear_place_followup(turn.user_id, turn.room_id)
    turn.place_followup = {
        **followup,
        "abandonment_requested": True,
    }
    return turn


def _privacy_safe_planner_attempts(metrics: PlannerMetrics) -> list[dict[str, Any]]:
    """Strip failed Planner attempts down to non-content schema diagnostics."""
    return [
        {
            "attempt": int(item.get("attempt", 0) or 0),
            "status": str(item.get("status") or "")[:40],
            "stage": str(item.get("stage") or "")[:40],
            "failure_code": str(item.get("failure_code") or "")[:80],
            "validation_fields": list(item.get("validation_fields") or [])[:8],
            "repair_codes": list(item.get("repair_codes") or [])[:4],
        }
        for item in (metrics.attempts or [])[:2]
        if isinstance(item, dict)
    ]


def _iter_runtime_registrations() -> list[RuntimeRegistration]:
    """Return the currently registered runtime records.

    ``patch.dict`` with a raw callable remains useful for focused tests during
    the staged migration; production values are always RuntimeRegistration.
    """
    return [
        registration
        for value in _SUB_AGENT_RUNNERS.values()
        if (registration := registration_for(value)) is not None
    ]

_TEST_MODE = os.getenv("AYUE_TEST_MODE", "").strip().lower() in {"1", "true", "on"}
if _TEST_MODE:
    from .test_store import MemoryCollection
    _TEST_COLLECTIONS = {
        "agent_runs": MemoryCollection(),
        "v3_pending_confirmations": MemoryCollection(),
    }


def _runtime_collection(name: str) -> Any:
    if _TEST_MODE:
        return _TEST_COLLECTIONS[name]
    return db[name]


RUNS = _runtime_collection("agent_runs")
_EXPLICIT_PLACE_HOURS_RE = re.compile(
    r"營業(?:時間|資訊)?|開(?:店|門)時間|關(?:店|門)時間|公休日?|休息日|幾點(?:開|關)|opening\s+hours?",
    re.IGNORECASE,
)


def ensure_indexes() -> None:
    try:
        RUNS.create_index("created_at", expireAfterSeconds=14 * 86400)
        RUNS.create_index([("user_id", 1), ("created_at", -1)])
    except Exception as exc:
        print(f"Agent run index setup skipped: {type(exc).__name__}")


def _persist_trace(run_id: str, ctx: Any, payload: dict[str, Any]) -> None:
    """Persist an allowlisted, privacy-safe V3 trace only."""
    try:
        RUNS.insert_one({
            "run_id": run_id,
            "user_id": ctx.user_id,
            "room_id": ctx.room_id,
            "agent_version": "v3",
            "created_at": time.time(),
            **payload,
        })
    except Exception as exc:
        print(f"Agent trace skipped: {type(exc).__name__}")


def _emit_progress(
    callback: ProgressCallback | None, event_type: str, *, trace: dict[str, Any] | None = None,
    **payload: Any,
) -> None:
    """Best-effort public progress event; never let stream delivery affect a run."""
    if trace is not None:
        trace["event_sequence"].append(event_type)
    if callback is None:
        return
    try:
        callback({"type": event_type, **payload})
    except Exception:
        pass


def _print_separator(title: str) -> None:
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")


def _metric_call_count(metrics: Any) -> int:
    """Use measured provider calls, with compatibility for old test metrics."""
    count = int(getattr(metrics, "llm_call_count", 0) or 0)
    if count == 0 and bool(getattr(metrics, "used_llm", False)):
        return 1
    return count


def _metric_model_name(metrics: Any) -> str:
    tier = str(getattr(metrics, "requested_model_tier", "main") or "main")
    if tier == "none":
        return "none (deterministic)"
    return get_effective_chat_model(requested_model_tier=tier)


def _print_llm_metrics(label: str, metrics: Any) -> None:
    inp = getattr(metrics, "input_tokens", 0)
    out = getattr(metrics, "output_tokens", 0)
    dur = getattr(metrics, "duration_ms", 0)
    tier = str(getattr(metrics, "requested_model_tier", "main") or "main")
    model = _metric_model_name(metrics)
    calls = _metric_call_count(metrics)
    print(
        f"  [{label}] model={model}  tier={tier}  llm_calls={calls}"
        f"  input_tokens={inp}  output_tokens={out}  duration={dur}ms"
    )
    requests = getattr(metrics, "llm_requests", None) or []
    for index, req in enumerate(requests):
        try:
            print(
                f"  [{label}#{index + 1}] request: input={int(req.get('input_tokens', 0) or 0)}"
                f"  output={int(req.get('output_tokens', 0) or 0)}"
                f"  duration={int(req.get('duration_ms', 0) or 0)}ms"
                f"  ttft={int(req.get('ttft_ms', 0) or 0)}ms"
                f"  tps={float(req.get('tps', 0) or 0):.1f}"
                f"  model={req.get('model_name', '')}"
            )
        except Exception:
            pass
    total = inp + out
    print(f"  [{label}] total_tokens={total}")


_CONFIRMATIONS = _runtime_collection("v3_pending_confirmations")


def clear_demo_runtime_state() -> None:
    """Clear in-memory V3 state when the demo database is reset."""
    # In production this module uses PyMongo collections.  PyMongo exposes
    # unknown attributes as sub-collections, so ``collection.clear`` is not a
    # callable in-memory method (calling it raises ``TypeError``).  Mongo is
    # cleared by the centralized demo cleanup service; this hook only needs to
    # clear the dict-backed stores used by the test runtime.
    if not _TEST_MODE:
        return
    for collection in (_CONFIRMATIONS, RUNS):
        clear = getattr(collection, "clear", None)
        if callable(clear):
            clear()

def _invoke_registered_runner(
    runner: RuntimeRegistration | Callable[..., Any],
    context_slice: AgentContextSlice,
    task: SubTask,
    services: GuardedReadExecutor,
) -> Any:
    """Call one uniform registered runner; no signature inspection is needed."""
    supplied_registration = isinstance(runner, RuntimeRegistration)
    registration = registration_for(runner)
    if registration is None:
        raise TypeError("runner is not registered")
    if supplied_registration and registration.legacy_signature:
        # Compatibility for focused tests/provider doubles during the staged
        # rollout.  Production registrations all use the uniform contract.
        return registration.runner(context_slice, task_brief=task.task_brief)
    return registration.runner(context_slice, task=task, services=services)


_PLACE_MODE_TOOLS: dict[str, frozenset[str]] = {
    "discover": frozenset({"places.search_nearby", "places.measure_distance"}),
    "details": frozenset({"places.resolve_place", "places.measure_distance"}),
    "reviews": frozenset({"places.resolve_place"}),
}
_PLACE_DISTANCE_REQUEST_RE = re.compile(
    r"距離|多遠|走路|步行|交通|怎麼去|怎麼走|要多久|幾分鐘|公里|公尺",
)


def _place_distance_requested(context_slice: AgentContextSlice) -> bool:
    text = str(context_slice.payload.get("message") or "")
    return bool(_PLACE_DISTANCE_REQUEST_RE.search(text))


def _run_registered_places(
    context_slice: AgentContextSlice,
    *,
    task: SubTask,
    services: GuardedReadExecutor,
) -> tuple[TaskRunnerResult, SubAgentMetrics]:
    """Run Places through the shared registration with a typed tool surface."""
    del services
    metrics = SubAgentMetrics()
    mode = str(task.place_mode or "")
    allowed_tools = _PLACE_MODE_TOOLS.get(mode)
    if allowed_tools is None:
        metrics.error = "place_mode_missing_or_invalid"
        metrics.rejected_calls.append("place_mode_invalid")
        return TaskRunnerResult.from_proposals([]), metrics

    mode_slice = context_slice.model_copy(update={
        "payload": {**context_slice.payload, "place_mode": mode},
    })
    proposals, metrics = run_places(
        mode_slice,
        task_brief=task.task_brief,
        tool_names=allowed_tools,
    )
    accepted = []
    for proposal in list(proposals or []):
        if (
            proposal.tool_name in allowed_tools
            and (
                proposal.tool_name != "places.measure_distance"
                or _place_distance_requested(mode_slice)
            )
        ):
            accepted.append(proposal)
        else:
            # The Planner's typed mode, not model-authored tool selection,
            # decides whether a Places proposal is allowed. In particular a
            # reviews task can never turn into a nearby search.
            metrics.rejected_calls.append("place_tool_intent_mismatch")
    return TaskRunnerResult.from_proposals(accepted), metrics


_SUB_AGENT_RUNNERS = {
    "calendar": RuntimeRegistration(
        runner=calendar_runtime.run,
        direct_chat_blocker=calendar_runtime.direct_chat_block_reason,
        confirmed_result_projector=calendar_runtime.confirmed_result_projection,
        step_prefix="",
    ),
    "places": RuntimeRegistration(runner=_run_registered_places),
    "web": RuntimeRegistration(runner=web_runtime.run),
    "match": RuntimeRegistration(runner=match_runtime.run),
    "relationship": RuntimeRegistration(runner=relationship_runtime.run),
    "profile": RuntimeRegistration(runner=proposal_runner(run_profile)),
    "product_info": RuntimeRegistration(
        runner=run_product_info,
        before_run=product_info_before_run,
        after_run=product_info_after_run,
    ),
}


def has_active_public_confirmation(user_id: str) -> bool:
    """Return whether the current V3 confirmation manager has active work."""
    try:
        return bool(ConfirmationManager(_CONFIRMATIONS).list_active(user_id=user_id))
    except Exception:
        return False


def mark_public_confirmation_presented(
    *, user_id: str, origin_run_id: str, message_id: str, persisted_content: str,
) -> bool:
    """Activate a prepared confirmation only after Public Chat persisted it."""
    return ConfirmationManager(_CONFIRMATIONS).mark_presented(
        user_id=user_id,
        origin_run_id=origin_run_id,
        message_id=message_id,
        persisted_content=persisted_content,
    )


def _topological_layers(plan: Plan) -> list[list[SubTask]]:
    """Group tasks into execution layers by dependency depth."""
    done: set[str] = set()
    layers: list[list[SubTask]] = []
    remaining = list(plan.tasks)
    while remaining:
        ready = [
            t for t in remaining
            if set(t.depends_on)
            | ({t.run_if.source_task_id} if t.run_if is not None else set())
            <= done
        ]
        if not ready:
            break
        layers.append(ready)
        for t in ready:
            done.add(t.id)
            remaining.remove(t)
    return layers


def _ensure_place_hours_fallback(plan: Plan, message: str) -> tuple[Plan, bool]:
    """Add the typed optional Web fallback when Planner omitted explicit hours verification."""
    if not _EXPLICIT_PLACE_HOURS_RE.search(str(message or "")):
        return plan, False
    if any(task.agent == "web" for task in plan.tasks):
        return plan, False
    place_tasks = [
        task for task in plan.tasks
        if task.agent == "places" and task.place_mode == "details"
    ]
    synthesizers = [task for task in plan.tasks if task.agent == "synthesizer"]
    if len(place_tasks) != 1 or len(synthesizers) != 1 or len(plan.tasks) >= 5:
        return plan, False
    place_task = place_tasks[0]
    synthesizer = synthesizers[0]
    used_ids = {task.id for task in plan.tasks}
    web_id = "web_hours_fallback"
    suffix = 2
    while web_id in used_ids:
        web_id = f"web_hours_fallback_{suffix}"
        suffix += 1
    web_task = SubTask(
        id=web_id,
        agent="web",
        web_mode="place_hours_fallback",
        evidence_policy="casual_discovery",
        depends_on=[place_task.id],
        task_brief=(
            "若 Places 沒有完整星期營業資訊，補查同一個 server 綁定店家的公開營業資訊；"
            "不得改查其他店家。"
        ),
    )
    synth_dependencies = [
        dependency for dependency in synthesizer.depends_on
        if dependency != place_task.id
    ]
    if web_id not in synth_dependencies:
        synth_dependencies.append(web_id)
    updated_synthesizer = synthesizer.model_copy(update={
        "depends_on": synth_dependencies,
    })
    tasks: list[SubTask] = []
    for task in plan.tasks:
        if task.id == synthesizer.id:
            tasks.extend([web_task, updated_synthesizer])
        else:
            tasks.append(task)
    return Plan.model_validate({
        **plan.model_dump(mode="python"),
        "tasks": [task.model_dump(mode="python") for task in tasks],
    }), True


def _observation_dict(task_id: str, result: "SubTaskResult") -> dict[str, Any]:
    """Project one sub-task result into the privacy-safe prior-observation shape."""
    return {
        "task_id": result.task_id,
        "status": result.status.value,
        "tool": result.tool_name,
        "result": result.observation,
        "error_code": result.error_code,
        "skip_reason": result.skip_reason,
        "outcome_codes": list(result.outcome_codes),
    }


def _pending_relationship_transaction_state(
    tool_name: str, payload: dict[str, Any],
) -> dict[str, str] | None:
    """Project a prompt-safe Relationship state beside a locked preview."""
    actions = {
        "relationship.start_date_coordination": "create_date_invitation",
        "relationship.cancel_date_coordination": "cancel_date_invitation",
    }
    action = actions.get(str(tool_name or ""))
    if action is None:
        return None
    data = payload.get("data") if isinstance(payload, dict) else None
    counterparty = str((data or {}).get("safe_label") or "").strip()[:30]
    state = {
        "schema_version": "relationship_transaction_state.v1",
        "action": action,
        "status": "pending_confirmation",
    }
    if counterparty:
        state["counterparty"] = counterparty
    return state


def _server_owned_date_coordination_reply(
    task_results: dict[str, list[SubTaskResult]],
) -> str | None:
    """Keep a date-card preview authoritative over model composition."""
    for results in task_results.values():
        for result in results:
            if result.tool_name not in {
                "relationship.start_date_coordination",
                "relationship.cancel_date_coordination",
            }:
                continue
            observation = result.observation or {}
            # A rejected preflight is just as authoritative as a successful
            # preview: it carries the canonical state/target explanation that
            # Synthesizer must not embellish with a Hub redirect.
            if result.status is SubTaskStatus.FAILED and result.error_code == "preflight_rejected":
                preview = str(observation.get("preview") or "").strip()
                if preview:
                    return preview
            if result.status is not SubTaskStatus.OK:
                continue
            if observation.get("pending_confirmation"):
                preview = str(observation.get("preview") or "").strip()
                if preview:
                    return preview
    return None


def _server_owned_date_coordination_failure_reply(
    task_results: dict[str, list[SubTaskResult]],
    *,
    write_intent: str,
) -> str | None:
    """Return the fixed, bounded failure copy for the typed write runtime."""
    if write_intent not in {
        DATE_INVITATION_WRITE_INTENT,
        DATE_COORDINATION_CANCEL_WRITE_INTENT,
    }:
        return None
    for results in task_results.values():
        for result in results:
            failure = result.observation.get("failure") if isinstance(result.observation, dict) else None
            failure_codes = {
                relationship_runtime.DATE_INVITATION_PROTOCOL_FAILURE_CODE:
                    relationship_runtime.DATE_INVITATION_PROTOCOL_FAILURE_REPLY,
                relationship_runtime.DATE_COORDINATION_CANCEL_PROTOCOL_FAILURE_CODE:
                    relationship_runtime.DATE_COORDINATION_CANCEL_PROTOCOL_FAILURE_REPLY,
            }
            reply = failure_codes.get(str(result.error_code or ""))
            if reply and isinstance(failure, dict) and failure.get("code") == str(result.error_code):
                return reply
    return None


def _server_owned_confirmed_date_reply(results: list[dict[str, Any]]) -> str | None:
    """Keep the canonical write result from being rewritten into a claim."""
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("tool_name") not in {
            "relationship.start_date_coordination",
            "relationship.cancel_date_coordination",
        }:
            continue
        if not result.get("ok"):
            continue
        reply = str((result.get("data") or {}).get("reply") or "").strip()
        if reply:
            return reply
    return None


def _condition_skip_reason(
    task: SubTask, task_results: dict[str, list[SubTaskResult]],
) -> str | None:
    """Evaluate a typed control edge after ordinary dependency checks.

    Domain payloads are intentionally opaque here.  The Scheduler only reads
    server-owned outcome codes from ``SubTaskResult`` and never inspects
    Calendar event fields.
    """
    condition = task.run_if
    if condition is None:
        return None
    source_results = task_results.get(condition.source_task_id, [])
    if not source_results:
        return "condition_unavailable"
    if condition.required_outcome == "task.finished":
        if any(result.status in {SubTaskStatus.OK, SubTaskStatus.FAILED} for result in source_results):
            return None
        return "condition_unavailable"

    if any(result.status in {SubTaskStatus.SKIPPED, SubTaskStatus.FAILED} for result in source_results):
        return "condition_unavailable"
    successful = [result for result in source_results if result.status is SubTaskStatus.OK]
    if len(successful) != 1:
        return "condition_unavailable"
    codes = {
        code
        for result in successful
        for code in result.outcome_codes
    }
    required = condition.required_outcome
    if required in codes and len(codes) == 1:
        return None
    if codes & {"calendar.no_scheduled_events", "calendar.has_scheduled_events"}:
        return "condition_not_met"
    return "condition_unavailable"


def _failure_observation_from_tool_result(tool_result: Any) -> dict[str, Any] | None:
    """Pass through only the bounded failure projection supplied by a tool."""
    data = getattr(tool_result, "data", None)
    failure = data.get("failure") if isinstance(data, dict) else None
    return {"failure": failure} if isinstance(failure, dict) else None


def _prior_observations_for(
    task: "SubTask",
    task_results: dict[str, list["SubTaskResult"]],
    task_by_id: dict[str, "SubTask"] | None = None,
) -> list[dict[str, Any]]:
    """Return only the observations of this task's declared dependencies.

    Unrelated completed tasks (e.g. a parallel places result while a
    relationship task runs) never leak into another agent's context slice.
    A task that produced multiple tool observations contributes each one.
    """
    prior: list[dict[str, Any]] = []
    def add_dependency(dep: str) -> None:
        for result in task_results.get(dep, []):
            if result.status is SubTaskStatus.SKIPPED:
                if (
                    result.skip_reason == "places_hours_sufficient"
                    and task_by_id is not None
                    and dep in task_by_id
                ):
                    for upstream in task_by_id[dep].depends_on:
                        add_dependency(upstream)
                continue
            else:
                prior.append(_observation_dict(dep, result))
    for dep in task.depends_on:
        add_dependency(dep)
    return prior


def _dependency_completed(results: list["SubTaskResult"]) -> bool:
    """Treat a Web no-op as satisfied while preserving real failure semantics."""
    return any(
        result.status is SubTaskStatus.OK
        or (
            result.status is SubTaskStatus.SKIPPED
            and result.skip_reason == "places_hours_sufficient"
        )
        for result in results
    )


def _public_sources(task_results: Any) -> list[dict[str, str]]:
    """Keep display-safe Web citations; map links stay on their place cards.

    A Google/OSM place URL proves that a map entity exists, not that a current
    criterion such as opening hours or an event schedule is true.  Publishing
    those URLs in the evidence section would visually mislabel candidates as
    Web support and duplicate the card's own map link.
    """
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    if isinstance(task_results, dict):
        result_groups = task_results.values()
    elif isinstance(task_results, list):
        result_groups = [task_results]
    else:
        result_groups = []
    for results in result_groups:
        for r in results:
            if isinstance(r, dict):
                tool_name = str(r.get("tool") or "")
                data = r.get("result") or {}
                ok = True
            else:
                if r.status is not SubTaskStatus.OK or not r.observation:
                    continue
                tool_name = r.tool_name or ""
                data = r.observation or {}
                ok = True
            if not ok:
                continue
            if data.get("schema_version") == "web_research.v1":
                candidates = data.get("sources") or []
            elif tool_name == "web.search":
                candidates = data.get("results") or []
            elif tool_name == "web.extract":
                candidates = data.get("pages") or []
            else:
                continue
            for item in candidates:
                url = str((item or {}).get("url") or "")
                if not is_safe_public_url(url) or url in seen:
                    continue
                seen.add(url)
                title = re.sub(r"\s+", " ", str((item or {}).get("title") or "")).strip()[:140]
                sources.append({"title": title or url, "url": url})
                if len(sources) == 5:
                    return sources
    return sources


def _apply_card_decision(
    candidate_cards: list[dict[str, Any]], decision: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Apply an explicit Synthesizer card decision to candidate cards.

    - browse → all bounded candidates
    - select → indices filtered and deduped
    - none → no cards

    Missing or invalid decisions never turn an evidence pool into public cards.
    """
    if not candidate_cards or not isinstance(decision, dict):
        return []
    mode = decision.get("mode")
    if mode == "none":
        return []
    if mode == "show_all":
        return candidate_cards if decision.get("card_intent") == "browse" else []
    if mode != "select":
        return []
    indices = decision.get("indices") or []
    selected: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    for index in indices:
        try:
            idx = int(index)
        except (TypeError, ValueError):
            continue
        if idx < 0 or idx >= len(candidate_cards) or idx in seen_ids:
            continue
        seen_ids.add(idx)
        selected.append(candidate_cards[idx])
    if not selected:
        return []
    return selected


def _resolve_presentation_blocks(
    raw_blocks: list[dict[str, Any]] | None,
    selected_cards: list[dict[str, Any]],
    messages: list[str],
) -> list[PresentationBlock]:
    """Resolve model candidate refs to safe final card indices for the UI."""
    if not selected_cards or not messages:
        return []
    ref_to_index = {
        str(card.get("candidate_ref")): index
        for index, card in enumerate(selected_cards)
        if card.get("candidate_ref")
    }
    blocks: list[PresentationBlock] = []
    assigned: set[int] = set()
    card_block_messages: set[int] = set()
    text_block_messages: set[int] = set()
    for raw in (raw_blocks or [])[:12]:
        if not isinstance(raw, dict):
            continue
        try:
            message_index = int(raw.get("message_index", 0))
        except (TypeError, ValueError):
            continue
        markdown = str(raw.get("markdown") or "").strip()[:1400]
        if message_index < 0 or message_index >= len(messages):
            continue
        card_indices: list[int] = []
        for ref in (raw.get("candidate_refs") or [])[:1]:
            index = ref_to_index.get(str(ref))
            if index is not None and index not in assigned:
                card_indices.append(index)
                assigned.add(index)
        if not markdown and not card_indices:
            continue
        if markdown:
            text_block_messages.add(message_index)
        if card_indices:
            card_block_messages.add(message_index)
        blocks.append(PresentationBlock(
            message_index=message_index,
            markdown=markdown,
            place_card_indices=card_indices,
        ))
    # Card-only blocks come from the Synthesizer's server-owned projection.
    # If it selected cards without a surrounding block, keep its authored
    # public message before the cards without inventing a card label.
    for message_index in sorted(card_block_messages - text_block_messages):
        fallback_text = str(messages[message_index] or "").strip()[:1400]
        if not fallback_text:
            continue
        blocks.insert(0, PresentationBlock(
            message_index=message_index,
            markdown=fallback_text,
            place_card_indices=[],
        ))
    return blocks[:12]


def _server_ordered_place_messages(
    messages: list[str], snapshot: dict[str, Any],
) -> list[str]:
    """Make the visible plain-text order come from the saved snapshot.

    The model may explain candidates in any prose order.  The list users act
    on is server-authored from the same snapshot that ordinal resolution reads,
    so a displayed second item can never drift from the stored second item.
    """
    candidates = [
        item for item in (snapshot.get("candidates") or [])
        if isinstance(item, dict) and str(item.get("label") or "").strip()
    ]
    candidates.sort(key=lambda item: int(item.get("ordinal", 0) or 0))
    if not candidates:
        return [str(item).strip() for item in messages if str(item).strip()][:3]
    candidate_summaries = []
    for item in candidates[:8]:
        label = str(item.get("label") or "地點").strip()[:80]
        candidate_summaries.append({
            "candidate_ref": str(item.get("reference") or ""),
            "name": label,
        })

    # The shared renderer is idempotent: it recognizes existing trusted rows,
    # preserves their descriptions, and reapplies snapshot order. Pre-stripping
    # would make the saved reply lose candidate-bound prose.
    rendered, _refs, _bindings = synthesizer._server_ordered_place_messages(
        messages, candidate_summaries,
    )
    return [str(item).strip()[:2400] for item in rendered if str(item).strip()][:3]


def _restore_snapshot_candidate_labels(
    value: str, snapshot: dict[str, Any] | None,
    *, extra_labels: list[str] | tuple[str, ...] = (),
) -> str:
    """Restore provider labels after public-language normalization."""
    text = str(value or "")
    candidates = (snapshot or {}).get("candidates") or []
    trusted_labels = [
        str(item.get("label") or "").strip()[:80]
        for item in candidates
        if isinstance(item, dict)
    ]
    trusted_labels.extend(
        str(label or "").strip()[:80]
        for label in extra_labels
        if str(label or "").strip()
    )
    replacements: list[tuple[str, str]] = []
    for trusted in dict.fromkeys(trusted_labels):
        normalized = normalize_public_reply(trusted)
        if trusted and normalized and trusted != normalized:
            replacements.append((normalized, trusted))
    for normalized, trusted in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
        text = text.replace(normalized, trusted)
    return text


def _strip_model_place_list_lines(
    messages: list[str], candidate_labels: list[str],
) -> list[str]:
    """Use the Synthesizer's trusted candidate-row sanitizer consistently."""
    return synthesizer.sanitize_candidate_presentation_messages(
        messages,
        [
            {"name": str(label).strip()}
            for label in candidate_labels
            if str(label).strip()
        ],
    )


def _same_public_place_reference(requested: Any, resolved: Any) -> bool:
    """Match a planner place phrase to a resolved public label."""
    def compact(value: Any) -> str:
        return re.sub(r"[\s\W_]+", "", str(value or "").lower(), flags=re.UNICODE)
    requested_key = compact(requested)
    resolved_key = compact(resolved)
    return bool(
        len(requested_key) >= 2 and len(resolved_key) >= 2
        and (requested_key in resolved_key or resolved_key in requested_key)
    )


def _place_binding_for_turn(turn_ctx: Any) -> dict[str, Any] | None:
    """Read the private provider identity behind the public place binding."""
    resolution = getattr(turn_ctx, "place_reference_resolution", None)
    if isinstance(resolution, dict) and resolution.get("status") not in {None, "resolved"}:
        return None
    if not isinstance(resolution, dict):
        resolution = getattr(turn_ctx, "recent_place_reference", None)
    if not isinstance(resolution, dict):
        return None
    reference = str(resolution.get("reference") or "").strip()
    if not reference:
        return None
    try:
        return get_place_candidate(turn_ctx.user_id, turn_ctx.room_id, reference)
    except Exception:
        return None


def _bound_place_query(turn_ctx: Any) -> str:
    """Build the provider query from the server-owned name/address projection."""
    resolution = getattr(turn_ctx, "place_reference_resolution", None)
    if not isinstance(resolution, dict):
        resolution = getattr(turn_ctx, "recent_place_reference", None)
    if not isinstance(resolution, dict):
        return ""
    label = str(resolution.get("label") or "").strip()[:100]
    address = str(resolution.get("address_summary") or "").strip()[:180]
    return " ".join(value for value in (label, address) if value)


def _place_provider_identity_matches(expected: dict[str, Any] | None, observed: Any) -> bool:
    if not isinstance(expected, dict) or not isinstance(observed, dict):
        return False
    expected_provider = str(expected.get("provider") or "").strip().lower()
    observed_provider = str(observed.get("provider") or "").strip().lower()
    if not expected_provider or expected_provider != observed_provider:
        return False
    if expected_provider == "google":
        return bool(
            str(expected.get("provider_place_id") or "").strip()
            and str(expected.get("provider_place_id") or "").strip()
            == str(observed.get("place_id") or "").strip()
        )
    return bool(
        str(expected.get("map_identity") or "").strip()
        and str(expected.get("map_identity") or "").strip()
        == str(observed.get("map_url") or "").strip()
    )


def _has_reusable_success(spec: Any, arguments: dict[str, Any], results: list[SubTaskResult]) -> bool:
    """Reuse a verified observation for bounded reads (currently distance)."""
    if not getattr(spec, "reuse_success_within_turn", False):
        return False
    for r in results:
        if r.status is not SubTaskStatus.OK or r.tool_name != spec.name or not r.observation:
            continue
        result = r.observation
        if spec.name == "places.measure_distance":
            if str(result.get("origin_kind") or "") == "saved_profile":
                origin_matches = bool(arguments.get("use_saved_origin"))
            else:
                origin_matches = not bool(arguments.get("use_saved_origin")) and _same_public_place_reference(
                    arguments.get("origin"), result.get("origin_label"),
                )
            if origin_matches and _same_public_place_reference(
                arguments.get("destination"), result.get("destination_label"),
            ):
                return True
    return False




def _run_sub_task(
    task: SubTask, turn_ctx: Any, prior_observations: list[dict[str, Any]],
    *, seen_keys: set[tuple[str, str]], step_counts: dict[str, int],
    read_budget_state: dict[str, int] | None = None,
    planner_write_intent: str = "none",
    guard_lock: threading.Lock,
    on_progress: ProgressCallback | None, run_id: str, trace: dict[str, Any],
    debug_enabled: bool = False,
) -> tuple[list[SubTaskResult], SubAgentMetrics | None]:
    """Run a single sub-task: slice context, call sub-agent, guard, execute.

    The sub-agent may emit multiple tool calls; each proposal is guarded and
    executed independently. A failing call does not discard the others, and
    Duplicate detection and the one-write budget are global to the run; the
    read budget is counted per task id. Shared state is guarded by guard_lock,
    never held around LLM or tool calls.
    """
    if read_budget_state is None:
        read_budget_state = {"count": 0}
    context_slice = slice_for_agent(task.agent, turn_ctx, prior_observations=prior_observations)
    if (
        task.agent == "calendar"
        and getattr(turn_ctx, "_place_selection_requires_calendar_create", False)
    ):
        # This boolean is an internal protocol hint.  It contains no place or
        # provider identity and is consumed only by the Calendar semantic agent.
        context_slice.payload = {
            **context_slice.payload,
            "_place_selection_requires_create": True,
        }
    registration = registration_for(_SUB_AGENT_RUNNERS.get(task.agent))
    if registration is None:
        return [SubTaskResult(task_id=task.id, status=SubTaskStatus.SKIPPED,
                              skip_reason=f"no runner for agent {task.agent}")], None

    print(f"\n  [{task.id}] sub_agent={task.agent}")
    _emit_progress(on_progress, "subagent_started", trace=trace, agent_run_id=run_id,
                    task_id=task.id, agent=task.agent)
    if debug_enabled:
        append_debug_event(
            run_id, "subagent_started", task_id=task.id, agent=task.agent,
            task_brief=task.task_brief, depends_on=task.depends_on,
            input_payload=context_slice.payload, prior_observations=prior_observations,
        )

    def _create_runtime_confirmation(**kwargs: Any) -> str:
        with guard_lock:
            if step_counts.get("__writes", 0) >= 1:
                raise ValueError("public confirmation budget exhausted")
            step_counts["__writes"] = 1
        return ConfirmationManager(_CONFIRMATIONS).create_confirmation(
            room_id=turn_ctx.room_id, surface=SURFACE_PUBLIC, **kwargs,
        )

    guarded_executor = GuardedReadExecutor(
        task_id=task.id,
        agent_name=task.agent,
        turn_ctx=turn_ctx,
        seen_keys=seen_keys,
        guard_lock=guard_lock,
        on_progress=on_progress,
        run_id=run_id,
        trace=trace,
        emit_progress=_emit_progress,
        append_debug_event=append_debug_event,
        debug_enabled=debug_enabled,
        prior_observations=list(prior_observations),
        max_reads=MAX_READS,
        global_read_count=read_budget_state,
        global_max_reads=MAX_TOTAL_READS,
        create_confirmation=_create_runtime_confirmation,
        print_llm_metrics=_print_llm_metrics,
    )
    guarded_executor.runtime_state["planner_write_intent"] = planner_write_intent
    guarded_executor.supersede_confirmation = lambda **kwargs: ConfirmationManager(
        _CONFIRMATIONS
    ).supersede_active(**kwargs)
    guarded_executor.step_prefix = registration.step_prefix
    # Keep the existing Scheduler test/provider seam while the guarded
    # adapter remains the single execution boundary for every runtime.
    guarded_executor.execute_tool_fn = execute_tool
    if registration.before_run is not None:
        registration.before_run(task, guarded_executor)

    try:
        raw_runner_output = _invoke_registered_runner(
            registration, context_slice, task, guarded_executor,
        )
        runner_result, agent_metrics = normalize_runner_output(raw_runner_output)
        if task.agent == "places" and agent_metrics and agent_metrics.rejected_calls:
            diagnostics = trace.setdefault("place_diagnostics", {})
            diagnostics["tool_intent_mismatch_codes"] = list(dict.fromkeys(
                [
                    *list(diagnostics.get("tool_intent_mismatch_codes") or []),
                    *[
                        str(code)[:80]
                        for code in agent_metrics.rejected_calls
                        if str(code).strip()
                    ],
                ]
            ))[:4]
    except Exception as exc:
        agent_metrics = SubAgentMetrics(error=str(exc))
        if registration.after_run is not None:
            registration.after_run(
                task,
                guarded_executor,
                TaskRunnerResult.from_completed([SubTaskResult(
                    task_id=task.id,
                    status=SubTaskStatus.FAILED,
                    error_code="sub_agent_exception",
                )]),
                agent_metrics,
            )
        print(f"  [{task.id}] sub_agent EXCEPTION: {type(exc).__name__}")
        return [SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED, error_code="sub_agent_exception")], agent_metrics

    if agent_metrics:
        _print_llm_metrics(f"{task.id}:{task.agent}", agent_metrics)
        if agent_metrics.error:
            print(f"  [{task.id}] error=sub_agent_failed")

    if runner_result.completed_results is not None:
        if registration.after_run is not None:
            registration.after_run(task, guarded_executor, runner_result, agent_metrics)
        completed = list(runner_result.completed_results)
        if task.agent == "match":
            trace.setdefault("match_results", []).extend({
                "intent": task.match_intent or "missing",
                "tool": item.tool_name,
                "status": item.status.value,
                "code": item.error_code or (item.observation or {}).get("match_runtime", {}).get("code") or "confirmation_prepared",
            } for item in completed)
        # Outcome codes are a server-owned control channel.  Only the
        # Calendar availability runtime may populate them; a malformed or
        # future specialist result cannot smuggle a branch signal into the
        # Scheduler.
        if task.outcome_contract != "calendar.availability.v1":
            for item in completed:
                item.outcome_codes = []
        return completed, agent_metrics

    proposals = list(runner_result.proposals or [])
    if registration.after_run is not None:
        registration.after_run(task, guarded_executor, runner_result, agent_metrics)

    if not proposals:
        if (
            task.agent == "relationship"
            and planner_write_intent == DATE_INVITATION_WRITE_INTENT
        ):
            failure_code = relationship_runtime.DATE_INVITATION_PROTOCOL_FAILURE_CODE
            return [SubTaskResult(
                task_id=task.id,
                status=SubTaskStatus.FAILED,
                error_code=failure_code,
                observation={
                    "failure": {
                        "code": failure_code,
                        "message": relationship_runtime.DATE_INVITATION_PROTOCOL_FAILURE_REPLY,
                    },
                },
            )], agent_metrics
        if (
            task.agent == "relationship"
            and planner_write_intent == DATE_COORDINATION_CANCEL_WRITE_INTENT
        ):
            failure_code = relationship_runtime.DATE_COORDINATION_CANCEL_PROTOCOL_FAILURE_CODE
            return [SubTaskResult(
                task_id=task.id,
                status=SubTaskStatus.FAILED,
                error_code=failure_code,
                observation={
                    "failure": {
                        "code": failure_code,
                        "message": relationship_runtime.DATE_COORDINATION_CANCEL_PROTOCOL_FAILURE_REPLY,
                    },
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

    bound_place = (
        _place_binding_for_turn(turn_ctx)
        if task.agent == "places" and task.place_mode in {"details", "reviews"}
        else None
    )
    bound_place_query = (
        _bound_place_query(turn_ctx)
        if task.agent == "places" and task.place_mode in {"details", "reviews"}
        else ""
    )
    results: list[SubTaskResult] = []
    for index, proposal in enumerate(proposals):
        if task.agent == "match" and not match_runtime.proposal_allowed(task.match_intent, proposal, allow_event=bool(turn_ctx.active_event_invitation)):
            trace.setdefault("match_actions", []).append({
                "intent": task.match_intent or "missing", "action": proposal.tool_name,
                "outcome": "intent_mismatch",
            })
            results.append(SubTaskResult(
                task_id=task.id, status=SubTaskStatus.FAILED,
                error_code="match_intent_mismatch",
                observation={"match_runtime": {"code": "intent_mismatch", "reply": "這次配對操作與你的要求不一致，沒有執行變更。"}},
            ))
            continue
        print(f"  [{task.id}#{index}] proposal: tool={proposal.tool_name}")

        with guard_lock:
            decision = guard_proposal(
                proposal, agent_name=task.agent,
                step_count=step_counts.get(f"__reads:{task.id}", 0),
                seen_keys=seen_keys,
                max_reads=MAX_READS,
            )
            trace["guard_results"].append(decision.code.value)
        if debug_enabled:
            append_debug_event(
                run_id, "function_call", task_id=task.id, agent=task.agent,
                call_index=index, call_id=f"{task.id}#{index}:{proposal.tool_name}",
                function=proposal.tool_name,
                planner_arguments=proposal.arguments,
                guard={"ok": decision.ok, "code": decision.code.value, "reason": decision.reason},
            )
        print(f"  [{task.id}#{index}] guard: ok={decision.ok}  code={decision.code.value}  reason={decision.reason}")

        if not decision.ok:
            if decision.code == GuardResultCode.WRITE_REQUIRES_CONFIRMATION:
                payload, preview = prepare_write_confirmation(
                    proposal.tool_name, proposal.arguments, turn_ctx._raw_ctx, turn_ctx,
                )
                if payload is None:
                    print(f"  [{task.id}#{index}] result=FAILED  preflight_rejected")
                    results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                                  tool_name=proposal.tool_name,
                                                  error_code="preflight_rejected",
                                                  observation={"preview": preview}))
                    continue
                with guard_lock:
                    if step_counts.get("__writes", 0) >= 1:
                        write_budget_available = False
                    else:
                        step_counts["__writes"] = 1
                        write_budget_available = True
                if not write_budget_available:
                    print(f"  [{task.id}#{index}] result=IGNORED (global one-write budget)")
                    results.append(SubTaskResult(
                        task_id=task.id,
                        status=SubTaskStatus.OK,
                        tool_name=proposal.tool_name,
                        observation={
                            "pending_confirmation": False,
                            "tool_name": proposal.tool_name,
                            "preview": "",
                            "ignored": "one_write_per_turn",
                        },
                    ))
                    continue
                ConfirmationManager(_CONFIRMATIONS).create_confirmation(
                    user_id=turn_ctx.user_id,
                    agent_name=task.agent,
                    tool_name=proposal.tool_name,
                    arguments=payload.get("arguments") or {},
                    payload=payload.get("data") or {},
                    origin_run_id=run_id,
                    preview=preview or "",
                    room_id=turn_ctx.room_id,
                    surface=SURFACE_PUBLIC,
                    interaction_mode=(
                        INTERACTION_BUBBLE
                        if proposal.tool_name in {
                            "match.start_search",
                            "match.cancel_search",
                            "match.decide_active_proposal",
                            "relationship.cancel_date_coordination",
                        }
                        else None
                    ),
                )
                print(f"  [{task.id}#{index}] result=OK (pending_confirmation for {proposal.tool_name})")
                transaction_state = _pending_relationship_transaction_state(
                    proposal.tool_name, payload,
                )
                pending_observation = {
                    "pending_confirmation": True,
                    "tool_name": proposal.tool_name,
                    "preview": preview or "",
                }
                if transaction_state is not None:
                    pending_observation["transaction_state"] = transaction_state
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                              tool_name=proposal.tool_name,
                                              observation=pending_observation))
                continue
            print(f"  [{task.id}#{index}] result=FAILED  guard_code={decision.code.value}")
            results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                          tool_name=proposal.tool_name, guard_code=decision.code))
            continue

        spec = TOOL_REGISTRY[proposal.tool_name]
        mentioned_ids = getattr(turn_ctx, "_mentioned_ids", []) or []
        if spec.argument_source in (ToolArgumentSource.MENTIONED_RELATIONSHIP,
                                    ToolArgumentSource.MENTIONED_CONTACTS) and not mentioned_ids:
            print(f"  [{task.id}#{index}] result=FAILED  error_code=mentioned_required")
            results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                          tool_name=proposal.tool_name,
                                          error_code="mentioned_required"))
            continue
        if getattr(spec, "reuse_success_within_turn", False):
            reused = _has_reusable_success(spec, proposal.arguments, results)
            if reused:
                print(f"  [{task.id}#{index}] result=OK (reused prior observation)")
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                              tool_name=proposal.tool_name,
                                              observation={"reused": True}))
                continue
        # Defensive fail-closed check for a malformed non-Web runner that
        # proposes a Web extraction. Normal Web tasks never enter this legacy
        # proposal path: Web Runtime uses GuardedReadExecutor directly.
        if proposal.tool_name == "web.extract":
            urls = [str(u) for u in (proposal.arguments.get("urls") or [])]
            if not _web_extract_urls_allowed(turn_ctx, results, urls):
                print(f"  [{task.id}#{index}] result=FAILED  error_code=web_extract_url_not_bound")
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                              tool_name=proposal.tool_name,
                                              error_code="web_extract_url_not_bound"))
                continue
        try:
            safe_args = executor_arguments_for_turn(
                spec, mentioned_ids,
                proposal.arguments if spec.argument_source.value == "planner_grounded" else None,
            )
        except Exception as exc:
            print(f"  [{task.id}#{index}] result=FAILED  error_code=executor_args_invalid")
            results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                          tool_name=proposal.tool_name,
                                          error_code="executor_args_invalid"))
            continue
        if (
            task.agent == "places"
            and task.place_mode in {"details", "reviews"}
            and proposal.tool_name == "places.resolve_place"
        ):
            if bound_place is None or not bound_place_query:
                print(f"  [{task.id}#{index}] result=FAILED  error_code=place_reference_unbound")
                results.append(SubTaskResult(
                    task_id=task.id,
                    status=SubTaskStatus.FAILED,
                    tool_name=proposal.tool_name,
                    error_code="place_reference_unbound",
                ))
                continue
            # Ignore any model-authored branch/name and query the provider with
            # the server-owned public name plus address summary. Identity is
            # checked again against the opaque snapshot after execution.
            safe_args = {**safe_args, "query": bound_place_query}
        elif (
            task.agent == "places"
            and task.place_mode == "details"
            and proposal.tool_name == "places.measure_distance"
        ):
            if bound_place is None or not bound_place_query:
                print(f"  [{task.id}#{index}] result=FAILED  error_code=place_reference_unbound")
                results.append(SubTaskResult(
                    task_id=task.id,
                    status=SubTaskStatus.FAILED,
                    tool_name=proposal.tool_name,
                    error_code="place_reference_unbound",
                ))
                continue
            safe_args = {**safe_args, "destination": bound_place_query}
        with guard_lock:
            key = tool_call_key(spec, safe_args)
            if key in seen_keys:
                print(f"  [{task.id}#{index}] guard: duplicate_call (re-checked under lock)")
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                              tool_name=proposal.tool_name,
                                              guard_code=GuardResultCode.DUPLICATE_CALL))
                continue
            read_key = f"__reads:{task.id}"
            if spec.risk is ToolRisk.READ and step_counts.get(read_key, 0) >= MAX_READS:
                print(f"  [{task.id}#{index}] guard: sub-task read budget exhausted")
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                              tool_name=proposal.tool_name,
                                              guard_code=GuardResultCode.STEP_LIMIT_EXCEEDED))
                continue
            if spec.risk is ToolRisk.READ and read_budget_state.get("count", 0) >= MAX_TOTAL_READS:
                print(f"  [{task.id}#{index}] guard: Public run read budget exhausted")
                trace["guard_results"].append(GuardResultCode.STEP_LIMIT_EXCEEDED.value)
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                              tool_name=proposal.tool_name,
                                              guard_code=GuardResultCode.STEP_LIMIT_EXCEEDED,
                                              error_code="public_read_budget_exhausted"))
                continue
            seen_keys.add(key)
            if spec.risk is ToolRisk.READ:
                step_counts[read_key] = step_counts.get(read_key, 0) + 1
                read_budget_state["count"] = read_budget_state.get("count", 0) + 1
        step_id = f"{task.id}#{index}:{proposal.tool_name}"
        _emit_progress(on_progress, "tool_started", trace=trace, agent_run_id=run_id,
                        step_id=step_id, text=spec.progress_text,
                        tool_name=proposal.tool_name)
        if debug_enabled:
            append_debug_event(
                run_id, "tool_started", task_id=task.id, agent=task.agent,
                step_id=step_id, function=proposal.tool_name,
                planner_arguments=proposal.arguments, executor_arguments=safe_args,
            )
        tool_started = time.perf_counter()
        try:
            tool_result = execute_tool(
                type("TC", (), {"name": proposal.tool_name, "arguments": safe_args})(),
                turn_ctx._raw_ctx, clock=turn_ctx.clock,
            )
        except Exception as exc:
            _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id,
                            step_id=step_id, outcome="error", tool_name=proposal.tool_name,
                            duration_ms=0)
            trace["tool_results"].append({"tool": proposal.tool_name, "ok": False, "code": "tool_exception"})
            if debug_enabled:
                append_debug_event(
                    run_id, "tool_finished", task_id=task.id, step_id=step_id,
                    function=proposal.tool_name, outcome="error", error_code="tool_exception",
                )
            print(f"  [{task.id}#{index}] tool EXCEPTION: {type(exc).__name__}")
            results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                          tool_name=proposal.tool_name, error_code="tool_exception"))
            continue
        tool_duration_ms = round((time.perf_counter() - tool_started) * 1000)
        print(f"  [{task.id}#{index}] tool_exec: {proposal.tool_name}  duration={tool_duration_ms}ms  ok={tool_result.ok}")
        if not tool_result.ok:
            _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id,
                            step_id=step_id, outcome="error", tool_name=proposal.tool_name,
                            duration_ms=tool_duration_ms)
            trace["tool_results"].append({"tool": proposal.tool_name, "ok": False, "code": tool_result.error_code})
            if debug_enabled:
                append_debug_event(
                    run_id, "tool_finished", task_id=task.id, step_id=step_id,
                    function=proposal.tool_name, outcome="error",
                    duration_ms=tool_duration_ms, error_code=tool_result.error_code,
                )
            print(f"  [{task.id}#{index}] result=FAILED  error_code={tool_result.error_code}")
            results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                          tool_name=proposal.tool_name, error_code=tool_result.error_code,
                                          observation=_failure_observation_from_tool_result(tool_result)))
            continue
        if (
            task.agent == "places"
            and task.place_mode in {"details", "reviews"}
            and proposal.tool_name == "places.resolve_place"
            and not _place_provider_identity_matches(
                bound_place,
                (tool_result.data or {}).get("place") if isinstance(tool_result.data, dict) else None,
            )
        ):
            _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id,
                            step_id=step_id, outcome="error", tool_name=proposal.tool_name,
                            duration_ms=tool_duration_ms)
            trace["tool_results"].append({
                "tool": proposal.tool_name,
                "ok": False,
                "code": "place_provider_identity_mismatch",
            })
            if debug_enabled:
                append_debug_event(
                    run_id, "tool_finished", task_id=task.id, step_id=step_id,
                    function=proposal.tool_name, outcome="error",
                    duration_ms=tool_duration_ms,
                    error_code="place_provider_identity_mismatch",
                )
            print(f"  [{task.id}#{index}] result=FAILED  error_code=place_provider_identity_mismatch")
            results.append(SubTaskResult(
                task_id=task.id,
                status=SubTaskStatus.FAILED,
                tool_name=proposal.tool_name,
                error_code="place_provider_identity_mismatch",
            ))
            continue
        private_data = tool_result.private_data or {}
        relationship_reference = (
            private_data.get("relationship_contact_reference")
            if isinstance(private_data, dict) else None
        )
        if isinstance(relationship_reference, dict):
            other_id = str(relationship_reference.get("other_id") or "")
            safe_label = str(relationship_reference.get("safe_label") or "")
            if other_id and safe_label:
                from .relationship_references import remember_contact
                remember_contact(turn_ctx.user_id, other_id, safe_label)
        _emit_progress(on_progress, "tool_finished", trace=trace, agent_run_id=run_id,
                        step_id=step_id, outcome="ok", tool_name=proposal.tool_name,
                        duration_ms=tool_duration_ms)
        trace["tool_results"].append({"tool": proposal.tool_name, "ok": True, "code": None})
        if debug_enabled:
            append_debug_event(
                run_id, "tool_finished", task_id=task.id, step_id=step_id,
                function=proposal.tool_name, outcome="ok", duration_ms=tool_duration_ms,
                result=tool_result.data,
            )
        print(f"  [{task.id}#{index}] result=OK")
        results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                       tool_name=proposal.tool_name, observation=tool_result.data))

    if any(r.status is SubTaskStatus.OK for r in results):
        print(f"  [{task.id}] result=OK ({len(results)} call(s))")
    elif results:
        print(f"  [{task.id}] result=FAILED (all {len(results)} call(s) failed)")
    return results, agent_metrics


def run_public_agent_turn_v3(
    ctx: AgentTurnContext, *, on_progress: ProgressCallback | None = None,
    on_token: Callable[[str], None] | None = None,
    debug_enabled: bool = False,
) -> AgentResult:
    """V3 sub-agent runtime entry point."""
    run_total_started = time.perf_counter()

    mentioned_ids, mention_overflow = validated_mentioned_contact_ids(ctx.user_id, ctx.mentioned_ids)
    ctx = ctx.model_copy(update={
        "mentioned_ids": mentioned_ids,
        "mention_overflow": bool(ctx.mention_overflow or mention_overflow),
    })
    mgr = ConfirmationManager(_CONFIRMATIONS)
    continuation_resolution: dict[str, Any] | None = None
    if ctx.choice_action is None and ctx.assessment_action is None:
        continuation_resolution = mgr.resolve_for_continuation(
            user_id=ctx.user_id,
            room_id=ctx.room_id,
            surface=SURFACE_PUBLIC,
        )
        if continuation_resolution:
            sync_choice_message_projection(
                messages_coll,
                room_id=ctx.room_id,
                projection=continuation_resolution,
            )
        # An awaiting assessment draft is itself a bubble choice. Continuing
        # the same room cancels the draft and then lets the new text reach the
        # ordinary planner. Pre-rollout sessions without a choice record fail
        # closed the same way instead of retaining text-token authority.
        awaiting = awaiting_assessment_commit(ctx.user_profile)
        if awaiting:
            cancel_assessment_session(
                ctx.user_id,
                str(awaiting.get("session_id") or ""),
                str(awaiting.get("kind") or ""),
            )
            profile_without_session = dict(ctx.user_profile)
            profile_without_session.pop("agentic_assessment_session", None)
            ctx = ctx.model_copy(update={"user_profile": profile_without_session})
    run_id = uuid.uuid4().hex
    clock = build_turn_clock(ctx.message)
    turn = build_public_agent_turn_context(ctx, clock=clock)
    turn._raw_ctx = ctx  # type: ignore[attr-defined]
    turn._mentioned_ids = mentioned_ids  # type: ignore[attr-defined]
    turn._place_resolution_origin_run_id = None  # type: ignore[attr-defined]
    turn._place_resolution_method = None  # type: ignore[attr-defined]
    turn._place_selection_requires_calendar_create = False  # type: ignore[attr-defined]
    trace: dict[str, Any] = {
        "plan": [], "guard_results": [], "tool_results": [],
        "event_sequence": [], "latency_ms": 0,
        "execution_mode": "dag", "llm_call_count": 0,
        "total_input_tokens": 0, "total_output_tokens": 0,
        "direct_chat_fallback_reason": None,
        "web_research": [],
        "place_diagnostics": {
            "resolution": {
                "status": "not_evaluated",
                "method": None,
                "candidate_count": 0,
                "origin_run_id": None,
            },
            "candidate_count": 0,
            "presentation_persistence": "not_attempted",
            "source_message_linked": False,
            "calendar_flow": False,
            "selection_commit": {"status": "not_attempted", "method": None},
        },
        "result": {"handled": True, "conversation_intent": "", "fallback_reason": None},
    }
    if debug_enabled:
        begin_debug_run(run_id, ctx.user_id)
        append_debug_event(
            run_id, "run_started", agent_run_id=run_id,
            clock=clock.model_dump(mode="json"),
        )
    _emit_progress(on_progress, "run_started", trace=trace, agent_run_id=run_id)

    token_state = {"emitted": False}

    def emit_token_fragment(fragment: str) -> None:
        if on_token is None or not fragment:
            return
        token_state["emitted"] = True
        on_token(fragment)

    def replay_reply_tokens(reply_text: str) -> None:
        if on_token is None or token_state["emitted"] or not reply_text:
            return
        token_state["emitted"] = True
        # The reply is already validated and normalized at this boundary. Keep
        # fragments comfortably below the HTTP sanitizer's 600-char cap without
        # adding seconds of artificial latency to long grounded answers.
        chunk_size = 120
        for start in range(0, len(reply_text), chunk_size):
            on_token(reply_text[start:start + chunk_size])
            time.sleep(0.01)

    _print_separator("V3 RUN START")
    print(f"  run_id={run_id}")
    print(f"  clock={clock.local_date} {clock.local_time} ({clock.weekday_zh_tw})")

    total_input_tokens = 0
    total_output_tokens = 0
    all_agent_metrics: list[tuple[str, SubAgentMetrics]] = []
    place_snapshot: dict[str, Any] | None = None

    def _finalize_debug(result: AgentResult) -> AgentResult:
        # Calendar context can outlive the turn that created it. Only a write
        # committed in this branch may upgrade an otherwise-casual result;
        # planned Calendar tasks set their own reason at the normal boundary.
        if (
            str(result.profile_write_reason or "casual") in {"", "casual"}
            and result.calendar_state_changed
        ):
            result = result.model_copy(update={
                "profile_write_reason": "calendar_operation",
            })
        # This is the single public V3 reply boundary.  Synthesizer output and
        # deterministic branches both pass through it, so model drift to
        # Simplified Chinese cannot leak to the user.  Opaque URLs/code/JSON
        # fragments are protected by normalize_public_reply.
        confirmation_run_id = result.agent_run_id or run_id
        button_choice_created = mgr.choice_for_run(
            user_id=ctx.user_id,
            room_id=ctx.room_id,
            surface=SURFACE_PUBLIC,
            origin_run_id=confirmation_run_id,
        )

        def _button_copy(text: Any) -> str:
            value = str(text or "")
            if not button_choice_created:
                return value
            return (
                value
                .replace(" 回覆「確認」才會真的變更。", " 請選擇是否套用這次變更。")
                .replace(
                    "要我現在開始找就回覆「確認」；也可以先補充條件。",
                    "要我現在開始找嗎？你也可以先補充條件。",
                )
                .replace(
                    "回覆「確認」就開始，也可以回覆「取消」。",
                    "請選擇是否開始。",
                )
            )

        normalized_reply = normalize_public_reply(_button_copy(result.reply))
        messages = [
            _button_copy(item).strip()
            for item in (result.messages or [])
            if str(item).strip()
        ]
        if not messages and normalized_reply:
            messages = [normalized_reply]
        presentation = build_presentation(messages, result.presentation_class) if messages else None
        if presentation is None and normalized_reply:
            presentation = build_presentation([normalized_reply], "fallback")
        if presentation is not None:
            place_labels = tuple(
                str(projection.get("label") or "").strip()
                for projection in (
                    getattr(turn, "place_reference_resolution", None),
                    getattr(turn, "recent_place_reference", None),
                )
                if isinstance(projection, dict) and str(projection.get("label") or "").strip()
            )
            messages = [
                _restore_snapshot_candidate_labels(
                    message, place_snapshot, extra_labels=place_labels,
                )
                for message in presentation.messages
            ]
            normalized_reply = "\n\n".join(messages)
        result = result.model_copy(update={"reply": normalized_reply, "messages": messages})
        # Confirmation preview hashes must bind the exact text that the HTTP
        # layer will persist, after this final normalization boundary.
        mgr.bind_final_preview(
            user_id=ctx.user_id,
            origin_run_id=confirmation_run_id,
            final_content=normalized_reply,
        )
        choice_prompt = mgr.choice_for_run(
            user_id=ctx.user_id,
            room_id=ctx.room_id,
            surface=SURFACE_PUBLIC,
            origin_run_id=confirmation_run_id,
        )
        resolution = result.choice_resolution or continuation_resolution
        if choice_prompt and continuation_resolution:
            replacement = mgr.mark_superseded(
                user_id=ctx.user_id,
                room_id=ctx.room_id,
                surface=SURFACE_PUBLIC,
                choice_id=str(continuation_resolution.get("id") or ""),
            )
            if replacement:
                resolution = replacement
                sync_choice_message_projection(
                    messages_coll,
                    room_id=ctx.room_id,
                    projection=replacement,
                )
        result = result.model_copy(update={
            "choice_prompt": choice_prompt or result.choice_prompt,
            "choice_resolution": resolution,
        })
        if debug_enabled:
            finish_debug_run(
                run_id, status="completed",
                response={
                    "reply": result.reply or "",
                    "fallback_reason": result.fallback_reason,
                    "agent_mode": result.agent_mode,
                    "llm_call_count": int(trace.get("llm_call_count", 0) or 0),
                    "llm_call_metrics": result.llm_call_metrics or [],
                },
            )
        if not result.place_presentation_required:
            replay_reply_tokens(result.reply or "")
        return result

    def _assessment_result(outcome: dict, session: dict, run_id: str) -> AgentResult:
        state = str(outcome.get("session_state") or outcome.get("status") or "active")
        return AgentResult(
            handled=True, reply=str(outcome.get("reply") or "你可以換個方式說說看？"),
            conversation_intent="assessment", agent_run_id=run_id, agent_mode="v3",
            profile_write_allowed=False, profile_write_reason="assessment",
            assessment_state=state,
            assessment_kind=str(outcome.get("kind") or session.get("kind") or "") or None,
            assessment_revision=outcome.get("revision", int(session.get("revision", 0) or 0)),
        )

    if ctx.choice_action is not None:
        choice_id = str(ctx.choice_id or "")
        record = mgr.record_for_choice(
            user_id=ctx.user_id,
            room_id=ctx.room_id,
            surface=SURFACE_PUBLIC,
            choice_id=choice_id,
        )
        if record is None:
            parent = mgr.record_for_choice(
                user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC,
                choice_id=choice_id, require_pending=False,
            )
            followup = (
                match_runtime.offer_restart_continuation(mgr, ctx, turn, parent, run_id)
                if ctx.choice_action == "confirm" and parent else None
            )
            if followup:
                trace["match_continuations"] = [{"result": "replayed"}]
                _persist_trace(run_id, ctx, trace)
                return _finalize_debug(AgentResult(
                    handled=True, reply=followup["reply"], agent_mode="v3",
                    agent_run_id=followup.get("origin_run_id") or run_id,
                    choice_prompt=followup.get("choice"),
                    choice_resolution=mgr.choice_projection(user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC, choice_id=choice_id),
                    conversation_intent="confirmation", presentation_class="transaction",
                ))
            resolution = mgr.choice_projection(
                user_id=ctx.user_id,
                room_id=ctx.room_id,
                surface=SURFACE_PUBLIC,
                choice_id=choice_id,
            )
            if resolution:
                sync_choice_message_projection(
                    messages_coll, room_id=ctx.room_id, projection=resolution,
                )
            return _finalize_debug(AgentResult(
                handled=True,
                reply=(
                    "這個選擇已經過期或處理完成；我沒有再次執行。"
                    if resolution
                    else "我找不到這個待確認操作，因此沒有執行任何變更。"
                ),
                presentation_class="transaction",
                conversation_intent="confirmation_missing",
                agent_run_id=run_id,
                agent_mode="v3",
                choice_resolution=resolution,
            ))

        action_name = str(record.get("tool_name") or "")
        payload = dict(record.get("payload") or {})
        if action_name in {
            "match.decide_active_proposal",
            "match.decide_active_event_invitation",
        }:
            if ctx.choice_action == "cancel":
                mgr.cancel_choice(
                    user_id=ctx.user_id,
                    room_id=ctx.room_id,
                    surface=SURFACE_PUBLIC,
                    choice_id=choice_id,
                )
            resolution = mgr.choice_projection(
                user_id=ctx.user_id, room_id=ctx.room_id,
                surface=SURFACE_PUBLIC, choice_id=choice_id,
            )
            return _finalize_debug(AgentResult(
                handled=True,
                reply="人物、主題或活動牽線邀請的接受、婉拒或撤回已移到「阿月牽線」的卡片上，請到專區查看後再操作。",
                presentation_class="transaction",
                conversation_intent="match_hub_redirect",
                agent_run_id=run_id,
                agent_mode="v3",
                choice_resolution=resolution,
            ))
        if ctx.choice_action == "cancel":
            resolution = mgr.cancel_choice(
                user_id=ctx.user_id,
                room_id=ctx.room_id,
                surface=SURFACE_PUBLIC,
                choice_id=choice_id,
            )
            if resolution:
                sync_choice_message_projection(
                    messages_coll, room_id=ctx.room_id, projection=resolution,
                )
            if action_name == ASSESSMENT_COMMIT_ACTION:
                session = {
                    "session_id": payload.get("session_id"),
                    "kind": payload.get("kind"),
                    "revision": payload.get("revision"),
                }
                outcome = cancel_assessment_session(
                    ctx.user_id,
                    str(payload.get("session_id") or ""),
                    str(payload.get("kind") or ""),
                )
                result = _assessment_result(outcome, session, run_id)
                return _finalize_debug(result.model_copy(update={
                    "choice_resolution": resolution,
                }))
            cancellation_reply = match_choice_cancel_reply(record) or PUBLIC_PENDING_CANCEL_REPLY
            if action_name.startswith("match.") and (resolution or {}).get("state") != "cancelled":
                cancellation_reply = "這個操作已由另一個請求處理；請以最新配對狀態為準。"
            return _finalize_debug(AgentResult(
                handled=True,
                reply=cancellation_reply,
                presentation_class="transaction",
                conversation_intent="confirmation_cancelled",
                agent_run_id=run_id,
                agent_mode="v3",
                choice_resolution=resolution,
            ))

        assessment_outcome: dict[str, Any] = {}

        def _button_executor(
            tool_name: str,
            arguments: dict[str, Any],
            _user_id: str,
            bound_payload: dict[str, Any],
        ) -> tuple[bool, str, str | None]:
            if tool_name == ASSESSMENT_COMMIT_ACTION:
                outcome = commit_assessment_session(
                    ctx.user_id,
                    str(bound_payload.get("session_id") or ""),
                    expected_revision=int(bound_payload.get("revision", 0) or 0),
                    idempotency_key=(
                        f"assessment-commit:{bound_payload.get('session_id')}:"
                        f"{int(bound_payload.get('revision', 0) or 0)}"
                    ),
                )
                assessment_outcome.update(outcome)
                ok = str(outcome.get("status") or "") in {"committed", "already_committed"}
                return ok, str(outcome.get("reply") or "這份結果沒有完成套用。"), None if ok else str(outcome.get("status") or "assessment_commit_failed")
            return execute_write(
                tool_name, arguments, ctx, turn, run_id, 0,
                confirmation_id=None, payload=bound_payload,
            )

        results = mgr.execute_confirmed(
            user_id=ctx.user_id,
            choice_id=choice_id,
            room_id=ctx.room_id,
            surface=SURFACE_PUBLIC,
            interaction_mode=INTERACTION_BUBBLE,
            executor=_button_executor,
        )
        resolution = mgr.choice_projection(
            user_id=ctx.user_id,
            room_id=ctx.room_id,
            surface=SURFACE_PUBLIC,
            choice_id=choice_id,
        )
        if resolution:
            sync_choice_message_projection(
                messages_coll, room_id=ctx.room_id, projection=resolution,
            )
        if action_name == ASSESSMENT_COMMIT_ACTION and assessment_outcome:
            result = _assessment_result(assessment_outcome, payload, run_id)
            return _finalize_debug(result.model_copy(update={
                "choice_resolution": resolution,
            }))
        if not results:
            return _finalize_debug(AgentResult(
                handled=True,
                reply="這次沒有執行新的變更；操作可能已由另一個請求處理。",
                presentation_class="transaction",
                conversation_intent="confirmation_missing",
                agent_run_id=run_id,
                agent_mode="v3",
                choice_resolution=resolution,
            ))
        if action_name.startswith("match."):
            parent = mgr.record_for_choice(
                user_id=ctx.user_id, room_id=ctx.room_id, surface=SURFACE_PUBLIC,
                choice_id=choice_id, require_pending=False,
            )
            followup = match_runtime.offer_restart_continuation(mgr, ctx, turn, parent or {}, run_id)
            reply = "\n".join(str((item.get("data") or {}).get("reply") or "") for item in results).strip()
            trace.setdefault("match_continuations", []).append({
                "result": "offered" if followup and followup.get("choice") else "not_offered",
                "outcomes": [item.get("error_code") or "applied" for item in results],
            })
            _persist_trace(run_id, ctx, trace)
            return _finalize_debug(AgentResult(
                handled=True, reply=followup["reply"] if followup else reply or "這次配對操作没有完成，沒有開始新的搜尋。",
                agent_run_id=(followup or {}).get("origin_run_id") or run_id,
                agent_mode="v3", conversation_intent="confirmation", presentation_class="transaction",
                choice_resolution=resolution, choice_prompt=(followup or {}).get("choice"),
                match_state_changed=any(bool(item.get("ok")) for item in results),
            ))
        synth_slice = slice_for_agent("synthesizer", turn, prior_observations=[{
            "task_id": "confirm", "status": "ok", "tool": None,
            "result": results, "error_code": None, "skip_reason": None,
        }])
        date_confirmation = any(
            isinstance(item, dict)
            and str(item.get("tool_name") or "") in {
                "relationship.start_date_coordination",
                "relationship.cancel_date_coordination",
            }
            for item in results
        )
        reply, _card_decision, synth_metrics = synthesizer.synthesize(
            synth_slice, on_token=None if date_confirmation else emit_token_fragment,
        )
        server_reply = _server_owned_confirmed_date_reply(results)
        if server_reply:
            reply = server_reply
            synth_metrics.presentation_messages = [server_reply]
            synth_metrics.presentation_class = "transaction"
        confirmed_updates: dict[str, Any] = {}
        for registration in _iter_runtime_registrations():
            if registration.confirmed_result_projector is None:
                continue
            projection = registration.confirmed_result_projector(results)
            if isinstance(projection, dict):
                confirmed_updates.update(projection)
        return _finalize_debug(AgentResult(
            handled=True,
            reply=reply,
            messages=synth_metrics.presentation_messages or [reply],
            presentation_class=synth_metrics.presentation_class,
            conversation_intent="confirmation",
            agent_run_id=run_id,
            agent_mode="v3",
            choice_resolution=resolution,
            match_state_changed=bool(
                action_name.startswith("match.")
                and any(bool(item.get("ok")) for item in results if isinstance(item, dict))
            ),
            **confirmed_updates,
        ))

    if ctx.assessment_action == "cancel":
        session = awaiting_assessment_commit(ctx.user_profile) or active_assessment_session(ctx.user_profile)
        if session:
            session_id = str(session.get("session_id") or "")
            kind = str(session.get("kind") or "")
            expires_at = float(session.get("expires_at", 0) or 0)
            if expires_at and expires_at <= time.time():
                outcome = expire_assessment_session(ctx.user_id, session_id, kind)
            else:
                outcome = cancel_assessment_session(ctx.user_id, session_id, kind)
            _print_separator("V3 RUN END")
            return _finalize_debug(_assessment_result(outcome, session, run_id))
        _print_separator("V3 RUN END")
        return _finalize_debug(AgentResult(
            handled=True,
            reply="目前沒有正在進行的測驗。",
            conversation_intent="assessment",
            agent_run_id=run_id,
            agent_mode="v3",
            assessment_state=None,
            assessment_kind=None,
            assessment_revision=None,
            profile_write_allowed=False,
            profile_write_reason="assessment",
        ))

    commit_session = awaiting_assessment_commit(ctx.user_profile)
    if commit_session:
        session_id = str(commit_session.get("session_id") or "")
        kind = str(commit_session.get("kind") or "")
        revision = int(commit_session.get("revision", 0) or 0)
        expires_at = float(commit_session.get("expires_at", 0) or 0)
        if expires_at and expires_at <= time.time():
            outcome = expire_assessment_session(ctx.user_id, session_id, kind)
            _print_separator("V3 RUN END")
            return _finalize_debug(_assessment_result(outcome, commit_session, run_id))
        choice = assessment_commit_choice(ctx.message)
        if choice == "none":
            _print_separator("V3 RUN END")
            return _finalize_debug(AgentResult(
                handled=True,
                reply="這份探索結果已整理好。回覆「確認」才會套用；想保留原本資料可以回覆「取消」。",
                conversation_intent="assessment", agent_run_id=run_id, agent_mode="v3",
                profile_write_allowed=False, profile_write_reason="assessment",
                assessment_state="awaiting_commit", assessment_kind=kind, assessment_revision=revision,
            ))
        if choice == "cancel":
            outcome = cancel_assessment_session(ctx.user_id, session_id, kind)
            _print_separator("V3 RUN END")
            return _finalize_debug(_assessment_result(outcome, commit_session, run_id))
        outcome = commit_assessment_session(
            ctx.user_id, session_id, expected_revision=revision,
            idempotency_key=f"assessment-commit:{session_id}:{revision}",
        )
        _print_separator("V3 RUN END")
        return _finalize_debug(_assessment_result(outcome, commit_session, run_id))

    active = active_assessment_session(ctx.user_profile)
    if active:
        session_id = str(active.get("session_id") or "")
        kind = str(active.get("kind") or "")
        expires_at = float(active.get("expires_at", 0) or 0)
        if expires_at and expires_at <= time.time():
            outcome = expire_assessment_session(ctx.user_id, session_id, kind)
            _print_separator("V3 RUN END")
            return _finalize_debug(_assessment_result(outcome, active, run_id))
        if assessment_cancel_choice(ctx.message):
            outcome = cancel_assessment_session(ctx.user_id, session_id, kind)
            _print_separator("V3 RUN END")
            return _finalize_debug(_assessment_result(outcome, active, run_id))
        outcome = advance_assessment_session(
            ctx.user_id, session_id, ctx.message, message_id=ctx.message_id,
        )
        result = _assessment_result(outcome, active, run_id)
        if str(outcome.get("session_state") or outcome.get("status") or "") == "awaiting_commit":
            button_reply = str(result.reply or "").replace(
                "這是新的草稿，回覆「確認」才會套用；想保留原本資料可回覆「取消」。",
                "這是新的草稿，請選擇是否套用。",
            )
            result = result.model_copy(update={"reply": button_reply})
            mgr.create_confirmation(
                user_id=ctx.user_id,
                agent_name="profile",
                tool_name=ASSESSMENT_COMMIT_ACTION,
                arguments={},
                payload={
                    "session_id": session_id,
                    "kind": kind,
                    "revision": int(outcome.get("revision", active.get("revision", 0)) or 0),
                },
                origin_run_id=run_id,
                preview=button_reply,
                room_id=ctx.room_id,
                surface=SURFACE_PUBLIC,
                interaction_mode=INTERACTION_BUBBLE,
            )
        _print_separator("V3 RUN END")
        return _finalize_debug(result)

    # Resolve a server-owned place referent before the Planner only as context.
    # This is not an intent gate: unresolved/ambiguous place language continues
    # through Planner/Calendar so typed date and time fields can be retained in
    # a draft while the user clarifies the place. Calendar draft candidates
    # retain precedence for their own same-domain ordinal follow-up.
    calendar_draft_candidates = (
        list((turn.calendar_draft or {}).get("candidates") or [])
        if isinstance(turn.calendar_draft, dict)
        else []
    )
    if not calendar_draft_candidates:
        place_resolution = resolve_message_reference(
            ctx.user_id, ctx.room_id, turn.message, commit_selection=False,
        )
        resolution_status = str(place_resolution.get("status") or "none")
        trace["place_diagnostics"]["resolution"] = {
            "status": resolution_status,
            "method": str(place_resolution.get("resolution_method") or "") or None,
            "candidate_count": int(place_resolution.get("candidate_count", 0) or 0),
            "origin_run_id": str(place_resolution.get("origin_run_id") or "") or None,
        }
        if resolution_status == "resolved":
            turn = turn.model_copy(update={
                "recent_place_candidates": place_candidate_projection(
                    get_place_candidate_set(ctx.user_id, ctx.room_id)
                ),
                "place_reference_resolution": public_place_resolution(place_resolution),
            })
            turn._place_resolution_origin_run_id = str(  # type: ignore[attr-defined]
                place_resolution.get("origin_run_id") or ""
            ).strip() or None
            turn._place_resolution_method = str(  # type: ignore[attr-defined]
                place_resolution.get("resolution_method") or ""
            ).strip() or None
            turn._raw_ctx = ctx  # type: ignore[attr-defined]
            turn._mentioned_ids = mentioned_ids  # type: ignore[attr-defined]
        elif resolution_status != "none":
            # Keep only safe, non-authoritative resolution state in the Planner
            # context. The Calendar runtime must not turn it into a provider
            # identity; it may use the status to preserve a date/time draft.
            issue_projection: dict[str, Any] = {
                "status": resolution_status,
                "candidate_count": int(place_resolution.get("candidate_count", 0) or 0),
            }
            options = place_resolution.get("candidate_options")
            if isinstance(options, list):
                issue_projection["candidate_options"] = [
                    {
                        "label": str(item.get("label") or "")[:80],
                        "address_summary": str(item.get("address_summary") or "")[:100],
                    }
                    for item in options[:3]
                    if isinstance(item, dict) and str(item.get("label") or "").strip()
                ]
            turn = turn.model_copy(update={"place_reference_resolution": issue_projection})
            turn._place_resolution_origin_run_id = None  # type: ignore[attr-defined]
            turn._place_resolution_method = None  # type: ignore[attr-defined]
            turn._raw_ctx = ctx  # type: ignore[attr-defined]
            turn._mentioned_ids = mentioned_ids  # type: ignore[attr-defined]
    elif trace["place_diagnostics"]["resolution"]["status"] == "not_evaluated":
        trace["place_diagnostics"]["resolution"] = {
            "status": "skipped_calendar_draft",
            "method": None,
            "candidate_count": len(calendar_draft_candidates),
            "origin_run_id": None,
        }

    turn = _abandon_place_followup_for_turn(turn)

    choice = "none" if continuation_resolution else confirmation_choice(ctx.message)
    pending_records = mgr.list_active(
        user_id=ctx.user_id,
        interaction_mode=INTERACTION_LEGACY,
    )
    # Decision confirmations were moved to the Hub card surface.  An old
    # bubble may still be present in a legacy room; consume neither decision
    # nor cancellation there, and point the user to the canonical card.
    legacy_match_decision = any(
        isinstance(record, dict)
        and str(record.get("tool_name") or "") in {
            "match.decide_active_proposal",
            "match.decide_active_event_invitation",
        }
        for record in pending_records
    )
    if legacy_match_decision and choice in {"confirm", "cancel"}:
        if choice == "cancel":
            mgr.cancel_legacy(user_id=ctx.user_id)
        return _finalize_debug(AgentResult(
            handled=True,
            reply="人物、主題或活動牽線邀請的接受、婉拒或撤回已移到「阿月牽線」的卡片上，請到專區查看後再操作。",
            presentation_class="transaction",
            conversation_intent="match_hub_redirect",
            agent_run_id=run_id,
            agent_mode="v3",
        ))
    active_offer = active_guidance_offer(ctx.user_profile or {})
    if not pending_records and active_offer and choice in {"confirm", "cancel"}:
        fingerprint = str(active_offer.get("fingerprint") or "")
        if choice == "cancel":
            decline_guidance_offer(ctx.user_id, fingerprint)
            return _finalize_debug(AgentResult(
                handled=True,
                reply="好，這次先不找人。",
                conversation_intent="match_guidance",
                agent_run_id=run_id,
                agent_mode="v3",
            ))
        if accept_guidance_offer(ctx.user_id, fingerprint):
            payload, preview = prepare_write_confirmation(
                "match.start_search", {}, ctx, turn,
            )
            if payload is None:
                return _finalize_debug(AgentResult(
                    handled=True,
                    reply=preview or "我現在還不能開始搜尋，請稍後再試。",
                    conversation_intent="match_guidance",
                    agent_run_id=run_id,
                    agent_mode="v3",
                ))
            confirmation_payload = dict(payload.get("data") or {})
            confirmation_payload.update({
                "source": "explicit_after_opportunity",
                "guidance_fingerprint": fingerprint,
            })
            mgr.create_confirmation(
                user_id=ctx.user_id,
                agent_name="match",
                tool_name="match.start_search",
                arguments=payload.get("arguments") or {},
                payload=confirmation_payload,
                origin_run_id=run_id,
                preview=preview or "",
                room_id=ctx.room_id,
                surface=SURFACE_PUBLIC,
            )
            synth_slice = slice_for_agent("synthesizer", turn, prior_observations=[{
                "task_id": "match_guidance",
                "status": "ok",
                "tool": None,
                "result": [{
                    "pending_confirmation": True,
                    "tool_name": "match.start_search",
                    "preview": preview or "",
                }],
                "error_code": None,
                "skip_reason": None,
            }])
            reply, _card_decision, synth_metrics = synthesizer.synthesize(synth_slice, on_token=emit_token_fragment)
            _print_llm_metrics("synthesizer", synth_metrics)
            return _finalize_debug(AgentResult(
                handled=True,
                reply=reply,
                messages=synth_metrics.presentation_messages or [reply],
                presentation_class=synth_metrics.presentation_class,
                conversation_intent="match_confirmation",
                agent_run_id=run_id,
                agent_mode="v3",
                match_readiness_state="ready",
            ))
    if choice in {"confirm", "cancel"} and not pending_records:
        if choice == "cancel":
            _print_separator("V3 RUN END")
            return _finalize_debug(AgentResult(
                handled=True,
                reply="目前沒有待取消的操作，因此沒有執行任何變更。",
                presentation_class="transaction",
                conversation_intent="confirmation_missing",
                agent_run_id=run_id,
                agent_mode="v3",
            ))
        # A confirmation token has authority only when ConfirmationManager
        # returned an active, persisted preview.  With no active state, even a
        # calendar draft is merely bounded context; let Planner interpret the
        # current acknowledgement (for example, accepting a prior read-only
        # Places retry offer) or ask for clarification. This deliberately
        # avoids a second keyword-based intent router.
        print(f"\n  [entry] confirmation={choice} → no typed pending state; defer to Planner")
        choice = "none"
    if choice == "confirm":
        print("\n  [entry] confirmation=confirm → executing preview-bound pending confirmation")
        results = mgr.execute_confirmed(
            user_id=ctx.user_id,
            interaction_mode=INTERACTION_LEGACY,
            executor=lambda tn, args, uid, payload: execute_write(
                tn, args, ctx, turn, run_id, 0,
                confirmation_id=None, payload=payload,
            ),
        )
        if not results:
            _print_separator("V3 RUN END")
            return _finalize_debug(AgentResult(
                handled=True,
                reply=(
                    "這次沒有執行新的變更；待確認操作可能已被另一個請求處理或已失效。"
                    "請先查看目前狀態，再重新提出需要的變更。"
                ),
                presentation_class="transaction",
                conversation_intent="confirmation_missing",
                agent_run_id=run_id,
                agent_mode="v3",
            ))
        synth_slice = slice_for_agent("synthesizer", turn, prior_observations=[{"task_id":"confirm","status":"ok","tool":None,"result":results,"error_code":None,"skip_reason":None}])
        reply, _card_decision, synth_metrics = synthesizer.synthesize(synth_slice, on_token=emit_token_fragment)
        server_reply = _server_owned_confirmed_date_reply(results)
        if server_reply:
            reply = server_reply
            synth_metrics.reply_source = "verified_observation"
            synth_metrics.presentation_messages = [server_reply]
            synth_metrics.presentation_blocks = None
            synth_metrics.presentation_class = "transaction"
        _print_llm_metrics("synthesizer", synth_metrics)
        total_input_tokens += synth_metrics.input_tokens
        total_output_tokens += synth_metrics.output_tokens
        _print_separator("V3 RUN END")
        print(f"  total_tokens={total_input_tokens + total_output_tokens} (in={total_input_tokens} out={total_output_tokens})")
        confirmed_updates: dict[str, Any] = {}
        for registration in _iter_runtime_registrations():
            if registration.confirmed_result_projector is None:
                continue
            projection = registration.confirmed_result_projector(results)
            if isinstance(projection, dict):
                confirmed_updates.update(projection)
        return _finalize_debug(AgentResult(
            handled=True, reply=reply,
            messages=synth_metrics.presentation_messages or [reply],
            presentation_class=synth_metrics.presentation_class,
            agent_run_id=run_id, agent_mode="v3",
            match_state_changed=any(
                isinstance(item, dict)
                and str(item.get("tool_name") or "").startswith("match.")
                and bool(item.get("ok"))
                for item in results
            ),
            **confirmed_updates,
        ))
    if choice == "cancel":
        print("\n  [entry] confirmation=cancel → clearing pending confirmations")
        mgr.cancel_legacy(user_id=ctx.user_id)
        _print_separator("V3 RUN END")
        return _finalize_debug(AgentResult(
            handled=True, reply=PUBLIC_PENDING_CANCEL_REPLY, agent_run_id=run_id, agent_mode="v3",
        ))

    # Normal flow: Planner → execute DAG → synthesizer
    _print_separator("PLANNER")
    if debug_enabled:
        append_debug_event(run_id, "stage_started", stage="planner", label="Planner 正在拆解任務")
    planner_started = time.perf_counter()
    try:
        plan, planner_metrics = plan_turn(turn)
    except Exception as exc:
        # Planner is an interpretation boundary, not an authority boundary.
        # An unexpected prompt/provider-shape bug must fail closed before any
        # domain tool (especially a write) can run.  Keep the public reply
        # generic while retaining a run-correlated, locals-free stack trace.
        _LOGGER.error(
            "V3 Planner escaped its failure contract run_id=%s error_type=%s\n%s",
            run_id,
            type(exc).__name__,
            "".join(traceback.format_tb(exc.__traceback__)),
        )
        planner_metrics = PlannerMetrics(
            duration_ms=round((time.perf_counter() - planner_started) * 1000),
            failure_code="planner_internal_error",
            error=type(exc).__name__,
            attempts=[{
                "attempt": 0,
                "status": "internal_error",
                "failure_code": "planner_internal_error",
                "error": type(exc).__name__,
            }],
        )
        plan = None
    _print_llm_metrics("planner", planner_metrics)
    total_input_tokens += planner_metrics.input_tokens
    total_output_tokens += planner_metrics.output_tokens
    if planner_metrics.error:
        print("  [planner] error=planner_failed")
    if planner_metrics.failure_code:
        print(
            f"  [planner] failure_code={planner_metrics.failure_code}"
            f" retry_count={planner_metrics.retry_count}"
        )
    if debug_enabled:
        append_debug_event(
            run_id, "planner_completed", stage="planner",
            status="ok" if plan is not None else "failed",
            prompt_raw=planner_metrics.prompt_raw,
            available_functions=planner_metrics.tools_raw or [],
            function_calls=planner_metrics.tool_calls_raw or [],
            content_raw=planner_metrics.raw_content,
            prompt_version=planner_metrics.prompt_version,
            error=planner_metrics.error,
            retry_count=planner_metrics.retry_count,
            retry_reason=planner_metrics.retry_reason,
            failure_code=planner_metrics.failure_code,
            attempts=planner_metrics.attempts,
            metrics={
                "input_tokens": planner_metrics.input_tokens,
                "output_tokens": planner_metrics.output_tokens,
                "duration_ms": planner_metrics.duration_ms,
                "llm_call_count": _metric_call_count(planner_metrics),
                "requested_model_tier": planner_metrics.requested_model_tier,
                "model_name": _metric_model_name(planner_metrics),
                "prompt_version": planner_metrics.prompt_version,
                "retry_count": planner_metrics.retry_count,
                "retry_reason": planner_metrics.retry_reason,
                "failure_code": planner_metrics.failure_code,
            },
            llm_requests=planner_metrics.llm_requests or [],
            mode=planner_metrics.decision_mode,
            direct_chat_fallback_reason=planner_metrics.direct_chat_fallback_reason or None,
            product_info_fallback_reason=planner_metrics.product_info_fallback_reason or None,
        )

    if plan is None:
        trace["llm_call_count"] = _metric_call_count(planner_metrics)
        trace["total_input_tokens"] = total_input_tokens
        trace["total_output_tokens"] = total_output_tokens
        trace["latency_ms"] = round((time.perf_counter() - run_total_started) * 1000)
        print("\n  [planner] result=FAILED → fail closed")
        _print_separator("V3 RUN END")
        print(f"  total_tokens={total_input_tokens + total_output_tokens} (in={total_input_tokens} out={total_output_tokens})")
        print(f"  [llm] total_calls={_metric_call_count(planner_metrics)}")
        trace["planner_failure"] = {
            "stage": "fallback",
            "failure_code": planner_metrics.failure_code,
            "retry_count": planner_metrics.retry_count,
            "retry_reason": planner_metrics.retry_reason,
            "attempts": _privacy_safe_planner_attempts(planner_metrics),
        }
        trace["result"] = {
            "handled": True,
            "conversation_intent": "clarification",
            "fallback_reason": "planner_invalid",
        }
        _persist_trace(run_id, ctx, trace)
        reply = _planner_failure_reply(turn)
        return _finalize_debug(AgentResult(
            handled=True, reply=reply,
            agent_run_id=run_id,
            agent_mode="v3",
            fallback_reason="planner_invalid",
            profile_write_reason="unknown",
        ))

    plan = normalize_plan_for_execution(plan, turn.message)
    plan, hours_fallback_injected = _ensure_place_hours_fallback(plan, turn.message)
    trace["place_diagnostics"]["hours_fallback_injected"] = hours_fallback_injected
    trace["place_diagnostics"]["task_modes"] = {
        task.id: task.place_mode
        for task in plan.tasks
        if task.agent == "places"
    }
    place_selection = getattr(plan, "place_selection", None)
    resolution_status = str(
        (getattr(turn, "place_reference_resolution", None) or {}).get("status") or "none"
    )
    if place_selection is not None:
        selection_text = str(getattr(place_selection, "selection_text", "") or "").strip()
        if not selection_text:
            place_resolution = {
                "status": "invalid_selection",
                "candidate_count": 0,
            }
        else:
            # Planner's selection text is an intent hint and may be normalized
            # (for example, 「第二個」 versus the user's 「第二間」). The
            # server resolver owns the actual place identity, so always give it
            # the complete original message instead of requiring a literal
            # substring match against model output.
            place_resolution = resolve_message_reference(
                ctx.user_id, ctx.room_id, turn.message, commit_selection=False,
            )
            if place_resolution.get("status") == "none":
                # A Planner place hint without a matching durable snapshot is
                # still a place continuation, but it is not permission to use
                # the model's guessed title as a provider identity.
                place_resolution = {
                    "status": "unavailable",
                    "candidate_count": 0,
                }
        resolution_status = str(place_resolution.get("status") or "none")
        trace["place_diagnostics"]["resolution"] = {
            "status": resolution_status,
            "method": str(place_resolution.get("resolution_method") or "") or None,
            "candidate_count": int(place_resolution.get("candidate_count", 0) or 0),
            "origin_run_id": str(place_resolution.get("origin_run_id") or "") or None,
        }
        if resolution_status == "resolved":
            turn = turn.model_copy(update={
                "place_reference_resolution": public_place_resolution(place_resolution),
            })
            turn._place_resolution_origin_run_id = str(  # type: ignore[attr-defined]
                place_resolution.get("origin_run_id") or ""
            ).strip() or None
            turn._place_resolution_method = str(  # type: ignore[attr-defined]
                place_resolution.get("resolution_method") or ""
            ).strip() or None
        else:
            issue_projection: dict[str, Any] = {
                "status": resolution_status,
                "candidate_count": int(place_resolution.get("candidate_count", 0) or 0),
            }
            options = place_resolution.get("candidate_options")
            if isinstance(options, list):
                issue_projection["candidate_options"] = [
                    {
                        "label": str(item.get("label") or "")[:80],
                        "address_summary": str(item.get("address_summary") or "")[:100],
                    }
                    for item in options[:3]
                    if isinstance(item, dict) and str(item.get("label") or "").strip()
                ]
            turn = turn.model_copy(update={
                "place_reference_resolution": issue_projection,
            })
            turn._place_resolution_origin_run_id = None  # type: ignore[attr-defined]
            turn._place_resolution_method = None  # type: ignore[attr-defined]
        turn._raw_ctx = ctx  # type: ignore[attr-defined]
        turn._mentioned_ids = mentioned_ids  # type: ignore[attr-defined]

    resolved_selection = getattr(turn, "place_reference_resolution", None)
    selection_method = str(getattr(turn, "_place_resolution_method", "") or "")
    has_place_selection_read = any(
        task.agent == "places" and task.place_mode in {"details", "reviews"}
        for task in plan.tasks
    )
    has_calendar_selection = bool(
        place_selection is not None
        and any(
            task.agent == "calendar" and task.outcome_contract is None
            for task in plan.tasks
        )
    )
    explicit_new_selection = bool(
        selection_method
        and selection_method not in {"selected_pronoun", "selected_deictic"}
    )
    should_commit_selection = bool(
        isinstance(resolved_selection, dict)
        and resolved_selection.get("status") == "resolved"
        and (
            has_calendar_selection
            or (has_place_selection_read and explicit_new_selection)
        )
    )
    if should_commit_selection:
        commit_input = {
            **resolved_selection,
            "origin_run_id": getattr(turn, "_place_resolution_origin_run_id", None),
        }
        selection_commit = commit_resolved_selection(
            ctx.user_id,
            ctx.room_id,
            commit_input,
        )
        commit_status = str(selection_commit.get("status") or "storage_unavailable")
        trace["place_diagnostics"]["selection_commit"] = {
            "status": commit_status,
            "method": selection_method or None,
        }
        if commit_status == "committed":
            selected_projection = selection_commit.get("selection")
            if isinstance(selected_projection, dict):
                turn = turn.model_copy(update={
                    "recent_place_reference": selected_projection,
                })
                turn._raw_ctx = ctx  # type: ignore[attr-defined]
                turn._mentioned_ids = mentioned_ids  # type: ignore[attr-defined]
        else:
            trace["llm_call_count"] = _metric_call_count(planner_metrics)
            trace["total_input_tokens"] = total_input_tokens
            trace["total_output_tokens"] = total_output_tokens
            trace["latency_ms"] = round((time.perf_counter() - run_total_started) * 1000)
            trace["result"] = {
                "handled": True,
                "conversation_intent": "clarification",
                "fallback_reason": "place_selection_persistence_failed",
            }
            _persist_trace(run_id, ctx, trace)
            reply = "這次有辨認到你選的店家，但暫時無法保存選擇，所以沒有繼續查詢。請重新選一次。"
            return _finalize_debug(AgentResult(
                handled=True,
                reply=reply,
                conversation_intent="clarification",
                agent_run_id=run_id,
                agent_mode="v3",
                fallback_reason="place_selection_persistence_failed",
                profile_write_reason="unknown",
            ))
    turn._place_selection_requires_calendar_create = bool(  # type: ignore[attr-defined]
        place_selection is not None
        and resolution_status == "resolved"
        and any(
            task.agent == "calendar" and task.outcome_contract is None
            for task in plan.tasks
        )
    )
    trace["place_diagnostics"]["calendar_flow"] = any(
        task.agent == "calendar" for task in plan.tasks
    )

    if plan.mode == "direct_chat":
        direct_reason = _direct_chat_block_reason(plan, turn, pending_records, active_offer)
        direct_validation = None
        direct_messages: list[str] = []
        if direct_reason is None:
            if plan.direct_messages:
                presentation = build_presentation(plan.direct_messages, "conversation")
                if presentation is None:
                    direct_reason = "messages_invalid"
                else:
                    direct_messages = presentation.messages
            else:
                direct_validation = validate_public_reply(
                    plan.direct_reply,
                    reject_internal_identifiers=True,
                    reject_structured_output=True,
                )
                if direct_validation.reply is None:
                    direct_reason = f"reply_{direct_validation.reason or 'invalid'}"
                else:
                    direct_messages = [direct_validation.reply]
        # A Planner tool result is complete JSON, not incremental user prose.
        # Stream clients use the existing plain-text Synthesizer path after
        # routing; never replay a completed Planner answer as fake live tokens.
        if direct_reason is None and on_token is not None:
            direct_reason = "streaming_composition"
        if direct_reason is not None:
            planner_metrics.direct_chat_fallback_reason = direct_reason
            trace["direct_chat_fallback_reason"] = direct_reason
            plan = _synthesizer_only_plan()
        else:
            reply = "\n\n".join(direct_messages)
            trace["execution_mode"] = "direct_chat"
            trace["llm_call_count"] = _metric_call_count(planner_metrics)
            trace["total_input_tokens"] = total_input_tokens
            trace["total_output_tokens"] = total_output_tokens
            trace["latency_ms"] = round((time.perf_counter() - run_total_started) * 1000)
            trace["plan"] = []
            trace["result"] = {
                "handled": True,
                "conversation_intent": "casual_chat",
                "fallback_reason": None,
            }
            _emit_progress(
                on_progress, "plan_created", trace=trace, agent_run_id=run_id,
                plan=[], planner_metrics={
                    "input_tokens": planner_metrics.input_tokens,
                    "output_tokens": planner_metrics.output_tokens,
                    "duration_ms": planner_metrics.duration_ms,
                    "call_count": _metric_call_count(planner_metrics),
                    "requested_model_tier": planner_metrics.requested_model_tier,
                    "model_name": _metric_model_name(planner_metrics),
                    "retry_count": planner_metrics.retry_count,
                    "retry_reason": planner_metrics.retry_reason,
                    "failure_code": planner_metrics.failure_code,
                     "mode": "direct_chat",
                },
            )
            if debug_enabled:
                append_debug_event(
                    run_id, "plan_created", plan=[], mode="direct_chat",
                    execution_mode="direct_chat", direct_reply=reply,
                    planner_metrics={
                        "input_tokens": planner_metrics.input_tokens,
                        "output_tokens": planner_metrics.output_tokens,
                        "duration_ms": planner_metrics.duration_ms,
                        "call_count": _metric_call_count(planner_metrics),
                        "requested_model_tier": planner_metrics.requested_model_tier,
                        "model_name": _metric_model_name(planner_metrics),
                        "retry_count": planner_metrics.retry_count,
                        "retry_reason": planner_metrics.retry_reason,
                        "failure_code": planner_metrics.failure_code,
                    },
                    prompt_raw=planner_metrics.prompt_raw,
                    content_raw=planner_metrics.raw_content,
                    function_calls=planner_metrics.tool_calls_raw or [],
                    available_functions=planner_metrics.tools_raw or [],
                    llm_requests=planner_metrics.llm_requests or [],
                )
                append_debug_event(
                    run_id, "direct_reply_selected", mode="direct_chat",
                    status="ok", duration_ms=planner_metrics.duration_ms, reply=reply,
                )
            _print_separator("DIRECT CHAT")
            print(f"  [direct_chat] reply={reply!r}")
            print(f"  [llm] total_calls={_metric_call_count(planner_metrics)}")
            _print_separator("V3 RUN END")
            print(f"  total_tokens={total_input_tokens + total_output_tokens} (in={total_input_tokens} out={total_output_tokens})")
            _persist_trace(run_id, ctx, trace)
            return _finalize_debug(AgentResult(
                handled=True,
                reply=reply,
                messages=[reply],
                conversation_intent="casual_chat",
                agent_run_id=run_id,
                agent_mode="v3",
                llm_call_metrics=[{
                    "agent": "planner",
                    "input_tokens": planner_metrics.input_tokens,
                    "output_tokens": planner_metrics.output_tokens,
                    "duration_ms": planner_metrics.duration_ms,
                    "call_count": _metric_call_count(planner_metrics),
                    "requested_model_tier": planner_metrics.requested_model_tier,
                    "model_name": _metric_model_name(planner_metrics),
                    "mode": "direct_chat",
                }],
            ))

    guidance_observations: list[dict[str, Any]] = []
    match_guidance_shown = False
    opportunity = getattr(plan, "opportunity", None)
    if opportunity is not None and opportunity.signal == "social_opening":
        if opportunity.confidence < 0.8 or not opportunity.evidence_span or opportunity.evidence_span not in turn.message:
            opportunity = None
    # An explicit match task owns matching semantics.  Ambient opportunity
    # guidance must never preempt a domain task or create a write confirmation.
    if opportunity is not None and any(task.agent == "match" for task in plan.tasks):
        opportunity = None
    if opportunity is not None and not any(task.agent != "synthesizer" for task in plan.tasks):
        assessment = assess_match_opportunity(ctx.user_profile or {}, ctx.user_id, explicit_search=False)
        if assessment.state == "ready" and claim_guidance_offer(
            ctx.user_id, assessment.fingerprint,
        ):
            match_guidance_shown = True
            guidance_observations.append({
                "task_id": "guidance",
                "status": "ok",
                "tool": None,
                "result": {
                    "match_opportunity_offer": {
                        "evidence_span": opportunity.evidence_span,
                        "expires_in_seconds": 900,
                    },
                },
                "error_code": None,
                "skip_reason": None,
            })

    _print_separator("PLAN")
    plan_tasks_json = [{
        "id": t.id,
        "agent": t.agent,
        "depends_on": t.depends_on,
        "task_brief": t.task_brief,
        **({"place_mode": t.place_mode} if t.agent == "places" and t.place_mode else {}),
        **({"evidence_policy": t.evidence_policy} if t.agent == "web" else {}),
        **({"web_mode": t.web_mode} if t.agent == "web" and t.web_mode else {}),
        **({"outcome_contract": t.outcome_contract} if t.outcome_contract else {}),
        **({"run_if": t.run_if.model_dump()} if t.run_if else {}),
    } for t in plan.tasks]
    trace["plan"] = [
        {
            "id": t.id,
            "agent": t.agent,
            "depends_on": t.depends_on,
            **({"place_mode": t.place_mode} if t.agent == "places" and t.place_mode else {}),
            **({"web_mode": t.web_mode} if t.agent == "web" and t.web_mode else {}),
            **({"match_intent": t.match_intent} if t.match_intent else {}),
            **({"outcome_contract": t.outcome_contract} if t.outcome_contract else {}),
            **({"run_if": t.run_if.model_dump()} if t.run_if else {}),
        }
        for t in plan.tasks
    ]
    for t in plan.tasks:
        print(f"  {t.id}: agent={t.agent}  depends_on={t.depends_on}")
    _emit_progress(on_progress, "plan_created", trace=trace, agent_run_id=run_id,
                    plan=plan_tasks_json,
                    planner_metrics={
                        "input_tokens": planner_metrics.input_tokens,
                        "output_tokens": planner_metrics.output_tokens,
                        "duration_ms": planner_metrics.duration_ms,
                    "call_count": _metric_call_count(planner_metrics),
                    "requested_model_tier": planner_metrics.requested_model_tier,
                    "model_name": _metric_model_name(planner_metrics),
                    "retry_count": planner_metrics.retry_count,
                    "retry_reason": planner_metrics.retry_reason,
                    "failure_code": planner_metrics.failure_code,
                    })
    if debug_enabled:
        append_debug_event(
            run_id, "plan_created", plan=plan_tasks_json,
            mode=planner_metrics.decision_mode or "tasks",
            execution_mode="dag",
            direct_chat_fallback_reason=planner_metrics.direct_chat_fallback_reason or None,
            product_info_fallback_reason=planner_metrics.product_info_fallback_reason or None,
            planner_metrics={
                "input_tokens": planner_metrics.input_tokens,
                "output_tokens": planner_metrics.output_tokens,
                "duration_ms": planner_metrics.duration_ms,
                "call_count": _metric_call_count(planner_metrics),
                "requested_model_tier": planner_metrics.requested_model_tier,
                "model_name": _metric_model_name(planner_metrics),
                "retry_count": planner_metrics.retry_count,
                "retry_reason": planner_metrics.retry_reason,
                "failure_code": planner_metrics.failure_code,
            },
            prompt_raw=planner_metrics.prompt_raw,
            content_raw=planner_metrics.raw_content,
            function_calls=planner_metrics.tool_calls_raw or [],
            available_functions=planner_metrics.tools_raw or [],
            llm_requests=planner_metrics.llm_requests or [],
        )

    guard_lock = threading.Lock()
    step_counts: dict[str, int] = {}
    read_budget_state: dict[str, int] = {"count": 0}
    seen_keys: set[tuple[str, str]] = set()
    task_results: dict[str, list[SubTaskResult]] = {}
    task_by_id = {task.id: task for task in plan.tasks}

    _print_separator("SUB-AGENT EXECUTION")
    for layer in _topological_layers(plan):
        layer_tasks = [t for t in layer if t.agent != "synthesizer"]
        if not layer_tasks:
            continue
        worker_count = min(len(layer_tasks), MAX_PARALLEL)

        def _run_one(task: SubTask) -> tuple[SubTask, list[SubTaskResult], SubAgentMetrics | None]:
            deps_ok = all(
                _dependency_completed(task_results.get(dep, []))
                for dep in task.depends_on
            )
            if not deps_ok:
                result = [SubTaskResult(task_id=task.id, status=SubTaskStatus.SKIPPED,
                                        skip_reason="dependency_failed")]
                print(f"\n  [{task.id}] SKIPPED (dependency_failed)")
                _emit_progress(on_progress, "subagent_finished", trace=trace, agent_run_id=run_id,
                                task_id=task.id, agent=task.agent,
                                status="skipped", error="dependency_failed",
                                input_tokens=0, output_tokens=0, duration_ms=0,
                                tool_name=None)
                return task, result, None
            condition_skip = _condition_skip_reason(task, task_results)
            if condition_skip is not None:
                result = [SubTaskResult(task_id=task.id, status=SubTaskStatus.SKIPPED,
                                        skip_reason=condition_skip)]
                print(f"\n  [{task.id}] SKIPPED ({condition_skip})")
                _emit_progress(on_progress, "subagent_finished", trace=trace, agent_run_id=run_id,
                                task_id=task.id, agent=task.agent,
                                status="skipped", error=condition_skip,
                                input_tokens=0, output_tokens=0, duration_ms=0,
                                tool_name=None)
                return task, result, None
            prior = _prior_observations_for(task, task_results, task_by_id)
            try:
                result, agent_metrics = _run_sub_task(task, turn, prior, seen_keys=seen_keys,
                                        step_counts=step_counts, read_budget_state=read_budget_state,
                                        planner_write_intent=plan.write_intent,
                                        guard_lock=guard_lock,
                                        on_progress=on_progress, run_id=run_id, trace=trace,
                                        debug_enabled=debug_enabled)
            except Exception as exc:
                # A sub-task must never crash the whole run: convert any
                # unexpected exception into a FAILED result so the synthesizer
                # can still answer with the observations that did succeed.
                print(f"  [{task.id}] sub_agent UNCAUGHT EXCEPTION: {type(exc).__name__}")
                agent_metrics = SubAgentMetrics(error=str(exc))
                result = [SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                        error_code="sub_agent_exception")]
            if agent_metrics:
                _emit_progress(on_progress, "subagent_finished", trace=trace, agent_run_id=run_id,
                                task_id=task.id, agent=task.agent,
                                status=result[0].status.value,
                                error=result[0].error_code or result[0].skip_reason or "",
                                input_tokens=agent_metrics.input_tokens,
                                output_tokens=agent_metrics.output_tokens,
                                duration_ms=agent_metrics.duration_ms,
                                tool_name=result[0].tool_name,
                                llm_call_count=_metric_call_count(agent_metrics),
                                requested_model_tier=agent_metrics.requested_model_tier,
                                model_name=_metric_model_name(agent_metrics))
                if debug_enabled:
                    append_debug_event(
                        run_id, "subagent_finished", task_id=task.id, agent=task.agent,
                        status=result[0].status.value,
                        error=result[0].error_code or result[0].skip_reason or agent_metrics.error or "",
                        input_tokens=agent_metrics.input_tokens,
                        output_tokens=agent_metrics.output_tokens,
                        duration_ms=agent_metrics.duration_ms,
                        prompt_raw=agent_metrics.prompt_raw,
                        input_payload=agent_metrics.input_payload,
                        available_functions=agent_metrics.tools_raw,
                        tool_calls_raw=agent_metrics.tool_calls_raw,
                        content_raw=agent_metrics.content_raw,
                        llm_call_count=_metric_call_count(agent_metrics),
                        requested_model_tier=agent_metrics.requested_model_tier,
                        model_name=_metric_model_name(agent_metrics),
                        proposal_parse_error=agent_metrics.error,
                        rejected_calls=agent_metrics.rejected_calls,
                        llm_requests=agent_metrics.llm_requests or [],
                        results=[item.model_dump(mode="json") for item in result],
                    )
            else:
                _emit_progress(on_progress, "subagent_finished", trace=trace, agent_run_id=run_id,
                                task_id=task.id, agent=task.agent,
                                status=result[0].status.value,
                                error=result[0].error_code or result[0].skip_reason or "",
                                input_tokens=0, output_tokens=0, duration_ms=0,
                                tool_name=result[0].tool_name)
            return task, result, agent_metrics

        if worker_count == 1:
            for task in layer_tasks:
                subtask, results, agent_metrics = _run_one(task)
                task_results[subtask.id] = results
                if agent_metrics:
                    total_input_tokens += agent_metrics.input_tokens
                    total_output_tokens += agent_metrics.output_tokens
                    all_agent_metrics.append((subtask.id, agent_metrics))
        else:
            with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="ayue-subagent") as pool:
                futures = {pool.submit(_run_one, t): t for t in layer_tasks}
                for fut in as_completed(futures):
                    subtask, results, agent_metrics = fut.result()
                    task_results[subtask.id] = results
                    if agent_metrics:
                        total_input_tokens += agent_metrics.input_tokens
                        total_output_tokens += agent_metrics.output_tokens
                        all_agent_metrics.append((subtask.id, agent_metrics))

    for task in plan.tasks:
        if task.id in task_results:
            continue
        if task.agent == "synthesizer":
            continue
        task_results[task.id] = [SubTaskResult(task_id=task.id, status=SubTaskStatus.SKIPPED,
                                                skip_reason="dependency_failed")]

    _print_separator("TASK RESULTS")
    for results in task_results.values():
        for r in results:
            print(f"  {r.task_id}: status={r.status.value}  tool={r.tool_name}  error={r.error_code}  skip={r.skip_reason}")
    web_diagnostics = []
    for results in task_results.values():
        for result in results:
            observation = result.observation if result.status is SubTaskStatus.OK else None
            if not isinstance(observation, dict) or observation.get("schema_version") != "web_research.v1":
                continue
            web_diagnostics.append({
                "task_id": result.task_id,
                "status": str(observation.get("status") or "")[:32],
                "coverage": str(observation.get("coverage") or "")[:32],
                "finding_count": len(observation.get("findings") or []),
                "source_count": len(observation.get("sources") or []),
            })
    trace["place_diagnostics"]["web"] = web_diagnostics

    _print_separator("SYNTHESIZER")
    prior: list[dict[str, Any]] = []
    prior.extend(guidance_observations)
    for task_id in sorted(task_results):
        for r in task_results[task_id]:
            if r.status is not SubTaskStatus.SKIPPED:
                prior.append(_observation_dict(task_id, r))
    synth_slice = slice_for_agent("synthesizer", turn, prior_observations=prior)
    synth_slice.payload["web_execution_failures"] = [
        {
            "task_id": task.id,
            "reason": result.error_code or result.skip_reason or "web_failed",
        }
        for task in plan.tasks
        if task.agent == "web"
        for result in task_results.get(task.id, [])
        if result.status in {SubTaskStatus.FAILED, SubTaskStatus.SKIPPED}
        and result.skip_reason != "places_hours_sufficient"
    ][:4]
    synth_slice.payload["presentation_mode"] = getattr(plan, "presentation_mode", "default")
    place_modes = {
        task.id: task.place_mode
        for task in plan.tasks
        if task.agent == "places" and task.place_mode
    }
    synth_slice.payload["place_modes"] = place_modes
    place_tasks = {
        task.id: task
        for task in plan.tasks
        if task.agent == "places"
    }
    discovery_results = [
        result
        for results in task_results.values()
        for result in results
        if (
            result.status is SubTaskStatus.OK
            and place_tasks.get(result.task_id) is not None
            and (place_tasks[result.task_id].place_mode or "discover") == "discover"
            and result.tool_name == "places.search_nearby"
        )
    ]
    candidate_cards = _public_place_cards(
        discovery_results,
        run_id=run_id,
        include_internal=True,
    )
    _emit_progress(on_progress, "subagent_started", trace=trace, agent_run_id=run_id,
                    task_id="synth", agent="synthesizer")
    if debug_enabled:
        append_debug_event(
            run_id, "subagent_started", task_id="synth", agent="synthesizer",
            task_brief="彙整所有已完成子任務並產生最終回覆",
            depends_on=[task.id for task in plan.tasks if task.agent != "synthesizer"],
            input_payload=synth_slice.payload, candidate_cards=candidate_cards,
        )
    # Candidate-bearing replies are persisted and published as one atomic
    # presentation. Do not stream model fragments before that handshake; the
    # normal conversational path still receives raw token streaming.
    has_grounded_observation = any(
        isinstance(item, dict)
        and (
            item.get("tool") in {"places.search_nearby", "places.resolve_place"}
            or str(item.get("tool") or "").startswith("calendar.")
            or str(item.get("tool") or "").startswith("web.")
            or (isinstance(item.get("result"), dict) and item["result"].get("schema_version") == "web_research.v1")
        )
        for item in prior
    )
    date_write_workflow = plan.write_intent in {
        DATE_INVITATION_WRITE_INTENT,
        DATE_COORDINATION_CANCEL_WRITE_INTENT,
    }
    synth_token_callback = (
        None if candidate_cards or has_grounded_observation or date_write_workflow
        else emit_token_fragment
    )
    reply, card_decision, synth_metrics = synthesizer.synthesize(
        synth_slice,
        candidate_cards=candidate_cards,
        on_token=synth_token_callback,
    )
    server_reply = _server_owned_date_coordination_reply(task_results)
    server_failure_reply = _server_owned_date_coordination_failure_reply(
        task_results, write_intent=plan.write_intent,
    )
    domain_tasks = [task for task in plan.tasks if task.agent != "synthesizer"]
    exclusive_date_transaction = bool(
        date_write_workflow
        and len(domain_tasks) == 1
        and domain_tasks[0].agent == "relationship"
    )
    if server_reply and exclusive_date_transaction:
        reply = server_reply
        synth_metrics.reply_source = "verified_observation"
        synth_metrics.presentation_messages = [server_reply]
        synth_metrics.presentation_blocks = None
        synth_metrics.presentation_class = "transaction"
    elif server_failure_reply:
        if exclusive_date_transaction:
            reply = server_failure_reply
            synth_metrics.reply_source = "verified_observation"
            synth_metrics.presentation_messages = [server_failure_reply]
            synth_metrics.presentation_blocks = None
            synth_metrics.presentation_class = "fallback"
        elif server_failure_reply not in reply:
            reply = "\n\n".join(part for part in (reply, server_failure_reply) if part)
            synth_metrics.presentation_messages = [reply]
    reply = normalize_public_reply(reply)
    _print_llm_metrics("synthesizer", synth_metrics)
    total_input_tokens += synth_metrics.input_tokens
    total_output_tokens += synth_metrics.output_tokens
    public_cards_enabled = public_place_cards_enabled()
    if not public_cards_enabled:
        card_decision = None
    print(f"  [synthesizer] card_decision={card_decision}")
    synth_status = "degraded" if synth_metrics.fallback_reason else "ok"
    synth_error = synth_metrics.error_code or ""
    _emit_progress(on_progress, "subagent_finished", trace=trace, agent_run_id=run_id,
                    task_id="synth", agent="synthesizer",
                    status=synth_status, error=synth_error,
                    input_tokens=synth_metrics.input_tokens,
                    output_tokens=synth_metrics.output_tokens,
                    duration_ms=synth_metrics.duration_ms,
                    tool_name=None)
    if debug_enabled:
        append_debug_event(
            run_id, "subagent_finished", task_id="synth", agent="synthesizer",
            status=synth_status, error=synth_error,
            input_tokens=synth_metrics.input_tokens,
            output_tokens=synth_metrics.output_tokens,
            duration_ms=synth_metrics.duration_ms,
            prompt_raw=synth_metrics.prompt_raw,
            input_payload=synth_metrics.input_payload or synth_slice.payload,
            available_functions=synth_metrics.tools_raw or [],
            tool_calls_raw=synth_metrics.tool_calls_raw or [],
            content_raw=synth_metrics.raw_content,
            reply_source=synth_metrics.reply_source,
            fallback_reason=synth_metrics.fallback_reason,
            error_code=synth_metrics.error_code,
            used_llm=synth_metrics.used_llm,
            llm_call_count=_metric_call_count(synth_metrics),
            requested_model_tier=synth_metrics.requested_model_tier,
            model_name=_metric_model_name(synth_metrics),
            llm_requests=synth_metrics.llm_requests or [],
            results=[{"reply": reply, "card_decision": card_decision}],
        )

    selected_place_cards = (
        _apply_card_decision(candidate_cards, card_decision)
        if public_cards_enabled else []
    )
    has_new_place_result = bool(discovery_results)
    place_snapshot = None
    place_persistence_failed = False
    if has_new_place_result:
        cards_by_ref = {
            str(card.get("candidate_ref") or ""): card
            for card in candidate_cards
            if str(card.get("candidate_ref") or "")
        }
        presented_refs = {
            str(reference)
            for reference in (synth_metrics.presented_candidate_refs or [])
            if str(reference) in cards_by_ref
        }
        # Candidate order is owned by the verified Places projection. Model
        # authored refs/ordinals may narrow the displayed set, but cannot
        # reorder it. Persist exactly the candidates visible in this reply.
        ordered_cards = [
            card for card in candidate_cards[:8]
            if str(card.get("candidate_ref") or "") in cards_by_ref
            and (
                not presented_refs
                or str(card.get("candidate_ref") or "") in presented_refs
            )
        ]
        ordered_ordinals = {
            str(card["candidate_ref"]): index
            for index, card in enumerate(ordered_cards, start=1)
        }
        try:
            place_snapshot = replace_presented_candidates(
                ctx.user_id,
                ctx.room_id,
                ordered_cards,
                presented_ordinals=ordered_ordinals,
                origin_run_id=run_id,
                source_message_id=ctx.message_id,
                # The HTTP boundary publishes this after saving the assistant
                # message. Direct unit/runtime callers without a source message
                # retain the historical synchronous seam.
                published=not bool(ctx.message_id),
            )
        except PlaceReferencePersistenceError as exc:
            place_persistence_failed = True
            _LOGGER.error(
                "V3 place presentation persistence failed run_id=%s reason=%s",
                run_id, str(exc),
            )
        if ordered_cards and place_snapshot is None and not place_persistence_failed:
            place_persistence_failed = True
        trace["place_diagnostics"].update({
            "candidate_count": len(ordered_cards),
            "presentation_persistence": (
                "failed" if place_persistence_failed
                else "saved_published" if place_snapshot and not ctx.message_id
                else "saved_pending_source_link" if place_snapshot
                else "failed"
            ),
            "source_message_linked": bool(place_snapshot and not ctx.message_id),
        })

    presentation_messages = synth_metrics.presentation_messages or [reply]
    if place_persistence_failed:
        reply = "這次找到地點，但暫時無法保存候選清單；請稍後再試，我還沒有替你建立行程。"
        presentation_messages = [reply]
        synth_metrics.presentation_messages = presentation_messages
        synth_metrics.presentation_class = "fallback"
        synth_metrics.fallback_reason = "place_presentation_persistence_failed"
        synth_metrics.presentation_blocks = None
    elif place_snapshot:
        # The visible candidate order must stay tied to the same immutable
        # snapshot whether optional place cards are enabled or not.  Cards can
        # be added by the UI, but they never change the text ordinal contract.
        presentation_messages = _server_ordered_place_messages(
            presentation_messages, place_snapshot,
        )
        reply = "\n\n".join(presentation_messages)
        synth_metrics.presentation_messages = presentation_messages
        if not public_cards_enabled:
            synth_metrics.presentation_blocks = []
        synth_metrics.presented_candidate_refs = [
            str(item.get("reference") or "")
            for item in (place_snapshot.get("candidates") or [])
            if isinstance(item, dict) and item.get("reference")
        ]
        synth_metrics.presented_candidate_bindings = [
            {"candidate_ref": str(item["reference"]), "presented_ordinal": int(item["ordinal"])}
            for item in (place_snapshot.get("candidates") or [])
            if isinstance(item, dict) and item.get("reference")
        ]
    else:
        synth_metrics.presentation_messages = presentation_messages
    presentation_blocks = _resolve_presentation_blocks(
        synth_metrics.presentation_blocks,
        selected_place_cards,
        presentation_messages,
    )
    place_cards = selected_place_cards
    place_cards = [
        {key: value for key, value in card.items() if key not in {"candidate_ref", "distance_m"}}
        for card in place_cards
    ]

    run_total_ms = round((time.perf_counter() - run_total_started) * 1000)

    _print_separator("V3 RUN END")
    print(f"  total_tokens={total_input_tokens + total_output_tokens}  (input={total_input_tokens}  output={total_output_tokens})")
    print(f"  total_duration={run_total_ms}ms")
    total_llm_calls = (
        _metric_call_count(planner_metrics)
        + sum(_metric_call_count(metrics) for _agent_id, metrics in all_agent_metrics)
        + _metric_call_count(synth_metrics)
    )
    print(f"  [llm] total_calls={total_llm_calls}")
    print(f"  agents_used={[aid for aid, _ in all_agent_metrics]}")
    print(f"{'='*60}\n")

    conversation_intent = (
        "product_info"
        if synth_metrics.presentation_class == "product_info"
        else "casual_chat"
    )
    relationship_recommendation_snapshot = next(
        (
            dict(result.observation)
            for results in task_results.values()
            for result in results
            if result.status is SubTaskStatus.OK
            and isinstance(result.observation, dict)
            and result.observation.get("schema_version") == "relationship_recommendation.v1"
        ),
        None,
    )
    result = AgentResult(
        handled=True,
        reply=reply,
        messages=presentation_messages,
        presentation_class=synth_metrics.presentation_class,
        conversation_intent=conversation_intent,
        agent_run_id=run_id,
        agent_mode="v3",
        fallback_reason=synth_metrics.fallback_reason,
        match_guidance_shown=match_guidance_shown,
        place_presentation_required=bool(
            place_snapshot is not None and not place_persistence_failed
        ),
        profile_write_reason=(
            "calendar_operation"
            if any(task.agent == "calendar" for task in plan.tasks)
            else "casual"
        ),
        relationship_recommendation_snapshot=relationship_recommendation_snapshot,
    )
    if place_cards:
        result.place_cards = place_cards
    if presentation_blocks:
        result.presentation_blocks = presentation_blocks
    result.sources = _public_sources(task_results)
    synth_call_count = _metric_call_count(synth_metrics)
    result.llm_call_metrics = [{
        "agent": "planner",
        "input_tokens": planner_metrics.input_tokens,
        "output_tokens": planner_metrics.output_tokens,
        "duration_ms": planner_metrics.duration_ms,
        "call_count": _metric_call_count(planner_metrics),
        "requested_model_tier": planner_metrics.requested_model_tier,
        "model_name": _metric_model_name(planner_metrics),
        "mode": "tasks",
    }] + [
        {
            "agent": agent_id,
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "duration_ms": m.duration_ms,
            "call_count": _metric_call_count(m),
            "requested_model_tier": m.requested_model_tier,
            "model_name": _metric_model_name(m),
        }
        for agent_id, m in all_agent_metrics
    ]
    if synth_call_count:
        result.llm_call_metrics.append({
            "agent": "synthesizer",
            "input_tokens": synth_metrics.input_tokens,
            "output_tokens": synth_metrics.output_tokens,
            "duration_ms": synth_metrics.duration_ms,
            "call_count": synth_call_count,
            "requested_model_tier": synth_metrics.requested_model_tier,
            "model_name": _metric_model_name(synth_metrics),
        })
    trace["latency_ms"] = run_total_ms
    trace["llm_call_count"] = (
        _metric_call_count(planner_metrics)
        + sum(_metric_call_count(metrics) for _agent_id, metrics in all_agent_metrics)
        + synth_call_count
    )
    trace["total_input_tokens"] = total_input_tokens
    trace["total_output_tokens"] = total_output_tokens
    trace["result"] = {
        "handled": result.handled,
        "conversation_intent": result.conversation_intent,
        "fallback_reason": result.fallback_reason,
    }
    trace["event_sequence"].append("final")
    _persist_trace(run_id, ctx, trace)
    return _finalize_debug(result)
