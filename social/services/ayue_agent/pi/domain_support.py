"""Small helpers shared by Pi domain modules.

Domain modules own their tool membership and dispatch choice.  The loop only
talks to the registry and never infers a domain from user-language keywords.
"""
from __future__ import annotations

from typing import Any

from services.ayue_agent.tool_registry import ToolRisk, get_tool_spec


def dispatch_registered(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    spec = get_tool_spec(name)
    if spec is None:
        return runtime.project(name=name, status="failed", error_code="tool_not_allowed")
    return runtime.read(name, arguments) if spec.risk is ToolRisk.READ else runtime.write(name, arguments)
