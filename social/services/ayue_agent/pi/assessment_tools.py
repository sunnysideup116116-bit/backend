"""Pi assessment entry tool and existing-session adapter contract."""
from __future__ import annotations

from typing import Any

from .domain_support import dispatch_registered


TOOL_NAMES = frozenset({"profile.start_assessment"})
PROMPT = """【性格探索】
使用者明確要求開始或重做性格探索時呼叫 profile.start_assessment；基本／大五用 basic，深層用 deep。
開始前及結果套用都需真實確認；題目生成、作答與分析沿用既有受控探索 session。"""


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return dispatch_registered(runtime, name, arguments)
