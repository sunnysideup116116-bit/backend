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
from services.ayue_agent.contracts import PublicAgentTurnContext
from services.ayue_agent.product_identity import (
    PUBLIC_AYUE_PERSONA,
    PUBLIC_VOICE_FEW_SHOTS,
    PUBLIC_REPLY_LENGTH,
    PUBLIC_REPLY_TONE,
)
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
    decision_mode: Literal["tasks", "direct_chat", "product_info"] | None = None
    direct_chat_fallback_reason: str = ""
    product_info_fallback_reason: str = ""


class _OpportunityArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal: Literal["none", "social_opening"] = "none"
    evidence_span: str = ""
    confidence: float = 0.0


class _DecomposeTasksArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["tasks", "direct_chat", "product_info"] = "tasks"
    tasks: list[SubTask] = Field(default_factory=list, max_length=4)
    direct_reply: str | None = Field(default=None, max_length=160)
    direct_messages: list[str] = Field(default_factory=list, max_length=3)
    product_info_topics: list[Literal[
        "capabilities", "same_identity", "surface_scope", "cross_surface_context",
        "private_message_visibility", "where_to_ask", "matching_principles",
        "relationship_chat_access",
    ]] = Field(
        default_factory=list,
        max_length=3,
        description=(
            "Use only these exact identifiers; never translate or paraphrase them: "
            "capabilities, same_identity, surface_scope, cross_surface_context, "
            "private_message_visibility, where_to_ask, matching_principles, "
            "relationship_chat_access"
        ),
    )
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
- `id` 必填且在整張 DAG 唯一；`agent` 必須使用下方七個值之一。
- `depends_on` 只能放同一張 DAG 已存在的 task id；沒有依賴就填空陣列。
- `task_brief` 必填，保留給該 agent 的完整本回合意圖。
- 欄位名稱只能使用 `id`、`agent`、`depends_on`、`task_brief`；絕對不要使用 `type`、`task_agent` 或其他替代名稱。

Agent routing catalog：
- `calendar`：查詢本人行程、建立／修改／取消行程與共同日期協調。
- `places`：搜尋附近地點、餐廳、景點與距離。
- `web`：查詢最新／目前公開資訊、新聞、文章、公開論壇或社群討論、使用者提供的公開 URL，以及需要 search → inspect → refine 的 bounded research；Web 不負責地點卡片。
- `match`：查詢配對狀態、候選摘要，或處理明確的開始／重新搜尋要求。
- `relationship`：查詢已驗證的 accepted relationship 與 mentioned contact projection。
- `profile`：讀取本人 profile／近期情境，或路由 assessment start/restart。
- `synthesizer`：只根據本回合 observations 與 bounded context 產生最終回覆。

硬性規則：
- 一般自然聊天若不需要任何 App/domain 狀態、工具或 workflow，可輸出 `mode="direct_chat"`、空 `tasks` 與一段不超過 160 字的 `direct_reply`。
- `direct_reply` 先回應使用者具體說的處境，再給一點有根據的看法或下一步；可自然追問，但不要用客服式開場、誇張心理分析、功能清單或硬轉配對。無需 App state、私人資料或外部查證的一般社交／約會看法（例如第一次約會去哪裡）可直接回答，不導向 Private。
- `direct_chat` 只能根據 current message 與 bounded recent_messages；不得回答行事曆、配對、profile、memory、relationship、places、外部／即時資料或產品能力問題。
- 使用者詢問阿月的身份、主聊天室／悄悄話分工、跨入口 context、私訊可見性、雙人聊天紀錄存取、應該在哪裡問，或 App 如何挑選與排序配對人選，必須輸出 `mode="product_info"`，不要用 `direct_chat`，也不要建立 domain task。這是語意判斷，不是關鍵字規則。
- `product_info_topics` 最多三個，而且每一項只能原樣填入下列英文 identifier：`capabilities`、`same_identity`、`surface_scope`、`cross_surface_context`、`private_message_visibility`、`where_to_ask`、`matching_principles`、`relationship_chat_access`。絕對不可翻成中文、不可填說明句、不可自行建立新 topic。
- Product-info function-call 範例：問「你是做什麼的？」→ `{"mode":"product_info","product_info_topics":["capabilities"]}`；問「另一個阿月也是你嗎？」或「另一個阿月是幹啥的？」→ `{"mode":"product_info","product_info_topics":["same_identity","surface_scope"]}`；問「兩邊都看得到完整對話嗎？」→ `{"mode":"product_info","product_info_topics":["cross_surface_context"]}`；問「你看得到我和對方剛聊什麼嗎？」→ `{"mode":"product_info","product_info_topics":["relationship_chat_access"]}`；問「你們怎麼配對、原理是什麼？」→ `{"mode":"product_info","product_info_topics":["matching_principles"]}`，不可退回 capabilities 身份介紹。
- 需要特定對方的實際聊天內容、近期互動節奏或針對該對話給聊天建議時，Public 看不到雙人聊天室紀錄，必須輸出 `mode="product_info"` 並使用 `relationship_chat_access` 與 `where_to_ask`，導引至該雙人聊天室的阿月悄悄話。一般、不依賴特定聊天紀錄的感情或聊天建議仍可 `direct_chat`。
- Web task 的 `task_brief` 必須保留使用者真正要查證的 proposition 與 evidence class（例如論壇／社群討論），並明確排除只提供背景的相鄰事實；不得把搜尋結果可能提到的其他事實改成新的問題。使用者要求最新／外部可驗證資訊、新聞、文章、公開論壇、社群或公開 URL 時，建立 `web` task，不把 Web 當成 `places` 的輔助工具。
- 一般活動、餐廳、旅遊或賽事探索沒有指定來源時，`task_brief` 不得自行提高成「只接受官方公告／官方網站／新聞稿」；主辦方、場館、店家、售票頁與公開社群公告都可作為候選來源，來源可信度由 Web Agent 另行標示。使用者沒有指定日期時，可查目前與近期公開資訊，但不得捏造精確日期區間或把問題縮成只查單一天。
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
- 使用者明確開始或重新開始 assessment 時建立 profile task，不由 synthesizer 自行出題。若使用者表示既有個性資料、性格結果或個性描述不符合本人，即使沒有明說「重做」，也視為想校正成更符合自己的版本：建立 profile task，要求 `profile.start_assessment(kind=basic)`；不只是追問哪一段不像，也不要由 Planner 或 Synthesizer自行修改 profile。這項判斷由模型理解整句語意，不使用關鍵字或 regex 路由。
- Assessment function-call 範例：說「我覺得我資料上的個性不是我欸」→ 建立 `profile` task，task_brief 明確要求 `profile.start_assessment(kind=basic)`，再建立依賴它的 terminal `synthesizer` task；真正開始前仍由既有 confirmation 流程確認。
- `opportunity.signal="social_opening"` 只用於使用者間接表達想找人一起參與某個活動、但尚未明確要求開始搜尋的情境；`evidence_span` 必須是本回合使用者訊息中的連續原文，`confidence` 必須至少 0.8。明確要求開始／重新配對時建立 match task，不只填 opportunity；單純旅行、寒暄、孤單或負面情緒填 `signal="none"`。
- 只呼叫 decompose_tasks，不輸出其他文字。"""

_PLANNER_SYSTEM = PUBLIC_AYUE_PERSONA + "\n\n" + _PLANNER_SYSTEM
_PLANNER_SYSTEM += f"\n\nPublic V3 reply contract：{PUBLIC_REPLY_LENGTH} {PUBLIC_REPLY_TONE}"
_PLANNER_SYSTEM += "\n\nPublic voice examples（只學口吻，不作 intent routing）：" + "；".join(
    f"使用者：{question} → 阿月：{reply}" for question, reply in PUBLIC_VOICE_FEW_SHOTS
)


# Keep the recent-mutation routing rule separate from the large catalog above.
_PLANNER_SYSTEM += """
- `calendar_recent_mutation` 存在且使用者是在追問上一筆行事曆寫入是否成功時，路由一個 calendar task 進行唯讀驗證；不要把它改寫成新的取消或修改操作。
- 若使用者明確提出新的行事曆變更，即使有 recent mutation，也照常路由 calendar task，讓 Calendar Agent 判斷 mutation intent。
"""


def _planner_prompt(turn_ctx: PublicAgentTurnContext) -> str:
    """Build only the user/context message for the Planner."""
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


def plan_turn(turn_ctx: PublicAgentTurnContext) -> tuple[Plan | None, PlannerMetrics]:
    """Call LLM (function calling) to decompose the request into a static Plan.

    Returns (plan_or_none, metrics). The tool-call arguments ARE the Plan.
    """
    metrics = PlannerMetrics()
    started = time.perf_counter()
    try:
        prompt = _planner_prompt(turn_ctx)
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
            # Product-info is a read-only presentation mode. If the model
            # correctly selected that mode but paraphrased the enum values,
            # discard every untrusted field and return a safe, generic product
            # projection instead of failing the whole turn. This is protocol
            # repair, not natural-language intent classification.
            if isinstance(arguments, dict) and arguments.get("mode") == "product_info":
                metrics.decision_mode = "product_info"
                metrics.product_info_fallback_reason = "product_info_topics_invalid"
                return Plan(
                    mode="product_info",
                    product_info_topics=["capabilities", "surface_scope"],
                ), metrics
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
                direct_messages=validated.direct_messages,
                product_info_topics=validated.product_info_topics,
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
