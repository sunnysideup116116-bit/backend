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
from .schema_utils import inline_json_schema_refs


@dataclass
class PlannerMetrics:
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    raw_content: str = ""
    prompt_raw: str = ""
    tool_calls_raw: list[dict] | None = None
    tools_raw: list[dict] | None = None
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


def _decompose_tool_schema() -> dict[str, Any]:
    """Build the Ollama tool definition for the single decompose_tasks function."""
    schema = inline_json_schema_refs(_DecomposeTasksArguments.model_json_schema())
    schema.pop("title", None)
    return {
        "type": "function",
        "function": {
            "name": "decompose_tasks",
            "description": "把使用者請求拆解成一張靜態子任務 DAG。",
            "parameters": schema,
        },
    }


_PLANNER_SYSTEM = """你是公開阿月 V3 的 Planner，只負責把本回合請求路由並拆成靜態 sub-task DAG。

你不回答 domain 問題、不呼叫 domain tool，也不解析 event identity、revision、confirmation 或其他 server state。

每個 task 都必須是且只能是以下四個欄位：
{"id":"t1","agent":"synthesizer","depends_on":[],"task_brief":"回覆使用者"}
- `id` 必填且在整張 DAG 唯一；`agent` 必須使用下方六個值之一。
- `depends_on` 只能放同一張 DAG 已存在的 task id；沒有依賴就填空陣列。
- `task_brief` 必填，保留給該 agent 的完整本回合意圖。
- 欄位名稱只能使用 `id`、`agent`、`depends_on`、`task_brief`；絕對不要使用 `type`、`task_agent` 或其他替代名稱。

Agent routing catalog：
- `calendar`：查詢本人行程、建立／修改／取消行程與共同日期協調。
- `places`：搜尋附近地點、餐廳、景點與距離。
- `match`：查詢配對狀態、候選摘要，或處理明確的開始／重新搜尋要求。
- `relationship`：查詢已驗證的 accepted relationship 與 mentioned contact projection。
- `profile`：讀取本人 profile／近期情境，或路由 assessment start/restart。
- `synthesizer`：只根據本回合 observations 與 bounded context 產生最終回覆。

硬性規則：
- 最多 3 個 domain task；簡單聊天只建立 synthesizer。
- 必須且只能有 1 個 terminal synthesizer，且依賴所有需要彙整的 task。
- 只有複合需求才拆成多個 domain task；可平行的 task 使用空 depends_on。
- Calendar query 建立 calendar task；Calendar create/update/cancel 建立一個 calendar task。
- 不要把 Calendar mutation 拆成 read → write；mutation task_brief 保留完整使用者 intent。
- 同回合多個 Calendar mutation 保留使用者描述順序與完整 task_brief。
- explicit match start/retry/search 才建立 match task；孤單或負面情緒本身不是搜尋。
- 使用者明確開始或重新開始 assessment 時建立 profile task，不由 synthesizer 自行出題。
- `opportunity.signal="social_opening"` 只用於使用者間接表達想找人一起參與某個活動、但尚未明確要求開始搜尋的情境；`evidence_span` 必須是本回合使用者訊息中的連續原文，`confidence` 必須至少 0.8。明確要求開始／重新配對時建立 match task，不只填 opportunity；單純旅行、寒暄、孤單或負面情緒填 `signal="none"`。
- 只呼叫 decompose_tasks，不輸出其他文字。"""


def _planner_prompt(turn_ctx: AgentTurnContextV2, pending_confirmations: list[dict[str, Any]] | None = None) -> str:
    """Build only the user/context message for the Planner.

    ``pending_confirmations`` remains an ignored compatibility argument for
    older tests/callers; Scheduler handles the closed confirmation protocol
    before Planner and normal routing does not need that state.
    """
    payload = {
        "message": turn_ctx.message,
        "recent_messages": turn_ctx.recent_messages,
        "clock": turn_ctx.clock.model_dump(),
        "active_proposal": turn_ctx.active_proposal,
        "calendar_draft": getattr(turn_ctx, "calendar_draft", None),
        "calendar_recent_reference": getattr(turn_ctx, "calendar_recent_reference", None),
        "mentioned_contacts": turn_ctx.mentioned_contacts,
    }
    return f"本回合 user request 與 routing context：{json.dumps(payload, ensure_ascii=False)}"


def plan_turn(turn_ctx: AgentTurnContextV2, *, pending_confirmations: list[dict[str, Any]] | None = None) -> tuple[Plan | None, PlannerMetrics]:
    """Call LLM (function calling) to decompose the request into a static Plan.

    Returns (plan_or_none, metrics). The tool-call arguments ARE the Plan.
    """
    metrics = PlannerMetrics()
    started = time.perf_counter()
    try:
        prompt = _planner_prompt(turn_ctx, pending_confirmations)
        metrics.prompt_raw = f"SYSTEM:\n{_PLANNER_SYSTEM}\nUSER:\n{prompt}"
        metrics.tools_raw = [_decompose_tool_schema()]
        result = generate_chat_completion_with_tools(
            prompt, metrics.tools_raw, temperature=0,
            system_prompt=_PLANNER_SYSTEM,
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
