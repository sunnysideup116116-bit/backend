from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = "你是公開阿月的個人檔案子代理：負責查看本人的 profile、近期情境與記憶搜尋。"
_TOOLS = frozenset({
    "profile.get_recent_context", "profile.get_self_summary",
    "profile.start_assessment", "memory.search_my_profile",
})


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )