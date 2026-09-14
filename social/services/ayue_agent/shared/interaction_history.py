"""Safe separation between historical UI interactions and current authority.

Historical cards are useful conversational context, but their rendered copy is
not an executable control.  Pi receives the bounded facts below separately
from assistant prose so an old card cannot be copied into a new reply.
"""
from __future__ import annotations

import re
from typing import Any


_HISTORICAL_CARD_HEADER = re.compile(
    r"^\s*[\[［【]\s*確認卡\s*[｜|].*?[\]］】]\s*$",
    re.IGNORECASE,
)
_HISTORICAL_CARD_FIELD = re.compile(
    r"^\s*(?:操作|內容|内容|確認後|确认后)\s*[：:].*$",
    re.IGNORECASE,
)


def strip_historical_card_copy(value: Any) -> str:
    """Remove persisted assistant fake-card copy, preserving other prose."""
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    output: list[str] = []
    skipping = False
    for line in text.split("\n"):
        if _HISTORICAL_CARD_HEADER.match(line):
            skipping = True
            continue
        if skipping and _HISTORICAL_CARD_FIELD.match(line):
            continue
        skipping = False
        output.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(output)).strip()


def historical_interaction_projection(item: dict[str, Any]) -> dict[str, Any] | None:
    """Project visible interaction facts without IDs or action authority."""
    metadata = item.get("metadata")
    if not isinstance(metadata, dict):
        return None
    choice = metadata.get("choice_prompt")
    if isinstance(choice, dict):
        display = choice.get("display")
        if not isinstance(display, dict):
            display = {}
        return {
            "kind": "confirmation",
            "historical_only": True,
            "state": str(choice.get("state") or "unknown")[:32],
            "display": {
                key: str(display.get(key) or "").strip()[:600]
                for key in ("title", "summary", "consequence")
                if str(display.get(key) or "").strip()
            },
        }
    blocks = metadata.get("interaction_blocks_v1")
    if isinstance(blocks, list):
        for block in blocks:
            if not isinstance(block, dict) or block.get("type") != "contact_selection":
                continue
            selection = block.get("selection")
            if not isinstance(selection, dict):
                continue
            return {
                "kind": "contact_selection",
                "historical_only": True,
                "state": str(selection.get("state") or "unknown")[:32],
                "name_hint": str(selection.get("name_hint") or "").strip()[:120],
                "candidates": [
                    str(candidate.get("display_name") or "").strip()[:120]
                    for candidate in (selection.get("candidates") or [])[:3]
                    if isinstance(candidate, dict)
                    and str(candidate.get("display_name") or "").strip()
                ],
            }
    return None
