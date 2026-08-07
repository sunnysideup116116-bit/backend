"""Lightweight V3 Planner: only decomposes the request into a static sub-task DAG.

Uses native function calling: the model emits a `decompose_tasks` tool call
whose arguments ARE the typed Plan. No free-text JSON parsing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from services.ai_service import generate_chat_completion_with_tools
from services.ayue_agent.contracts import AgentTurnContextV2
from .contracts import OpportunitySignal, Plan, SubTask


@dataclass
class PlannerMetrics:
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    raw_content: str = ""
    prompt_raw: str = ""
    tool_calls_raw: list[dict] | None = None
    error: str = ""


class _OpportunityArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal: Literal["none", "social_opening"] = "none"
    evidence_span: str = ""
    confidence: float = 0.0


class _DecomposeTasksArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[SubTask] = []
    opportunity: _OpportunityArguments | None = None


_AGENT_DESCRIPTIONS = {
    "calendar": "行事曆小幫手：查詢本人行程、判斷空檔、建立/更新約會與共同約會。",
    "places": "地點小幫手：查詢附近餐廳、景點，依條件篩選並推薦約會地點。",
    "match": "配對小幫手：依條件搜尋候選人選、回報目前配對狀態與結果。",
    "relationship": "關係小幫手：查詢已建立聯絡的對象資訊與互動脈絡。",
    "profile": "個人資料小幫手：讀取或維護本人 profile 偏好與近期情境。",
    "synthesizer": "綜合小幫手：彙整其他 sub-agent 的觀察結果，產生最終回覆給使用者。",
}


def _decompose_tool_schema() -> dict[str, Any]:
    """Build the Ollama tool definition for the single decompose_tasks function."""
    schema = _DecomposeTasksArguments.model_json_schema()
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        prop.pop("title", None)
    if "$defs" in schema:
        for defn in schema["$defs"].values():
            defn.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": "decompose_tasks",
            "description": "把使用者請求拆解成一張靜態子任務 DAG。",
            "parameters": schema,
        },
    }


def _planner_prompt(turn_ctx: AgentTurnContextV2, pending_confirmations: list[dict[str, Any]]) -> str:
    agents_desc = "\n".join(f"- {k}: {v}" for k, v in _AGENT_DESCRIPTIONS.items())
    payload = {
        "message": turn_ctx.message,
        "recent_messages": turn_ctx.recent_messages,
        "recent_context": turn_ctx.recent_context,
        "user_location": turn_ctx.user_location,
        "relevant_memories": turn_ctx.relevant_memories,
        "clock": turn_ctx.clock.model_dump(),
        "active_proposal": turn_ctx.active_proposal,
        "mentioned_contacts": turn_ctx.mentioned_contacts,
        "pending_confirmations": pending_confirmations,
    }
    return f"""你是拆解小幫手。請把使用者這一回合的訊息拆解成一個靜態的子任務 DAG，並呼叫 decompose_tasks 工具輸出。
可選用的 sub-agent：
{agents_desc}

規則：
1. 每個 task 必須有 id、agent、depends_on（陣列，可為空）、task_brief（給該 sub-agent 的簡短中文指示）。
2. 可以多個任務並行時用 depends_on=[]，彼此不能互相依賴 id。
3. 一定要有一個 synthesizer task 當作終端，synthesizer 必須依賴所有需要彙整的 task。
4. 需要查行程/約會/共同約會的任務用 calendar agent。
5. 需要找地點/餐廳/約會地點的任務用 places agent。
6. 需要配對/候選人選的任務用 match agent。
7. 需要 @ 對象或查關係脈絡的任務用 relationship agent。
8. 需要更新偏好/近期情境的任務用 profile agent。
9. 不確定時用 synthesizer 回覆即可。
10. 若使用者表達想有人陪、想認識人或獨自參加不舒服時，在 opportunity 欄位填 signal="social_opening"、evidence_span（原句連續子字串）、confidence（0.0-1.0，需 ≥0.8）。只有明確表達期待或投入時才標；單純提到旅行、普通寒暄或負面情緒一律填 none。
11. 「新增行程」不需要先查詢：用一個 calendar 任務即可，task_brief 直接描述要新增的行程（標題、日期、時間），不指定工具名稱；一次要新增多筆行程時併入同一個 calendar 任務。
12. 當使用者要求「修改或取消」行程（例如「把 8/25 的雞排約會改到 8/15」「移除出國行程」）時，拆成兩個 calendar 任務：
    - 第一個是「查詢任務」：task_brief 描述「找出使用者提到的所有原本行程」（例如「找出『看醫生』與『出國』兩筆行程」），不指定工具名稱；sub-agent 會自行用查詢工具一次找出所有候選。
    - 第二個是「寫入任務」，depends_on 查詢任務：task_brief 描述修改與移除的目標（例如「把看醫生改到 8/10、移除出國」），不指定工具名稱；sub-agent 會依前一任務的候選結果提出對應寫入。
    - 若同時還有「新增」需求，新增獨立成一個 calendar 任務（可與查詢任務並行）。
13. 只呼叫 decompose_tasks 工具，不要輸出其他文字。

本回合 context：{json.dumps(payload, ensure_ascii=False)}"""


def plan_turn(turn_ctx: AgentTurnContextV2, *, pending_confirmations: list[dict[str, Any]]) -> tuple[Plan | None, PlannerMetrics]:
    """Call LLM (function calling) to decompose the request into a static Plan.

    Returns (plan_or_none, metrics). The tool-call arguments ARE the Plan.
    """
    metrics = PlannerMetrics()
    started = time.perf_counter()
    try:
        prompt = _planner_prompt(turn_ctx, pending_confirmations)
        metrics.prompt_raw = prompt
        result = generate_chat_completion_with_tools(
            prompt, [_decompose_tool_schema()], temperature=0,
        )
        metrics.input_tokens = result.input_tokens
        metrics.output_tokens = result.output_tokens
        metrics.duration_ms = result.duration_ms
        metrics.raw_content = str(result.content or "")
        metrics.tool_calls_raw = result.tool_calls or []
        if not result.tool_calls:
            return None, metrics
        tc = result.tool_calls[0]
        if tc.get("name") != "decompose_tasks":
            return None, metrics
        arguments = tc.get("arguments") or {}
        validated = _DecomposeTasksArguments.model_validate(arguments)
        opportunity = None
        if validated.opportunity is not None and validated.opportunity.signal == "social_opening":
            opportunity = OpportunitySignal(
                signal="social_opening",
                evidence_span=validated.opportunity.evidence_span,
                confidence=max(0.0, min(1.0, validated.opportunity.confidence)),
            )
        plan = Plan(tasks=validated.tasks, opportunity=opportunity)
        return plan, metrics
    except Exception as exc:
        metrics.error = str(exc)
        metrics.duration_ms = round((time.perf_counter() - started) * 1000)
        return None, metrics
