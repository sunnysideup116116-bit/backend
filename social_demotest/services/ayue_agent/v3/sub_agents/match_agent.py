from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = "你是公開阿月的配對子代理：負責查詢配對狀態、對方摘要與發起搜尋。"
_TOOLS = frozenset({
    "match.get_status", "match.get_counterparty_summary",
    "match.start_search", "match.decide_active_proposal",
})


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )