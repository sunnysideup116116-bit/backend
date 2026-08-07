# social_demotest/services/ayue_agent/v3/scheduler.py
"""V3 Scheduler / Orchestrator: pure-code orchestration of the sub-agent runtime."""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import parse_qs, urlencode, urlsplit

import config
from services.ayue_agent.contracts import AgentResult, AgentTurnContext
from services.ayue_agent.context import build_agent_turn_context_v2
from services.ayue_agent.public_relationship_projection import validated_mentioned_contact_ids
from services.ayue_agent.router import confirmation_choice
from services.ayue_agent.time_context import build_turn_clock
from services.ayue_agent.tool_registry import (
    TOOL_REGISTRY, ToolArgumentSource, ToolRisk, executor_arguments_for_turn, tool_call_key,
)
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.web_tools import is_safe_public_url
from services.ayue_agent.match_opportunity import assess_match_opportunity
from services.assessment_session_service import (
    active_assessment_session, advance_assessment_session,
    assessment_cancel_choice, assessment_commit_choice,
    awaiting_assessment_commit, cancel_assessment_session,
    commit_assessment_session, expire_assessment_session,
)
from database import db

from .contracts import (
    AgentContextSlice, GuardDecision, GuardResultCode, Plan, SubTask,
    SubTaskResult, SubTaskStatus, ToolProposal,
)
from .context_slicer import slice_for_agent
from .guard import guard_proposal
from .planner import plan_turn, PlannerMetrics
from . import synthesizer
from .synthesizer import SynthesizerMetrics
from .sub_agents.base import SubAgentMetrics
from .confirmation import ConfirmationManager
from .sub_agents.calendar_agent import run as run_calendar
from .sub_agents.places_agent import run as run_places
from .sub_agents.match_agent import run as run_match
from .sub_agents.relationship_agent import run as run_relationship
from .sub_agents.profile_agent import run as run_profile
from .write_executors import execute_write, prepare_write_confirmation
from .debug_trace import (
    append_event as append_debug_event,
    begin_run as begin_debug_run,
    finish_run as finish_debug_run,
)


ProgressCallback = Callable[[dict[str, Any]], Any]
MAX_READS = max(1, min(int(os.getenv("AYUE_SUBAGENT_MAX_READS", "3") or "3"), 3))
MAX_PARALLEL = max(1, min(int(os.getenv("AYUE_SUBAGENT_MAX_PARALLEL", "2") or "2"), 2))

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

_SUB_AGENT_RUNNERS = {
    "calendar": run_calendar,
    "places": run_places,
    "match": run_match,
    "relationship": run_relationship,
    "profile": run_profile,
}


def agent_mode_for_user_v3(user_id: str) -> str:
    mode = os.getenv("AYUE_AGENT_V3_MODE", "off").strip().lower()
    if mode not in {"off", "on"}:
        mode = "off"
    allowlist = {v.strip() for v in os.getenv("AYUE_AGENT_V3_USER_ALLOWLIST", "").split(",") if v.strip()}
    return mode if not allowlist or user_id in allowlist else "off"


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


_PLACE_CATEGORIES = ("restaurant", "cafe", "bar", "attraction", "park")
MAX_PLACE_CARDS = 8
MAX_PLACE_CARDS_PER_CATEGORY = 5


def _osm_embed_url(map_url: str) -> str:
    """Build a bounded OSM embed URL only from our typed public map link."""
    try:
        parsed = urlsplit(map_url)
        if parsed.scheme != "https" or parsed.hostname != "www.openstreetmap.org":
            return ""
        query = parse_qs(parsed.query)
        lat = float((query.get("mlat") or [""])[0])
        lon = float((query.get("mlon") or [""])[0])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return ""
    except (TypeError, ValueError):
        return ""
    params = urlencode({
        "bbox": f"{lon - .004:.6f},{lat - .003:.6f},{lon + .004:.6f},{lat + .003:.6f}",
        "layer": "mapnik",
        "marker": f"{lat:.6f},{lon:.6f}",
    })
    return f"https://www.openstreetmap.org/export/embed.html?{params}"


def _google_embed_url(place_id: str) -> str:
    """Build a Google Maps Embed URL for one validated Google Place ID."""
    place_id = str(place_id or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,180}", place_id):
        return ""
    browser_key = str(getattr(config, "GOOGLE_MAPS_BROWSER_API_KEY", "") or "")
    if not browser_key:
        return ""
    params = urlencode({"key": browser_key, "q": f"place_id:{place_id}"})
    return f"https://www.google.com/maps/embed/v1/place?{params}"


def _distance_label(value: Any) -> str:
    try:
        distance = max(0, int(value))
    except (TypeError, ValueError):
        return ""
    if distance <= 0:
        return ""
    if distance < 1_000:
        return f"約 {distance} 公尺"
    return f"約 {distance / 1_000:.1f} 公里"


def _public_sources(task_results: Any) -> list[dict[str, str]]:
    """Keep only display-safe citations; never persist web page content."""
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
            if tool_name == "web.search":
                candidates = data.get("results") or []
            elif tool_name == "web.extract":
                candidates = data.get("pages") or []
            elif tool_name == "places.search_nearby":
                candidates = [
                    {"title": str(item.get("name") or "地圖"), "url": str(item.get("map_url") or "")}
                    for item in (data.get("places") or [])
                ]
            elif tool_name == "places.resolve_place":
                place = data.get("place") or {}
                candidates = [{"title": str(place.get("name") or "地圖"), "url": str(place.get("map_url") or "")}]
            elif tool_name == "places.measure_distance":
                candidates = [{
                    "title": str(data.get("attribution") or "OpenStreetMap"),
                    "url": str(data.get("attribution_url") or ""),
                }]
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


def _public_place_cards(task_results: Any) -> list[dict[str, str]]:
    """Project verified places observations into bounded provider-neutral cards.

    V3-specific projection: collects cards from ALL places observations (no
    early return), caps each category at MAX_PLACE_CARDS_PER_CATEGORY, then
    round-robins across categories up to MAX_PLACE_CARDS total. This keeps a
    mixed request (e.g. 牛排 + 冰) balanced instead of letting the first
    category fill the whole budget. Validation rules mirror the V2 projection
    (provider allowlist, place_id/map-url safety, category allowlist, dedup).
    """
    cards_by_category: dict[str, list[dict[str, str]]] = {c: [] for c in _PLACE_CATEGORIES}
    seen: set[str] = set()
    for result in task_results:
        if result.status is not SubTaskStatus.OK or not result.observation:
            continue
        tool_name = result.tool_name
        if tool_name == "places.search_nearby":
            places = (result.observation or {}).get("places") or []
        elif tool_name == "places.resolve_place":
            place = (result.observation or {}).get("place") or {}
            places = [place] if place else []
        else:
            continue
        attribution = re.sub(r"\s+", " ", str((result.observation or {}).get("attribution") or "")).strip()[:80]
        attribution_url = str((result.observation or {}).get("attribution_url") or "")
        if not is_safe_public_url(attribution_url):
            attribution_url = ""
        for item in places:
            provider = str(item.get("provider") or "openstreetmap")
            if provider not in {"openstreetmap", "google"}:
                continue
            place_id = str(item.get("place_id") or "").strip()
            map_url = str(item.get("map_url") or "").strip()
            if map_url and not is_safe_public_url(map_url):
                map_url = ""
            if provider == "google":
                if not re.fullmatch(r"[A-Za-z0-9_-]{3,180}", place_id):
                    continue
                unique_key = f"google:{place_id}"
            else:
                if not map_url:
                    continue
                unique_key = f"openstreetmap:{map_url}"
            if unique_key in seen:
                continue
            seen.add(unique_key)
            category = str(item.get("category") or "attraction")
            if category not in _PLACE_CATEGORIES:
                category = "attraction"
            if len(cards_by_category[category]) >= MAX_PLACE_CARDS_PER_CATEGORY:
                continue
            card = {
                "provider": provider,
                "place_id": place_id if provider == "google" else "",
                "name": re.sub(r"\s+", " ", str(item.get("name") or "地點")).strip()[:80],
                "category": category,
                "address_summary": re.sub(r"\s+", " ", str(item.get("address_summary") or "")).strip()[:180],
                "distance_label": _distance_label(item.get("distance_m")),
                "map_url": map_url,
                "embed_url": (_google_embed_url(place_id) if provider == "google" else _osm_embed_url(map_url)),
                "attribution": attribution or ("Google Maps" if provider == "google" else "© OpenStreetMap contributors"),
                "attribution_url": attribution_url,
            }
            if provider == "google":
                photo_url = str(item.get("photo_url") or "")
                if photo_url:
                    try:
                        parsed_photo = urlsplit(photo_url)
                        if (
                            parsed_photo.scheme == "https"
                            and parsed_photo.hostname.lower() == "places.googleapis.com"
                            and parsed_photo.path.startswith("/v1/places/")
                            and parsed_photo.path.endswith("/media")
                        ):
                            card["photo_url"] = photo_url
                    except (TypeError, ValueError):
                        pass
            cards_by_category[category].append(card)

    # Round-robin across categories so a mixed request stays balanced.
    balanced: list[dict[str, str]] = []
    active = [c for c in _PLACE_CATEGORIES if cards_by_category[c]]
    while active and len(balanced) < MAX_PLACE_CARDS:
        next_active: list[str] = []
        for category in active:
            if len(balanced) >= MAX_PLACE_CARDS:
                break
            balanced.append(cards_by_category[category].pop(0))
            if cards_by_category[category]:
                next_active.append(category)
        active = next_active
    return balanced


def _apply_card_decision(
    candidate_cards: list[dict[str, str]], decision: dict[str, Any] | None,
) -> list[dict[str, str]]:
    """Apply the synthesizer's decide_place_cards decision to candidate cards.

    - None / show_all / invalid → all candidates (fallback)
    - select → indices filtered, deduped; empty result falls back to all
    - none → no cards
    """
    if not candidate_cards:
        return []
    if decision is None:
        return candidate_cards
    mode = decision.get("mode")
    if mode == "none":
        return []
    if mode != "select":
        return candidate_cards
    indices = decision.get("indices") or []
    selected: list[dict[str, str]] = []
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
        return candidate_cards
    return selected


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


def _web_extract_urls_allowed(turn_ctx: Any, results: list[Any], urls: list[str]) -> bool:
    """Bind extraction to a search result or an URL the owner actually supplied."""
    allowed: set[str] = set()
    for r in results:
        if isinstance(r, dict):
            if r.get("tool") != "web.search":
                continue
            data = r.get("result") or {}
        else:
            if r.status is not SubTaskStatus.OK or r.tool_name != "web.search" or not r.observation:
                continue
            data = r.observation or {}
        for item in (data.get("results") or []):
            url = str((item or {}).get("url") or "")
            if is_safe_public_url(url):
                allowed.add(url)
    for raw in re.findall(r"https?://[^\s<>\]\[\"']+", str(getattr(turn_ctx, "message", "") or "")):
        url = raw.rstrip(".,，。!?！？:：;；)")
        if is_safe_public_url(url):
            allowed.add(url)
    return bool(urls) and all(str(url) in allowed for url in urls)


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
    Duplicate detection and execution budgets are global to the run. Shared
    state is guarded by guard_lock, never held around LLM or tool calls.
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
    sub_started = time.perf_counter()
    try:
        proposals, agent_metrics = runner(context_slice, task_brief=task.task_brief)
    except Exception as exc:
        agent_metrics = SubAgentMetrics(error=str(exc))
        print(f"  [{task.id}] sub_agent EXCEPTION: {type(exc).__name__}")
        return [SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED, error_code="sub_agent_exception")], agent_metrics

    if agent_metrics:
        _print_llm_metrics(f"{task.id}:{task.agent}", agent_metrics)
        if agent_metrics.error:
            print(f"  [{task.id}] error=sub_agent_failed")

    if not proposals:
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
            print(f"  [{task.id}] result=OK (no_write_proposed)")
            return [SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                  tool_name="calendar.find_my_event",
                                  observation={
                                      "no_write_proposed": True,
                                      "not_found_queries": not_found_queries,
                                  })], agent_metrics
        print(f"  [{task.id}] result=FAILED  reason=no_proposal")
        return [SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED, error_code="sub_agent_no_proposal")], agent_metrics

    results: list[SubTaskResult] = []
    for index, proposal in enumerate(proposals):
        print(f"  [{task.id}#{index}] proposal: tool={proposal.tool_name}")

        with guard_lock:
            decision = guard_proposal(
                proposal, agent_name=task.agent,
                seen_keys=seen_keys, step_count=step_counts.get("__reads", 0),
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
            if spec.risk is ToolRisk.READ and step_counts.get("__reads", 0) >= MAX_READS:
                print(f"  [{task.id}#{index}] guard: global read budget exhausted")
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                              tool_name=proposal.tool_name,
                                              guard_code=GuardResultCode.STEP_LIMIT_EXCEEDED))
                continue
            seen_keys.add(key)
            step_counts["__reads"] = step_counts.get("__reads", 0) + 1
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
        print(f"  [{task.id}#{index}] result=OK")
        results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                      tool_name=proposal.tool_name, observation=tool_result.data))

    if any(r.status is SubTaskStatus.OK for r in results):
        print(f"  [{task.id}] result=OK ({len(results)} call(s))")
    elif results:
        print(f"  [{task.id}] result=FAILED (all {len(results)} call(s) failed)")
    return results, agent_metrics


def run_public_agent_turn_v3(
    ctx: AgentTurnContext, *, mode: str = "on", on_progress: ProgressCallback | None = None,
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
    turn = build_agent_turn_context_v2(ctx, clock=clock)
    turn._raw_ctx = ctx  # type: ignore[attr-defined]
    turn._mentioned_ids = mentioned_ids  # type: ignore[attr-defined]
    trace: dict[str, Any] = {
        "plan": [], "guard_results": [], "tool_results": [],
        "event_sequence": [], "latency_ms": 0,
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
        return _finalize_debug(AgentResult(handled=True, reply=reply, agent_run_id=run_id, agent_mode="v3"))
    if choice == "cancel":
        print("\n  [entry] confirmation=cancel → clearing pending confirmations")
        mgr.cancel_all(user_id=ctx.user_id)
        _print_separator("V3 RUN END")
        return _finalize_debug(AgentResult(
            handled=True, reply="已取消待確認的操作", agent_run_id=run_id, agent_mode="v3",
        ))

    # Normal flow: Planner → execute DAG → synthesizer
    _print_separator("PLANNER")
    if debug_enabled:
        append_debug_event(run_id, "stage_started", stage="planner", label="Planner 正在拆解任務")
    plan, planner_metrics = plan_turn(
        turn,
        pending_confirmations=mgr.planner_projection(user_id=ctx.user_id),
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
        )

    if plan is None:
        print("\n  [planner] result=FAILED → fail closed")
        _print_separator("V3 RUN END")
        print(f"  total_tokens={total_input_tokens + total_output_tokens} (in={total_input_tokens} out={total_output_tokens})")
        return _finalize_debug(AgentResult(
            handled=True, reply="我現在沒辦法判斷這個請求要不要執行，先跟你聊聊",
            agent_run_id=run_id, agent_mode="v3", fallback_reason="planner_invalid",
        ))

    opportunity = getattr(plan, "opportunity", None)
    if opportunity is not None and opportunity.signal == "social_opening":
        if opportunity.confidence < 0.8 or not opportunity.evidence_span or opportunity.evidence_span not in turn.message:
            opportunity = None
    if opportunity is not None:
        assessment = assess_match_opportunity(ctx.user_profile or {}, ctx.user_id, explicit_search=False)
        if assessment.state == "ready":
            lead = f"你提到「{opportunity.evidence_span}」。" if opportunity.evidence_span in turn.message else ""
            preview = (
                f"{lead}感覺這件事有人一起也不錯。"
                "我可以依你的近況和個性找一位合適人選，不會隨機配；要試試看嗎？"
            )
            mgr.create_confirmation(
                user_id=ctx.user_id,
                agent_name="match",
                tool_name="match.start_search",
                arguments={},
                payload={"source": "opportunity_guidance", "guidance_fingerprint": assessment.fingerprint},
                origin_run_id=run_id,
                preview=preview,
            )
            synth_slice = slice_for_agent("synthesizer", turn, prior_observations=[{
                "task_id": "guidance", "status": "ok", "tool": None,
                "result": [{"pending_confirmation": True, "tool_name": "match.start_search", "preview": preview}],
                "error_code": None, "skip_reason": None,
            }])
            reply, _card_decision, synth_metrics = synthesizer.synthesize(synth_slice)
            _print_llm_metrics("synthesizer", synth_metrics)
            _print_separator("V3 RUN END")
            return _finalize_debug(AgentResult(
                handled=True, reply=reply, agent_run_id=run_id, agent_mode="v3",
                match_readiness_state="ready", match_guidance_shown=True,
            ))
        if assessment.state == "not_ready":
            from services.ayue_agent.match_opportunity import missing_basis_question
            reply = "我想先多了解你的方向，才能幫你找得更準。" + missing_basis_question(assessment)
            _print_separator("V3 RUN END")
            return _finalize_debug(AgentResult(
                handled=True, reply=reply, agent_run_id=run_id, agent_mode="v3",
                match_readiness_state="not_ready",
            ))

    _print_separator("PLAN")
    plan_tasks_json = [{"id": t.id, "agent": t.agent, "depends_on": t.depends_on, "task_brief": t.task_brief} for t in plan.tasks]
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
                task_results.get(dep, [SubTaskResult(task_id=dep, status=SubTaskStatus.FAILED)])[0].status == SubTaskStatus.OK
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
    for task_id in sorted(task_results):
        for r in task_results[task_id]:
            if r.status is not SubTaskStatus.SKIPPED:
                prior.append(_observation_dict(task_id, r))
    synth_slice = slice_for_agent("synthesizer", turn, prior_observations=prior)
    candidate_cards = _public_place_cards([r for results in task_results.values() for r in results])
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
    _print_llm_metrics("synthesizer", synth_metrics)
    total_input_tokens += synth_metrics.input_tokens
    total_output_tokens += synth_metrics.output_tokens
    print(f"  [synthesizer] card_decision={card_decision}")
    _emit_progress(on_progress, "subagent_finished", trace=trace, agent_run_id=run_id,
                    task_id="synth", agent="synthesizer",
                    status="ok", error="",
                    input_tokens=synth_metrics.input_tokens,
                    output_tokens=synth_metrics.output_tokens,
                    duration_ms=synth_metrics.duration_ms,
                    tool_name=None)
    if debug_enabled:
        append_debug_event(
            run_id, "subagent_finished", task_id="synth", agent="synthesizer",
            status="ok", error="",
            input_tokens=synth_metrics.input_tokens,
            output_tokens=synth_metrics.output_tokens,
            duration_ms=synth_metrics.duration_ms,
            prompt_raw=synth_metrics.prompt_raw,
            input_payload=synth_metrics.input_payload or {},
            available_functions=synth_metrics.tools_raw or [],
            tool_calls_raw=synth_metrics.tool_calls_raw or [],
            content_raw=synth_metrics.raw_content,
            results=[{"reply": reply, "card_decision": card_decision}],
        )

    place_cards = _apply_card_decision(candidate_cards, card_decision)

    run_total_ms = round((time.perf_counter() - run_total_started) * 1000)

    _print_separator("V3 RUN END")
    print(f"  total_tokens={total_input_tokens + total_output_tokens}  (input={total_input_tokens}  output={total_output_tokens})")
    print(f"  total_duration={run_total_ms}ms")
    print(f"  agents_used={[aid for aid, _ in all_agent_metrics]}")
    print(f"{'='*60}\n")

    result = AgentResult(handled=True, reply=reply, agent_run_id=run_id, agent_mode="v3")
    if place_cards:
        result.place_cards = place_cards
    result.sources = _public_sources(task_results)
    result.llm_call_metrics = [
        {
            "agent": agent_id,
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "duration_ms": m.duration_ms,
        }
        for agent_id, m in all_agent_metrics
    ]
    trace["latency_ms"] = run_total_ms
    trace["result"] = {
        "handled": result.handled,
        "conversation_intent": result.conversation_intent,
        "fallback_reason": result.fallback_reason,
    }
    trace["event_sequence"].append("final")
    _persist_trace(run_id, ctx, trace)
    return _finalize_debug(result)
