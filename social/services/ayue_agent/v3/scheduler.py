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
from services.ayue_agent.product_identity import PUBLIC_PENDING_CANCEL_REPLY, PUBLIC_PLANNER_INVALID_REPLY
from services.assessment_session_service import (
    active_assessment_session, advance_assessment_session,
    assessment_cancel_choice, assessment_commit_choice,
    awaiting_assessment_commit, cancel_assessment_session,
    commit_assessment_session, expire_assessment_session,
)
from database import db, messages_coll
from services.ai_service import get_effective_chat_model

from .contracts import (
    DATE_INVITATION_WRITE_INTENT, AgentContextSlice, GuardDecision, GuardResultCode, Plan, SubTask,
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
    get_candidate_set as get_place_candidate_set,
    public_projection as place_candidate_projection,
    public_resolution as public_place_resolution,
    replace_presented_candidates,
    resolve_message_reference,
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
    for registered in _iter_runtime_registrations():
        if registered.direct_chat_blocker is None:
            continue
        reason = registered.direct_chat_blocker(turn)
        if reason:
            return reason
    if getattr(turn, "recent_context_draft", None):
        return "active_draft"
    if getattr(turn, "active_proposal", None):
        return "active_match_proposal"
    if active_offer:
        return "active_match_guidance"
    if getattr(turn, "mentioned_contact_overflow", False):
        return "mentioned_contact_overflow"
    if getattr(turn, "place_reference_resolution", None):
        return "place_reference_resolution"
    if plan.opportunity is not None and plan.opportunity.signal != "none":
        return "opportunity"
    return None


def _planner_failure_reply(turn: Any) -> str:
    """Return a state-aware clarification without inferring write authority."""
    active = getattr(turn, "active_proposal", None) or {}
    allowed = set(active.get("allowed_actions") or [])
    if "cancelled" in allowed:
        return "目前這張提案正在等對方回覆。你是想查看狀態，還是撤回這次配對？"
    if allowed:
        return "目前有一張待決定的配對提案。你可以直接告訴我要接受、婉拒，或只查看狀態。"
    search = getattr(turn, "match_search", None) or {}
    if search.get("cancellable"):
        return "目前仍在搜尋人選。你是想查看進度，還是取消這次搜尋？"
    if re.search(r"配對|媒合|牽線|找.{0,3}人|人選", str(getattr(turn, "message", "") or "")):
        return "你是想了解配對方式，還是要我現在開始找人？直接選一個就好。"
    return PUBLIC_PLANNER_INVALID_REPLY


def _privacy_safe_planner_attempts(metrics: PlannerMetrics) -> list[dict[str, Any]]:
    """Strip failed Planner attempts down to non-content schema diagnostics."""
    return [
        {
            "attempt": int(item.get("attempt", 0) or 0),
            "status": str(item.get("status") or "")[:40],
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


_SUB_AGENT_RUNNERS = {
    "calendar": RuntimeRegistration(
        runner=calendar_runtime.run,
        direct_chat_blocker=calendar_runtime.direct_chat_block_reason,
        confirmed_result_projector=calendar_runtime.confirmed_result_projection,
        step_prefix="",
    ),
    "places": RuntimeRegistration(runner=proposal_runner(run_places)),
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


def _server_owned_date_coordination_reply(
    task_results: dict[str, list[SubTaskResult]],
) -> str | None:
    """Keep a date-card preview authoritative over model composition."""
    for results in task_results.values():
        for result in results:
            if result.status is not SubTaskStatus.OK:
                continue
            if result.tool_name != "relationship.start_date_coordination":
                continue
            observation = result.observation or {}
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
    if write_intent != DATE_INVITATION_WRITE_INTENT:
        return None
    for results in task_results.values():
        for result in results:
            failure = result.observation.get("failure") if isinstance(result.observation, dict) else None
            if (
                result.error_code == relationship_runtime.DATE_INVITATION_PROTOCOL_FAILURE_CODE
                and isinstance(failure, dict)
                and failure.get("code") == relationship_runtime.DATE_INVITATION_PROTOCOL_FAILURE_CODE
            ):
                return relationship_runtime.DATE_INVITATION_PROTOCOL_FAILURE_REPLY
    return None


def _server_owned_confirmed_date_reply(results: list[dict[str, Any]]) -> str | None:
    """Keep the canonical write result from being rewritten into a claim."""
    for result in results:
        if not isinstance(result, dict):
            continue
        if result.get("tool_name") != "relationship.start_date_coordination":
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
                        }
                        else None
                    ),
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

    def _finalize_debug(result: AgentResult) -> AgentResult:
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
            messages = presentation.messages
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
        reply, _card_decision, synth_metrics = synthesizer.synthesize(
            synth_slice, on_token=emit_token_fragment,
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

    # Resolve only a closed ordinal/deictic place reference here. Intent still
    # belongs to Planner/Calendar; this gate only binds identity and fails
    # closed before an invalid referent can reach a write flow. Calendar draft
    # candidates retain precedence for their own same-domain ordinal follow-up.
    calendar_draft_candidates = (
        list((turn.calendar_draft or {}).get("candidates") or [])
        if isinstance(turn.calendar_draft, dict)
        else []
    )
    if not calendar_draft_candidates:
        place_resolution = resolve_message_reference(
            ctx.user_id, ctx.room_id, turn.message,
        )
        if place_resolution.get("status") == "resolved":
            turn = turn.model_copy(update={
                "recent_place_candidates": place_candidate_projection(
                    get_place_candidate_set(ctx.user_id, ctx.room_id)
                ),
                "place_reference_resolution": public_place_resolution(place_resolution),
            })
            turn._raw_ctx = ctx  # type: ignore[attr-defined]
            turn._mentioned_ids = mentioned_ids  # type: ignore[attr-defined]
        elif place_resolution.get("status") != "none":
            return _finalize_debug(AgentResult(
                handled=True,
                reply=place_reference_clarification(place_resolution),
                presentation_class="fallback",
                conversation_intent="place_reference_clarification",
                agent_run_id=run_id,
                agent_mode="v3",
                profile_write_allowed=False,
                profile_write_reason="place_reference_clarification",
            ))

    choice = "none" if continuation_resolution else confirmation_choice(ctx.message)
    pending_records = mgr.list_active(
        user_id=ctx.user_id,
        interaction_mode=INTERACTION_LEGACY,
    )
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
        match_requested = any(
            isinstance(call, dict) and isinstance(call.get("arguments"), dict)
            and any(isinstance(task, dict) and task.get("agent") == "match" for task in (call["arguments"].get("tasks") if isinstance(call["arguments"].get("tasks"), list) else []))
            for call in (planner_metrics.tool_calls_raw or [])
        )
        reply = ("這次未能解析配對操作，沒有執行變更。" + match_runtime.safe_status_reply(ctx.user_id)
                 if match_requested else _planner_failure_reply(turn))
        return _finalize_debug(AgentResult(
            handled=True, reply=reply,
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
        **({"evidence_policy": t.evidence_policy} if t.agent == "web" else {}),
        **({"outcome_contract": t.outcome_contract} if t.outcome_contract else {}),
        **({"run_if": t.run_if.model_dump()} if t.run_if else {}),
    } for t in plan.tasks]
    trace["plan"] = [
        {
            "id": t.id,
            "agent": t.agent,
            "depends_on": t.depends_on,
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
            prior = _prior_observations_for(task, task_results)
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
    reply, card_decision, synth_metrics = synthesizer.synthesize(synth_slice, candidate_cards=candidate_cards, on_token=emit_token_fragment)
    server_reply = _server_owned_date_coordination_reply(task_results)
    server_failure_reply = _server_owned_date_coordination_failure_reply(
        task_results, write_intent=plan.write_intent,
    )
    if server_reply:
        reply = server_reply
        synth_metrics.reply_source = "verified_observation"
        synth_metrics.presentation_messages = [server_reply]
        synth_metrics.presentation_blocks = None
        synth_metrics.presentation_class = "transaction"
    elif server_failure_reply:
        reply = server_failure_reply
        synth_metrics.reply_source = "verified_observation"
        synth_metrics.presentation_messages = [server_failure_reply]
        synth_metrics.presentation_blocks = None
        synth_metrics.presentation_class = "fallback"
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
    has_new_place_result = any(
        result.status is SubTaskStatus.OK
        and result.tool_name in {"places.search_nearby", "places.resolve_place"}
        for results in task_results.values()
        for result in results
    )
    if has_new_place_result:
        cards_by_ref = {
            str(card.get("candidate_ref") or ""): card
            for card in candidate_cards
            if str(card.get("candidate_ref") or "")
        }
        explicit_bindings = getattr(synth_metrics, "presented_candidate_bindings", None)
        presented_refs: list[str] = []
        presented_ordinals: dict[str, int] = {}
        if explicit_bindings is None:
            # Compatibility seam for older provider doubles/tests. Real
            # Synthesizer responses always set [] or explicit bindings, so
            # plain-text name matches cannot become production authority.
            for ordinal, reference in enumerate(synth_metrics.presented_candidate_refs, start=1):
                reference = str(reference or "")
                if reference in cards_by_ref and reference not in presented_ordinals:
                    presented_refs.append(reference)
                    presented_ordinals[reference] = ordinal
        else:
            seen_refs: set[str] = set()
            seen_ordinals: set[int] = set()
            valid = True
            for item in explicit_bindings:
                if not isinstance(item, dict):
                    valid = False
                    break
                reference = str(item.get("candidate_ref") or "")
                ordinal = item.get("presented_ordinal")
                if (
                    reference not in cards_by_ref
                    or isinstance(ordinal, bool)
                    or not isinstance(ordinal, int)
                    or not 1 <= ordinal <= len(candidate_cards)
                    or reference in seen_refs
                    or ordinal in seen_ordinals
                ):
                    valid = False
                    break
                seen_refs.add(reference)
                seen_ordinals.add(ordinal)
                presented_ordinals[reference] = ordinal
            if valid and set(seen_ordinals) != set(range(1, len(seen_ordinals) + 1)):
                valid = False
            if valid:
                presented_refs = [
                    reference
                    for reference, _ordinal in sorted(
                        presented_ordinals.items(), key=lambda pair: pair[1],
                    )
                ]
            else:
                presented_refs = []
                presented_ordinals = {}
        replace_presented_candidates(
            ctx.user_id,
            ctx.room_id,
            [cards_by_ref[reference] for reference in presented_refs],
            presented_ordinals=presented_ordinals,
        )
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
