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
    error: str = ""


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


def _agent_prompt(system_line: str, task_brief: str, slice_payload: dict[str, Any]) -> str:
    return f"""{system_line}

任務說明：{task_brief}

重要規則：你只能使用上述工具，且不可自行填寫使用者 ID、match ID、event ID 或 revision。這些欄位由系統依登入者與目前狀態自動注入。請只提供上述 schema 允許的欄位。

目前可公開的 context：
{json.dumps(slice_payload, ensure_ascii=False)}"""


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
        repaired = ["cafe"]
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
        prompt = _agent_prompt(system_line, task_brief, context_slice.payload)
        metrics.prompt_raw = prompt
        started = time.perf_counter()
        result = generate_chat_completion_with_tools(prompt, tools, temperature=0)
        metrics.input_tokens = result.input_tokens
        metrics.output_tokens = result.output_tokens
        metrics.duration_ms = result.duration_ms
        metrics.tool_calls_raw = result.tool_calls or []
        metrics.content_raw = result.content or ""
        for tc in result.tool_calls or []:
            tool_name = tc.get("name", "")
            arguments = tc.get("arguments", {}) or {}
            if tool_name not in tool_names:
                continue
            spec = get_tool_spec(tool_name)
            if spec is None:
                continue
            arguments = _repair_categories(tool_name, arguments)
            if not planner_arguments_allowed(spec, arguments):
                continue
            try:
                proposals.append(ToolProposal(tool_name=tool_name, arguments=arguments))
            except Exception:
                continue
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