"""Shared guarded read execution for bounded specialist runtimes."""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from services.ayue_agent.tool_registry import (
    TOOL_REGISTRY,
    executor_arguments_for_turn,
    get_tool_spec,
    tool_call_key,
)
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.web_tools import is_safe_public_url

from .contracts import GuardResultCode, SubTaskResult, SubTaskStatus, ToolProposal
from .guard import guard_proposal


ProgressEmitter = Callable[..., Any]
DebugEmitter = Callable[..., Any]


def web_extract_urls_allowed(turn_ctx: Any, results: Sequence[Any], urls: Sequence[str]) -> bool:
    """Bind extraction to a search result or an URL the owner supplied."""
    allowed: set[str] = set()
    for result in results:
        if isinstance(result, dict):
            if result.get("tool") != "web.search":
                continue
            data = result.get("result") or {}
        else:
            if result.status is not SubTaskStatus.OK or result.tool_name != "web.search" or not result.observation:
                continue
            data = result.observation or {}
        for item in (data.get("results") or []):
            url = str((item or {}).get("url") or "")
            if is_safe_public_url(url):
                allowed.add(url)
    for raw in re.findall(r"https?://[^\s<>\]\[\"']+", str(getattr(turn_ctx, "message", "") or "")):
        url = raw.rstrip(".,，。!?！？:：;；)")
        if is_safe_public_url(url):
            allowed.add(url)
    return bool(urls) and all(str(url) in allowed for url in urls)


@dataclass(frozen=True)
class GuardedExecutionOutcome:
    result: SubTaskResult
    attempted: bool


@dataclass
class GuardedReadExecutor:
    """Task-bound Guard → executor-argument → tool execution adapter.

    The adapter contains no research policy. The caller owns action, rounds and
    budgets, and supplies the allowed tool set and current read step.
    """

    task_id: str
    agent_name: str
    turn_ctx: Any
    seen_keys: set[tuple[str, str]]
    guard_lock: threading.Lock
    on_progress: Callable[[dict[str, Any]], Any] | None
    run_id: str
    trace: dict[str, Any]
    emit_progress: ProgressEmitter
    append_debug_event: DebugEmitter
    debug_enabled: bool = False

    def execute(
        self,
        proposal: ToolProposal,
        *,
        allowed_tools: frozenset[str],
        step_count: int,
        max_reads: int,
        prior_observations: Sequence[dict[str, Any]],
        call_index: int,
    ) -> GuardedExecutionOutcome:
        if proposal.tool_name not in allowed_tools:
            result = SubTaskResult(
                task_id=self.task_id,
                status=SubTaskStatus.FAILED,
                tool_name=proposal.tool_name,
                error_code="tool_not_registered",
                guard_code=GuardResultCode.TOOL_NOT_REGISTERED,
            )
            self.trace["guard_results"].append(GuardResultCode.TOOL_NOT_REGISTERED.value)
            return GuardedExecutionOutcome(result=result, attempted=False)

        with self.guard_lock:
            decision = guard_proposal(
                proposal,
                agent_name=self.agent_name,
                seen_keys=self.seen_keys,
                step_count=step_count,
                max_reads=max_reads,
            )
            self.trace["guard_results"].append(decision.code.value)
        if self.debug_enabled:
            self.append_debug_event(
                self.run_id,
                "function_call",
                task_id=self.task_id,
                agent=self.agent_name,
                function=proposal.tool_name,
                planner_arguments=proposal.arguments,
                guard={"ok": decision.ok, "code": decision.code.value, "reason": decision.reason},
            )
        if not decision.ok:
            return GuardedExecutionOutcome(
                result=SubTaskResult(
                    task_id=self.task_id,
                    status=SubTaskStatus.FAILED,
                    tool_name=proposal.tool_name,
                    guard_code=decision.code,
                ),
                attempted=False,
            )

        if proposal.tool_name == "web.extract":
            urls = [str(url) for url in (proposal.arguments.get("urls") or [])]
            if not web_extract_urls_allowed(self.turn_ctx, prior_observations, urls):
                return GuardedExecutionOutcome(
                    result=SubTaskResult(
                        task_id=self.task_id,
                        status=SubTaskStatus.FAILED,
                        tool_name=proposal.tool_name,
                        error_code="web_extract_url_not_bound",
                    ),
                    attempted=False,
                )

        spec = TOOL_REGISTRY.get(proposal.tool_name) or get_tool_spec(proposal.tool_name)
        if spec is None:
            return GuardedExecutionOutcome(
                result=SubTaskResult(
                    task_id=self.task_id,
                    status=SubTaskStatus.FAILED,
                    tool_name=proposal.tool_name,
                    error_code="tool_not_registered",
                ),
                attempted=False,
            )
        try:
            safe_args = executor_arguments_for_turn(spec, [], proposal.arguments)
        except Exception:
            return GuardedExecutionOutcome(
                result=SubTaskResult(
                    task_id=self.task_id,
                    status=SubTaskStatus.FAILED,
                    tool_name=proposal.tool_name,
                    error_code="executor_args_invalid",
                ),
                attempted=False,
            )

        with self.guard_lock:
            key = tool_call_key(spec, safe_args)
            if key in self.seen_keys:
                return GuardedExecutionOutcome(
                    result=SubTaskResult(
                        task_id=self.task_id,
                        status=SubTaskStatus.FAILED,
                        tool_name=proposal.tool_name,
                        guard_code=GuardResultCode.DUPLICATE_CALL,
                    ),
                    attempted=False,
                )
            self.seen_keys.add(key)

        step_id = f"{self.task_id}:web#{call_index}:{proposal.tool_name}"
        self.emit_progress(
            self.on_progress,
            "tool_started",
            trace=self.trace,
            agent_run_id=self.run_id,
            step_id=step_id,
            text=spec.progress_text,
            tool_name=proposal.tool_name,
        )
        if self.debug_enabled:
            self.append_debug_event(
                self.run_id,
                "tool_started",
                task_id=self.task_id,
                agent=self.agent_name,
                step_id=step_id,
                function=proposal.tool_name,
                planner_arguments=proposal.arguments,
                executor_arguments=safe_args,
            )
        started = time.perf_counter()
        try:
            tool_result = execute_tool(
                type("TC", (), {"name": proposal.tool_name, "arguments": safe_args})(),
                self.turn_ctx._raw_ctx,
                clock=self.turn_ctx.clock,
            )
        except Exception:
            duration_ms = round((time.perf_counter() - started) * 1000)
            self.emit_progress(
                self.on_progress,
                "tool_finished",
                trace=self.trace,
                agent_run_id=self.run_id,
                step_id=step_id,
                outcome="error",
                tool_name=proposal.tool_name,
                duration_ms=duration_ms,
            )
            self.trace["tool_results"].append({"tool": proposal.tool_name, "ok": False, "code": "tool_exception"})
            return GuardedExecutionOutcome(
                result=SubTaskResult(
                    task_id=self.task_id,
                    status=SubTaskStatus.FAILED,
                    tool_name=proposal.tool_name,
                    error_code="tool_exception",
                ),
                attempted=True,
            )

        duration_ms = round((time.perf_counter() - started) * 1000)
        if not tool_result.ok:
            self.emit_progress(
                self.on_progress,
                "tool_finished",
                trace=self.trace,
                agent_run_id=self.run_id,
                step_id=step_id,
                outcome="error",
                tool_name=proposal.tool_name,
                duration_ms=duration_ms,
            )
            self.trace["tool_results"].append({
                "tool": proposal.tool_name,
                "ok": False,
                "code": tool_result.error_code,
            })
            return GuardedExecutionOutcome(
                result=SubTaskResult(
                    task_id=self.task_id,
                    status=SubTaskStatus.FAILED,
                    tool_name=proposal.tool_name,
                    error_code=tool_result.error_code,
                ),
                attempted=True,
            )

        self.emit_progress(
            self.on_progress,
            "tool_finished",
            trace=self.trace,
            agent_run_id=self.run_id,
            step_id=step_id,
            outcome="ok",
            tool_name=proposal.tool_name,
            duration_ms=duration_ms,
        )
        self.trace["tool_results"].append({"tool": proposal.tool_name, "ok": True, "code": None})
        result = SubTaskResult(
            task_id=self.task_id,
            status=SubTaskStatus.OK,
            tool_name=proposal.tool_name,
            observation=tool_result.data,
        )
        if self.debug_enabled:
            self.append_debug_event(
                self.run_id,
                "tool_finished",
                task_id=self.task_id,
                agent=self.agent_name,
                step_id=step_id,
                function=proposal.tool_name,
                outcome="ok",
                duration_ms=duration_ms,
                result=tool_result.data,
            )
        return GuardedExecutionOutcome(result=result, attempted=True)

