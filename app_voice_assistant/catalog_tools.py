"""Shared signed-capability tool contracts for proxy and template sessions."""

from typing import Any
from .drafts import draft_tool
from .voice_experience import experience_tools


def catalog_function_declarations(
    types: Any, *, direct_tools_available: bool = False,
) -> list[Any]:
    discovery_scope = (
        "Use for unified daily digests, authorized cross-domain App search, personal "
        "routines, fixed workflows, multi-step requests, or capability/permission help. "
        "Keep ordinary single-domain reads, navigation and writes on the direct tools. "
        if direct_tools_available else
        "Use for every Folks App feature or live domain-data request, including "
        "weather, match status/progress, calendar, dates, contacts, memory, search, "
        "navigation, and writes. "
    )
    return [
        draft_tool(types),
        *experience_tools(types),
        types.FunctionDeclaration(
            name="find_app_capabilities",
            description=(
                discovery_scope
                + "Always call before run_app_capabilities; use explain only for how-to "
                "questions and perform for reading data or taking action. A single verified "
                "read or navigation may execute directly and return its result. When "
                "recommended_operations is returned, immediately call run_app_capabilities "
                "and copy that array exactly."
            ),
            parameters_json_schema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "query": {"type": "string", "maxLength": 1200},
                    "mode": {"type": "string", "enum": ["explain", "perform"]},
                },
                "required": ["query", "mode"],
            },
        ),
        types.FunctionDeclaration(
            name="run_app_capabilities",
            description=(
                "Run one to eight capabilities returned by find_app_capabilities. "
                "Copy capability_ref and suggested_arguments exactly when provided. "
                "Never invent authority fields."
            ),
            parameters_json_schema={
                "type": "object", "additionalProperties": False,
                "properties": {
                    "operations": {
                        "type": "array", "minItems": 1, "maxItems": 8,
                        "items": {
                            "type": "object", "additionalProperties": False,
                            "properties": {
                                "operation_key": {"type": "string", "maxLength": 40},
                                "capability_ref": {"type": "string", "maxLength": 1200},
                                "arguments": {"type": "object"},
                                "depends_on": {
                                    "type": "array", "maxItems": 8,
                                    "items": {"type": "string", "maxLength": 40},
                                },
                            },
                            "required": ["operation_key", "capability_ref", "arguments"],
                        },
                    },
                },
                "required": ["operations"],
            },
        ),
    ]
