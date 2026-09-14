"""Pi Relationship tools and prompt contract."""
from __future__ import annotations

from typing import Any

from .domain_support import dispatch_registered


TOOL_NAMES = frozenset({
    "relationship.cancel_date_coordination",
    "relationship.get_contact_evidence",
    "relationship.get_mentioned_contact_summary",
    "relationship.get_verified_evidence",
    "relationship.list_accepted_contacts",
    "relationship.start_date_coordination",
})

PROMPT = """【聯絡人與約會邀約】
約會邀請只建立空白卡，日期與時間由雙方稍後填寫，不要為建立空白邀請追問日期。
原句已有名字時，用 relationship.start_date_coordination，target_source=name，target_evidence_span 只放原文名字。
不要讓模型保存 contact reference。工具依名字、已驗證 mention 與必要條件找正式對象；同名或模糊時由後端回真實選人卡。
不得猜 user_id，也不得替使用者按確認、接受、婉拒或撤回。"""


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return dispatch_registered(runtime, name, arguments)
