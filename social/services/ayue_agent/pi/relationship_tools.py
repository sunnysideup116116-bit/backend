"""Pi Relationship tools and prompt contract."""
from __future__ import annotations

from typing import Any

from .domain_support import dispatch_registered


TOOL_NAMES = frozenset({
    "relationship.cancel_date_coordination",
    "relationship.get_contact_evidence",
    "relationship.get_mentioned_contact_summary",
    "relationship.get_my_views",
    "relationship.get_verified_evidence",
    "relationship.list_accepted_contacts",
    "relationship.start_date_coordination",
})

PROMPT = """【聯絡人、本人關係看法與約會邀約】
當使用者談到和特定已接受聯絡人的相處、問自己以前怎麼看對方、要不要繼續認識，或比較已認識的對象時，主動使用 relationship.get_my_views。它只讀取使用者本人的私人看法。
target_source 使用 mention、name、contact_refs 或 recent_contact；name 的 target_evidence_span 只放本回合原文名字；contact_refs 只能使用本回合 relationship.list_accepted_contacts 回傳的 ref。
回答時說「你之前提到／你目前覺得」，不要把主觀看法說成對方的客觀人格，不推測對方是否喜歡使用者，也不要把對某人的看法推成全域擇偶偏好。
嚴格保留原觀點強度：「覺得對方很帥」不等於「外型吸引你」或「你喜歡他」；「聊天有點冷」不等於不適合交往。可以說兩個感受同時存在，但不能替使用者下新的總結。
只問姓名、公開興趣或約會卡狀態時，不需要讀私人看法。無明確對象時先釐清或列出已接受聯絡人，不要掃描所有人的私人記憶。
約會邀請只建立空白卡，日期與時間由雙方稍後填寫，不要為建立空白邀請追問日期。
原句已有名字時，用 relationship.start_date_coordination，target_source=name，target_evidence_span 只放原文名字。
不要讓模型保存 contact reference。工具依名字、已驗證 mention 與必要條件找正式對象；同名或模糊時由後端回真實選人卡。
不得猜 user_id，也不得替使用者按確認、接受、婉拒或撤回。"""


def handle(runtime: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return dispatch_registered(runtime, name, arguments)
