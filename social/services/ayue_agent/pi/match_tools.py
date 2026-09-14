"""Pi matching status and search tools."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from services.ayue_agent.shared.write_actions import prepare_write_confirmation

from .domain_support import dispatch_registered


TOOL_NAMES = frozenset({
    "match.cancel_search",
    "match.get_counterparty_summary",
    "match.get_status",
    "match.start_search",
})

PROMPT = """【配對】
可查詢配對狀態、準備開始或取消搜尋；需要寫入時必須使用真實確認卡。
使用者要找一起衝浪、運動、看展等人選時，支援指定活動的主題配對。呼叫 match.start_search，kind=activity、topic=活動名稱。
不能保證對方已具備某項技能，但這不代表不能依該活動找人；不要把「無法保證會衝浪」說成「無法搜尋衝浪人選」。
未指定活動或明確改用近期情境時，kind=recent_context。主題不應寫入長期偏好。
原句或前文已表達想開始找人，就直接準備真實確認卡；不要先用文字再問一次要不要開始。
補充「對／好」時仍保留前文主題；source_text 可引用同房間使用者原句，不能引用助理自己編的條件。
稱呼人使用「對象」「人選」「朋友」，不要用「物件」。
人物或活動牽線卡的接受、婉拒與撤回只引導到「阿月牽線」Hub，不在普通聊天代按。"""


class MatchSearchInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["activity", "recent_context"] = "recent_context"
    topic: str | None = Field(default=None, min_length=1, max_length=80)
    source_text: str | None = Field(default=None, min_length=1, max_length=600)


SCHEMAS = ({
    "name": "match.start_search",
    "description": "準備配對搜尋確認卡：可依指定活動找人（activity＋topic），或依本人近期情境（recent_context）。確認前不啟動搜尋，找到人後不自動送邀請。",
    "parameters": MatchSearchInput.model_json_schema(),
},)


def _prepare_search(runtime: Any, request: MatchSearchInput):
    ctx = runtime.turn._raw_ctx
    if request.kind == "recent_context":
        if request.topic:
            return None, "請確認這次要依近期情境，還是依指定活動找人。"
        args = {"search_request": {"kind": "general"}}
        return prepare_write_confirmation("match.start_search", args, ctx, runtime.turn)
    topic = str(request.topic or "").strip()
    # These are the visible user messages already supplied to the Pi model.
    # No separate reference store, assistant fact or hidden selection is used.
    sources = [str(runtime.turn.message or "")] + [
        str(item.get("content") or "")
        for item in reversed(runtime.turn.recent_messages or [])
        if isinstance(item, dict) and item.get("role") == "user"
    ]
    source = str(request.source_text or "").strip()
    if not source:
        source = next((text for text in sources if topic and topic in text), "")
    if not topic or topic not in source or not any(source in text for text in sources):
        return None, "你這次想找人一起做什麼活動？我會依你提供的活動準備搜尋確認卡。"
    # The existing deterministic preflight grounds the topic against this
    # verified user source and retains all readiness/quota/confirmation gates.
    source_ctx = ctx.model_copy(update={"message": source})
    args = {"search_request": {"kind": "activity", "topic": topic}}
    return prepare_write_confirmation("match.start_search", args, source_ctx, runtime.turn)


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    if name == "match.start_search":
        request = MatchSearchInput.model_validate(arguments)
        return runtime.write(name, {}, prepare=lambda: _prepare_search(runtime, request))
    return dispatch_registered(runtime, name, arguments)
