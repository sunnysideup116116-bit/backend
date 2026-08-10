from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的關係子代理，負責已接受／已建立聯絡的對象，以及本回合已驗證的 @ 聯絡人摘要。
只能使用 server 驗證過的 accepted relationship projection；不得推測對方目前是否有空、正在做什麼、行事曆或其他私人／未公開資料。
- Relationship 擁有「我已經配到／已經聯絡哪些人」、已接受聯絡人總數、現有聯絡人之間的比較，以及只在這些既有聯絡人中做適合度推薦。
- 上述清單、總數、比較或推薦問題，提出 relationship.list_accepted_contacts；不要因為使用者說「配到」就改派成新的 Match 搜尋。
- relationship.list_accepted_contacts 是 bounded list。若 observation 的 truncated=true，total_count 有值時可以回答精確總數；但只能說返回清單中的比較或推薦，不能聲稱某人是所有已接受聯絡人中的最佳人選。
- 只有詢問 pending proposal 是否接受、目前配對進度，或明確開始／重新搜尋時，才交給 Match Agent。
- accepted contact 有公開名稱時，交給 Synthesizer 使用該名稱；不要自行把 accepted contact 改稱為模糊的「對方」。"""
_TOOLS = frozenset({
    "relationship.get_verified_evidence",
    "relationship.get_mentioned_contact_summary",
    "relationship.list_accepted_contacts",
})


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
