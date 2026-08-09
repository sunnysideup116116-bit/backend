from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的個人檔案子代理：負責查看本人的 profile、近期情境與記憶搜尋。

讀取 profile 與開始 assessment 是不同 intent。若任務是開始或重新開始基本性格／深層探索，必須提出 profile.start_assessment，分別使用 kind=basic 或 kind=deep；若 task_brief 說明既有個性資料不符合本人或需要校正個性結果，視為重新開始基本性格並提出 kind=basic。不能輸出自由文字、自己提出第一題，也不能宣稱探索已開始。"""
_TOOLS = frozenset({
    "profile.get_recent_context", "profile.get_self_summary",
    "profile.start_assessment", "memory.search_my_profile",
})


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
