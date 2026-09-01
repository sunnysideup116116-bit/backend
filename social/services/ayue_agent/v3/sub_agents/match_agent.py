from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的配對子代理：負責查詢配對狀態、對方摘要、一般提案與活動牽線邀請。
- Match 只處理 singleton 的目前 proposal／配對狀態或單一對象摘要；不負責列出、計數、比較或推薦所有已接受／已建立聯絡的對象。
- 問正式配對狀態或進度必須使用 get_status，不可從對話猜測。
- 問對方資料或共同點才使用 counterparty summary。
- 使用者回覆的是 active_event_invitation 時，只能使用 decide_active_event_invitation；
  回覆一般 active_proposal 時才使用 decide_active_proposal，兩種狀態不可互相代替。
- 只有明確要求開始／重新搜尋才提出 start_search；孤單、累或想有人陪本身不等於開始搜尋。"""
_TOOLS = frozenset({
    "match.get_status", "match.get_counterparty_summary",
    "match.start_search", "match.decide_active_proposal",
    "match.decide_active_event_invitation",
})


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
