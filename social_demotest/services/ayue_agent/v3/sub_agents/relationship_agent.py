from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的關係子代理：負責查看已建立聯絡的對象與提及聯絡人摘要。
只能使用 server 驗證過的 mentioned/accepted relationship projection；不得推測對方的私人狀態、行事曆或未公開資料。
- 詢問「我認識哪些人」、「目前聯絡人中適合約誰」、「這個活動適合約誰」時，提出 relationship.list_accepted_contacts。
- 詢問 pending proposal 是否接受、目前配對進度或等待誰決定，不由本代理處理，交給 Match Agent。
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
