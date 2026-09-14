"""Closed, backend-owned Pi tool registry.

Every authorized core tool is visible on every decision turn. Adding a shared
tool does not expose it to Pi until explicitly listed here.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from services.ayue_agent.tool_registry import get_tool_spec, planner_arguments_schema

from . import (
    assessment_tools, calendar_tools, match_tools, places_tools, product_tools, profile_tools,
    relationship_tools, system_tools, web_tools, workflow_tools,
)


DomainHandler = Callable[[Any, str, dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class PiDomain:
    name: str
    tool_names: frozenset[str]
    prompt: str
    handler: DomainHandler


DOMAINS = (
    PiDomain("system", system_tools.TOOL_NAMES, system_tools.PROMPT, system_tools.handle),
    PiDomain("calendar", calendar_tools.TOOLS, calendar_tools.PROMPT, calendar_tools.handle),
    PiDomain("relationship", relationship_tools.TOOL_NAMES, relationship_tools.PROMPT, relationship_tools.handle),
    PiDomain("profile", profile_tools.TOOL_NAMES, profile_tools.PROMPT, profile_tools.handle),
    PiDomain("assessment", assessment_tools.TOOL_NAMES, assessment_tools.PROMPT, assessment_tools.handle),
    PiDomain("places", places_tools.TOOL_NAMES, places_tools.PROMPT, places_tools.handle),
    PiDomain("web", web_tools.TOOL_NAMES, web_tools.PROMPT, web_tools.handle),
    PiDomain("match", match_tools.TOOL_NAMES, match_tools.PROMPT, match_tools.handle),
    PiDomain("product", product_tools.TOOL_NAMES, product_tools.PROMPT, product_tools.handle),
    PiDomain("workflow", workflow_tools.TOOL_NAMES, workflow_tools.PROMPT, workflow_tools.handle),
)

TOOL_HANDLERS: dict[str, DomainHandler] = {
    tool_name: domain.handler for domain in DOMAINS for tool_name in domain.tool_names
}
PI_TOOL_NAMES = frozenset(TOOL_HANDLERS)
EXCLUDED_TOOLS = frozenset({
    "calendar.submit_commands", "match.decide_active_event_invitation",
    "match.decide_active_proposal", "tools.enable_domain",
})


def _inline_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    definitions = schema.get("$defs", {})

    def expand(node: Any, resolving: tuple[str, ...] = ()) -> Any:
        if isinstance(node, list):
            return [expand(item, resolving) for item in node]
        if not isinstance(node, dict):
            return node
        reference = node.get("$ref")
        if reference:
            name = str(reference).removeprefix("#/$defs/")
            if name not in definitions or name in resolving:
                raise ValueError("invalid_schema_reference")
            merged = {**definitions[name], **{key: value for key, value in node.items() if key != "$ref"}}
            return expand(merged, (*resolving, name))
        return {key: expand(value, resolving) for key, value in node.items() if key != "$defs"}

    return expand(schema)


def tool_schemas() -> list[dict[str, Any]]:
    custom = {
        item["name"]: item
        for item in (
            *calendar_tools.write_tool_schemas(), *places_tools.SCHEMAS,
            *product_tools.SCHEMAS, *workflow_tools.SCHEMAS, *match_tools.SCHEMAS,
        )
    }
    result: list[dict[str, Any]] = []
    for name in sorted(PI_TOOL_NAMES):
        if name in custom:
            result.append(custom[name])
            continue
        spec = get_tool_spec(name)
        if spec is None:
            raise RuntimeError(f"pi_registry_missing_tool:{name}")
        result.append({
            "name": name,
            "description": spec.description,
            "parameters": _inline_tool_schema(planner_arguments_schema(spec)),
        })
    return result


def dispatch(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return runtime.project(name=name, status="failed", error_code="tool_not_allowed")
    return handler(runtime, name, arguments)
