from ..contracts import AgentContextSlice
from .base import run_required_sub_agent, run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的關係子代理，負責已接受／已建立聯絡的對象，以及本回合已驗證的 @ 聯絡人摘要。
只能使用 server 驗證過的 accepted relationship projection；不得推測對方目前是否有空、正在做什麼、行事曆或其他私人／未公開資料。
- Relationship 擁有「我已經配到／已經聯絡哪些人」、已接受聯絡人總數、現有聯絡人之間的比較，以及只在這些既有聯絡人中做適合度推薦。
- 上述清單、總數、比較或推薦問題，提出 relationship.list_accepted_contacts；不要因為使用者說「配到」就改派成新的 Match 搜尋。
- relationship.list_accepted_contacts 是 bounded list。若 observation 的 truncated=true，total_count 有值時可以回答精確總數；但只能說返回清單中的比較或推薦，不能聲稱某人是所有已接受聯絡人中的最佳人選。
- 只有詢問 pending proposal 是否接受、目前配對進度，或明確開始／重新搜尋時，才交給 Match Agent。
- accepted contact 有公開名稱時，交給 Synthesizer 使用該名稱；不要自行把 accepted contact 改稱為模糊的「對方」。"""
_READ_TOOLS = frozenset({
    "relationship.get_verified_evidence",
    "relationship.get_mentioned_contact_summary",
    "relationship.list_accepted_contacts",
})
_DATE_INVITATION_TOOL = "relationship.start_date_coordination"
_DATE_INVITATION_SYSTEM = """你是公開阿月的 Relationship write specialist。
這是已由 Planner 確認的空白約會邀請卡建立任務。只可呼叫
`relationship.start_date_coordination` 一次，不可先查聯絡人清單。
使用一位已驗證的 @ 對象時選 `mention`；使用目前訊息中的名字時選
`name`，並把連續原文名字放進 `target_evidence_span`；只有 context 明確
提供 recent contact reference 時才選 `recent_contact`。不要填入 ID、日期、
時間、地點、活動或備註。沒有可 grounding 的對象時不要猜。
"""
_DATE_INVITATION_RETRY_HINT = (
    "Protocol correction: call relationship.start_date_coordination exactly once "
    "with one grounded target reference. Do not call a read function, emit multiple "
    "calls, or output ordinary text."
)


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_READ_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )


def run_date_invitation(
    context_slice: AgentContextSlice, *, task_brief: str,
) -> tuple[list, SubAgentMetrics]:
    return run_required_sub_agent(
        tool_name=_DATE_INVITATION_TOOL,
        system_line=_DATE_INVITATION_SYSTEM,
        context_slice=context_slice,
        task_brief=task_brief,
        retry_hint=_DATE_INVITATION_RETRY_HINT,
        max_attempts=2,
    )
