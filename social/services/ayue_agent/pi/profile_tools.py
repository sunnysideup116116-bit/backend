"""Pi owner-profile, memory and assessment tools."""
from __future__ import annotations

from typing import Any

from .domain_support import dispatch_registered


TOOL_NAMES = frozenset({
    "memory.search_my_profile",
    "profile.get_recent_context",
    "profile.get_self_summary",
})

PROMPT = """【本人資料、記憶與探索】
本人近況與記憶只用 profile／memory 工具查證；不要從別的聊天室補資料。
探索 session 的題目與提交由 Assessment 領域處理。"""


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return dispatch_registered(runtime, name, arguments)
