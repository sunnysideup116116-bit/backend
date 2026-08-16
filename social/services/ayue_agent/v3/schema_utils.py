"""Provider-safe normalization for Pydantic JSON schemas."""

from __future__ import annotations

from typing import Any


def inline_json_schema_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline local Pydantic ``$defs``/``$ref`` nodes for tool providers.

    Several function-calling providers do not reliably resolve JSON Schema
    local references.  Keep the source schema untouched, inline every local
    ``#/$defs/...`` reference, and remove presentation-only ``title`` fields.
    """
    source = dict(schema)
    definitions = dict(source.pop("$defs", {}) or {})

    def clean(node: Any, resolving: tuple[str, ...] = ()) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                name = ref.rsplit("/", 1)[-1]
                if name in resolving:
                    raise ValueError(f"recursive JSON schema reference: {name}")
                definition = definitions.get(name)
                if definition is None:
                    raise ValueError(f"unresolved JSON schema reference: {ref}")
                return clean(dict(definition), resolving + (name,))
            return {
                key: clean(value, resolving)
                for key, value in node.items()
                if key not in {"title", "$defs"}
            }
        if isinstance(node, list):
            return [clean(value, resolving) for value in node]
        return node

    normalized = clean(source)
    if not isinstance(normalized, dict):
        raise ValueError("JSON schema root must be an object")
    return normalized
