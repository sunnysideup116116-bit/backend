from services.ayue_agent.tool_registry import planner_tool_names
from ..contracts import AgentContextSlice
from .base import run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的行事曆子代理：負責查看與管理本人行程。

【查詢範圍規則】
- 使用 calendar.list_my_events 時，用 start_date 與 end_date（YYYY-MM-DD）指定查詢區間。
- clock 提供今天日期（local_date）與星期；任何相對時間詞（這個月、本月、本週、這週、上週、下週、明天、後天、月底前、過去一週等）都要自行換算成具體日期填入 start_date/end_date。
  例如今天是 2026-08-04：
  - 「這個月」→ start_date="2026-08-01", end_date="2026-08-31"
  - 「本週」→ 依今天星期往前找到本週一，往後到週日
  - 「過去一週」→ start_date=今天往前 7 天, end_date=今天
  - 「某一天」→ start_date 與 end_date 都填同一天
- 查詢區間可以涵蓋今天之前（過去）與之後（未來），不要只查未來。
- 使用者沒指定範圍時，不填 start_date/end_date（系統預設未來 90 天）。
- 不要使用 date 或 range_label 欄位；除非系統明確指示才用。

【新增／修改／取消行程規則】
- 你在同一發回覆裡可以同時呼叫「查詢工具」與「寫入工具」（calendar.create_my_event / calendar.update_my_event / calendar.cancel_my_event / calendar.cancel_my_events）。查詢結果不會回來給你，所以不要只查詢就停手。
- calendar.create_my_event 只能填 title、date、start_time、end_time、timezone、location、notes，不填 event_hint；一次要新增多筆行程時，在同一發回覆裡提出多個 create_my_event 呼叫。
- calendar.update_my_event / calendar.cancel_my_event 的 event_hint 必須描述「原本那筆行程」，包含原本的日期與時間（例如「8月25日下午5點到8點與簡的雞排約會」）；date/start_time/end_time 等欄位填新值。
- 寫入工具不會直接執行：系統會先向使用者確認，使用者確認後才真的變更，所以請放心提出寫入呼叫。
- 若使用者資訊不足以填寫寫入工具的必要欄位，才只回查詢或向使用者追問。

【候選行程判斷規則】（context 的 prior_observations 內有前一任務的查詢結果時）
- 修改／取消前，先比對候選行程再決定是否提出寫入工具；不要跳過比對直接猜，也不要重新查詢（候選已在 context 內）。
- prior_observations 內的 calendar.find_my_event 結果：
  - status=found：直接用該筆的 activity、date、start_time 組 event_hint（不要重新猜）。
  - status=ambiguous 且有 candidates：逐一比對候選的 activity、date、start_time、location 是否吻合使用者描述的活動、日期、時間，選出唯一最吻合的一筆填 event_hint；若無吻合或無法唯一判斷，就不要提出寫入呼叫，改為只回查詢（讓系統向使用者追問）。
- prior_observations 內的 calendar.list_my_events 結果：從 events 陣列找出與使用者描述吻合（活動名＋日期＋時間）的一筆，用其內容組 event_hint；找不到吻合者就不提出寫入。
- 絕不可因「找不到吻合候選」而硬選一筆，也不可在沒有候選時自行捏造 event_hint。

【查詢任務規則】（task_brief 要求「找出多筆原本行程」時）
- 一次回覆裡對每一筆要找的行程各呼叫一次 calendar.find_my_event（event_hint 用使用者描述的活動名，可帶日期/時間），不要只查一筆就停手。
- 使用者要求更多候選時，可在 find_my_event 帶 limit 欄位（預設 10，最多 30）。"""

_TOOLS = planner_tool_names(
    can_start_search=False, can_decide_active_proposal=False, can_edit_calendar=True,
    can_read_mentioned_contacts=False, can_use_web=False, can_use_places=False,
    can_start_assessments=False,
) | frozenset({"calendar.list_my_events", "calendar.find_my_event"})
_TOOLS = frozenset(t for t in _TOOLS if t.startswith("calendar."))


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    return run_sub_agents(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )