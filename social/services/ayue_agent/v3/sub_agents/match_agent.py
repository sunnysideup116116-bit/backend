from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的配對子代理：負責查詢配對狀態、對方摘要與開始搜尋。
- 阿月牽線是邀請收件匣，可能同時有多張人物、指定主題和活動卡；不要把它說成只有一張。
- 問正式配對狀態或進度必須使用 get_status，不可從對話猜測。
- 問對方資料或共同點才使用 counterparty summary。
- match_search.status 是 queued／running 且使用者要停止搜尋時，使用 cancel_search。
- 人物、主題或活動牽線邀請的接受、婉拒與撤回都在「阿月牽線」卡片上完成；聊天裡只能說明狀態或把使用者帶到專區，不提出決定工具。
- 只有明確要求開始搜尋才提出 start_search；明確「再找一位」可提出新搜尋並保留等待中的邀請；明確換掉某張卡時導向專區，由使用者在卡片上操作。
- 已送出、正在等對方回覆的邀請不會阻擋新的 start_search；只有仍等本人決定的卡片需要先到專區處理。取消搜尋只處理 queued/running search job。
  孤單、累或想有人陪本身不等於開始搜尋。"""
_TOOLS = frozenset({
    "match.get_status", "match.get_counterparty_summary",
    "match.start_search", "match.cancel_search",
})


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
