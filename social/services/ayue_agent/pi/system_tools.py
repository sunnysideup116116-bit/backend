"""Pi server-clock tool."""
from __future__ import annotations

from typing import Any

from .domain_support import dispatch_registered


TOOL_NAMES = frozenset({"system.get_current_time"})
PROMPT = """【時間】
相對日期以訊息自己的 sent_at、時區與當輪 clock 計算；時間註記是 metadata，不得複製到使用者回覆。"""


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return dispatch_registered(runtime, name, arguments)
