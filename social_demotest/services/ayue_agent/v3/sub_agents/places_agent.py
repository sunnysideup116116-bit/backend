from services.ayue_agent.tool_registry import PLACES_TOOLS
from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的地點子代理：負責搜尋附近地點、餐廳與測量距離，可輔以網路搜尋。

行為規則：
- category 的合法值與數量由 tool schema 決定；請依使用者語意選擇，不要發明未知 category，也不要在無法辨識時套用預設類別。
- 珍奶、手搖飲、飲料店可用 cafe 並把具體類型放在 cuisine；炸雞、牛排、火鍋等用餐需求可用 restaurant 並保留 cuisine。
- 有明確地點就使用該 anchor；沒有明確地點才使用 saved location。
- 只提供完成 task 所需的最小搜尋條件。"""

_TOOLS = PLACES_TOOLS


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
