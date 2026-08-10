"""Shared sub-agent execution: build tools, call LLM, parse proposal."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from services.ai_service import generate_chat_completion_with_tools
from services.ayue_agent.tool_registry import (
    TOOL_REGISTRY,
    get_tool_spec,
    planner_arguments_allowed,
    planner_arguments_schema,
)
from ..contracts import AgentContextSlice, ToolProposal


@dataclass
class SubAgentMetrics:
    """Metrics returned by a sub-agent LLM call."""
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    tool_calls_raw: list[dict] = field(default_factory=list)
    content_raw: str = ""
    prompt_raw: str = ""
    tools_raw: list[dict] = field(default_factory=list)
    input_payload: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    # Safe parser outcomes for local debug and scheduler classification.  This
    # contains codes only; raw arguments remain in the existing loopback-only
    # debug payload and never enter public trace.
    rejected_calls: list[str] = field(default_factory=list)


def _build_tools(tool_names: Iterable[str]) -> list[dict]:
    tools = []
    for name in sorted(tool_names):
        spec = get_tool_spec(name)
        if spec is None:
            continue
        schema = planner_arguments_schema(spec)
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        if "$defs" in schema:
            for defn in schema["$defs"].values():
                defn.pop("title", None)
        tools.append({
            "type": "function",
            "function": {"name": name, "description": spec.description, "parameters": schema},
        })
    return tools


_SHARED_SYSTEM_POLICY = """所有 sub-agent 共用的語意規則：
- 不得自行補入使用者未提供、最近對話未明確延續或 server context 未明確提供的事實。
- tool arguments 必須能由 current message、allowed context 或 prior observation 明確 grounding。
- 只呼叫完成 task 所需的最小工具集合。
- 只有在 prior observation 已驗證同一個 query 所需的相同 fact/arguments 時，才可避免完全相同或冗餘的 read；不同 target、日期、欄位或問題仍必須重新查詢。
- 無法安全映射成 schema value 時，不要選最接近的值，也不要 invent default。
- 不得自行提供 user ID、match ID、event ID、revision 或其他 server-owned authority field。
"""


def _agent_user_prompt(task_brief: str, slice_payload: dict[str, Any]) -> str:
    return f"""任務說明：{task_brief}

目前可公開的 context：
{json.dumps(slice_payload, ensure_ascii=False)}

只呼叫可用 schema 允許的 function，不要輸出其他文字。"""


def _agent_prompt(system_line: str, task_brief: str, slice_payload: dict[str, Any]) -> str:
    """Backward-compatible debug rendering of the two prompt sections."""
    return f"SYSTEM:\n{_SHARED_SYSTEM_POLICY}\n{system_line}\nUSER:\n{_agent_user_prompt(task_brief, slice_payload)}"


def _repair_categories(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """Map invalid place categories to the schema's allowlist before validation.

    Models frequently emit free-text categories (e.g. "drink", "bubble_tea").
    This deterministic repair keeps the proposal on-schema without extra LLM
    round-trips. Only places tools carry the categories field.
    """
    if tool_name != "places.search_nearby":
        return arguments
    categories = arguments.get("categories")
    if not isinstance(categories, list) or not categories:
        return arguments
    allowed = {"restaurant", "cafe", "bar", "attraction", "park"}
    repaired: list[str] = []
    for item in categories:
        value = str(item or "").strip().lower()
        if value in allowed:
            repaired.append(value)
        elif value in {"drink", "drinks", "bubble_tea", "milk_tea", "boba", "tea", "juice"}:
            repaired.append("cafe")
        elif value in {"chicken", "fried_chicken", "steak", "hotpot", "noodle", "sushi", "bbq", "ramen"}:
            repaired.append("restaurant")
        elif value in {"coffee", "coffee_shop", "dessert", "sweets", "cake", "bakery"}:
            repaired.append("cafe")
        elif value in {"bar", "pub", "club", "nightlife"}:
            repaired.append("bar")
        elif value in {"sightseeing", "museum", "tourist", "landmark", "scenic"}:
            repaired.append("attraction")
        elif value in {"park", "garden", "playground"}:
            repaired.append("park")
    repaired = list(dict.fromkeys(repaired))[:3]
    if not repaired:
        # Unknown-only input must fail schema validation instead of silently
        # changing the user's intent into a cafe search.
        repaired = [str(item or "").strip().lower() for item in categories if str(item or "").strip()]
    arguments = dict(arguments)
    arguments["categories"] = repaired
    return arguments


def run_sub_agents(
    *, tool_names: frozenset[str], system_line: str,
    context_slice: AgentContextSlice, task_brief: str,
) -> tuple[list[ToolProposal], SubAgentMetrics]:
    """Call the LLM with function calling and parse ALL tool calls into proposals.

    Models legitimately emit multiple tool calls in one response (e.g. a places
    agent searching both 牛排 and 冰 in a single turn). Every call is parsed and
    returned; the Scheduler guards and executes each independently, so one
    invalid/duplicate call never discards the others.

    Returns (proposals, metrics). Empty list means no usable proposal.
    """
    metrics = SubAgentMetrics()
    proposals: list[ToolProposal] = []
    try:
        tools = _build_tools(tool_names)
        prompt = _agent_user_prompt(task_brief, context_slice.payload)
        system_prompt = f"{_SHARED_SYSTEM_POLICY}\n{system_line}"
        metrics.prompt_raw = f"SYSTEM:\n{system_prompt}\nUSER:\n{prompt}"
        metrics.tools_raw = tools
        metrics.input_payload = context_slice.payload
        started = time.perf_counter()
        result = generate_chat_completion_with_tools(
            prompt, tools, temperature=0, system_prompt=system_prompt,
        )
        metrics.input_tokens = result.input_tokens
        metrics.output_tokens = result.output_tokens
        metrics.duration_ms = result.duration_ms
        metrics.tool_calls_raw = result.tool_calls or []
        metrics.content_raw = result.content or ""
        for tc in result.tool_calls or []:
            tool_name = tc.get("name", "")
            arguments = tc.get("arguments", {}) or {}
            if tool_name not in tool_names:
                metrics.rejected_calls.append("tool_not_visible")
                continue
            spec = get_tool_spec(tool_name)
            if spec is None:
                metrics.rejected_calls.append("tool_not_registered")
                continue
            arguments = _repair_categories(tool_name, arguments)
            if not planner_arguments_allowed(spec, arguments):
                metrics.rejected_calls.append("schema_invalid")
                continue
            try:
                proposals.append(ToolProposal(tool_name=tool_name, arguments=arguments))
            except Exception:
                metrics.rejected_calls.append("forbidden_or_invalid_arguments")
                continue
        if result.tool_calls and not proposals and metrics.rejected_calls:
            metrics.error = "no_valid_proposal"
        return proposals, metrics
    except Exception as exc:
        metrics.error = str(exc)
        metrics.duration_ms = round((time.perf_counter() - started) * 1000) if "started" in dir() else 0
        return [], metrics


def run_sub_agent(
    *, tool_names: frozenset[str], system_line: str,
    context_slice: AgentContextSlice, task_brief: str,
) -> tuple[ToolProposal | None, SubAgentMetrics]:
    """Backward-compatible single-proposal view of run_sub_agents.

    Kept for tests that drive one proposal per sub-task; the Scheduler uses
    run_sub_agents so multi-call responses are fully executed.
    """
    proposals, metrics = run_sub_agents(
        tool_names=tool_names, system_line=system_line,
        context_slice=context_slice, task_brief=task_brief,
    )
    return (proposals[0], metrics) if proposals else (None, metrics)
