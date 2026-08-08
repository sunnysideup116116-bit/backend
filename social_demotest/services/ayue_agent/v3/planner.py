"""Lightweight V3 Planner: only decomposes the request into a static sub-task DAG.

Uses native function calling: the model emits a `decompose_tasks` tool call
whose arguments ARE the typed Plan. No free-text JSON parsing.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from services.ai_service import generate_chat_completion_with_tools
from services.ayue_agent.contracts import AgentTurnContextV2
from services.ayue_agent.product_identity import PUBLIC_AYUE_PERSONA
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
    decision_mode: Literal["tasks", "direct_chat"] | None = None
    direct_chat_fallback_reason: str = ""


class _OpportunityArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal: Literal["none", "social_opening"] = "none"
    evidence_span: str = ""
    confidence: float = 0.0


class _DecomposeTasksArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["tasks", "direct_chat"] = "tasks"
    tasks: list[SubTask] = Field(default_factory=list, max_length=4)
    direct_reply: str | None = Field(default=None, max_length=160)
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
- 一般自然聊天若不需要任何 App/domain 狀態、工具或 workflow，可輸出 `mode="direct_chat"`、空 `tasks` 與一段不超過 160 字的 `direct_reply`。
- `direct_chat` 只能根據 current message 與 bounded recent_messages；不得回答行事曆、配對、profile、memory、relationship、places、外部／即時資料或產品能力問題。
- 只要不確定是否需要查證、工具或 workflow，就輸出 `mode="tasks"`，讓既有 Synthesizer／domain flow 處理。
- `direct_chat` 不得同時有任何 task，也不得帶 `social_opening` opportunity；不可把 direct reply 與 domain task 混在同一回合。
- 最多 3 個 domain task；簡單聊天只建立 synthesizer。
- 必須且只能有 1 個 terminal synthesizer，且依賴所有需要彙整的 task。
- 只有複合需求才拆成多個 domain task；可平行的 task 使用空 depends_on。
- Calendar query 建立 calendar task；Calendar create/update/cancel 建立一個 calendar task。
- 不要把 Calendar mutation 拆成 read → write；mutation task_brief 保留完整使用者 intent。
- 同回合多個 Calendar mutation 保留使用者描述順序與完整 task_brief。
- 若 `calendar_draft` 存在，且本回合看起來是在補 `missing_fields`、修正該 draft，或選擇其 candidates，必須路由一個 calendar task；只負責路由，不合併 draft，也不自行計算日期。
- explicit match start/retry/search 才建立 match task；孤單或負面情緒本身不是搜尋。
- 使用者明確開始或重新開始 assessment 時建立 profile task，不由 synthesizer 自行出題。
- `opportunity.signal="social_opening"` 只用於使用者間接表達想找人一起參與某個活動、但尚未明確要求開始搜尋的情境；`evidence_span` 必須是本回合使用者訊息中的連續原文，`confidence` 必須至少 0.8。明確要求開始／重新配對時建立 match task，不只填 opportunity；單純旅行、寒暄、孤單或負面情緒填 `signal="none"`。
- 只呼叫 decompose_tasks，不輸出其他文字。"""

_PLANNER_SYSTEM = PUBLIC_AYUE_PERSONA + "\n\n" + _PLANNER_SYSTEM


# Keep the recent-mutation routing rule separate from the large catalog above.
_PLANNER_SYSTEM += """
- `calendar_recent_mutation` 存在且使用者是在追問上一筆行事曆寫入是否成功時，路由一個 calendar task 進行唯讀驗證；不要把它改寫成新的取消或修改操作。
- 若使用者明確提出新的行事曆變更，即使有 recent mutation，也照常路由 calendar task，讓 Calendar Agent 判斷 mutation intent。
"""


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
        "calendar_recent_mutation": getattr(turn_ctx, "calendar_recent_mutation", None),
        "mentioned_contacts": turn_ctx.mentioned_contacts,
    }
    return f"本回合 user request 與 routing context：{json.dumps(payload, ensure_ascii=False)}"


def _opportunity_from_arguments(validated: _DecomposeTasksArguments) -> OpportunitySignal | None:
    if validated.opportunity is None or validated.opportunity.signal != "social_opening":
        return None
    return OpportunitySignal(
        signal="social_opening",
        evidence_span=validated.opportunity.evidence_span,
        confidence=max(0.0, min(1.0, validated.opportunity.confidence)),
    )


def _synthesizer_only_plan(*, opportunity: OpportunitySignal | None = None) -> Plan:
    """Build the safe normal-path fallback for a rejected direct reply."""
    return Plan(
        mode="tasks",
        tasks=[SubTask(
            id="synth_fallback",
            agent="synthesizer",
            depends_on=[],
            task_brief="根據本回合訊息與 bounded context 回覆使用者",
        )],
        opportunity=opportunity,
    )


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
        try:
            validated = _DecomposeTasksArguments.model_validate(arguments)
        except Exception:
            # A provider that explicitly attempted direct_chat must never make
            # the turn fail closed merely because its conversational payload is
            # malformed.  No domain task is trusted from the malformed object;
            # fall back to the existing Synthesizer-only path.
            if isinstance(arguments, dict) and arguments.get("mode") == "direct_chat":
                metrics.decision_mode = "tasks"
                metrics.direct_chat_fallback_reason = "direct_chat_schema_invalid"
                return _synthesizer_only_plan(), metrics
            raise

        opportunity = _opportunity_from_arguments(validated)
        try:
            plan = Plan(
                mode=validated.mode,
                tasks=validated.tasks,
                direct_reply=validated.direct_reply,
                opportunity=opportunity,
            )
        except Exception:
            # Preserve a valid domain DAG if the provider incorrectly adds a
            # direct reply alongside it.  Otherwise use a task-free semantic
            # fallback; neither branch executes a tool on its own.
            try:
                if validated.tasks:
                    plan = Plan(mode="tasks", tasks=validated.tasks, opportunity=opportunity)
                else:
                    plan = _synthesizer_only_plan(opportunity=opportunity)
            except Exception:
                plan = _synthesizer_only_plan(opportunity=opportunity)
            metrics.direct_chat_fallback_reason = "incompatible_direct_chat_payload"
        metrics.decision_mode = plan.mode
        return plan, metrics
    except Exception as exc:
        metrics.error = str(exc)
        metrics.duration_ms = round((time.perf_counter() - started) * 1000)
        return None, metrics
