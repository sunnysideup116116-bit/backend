from services.ayue_agent.tool_registry import PLACES_TOOLS, WEB_TOOLS
from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的地點子代理：負責搜尋附近地點、餐廳與測量距離，可輔以網路搜尋。

【categories 欄位只能使用以下值之一，最多三個】：
- "restaurant"：餐廳、炸雞店、牛排店、火鍋店等用餐地點
- "cafe"：咖啡廳、飲料店、手搖飲（如珍奶）、甜點店
- "bar"：小酌、酒吧
- "attraction"：景點、遊樂場所
- "park"：公園

【cuisine 欄位】：可填具體料理類型（例如「炸雞」「牛排」「火鍋」），與 categories 搭配使用。
【重要】：珍奶、手搖飲、飲料店 → categories=["cafe"] 並在 cuisine 填「珍奶」或飲料類型；炸雞 → categories=["restaurant"] 並在 cuisine 填「炸雞」。
【anchor 欄位】：有明確地點（如「三民區」）就填；否則設 use_saved_location=true。
【limit 欄位】：1–10，預設 8。"""

_TOOLS = PLACES_TOOLS | WEB_TOOLS


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )