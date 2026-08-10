# social_demotest/services/ayue_agent/v3/scheduler.py
"""V3 Scheduler / Orchestrator: pure-code orchestration of the sub-agent runtime."""

from __future__ import annotations

import os
import inspect
import re
import threading
import time
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
from services.ayue_agent.product_identity import PUBLIC_PENDING_CANCEL_REPLY, PUBLIC_PLANNER_INVALID_REPLY
from services.assessment_session_service import (
    active_assessment_session, advance_assessment_session,
    assessment_cancel_choice, assessment_commit_choice,
    awaiting_assessment_commit, cancel_assessment_session,
    commit_assessment_session, expire_assessment_session,
)
from database import db

from .contracts import (
    AgentContextSlice, GuardDecision, GuardResultCode, Plan, SubTask,
    SubTaskResult, SubTaskStatus, ToolProposal, normalize_plan_for_execution,
)
from .context_slicer import slice_for_agent
from .guard import guard_proposal
from .guard import guard_calendar_commands
from .guarded_execution import GuardedReadExecutor, web_extract_urls_allowed as _web_extract_urls_allowed
from .calendar_commands import canonicalize_calendar_command, preflight_calendar_commands
from .calendar_drafts import (
    candidate_reference_allowed, clear_draft, get_draft, merge_command, resolved_target_replaced, save_draft,
)
from .calendar_references import (
    DRAFT_TARGET_REFERENCE_KEY,
    clear_reference,
    get_reference,
    remember_event,
    remember_resolved_target,
    remember_candidates,
    public_projection,
)
from services.calendar_service import resolve_owned_event_with_candidates
from .planner import plan_turn, PlannerMetrics, _synthesizer_only_plan
from . import synthesizer
from .synthesizer import SynthesizerMetrics
from .public_reply import build_presentation, validate_public_reply
from .sub_agents.base import StructuredAgentResult, SubAgentMetrics
from .confirmation import ConfirmationManager
from .sub_agents.calendar_agent import run as run_calendar
from .sub_agents.places_agent import run as run_places
from .sub_agents.match_agent import run as run_match
from .sub_agents.relationship_agent import run as run_relationship
from .sub_agents.profile_agent import run as run_profile
from .sub_agents.product_info_agent import run as run_product_info
from .place_projection import (
    MAX_PLACE_CARDS, MAX_PLACE_CARDS_PER_CATEGORY, _PLACE_CATEGORIES, _distance_label,
    _google_embed_url, _osm_embed_url, _place_candidate_ref,
    public_place_cards as _public_place_cards,
)
from . import web_runtime
from .write_executors import execute_write, prepare_write_confirmation
from .debug_trace import (
    append_event as append_debug_event,
    begin_run as begin_debug_run,
    finish_run as finish_debug_run,
)


ProgressCallback = Callable[[dict[str, Any]], Any]
MAX_READS = max(1, min(int(os.getenv("AYUE_SUBAGENT_MAX_READS", "3") or "3"), 3))
MAX_PARALLEL = max(1, min(int(os.getenv("AYUE_SUBAGENT_MAX_PARALLEL", "2") or "2"), 2))
_ASSESSMENT_START_CONFIRMATIONS = frozenset({"開始", "開始吧", "開始啊", "開始阿"})


def _assessment_start_confirmation_requested(message: str, pending: list[dict[str, Any]]) -> bool:
    """Accept bounded start wording only for an assessment confirmation."""
    compact = re.sub(r"\s+", "", str(message or "")).lower()
    if compact not in _ASSESSMENT_START_CONFIRMATIONS or len(pending) != 1:
        return False
    return str(pending[0].get("tool_name") or "") == "profile.start_assessment"


def _direct_chat_fast_path_enabled() -> bool:
    return os.getenv("AYUE_V3_SIMPLE_CHAT_FAST_PATH", "off").strip().lower() in {
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
    if getattr(turn, "calendar_draft", None):
        return "calendar_draft"
    if getattr(turn, "calendar_recent_mutation", None):
        return "recent_calendar_mutation"
    if getattr(turn, "recent_context_draft", None):
        return "active_draft"
    if getattr(turn, "active_proposal", None):
        return "active_match_proposal"
    if active_offer:
        return "active_match_guidance"
    if getattr(turn, "mentioned_contact_overflow", False):
        return "mentioned_contact_overflow"
    if plan.opportunity is not None and plan.opportunity.signal != "none":
        return "opportunity"
    return None

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


def _print_llm_metrics(label: str, metrics: Any) -> None:
    inp = getattr(metrics, "input_tokens", 0)
    out = getattr(metrics, "output_tokens", 0)
    dur = getattr(metrics, "duration_ms", 0)
    print(f"  [{label}] input_tokens={inp}  output_tokens={out}  duration={dur}ms")
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

def _proposal_runner(run_fn: Callable[..., Any]) -> Callable[..., Any]:
    """Adapt proposal-based agents to the shared task runner signature."""
    def invoke(context_slice: AgentContextSlice, *, task: SubTask, services: GuardedReadExecutor) -> Any:
        return run_fn(context_slice, task_brief=task.task_brief)
    return invoke


def _invoke_registered_runner(
    runner: Callable[..., Any],
    context_slice: AgentContextSlice,
    task: SubTask,
    services: GuardedReadExecutor,
) -> Any:
    """Call the current runner contract while tolerating test-time old stubs."""
    try:
        signature_target = getattr(runner, "side_effect", None)
        if not callable(signature_target):
            signature_target = runner
        parameters = inspect.signature(signature_target).parameters.values()
        accepts_services = any(
            parameter.name == "services" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
    except (TypeError, ValueError):
        accepts_services = True
    if accepts_services:
        return runner(context_slice, task=task, services=services)
    return runner(context_slice, task_brief=task.task_brief)


_SUB_AGENT_RUNNERS = {
    "calendar": _proposal_runner(run_calendar),
    "places": _proposal_runner(run_places),
    "web": web_runtime.run,
    "match": _proposal_runner(run_match),
    "relationship": _proposal_runner(run_relationship),
    "profile": _proposal_runner(run_profile),
    "product_info": _proposal_runner(run_product_info),
}


def has_active_public_confirmation(user_id: str) -> bool:
    """Return whether the current V3 confirmation manager has active work."""
    try:
        return bool(ConfirmationManager(_CONFIRMATIONS).list_active(user_id=user_id))
    except Exception:
        return False


def _topological_layers(plan: Plan) -> list[list[SubTask]]:
    """Group tasks into execution layers by dependency depth."""
    done: set[str] = set()
    layers: list[list[SubTask]] = []
    remaining = list(plan.tasks)
    while remaining:
        ready = [t for t in remaining if all(dep in done for dep in t.depends_on)]
        if not ready:
            break
        layers.append(ready)
        for t in ready:
            done.add(t.id)
            remaining.remove(t)
    return layers


def _observation_dict(task_id: str, result: "SubTaskResult") -> dict[str, Any]:
    """Project one sub-task result into the privacy-safe prior-observation shape."""
    return {
        "task_id": result.task_id,
        "status": result.status.value,
        "tool": result.tool_name,
        "result": result.observation,
        "error_code": result.error_code,
        "skip_reason": result.skip_reason,
    }


def _prior_observations_for(
    task: "SubTask", task_results: dict[str, list["SubTaskResult"]],
) -> list[dict[str, Any]]:
    """Return only the observations of this task's declared dependencies.

    Unrelated completed tasks (e.g. a parallel places result while a
    relationship task runs) never leak into another agent's context slice.
    A task that produced multiple tool observations contributes each one.
    """
    prior: list[dict[str, Any]] = []
    for dep in task.depends_on:
        for result in task_results.get(dep, []):
            if result.status is not SubTaskStatus.SKIPPED:
                prior.append(_observation_dict(dep, result))
    return prior


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
    """Apply the synthesizer's decide_place_cards decision to candidate cards.

    - show_all → all candidates; None / invalid → a bounded first-three fallback
    - select → indices filtered, deduped; empty result uses the same fallback
    - none → no cards
    """
    if not candidate_cards:
        return []
    if decision is None:
        return candidate_cards[:3]
    mode = decision.get("mode")
    if mode == "none":
        return []
    if mode == "show_all":
        # The legacy decision shape has no semantic browse intent. Keep its
        # broad display bounded; only the active compose contract can opt into
        # all retrieved cards explicitly with card_intent=browse.
        return candidate_cards if decision.get("card_intent") == "browse" else candidate_cards[:3]
    if mode != "select":
        return candidate_cards[:3]
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
        return candidate_cards[:3]
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
        # Older composer payloads put the user-facing explanation in
        # card_description. Keep that explanation as ordinary Markdown so
        # the card remains only the map/place surface; never pass the legacy
        # field to the UI card renderer.
        if not markdown:
            markdown = str(raw.get("card_description") or "").strip()[:1400]
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
    # A model may choose cards without emitting blocks (old fixtures) or omit
    # one of its selected refs. Keep every rendered card paired with a bounded
    # Markdown label instead of dumping all cards at the final bubble.
    for index, card in enumerate(selected_cards):
        if index in assigned:
            continue
        category_labels = {
            "restaurant": "餐廳", "cafe": "咖啡廳", "bar": "酒吧",
            "attraction": "景點", "park": "公園",
        }
        category = category_labels.get(str(card.get("category") or ""), "地點")
        distance = str(card.get("distance_label") or "").strip()
        suffix = "，".join(value for value in (category, distance) if value)
        name = re.sub(r"\s+", " ", str(card.get("name") or "地點")).strip()[:80]
        blocks.append(PresentationBlock(
            message_index=min(index, len(messages) - 1, 2),
            markdown=f"- **{name}**" + (f" — {suffix}" if suffix else ""),
            place_card_indices=[index],
        ))
        card_block_messages.add(min(index, len(messages) - 1, 2))
    # New card-only blocks must not hide the compatibility reply.  If the
    # synthesizer omitted a standalone text block, keep its validated bubble
    # before the cards.  Older markdown blocks take precedence unchanged.
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
    context_slice = slice_for_agent(task.agent, turn_ctx, prior_observations=prior_observations)
    runner = _SUB_AGENT_RUNNERS.get(task.agent)
    if runner is None:
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
    )

    # ProductInfo is a structured read-only specialist rather than a Tool
    # Registry proposal, but it still needs a user-facing progress state.  Use
    # the existing bounded public progress envelope so the UI does not sit on
    # the generic spinner while ProductInfo prepares the Synthesizer input.
    product_info_started: float | None = None
    product_info_step_id = f"{task.id}:product_info"

    def _finish_product_info_process(outcome: str) -> None:
        if product_info_started is None:
            return
        duration_ms = round((time.perf_counter() - product_info_started) * 1000)
        _emit_progress(
            on_progress, "tool_finished", trace=trace, agent_run_id=run_id,
            step_id=product_info_step_id, outcome=outcome,
            duration_ms=duration_ms, tool_name="product_info.process",
        )
        if debug_enabled:
            append_debug_event(
                run_id, "tool_finished", task_id=task.id, agent=task.agent,
                step_id=product_info_step_id, function="product_info.process",
                outcome=outcome, duration_ms=duration_ms,
            )

    if task.agent == "product_info":
        product_info_started = time.perf_counter()
        _emit_progress(
            on_progress, "tool_started", trace=trace, agent_run_id=run_id,
            step_id=product_info_step_id,
            text="我先整理一下產品資訊…",
            tool_name="product_info.process",
        )
        if debug_enabled:
            append_debug_event(
                run_id, "tool_started", task_id=task.id, agent=task.agent,
                step_id=product_info_step_id, function="product_info.process",
                process="bounded_product_knowledge",
            )

    sub_started = time.perf_counter()
    try:
        proposals, agent_metrics = _invoke_registered_runner(
            runner, context_slice, task, guarded_executor,
        )
    except Exception as exc:
        agent_metrics = SubAgentMetrics(error=str(exc))
        _finish_product_info_process("error")
        print(f"  [{task.id}] sub_agent EXCEPTION: {type(exc).__name__}")
        return [SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED, error_code="sub_agent_exception")], agent_metrics

    if agent_metrics:
        _print_llm_metrics(f"{task.id}:{task.agent}", agent_metrics)
        if agent_metrics.error:
            print(f"  [{task.id}] error=sub_agent_failed")

    # A structured read-only specialist may return a typed observation directly
    # instead of tool proposals.  This is a generic runner contract; the
    # Scheduler does not inspect the specialist or its retrieval internals.
    if isinstance(proposals, StructuredAgentResult):
        _finish_product_info_process(
            "ok" if proposals.status is SubTaskStatus.OK else "error"
        )
        return [SubTaskResult(
            task_id=task.id,
            status=proposals.status,
            observation=proposals.observation,
            error_code=proposals.error_code,
        )], agent_metrics

    _finish_product_info_process("error" if agent_metrics and agent_metrics.error else "ok")

    calendar_commands = list(getattr(proposals, "commands", []) or [])
    calendar_command_errors = list(getattr(proposals, "command_errors", []) or [])
    if not proposals and not calendar_commands:
        if calendar_command_errors:
            clarification = calendar_command_errors[0]
            return [SubTaskResult(
                task_id=task.id,
                status=SubTaskStatus.OK,
                tool_name="calendar.submit_commands",
                observation={
                    "calendar_command_result": {
                        "status": "needs_clarification",
                        "clarification": {
                            "code": str(clarification.get("code") or "invalid_command"),
                            "message": str(clarification.get("message") or "這次行程指令格式無法驗證，請重新描述需求。"),
                            "command_index": 0,
                            "missing_fields": [],
                        },
                    }
                },
            )], agent_metrics
        # LEGACY-ONLY compatibility path: old read-tool trajectories may still
        # return a bounded candidate projection, but normal V3 Calendar writes
        # use submit_commands → preflight and must not chain read → LLM → write.
        # 寫入任務在候選查詢後沒有提出任何寫入（例如 find 回 not_found、
        # 或候選歧義無法唯一判斷）是「正確地不動作」，不是失敗。
        # 把 prior 的 not_found 查詢帶給 synthesizer，讓它優雅回覆。
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
                projections = [public_projection(record) for record in records if public_projection(record)]
                if projections:
                    # Candidate suggestions are retrieval-only.  Do not arm a
                    # fuzzy result as ``recent_event``: a later terse cancel
                    # must still carry an explicit candidate choice before any
                    # destructive plan can be built.
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
            return [SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                  tool_name="calendar.find_my_event",
                                  observation={
                                      "no_write_proposed": True,
                                      "not_found_queries": not_found_queries,
                                  })], agent_metrics
        error_code = "sub_agent_invalid_proposal" if agent_metrics and agent_metrics.rejected_calls else "sub_agent_no_proposal"
        print(f"  [{task.id}] result=FAILED  reason={error_code}")
        return [SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED, error_code=error_code)], agent_metrics

    results: list[SubTaskResult] = []
    if calendar_command_errors:
        clarification = calendar_command_errors[0]
        results.append(SubTaskResult(
            task_id=task.id,
            status=SubTaskStatus.OK,
            tool_name="calendar.submit_commands",
            observation={
                "calendar_command_result": {
                    "status": "needs_clarification",
                    "clarification": {
                        "code": str(clarification.get("code") or "invalid_command"),
                        "message": str(clarification.get("message") or "這次行程指令格式無法驗證，請重新描述需求。"),
                        "command_index": 0,
                        "missing_fields": [],
                    },
                }
            },
        ))
    for index, proposal in enumerate(proposals):
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
                )
                print(f"  [{task.id}#{index}] result=OK (pending_confirmation for {proposal.tool_name})")
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                              tool_name=proposal.tool_name,
                                              observation={
                                                  "pending_confirmation": True,
                                                  "tool_name": proposal.tool_name,
                                                  "preview": preview or "",
                                              }))
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
            seen_keys.add(key)
            if spec.risk is ToolRisk.READ:
                step_counts[read_key] = step_counts.get(read_key, 0) + 1
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
                                          tool_name=proposal.tool_name, error_code=tool_result.error_code))
            continue
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
        private_data = getattr(tool_result, "private_data", {}) or {}
        reference_payload = private_data.get("calendar_event_reference") if isinstance(private_data, dict) else None
        if proposal.tool_name.startswith("calendar.") and isinstance(reference_payload, dict):
            event = reference_payload.get("event")
            if isinstance(event, dict):
                remember_event(
                    turn_ctx.user_id,
                    event,
                    safe_label=str(reference_payload.get("safe_label") or ""),
                )
        if proposal.tool_name in {"calendar.find_my_event", "calendar.list_my_events"}:
            if not reference_payload:
                # A new ambiguous/not-found/list-of-many read must not leave a
                # previous referent armed for a later 「這筆」 mutation.
                clear_reference(turn_ctx.user_id)
        print(f"  [{task.id}#{index}] result=OK")
        results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                       tool_name=proposal.tool_name, observation=tool_result.data))

    if calendar_commands:
        print(f"  [{task.id}] typed calendar commands: {len(calendar_commands)}")
        draft = get_draft(turn_ctx.user_id) if len(calendar_commands) == 1 else None
        if draft is not None:
            try:
                if resolved_target_replaced(calendar_commands[0], draft):
                    clear_draft(turn_ctx.user_id)
                    draft = None
                calendar_commands = [merge_command(calendar_commands[0], draft)]
            except Exception:
                # A stale/incompatible draft must not turn into an executor
                # failure; the current typed command remains authoritative.
                clear_draft(turn_ctx.user_id)
        # Canonicalize before either guard/preflight or clarification
        # persistence.  The same helper is also used by preflight for direct
        # callers, so a relative date can never leak into the next turn's draft.
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
        reference_map = {}
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
                run_id, "function_call", task_id=task.id, agent=task.agent,
                call_index=len(results), call_id=f"{task.id}:calendar.submit_commands",
                function="calendar.submit_commands",
                planner_arguments={"commands": [command.model_dump(exclude_none=True) for command in calendar_commands]},
                guard={"ok": command_guard.ok, "code": command_guard.code.value, "reason": command_guard.reason},
            )
        if not command_guard.ok:
            results.append(SubTaskResult(
                task_id=task.id, status=SubTaskStatus.FAILED,
                tool_name="calendar.submit_commands", guard_code=command_guard.code,
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
                if preflight.clarification is not None:
                    safe_result["calendar_command_result"]["clarification"] = preflight.clarification.model_dump()
                print(f"  [{task.id}] result=OK (calendar {preflight.status})")
                results.append(SubTaskResult(
                    task_id=task.id, status=SubTaskStatus.OK,
                    tool_name="calendar.submit_commands", observation=safe_result,
                ))
            else:
                clear_draft(turn_ctx.user_id)
                plans = [plan.model_dump(exclude_none=True) for plan in preflight.plans]
                payload: dict[str, Any] = {
                    "calendar_plan_version": 1,
                    "plans": plans,
                }
                # A typed Calendar batch is one logical confirmation even when
                # it contains several ordered mutations.
                ConfirmationManager(_CONFIRMATIONS).create_confirmation(
                    user_id=turn_ctx.user_id,
                    agent_name=task.agent,
                    tool_name="calendar.submit_commands",
                    arguments={},
                    payload=payload,
                    origin_run_id=run_id,
                    preview=preflight.preview or "",
                )
                results.append(SubTaskResult(
                    task_id=task.id, status=SubTaskStatus.OK,
                    tool_name="calendar.submit_commands",
                    observation={
                        "pending_confirmation": True,
                        "tool_name": "calendar.submit_commands",
                        "preview": preflight.preview or "",
                    },
                ))

    if any(r.status is SubTaskStatus.OK for r in results):
        print(f"  [{task.id}] result=OK ({len(results)} call(s))")
    elif results:
        print(f"  [{task.id}] result=FAILED (all {len(results)} call(s) failed)")
    return results, agent_metrics


def run_public_agent_turn_v3(
    ctx: AgentTurnContext, *, on_progress: ProgressCallback | None = None,
    debug_enabled: bool = False,
) -> AgentResult:
    """V3 sub-agent runtime entry point."""
    run_total_started = time.perf_counter()

    mentioned_ids, mention_overflow = validated_mentioned_contact_ids(ctx.user_id, ctx.mentioned_ids)
    ctx = ctx.model_copy(update={
        "mentioned_ids": mentioned_ids,
        "mention_overflow": bool(ctx.mention_overflow or mention_overflow),
    })
    run_id = uuid.uuid4().hex
    clock = build_turn_clock(ctx.message)
    turn = build_public_agent_turn_context(ctx, clock=clock)
    turn._raw_ctx = ctx  # type: ignore[attr-defined]
    turn._mentioned_ids = mentioned_ids  # type: ignore[attr-defined]
    trace: dict[str, Any] = {
        "plan": [], "guard_results": [], "tool_results": [],
        "event_sequence": [], "latency_ms": 0,
        "execution_mode": "dag", "llm_call_count": 0,
        "total_input_tokens": 0, "total_output_tokens": 0,
        "direct_chat_fallback_reason": None,
        "result": {"handled": True, "conversation_intent": "", "fallback_reason": None},
    }
    if debug_enabled:
        begin_debug_run(run_id, ctx.user_id)
        append_debug_event(
            run_id, "run_started", agent_run_id=run_id,
            clock=clock.model_dump(mode="json"),
        )
    _emit_progress(on_progress, "run_started", trace=trace, agent_run_id=run_id)

    _print_separator("V3 RUN START")
    print(f"  run_id={run_id}")
    print(f"  clock={clock.local_date} {clock.local_time} ({clock.weekday_zh_tw})")

    total_input_tokens = 0
    total_output_tokens = 0
    all_agent_metrics: list[tuple[str, SubAgentMetrics]] = []

    def _finalize_debug(result: AgentResult) -> AgentResult:
        # This is the single public V3 reply boundary.  Synthesizer output and
        # deterministic branches both pass through it, so model drift to
        # Simplified Chinese cannot leak to the user.  Opaque URLs/code/JSON
        # fragments are protected by normalize_public_reply.
        normalized_reply = normalize_public_reply(result.reply)
        messages = [str(item).strip() for item in (result.messages or []) if str(item).strip()]
        if not messages and normalized_reply:
            messages = [normalized_reply]
        presentation = build_presentation(messages, result.presentation_class) if messages else None
        if presentation is None and normalized_reply:
            presentation = build_presentation([normalized_reply], "fallback")
        if presentation is not None:
            messages = presentation.messages
            normalized_reply = "\n\n".join(messages)
        result = result.model_copy(update={"reply": normalized_reply, "messages": messages})
        if debug_enabled:
            finish_debug_run(
                run_id, status="completed",
                response={
                    "reply": result.reply or "",
                    "fallback_reason": result.fallback_reason,
                    "agent_mode": result.agent_mode,
                },
            )
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
        _print_separator("V3 RUN END")
        return _finalize_debug(_assessment_result(outcome, active, run_id))

    choice = confirmation_choice(ctx.message)
    mgr = ConfirmationManager(_CONFIRMATIONS)
    pending_records = mgr.list_active(user_id=ctx.user_id)
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
            reply, _card_decision, synth_metrics = synthesizer.synthesize(synth_slice)
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
    if choice == "none" and _assessment_start_confirmation_requested(ctx.message, pending_records):
        choice = "confirm"
    if choice == "confirm":
        print("\n  [entry] confirmation=confirm → executing preview-bound pending confirmation")
        results = mgr.execute_confirmed(
            user_id=ctx.user_id,
            executor=lambda tn, args, uid, payload: execute_write(
                tn, args, ctx, turn, run_id, 0,
                confirmation_id=None, payload=payload,
            ),
        )
        synth_slice = slice_for_agent("synthesizer", turn, prior_observations=[{"task_id":"confirm","status":"ok","tool":None,"result":results,"error_code":None,"skip_reason":None}])
        reply, _card_decision, synth_metrics = synthesizer.synthesize(synth_slice)
        _print_llm_metrics("synthesizer", synth_metrics)
        total_input_tokens += synth_metrics.input_tokens
        total_output_tokens += synth_metrics.output_tokens
        _print_separator("V3 RUN END")
        print(f"  total_tokens={total_input_tokens + total_output_tokens} (in={total_input_tokens} out={total_output_tokens})")
        calendar_state_changed = any(
            bool(item.get("ok")) and str(item.get("tool_name") or "").startswith("calendar.")
            for item in results if isinstance(item, dict)
        )
        return _finalize_debug(AgentResult(
            handled=True, reply=reply,
            messages=synth_metrics.presentation_messages or [reply],
            presentation_class=synth_metrics.presentation_class,
            agent_run_id=run_id, agent_mode="v3",
            calendar_state_changed=calendar_state_changed,
        ))
    if choice == "cancel":
        print("\n  [entry] confirmation=cancel → clearing pending confirmations")
        mgr.cancel_all(user_id=ctx.user_id)
        _print_separator("V3 RUN END")
        return _finalize_debug(AgentResult(
            handled=True, reply=PUBLIC_PENDING_CANCEL_REPLY, agent_run_id=run_id, agent_mode="v3",
        ))

    # Normal flow: Planner → execute DAG → synthesizer
    _print_separator("PLANNER")
    if debug_enabled:
        append_debug_event(run_id, "stage_started", stage="planner", label="Planner 正在拆解任務")
    plan, planner_metrics = plan_turn(
        turn,
    )
    _print_llm_metrics("planner", planner_metrics)
    total_input_tokens += planner_metrics.input_tokens
    total_output_tokens += planner_metrics.output_tokens
    if planner_metrics.error:
        print("  [planner] error=planner_failed")

    if debug_enabled:
        append_debug_event(
            run_id, "planner_completed", stage="planner",
            status="ok" if plan is not None else "failed",
            prompt_raw=planner_metrics.prompt_raw,
            available_functions=planner_metrics.tools_raw or [],
            function_calls=planner_metrics.tool_calls_raw or [],
            content_raw=planner_metrics.raw_content,
            error=planner_metrics.error,
            metrics={
                "input_tokens": planner_metrics.input_tokens,
                "output_tokens": planner_metrics.output_tokens,
                "duration_ms": planner_metrics.duration_ms,
            },
            mode=planner_metrics.decision_mode or "tasks",
            direct_chat_fallback_reason=planner_metrics.direct_chat_fallback_reason or None,
            product_info_fallback_reason=planner_metrics.product_info_fallback_reason or None,
        )

    if plan is None:
        print("\n  [planner] result=FAILED → fail closed")
        _print_separator("V3 RUN END")
        print(f"  total_tokens={total_input_tokens + total_output_tokens} (in={total_input_tokens} out={total_output_tokens})")
        return _finalize_debug(AgentResult(
            handled=True, reply=PUBLIC_PLANNER_INVALID_REPLY,
            agent_run_id=run_id, agent_mode="v3", fallback_reason="planner_invalid",
        ))

    plan = normalize_plan_for_execution(plan, turn.message)

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
        if direct_reason is not None:
            planner_metrics.direct_chat_fallback_reason = direct_reason
            trace["direct_chat_fallback_reason"] = direct_reason
            plan = _synthesizer_only_plan()
        else:
            reply = "\n\n".join(direct_messages)
            trace["execution_mode"] = "direct_chat"
            trace["llm_call_count"] = 1
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
                    },
                    prompt_raw=planner_metrics.prompt_raw,
                    content_raw=planner_metrics.raw_content,
                    function_calls=planner_metrics.tool_calls_raw or [],
                    available_functions=planner_metrics.tools_raw or [],
                )
                append_debug_event(
                    run_id, "direct_reply_selected", mode="direct_chat",
                    status="ok", duration_ms=planner_metrics.duration_ms, reply=reply,
                )
            _print_separator("DIRECT CHAT")
            print(f"  [direct_chat] reply={reply!r}")
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
        **({"evidence_policy": t.evidence_policy} if t.agent == "web" else {}),
    } for t in plan.tasks]
    trace["plan"] = [
        {"id": t.id, "agent": t.agent, "depends_on": t.depends_on}
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
            },
            prompt_raw=planner_metrics.prompt_raw,
            content_raw=planner_metrics.raw_content,
            function_calls=planner_metrics.tool_calls_raw or [],
            available_functions=planner_metrics.tools_raw or [],
        )

    guard_lock = threading.Lock()
    step_counts: dict[str, int] = {}
    seen_keys: set[tuple[str, str]] = set()
    task_results: dict[str, list[SubTaskResult]] = {}

    _print_separator("SUB-AGENT EXECUTION")
    for layer in _topological_layers(plan):
        layer_tasks = [t for t in layer if t.agent != "synthesizer"]
        if not layer_tasks:
            continue
        worker_count = min(len(layer_tasks), MAX_PARALLEL)

        def _run_one(task: SubTask) -> tuple[SubTask, list[SubTaskResult], SubAgentMetrics | None]:
            deps_ok = all(
                any(result.status is SubTaskStatus.OK for result in task_results.get(dep, []))
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
            prior = _prior_observations_for(task, task_results)
            try:
                result, agent_metrics = _run_sub_task(task, turn, prior, seen_keys=seen_keys,
                                        step_counts=step_counts, guard_lock=guard_lock,
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
                                tool_name=result[0].tool_name)
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
                        proposal_parse_error=agent_metrics.error,
                        rejected_calls=agent_metrics.rejected_calls,
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

    _print_separator("SYNTHESIZER")
    prior: list[dict[str, Any]] = []
    prior.extend(guidance_observations)
    for task_id in sorted(task_results):
        for r in task_results[task_id]:
            if r.status is not SubTaskStatus.SKIPPED:
                prior.append(_observation_dict(task_id, r))
    synth_slice = slice_for_agent("synthesizer", turn, prior_observations=prior)
    synth_slice.payload["presentation_mode"] = getattr(plan, "presentation_mode", "default")
    candidate_cards = _public_place_cards(
        [r for results in task_results.values() for r in results],
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
    reply, card_decision, synth_metrics = synthesizer.synthesize(synth_slice, candidate_cards=candidate_cards)
    reply = normalize_public_reply(reply)
    _print_llm_metrics("synthesizer", synth_metrics)
    total_input_tokens += synth_metrics.input_tokens
    total_output_tokens += synth_metrics.output_tokens
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
            results=[{"reply": reply, "card_decision": card_decision}],
        )

    selected_place_cards = _apply_card_decision(candidate_cards, card_decision)
    presentation_messages = synth_metrics.presentation_messages or [reply]
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
    print(f"  agents_used={[aid for aid, _ in all_agent_metrics]}")
    print(f"{'='*60}\n")

    conversation_intent = (
        "product_info"
        if synth_metrics.presentation_class == "product_info"
        else "casual_chat"
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
    )
    if place_cards:
        result.place_cards = place_cards
    if presentation_blocks:
        result.presentation_blocks = presentation_blocks
    result.sources = _public_sources(task_results)
    result.llm_call_metrics = [{
        "agent": "planner",
        "input_tokens": planner_metrics.input_tokens,
        "output_tokens": planner_metrics.output_tokens,
        "duration_ms": planner_metrics.duration_ms,
        "mode": "tasks",
    }] + [
        {
            "agent": agent_id,
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "duration_ms": m.duration_ms,
        }
        for agent_id, m in all_agent_metrics
    ]
    if synth_metrics.used_llm:
        result.llm_call_metrics.append({
            "agent": "synthesizer",
            "input_tokens": synth_metrics.input_tokens,
            "output_tokens": synth_metrics.output_tokens,
            "duration_ms": synth_metrics.duration_ms,
        })
    trace["latency_ms"] = run_total_ms
    trace["llm_call_count"] = 1 + len(all_agent_metrics) + (1 if synth_metrics.used_llm else 0)
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
