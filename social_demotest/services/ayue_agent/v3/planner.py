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
    mode: Literal["tasks", "direct_chat"] = "tasks"
    presentation_mode: Literal["default", "itinerary"] = "default"
    tasks: list[SubTask] = Field(default_factory=list, max_length=4)
    direct_reply: str | None = Field(default=None, max_length=160)
    direct_messages: list[str] = Field(default_factory=list, max_length=3)
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

每個 task 都必須使用以下欄位；只有 web task 可額外填 evidence_policy：
{"id":"t1","agent":"synthesizer","depends_on":[],"task_brief":"回覆使用者"}
- `id` 必填且在整張 DAG 唯一；`agent` 必須使用下方八個值之一。
- `depends_on` 只能放同一張 DAG 已存在的 task id；沒有依賴就填空陣列。
- `task_brief` 必填，保留給該 agent 的完整本回合意圖。
- 欄位名稱只能使用 `id`、`agent`、`depends_on`、`task_brief`，以及 web task 的 `evidence_policy`；絕對不要使用 `type`、`task_agent` 或其他替代名稱。

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
- Web task 的 `task_brief` 必須保留使用者真正要查證的 proposition 與 evidence class（例如論壇／社群討論），並明確排除只提供背景的相鄰事實；不得把搜尋結果可能提到的其他事實改成新的問題。使用者要求最新／外部可驗證資訊、新聞、文章、公開論壇、社群或公開 URL 時，建立 `web` task，不把 Web 當成 `places` 的輔助工具。
- Web task 的 `evidence_policy` 只能是 `casual_discovery` 或 `strict_verification`。一般活動、優惠、餐廳、旅遊、賽事與商店探索使用 `casual_discovery`；使用者明確要求官方／確定查證，或問題屬醫療、法律、金融與安全風險時使用 `strict_verification`。這是語意判斷，不使用關鍵字 router；省略時 runtime 預設 `casual_discovery`。
- 一般活動、餐廳、旅遊或賽事探索沒有指定來源時，`task_brief` 不得自行提高成「只接受官方公告／官方網站／新聞稿」；主辦方、場館、店家、售票頁與公開社群公告都可作為候選來源，來源可信度由 Web Agent 另行標示。使用者沒有指定日期時，可查目前與近期公開資訊，但不得捏造精確日期區間或把問題縮成只查單一天。
- 只要不確定是否需要查證、工具或 workflow，就輸出 `mode="tasks"`，讓既有 Synthesizer／domain flow 處理。
- `direct_chat` 不得同時有任何 task，也不得帶 `social_opening` opportunity；不可把 direct reply 與 domain task 混在同一回合。
- `presentation_mode` 只能是 `default` 或 `itinerary`。一般聊天、產品資訊與非行程請求使用 `default`。
- 使用者要求一日遊、半日遊、整天安排或把活動串成多個時段時，使用 `presentation_mode="itinerary"`；沒有指定日期也要直接產生建議版，不要先追問日期。
- 最多 3 個 domain task；簡單聊天只建立 synthesizer。
- 必須且只能有 1 個 terminal synthesizer，且依賴所有需要彙整的 task。
- 只有複合需求才拆成多個 domain task；可平行的 task 使用空 depends_on。
- Calendar query 建立 calendar task；Calendar create/update/cancel 建立一個 calendar task。
- 不要把 Calendar mutation 拆成 read → write；mutation task_brief 保留完整使用者 intent。
- 同回合多個 Calendar mutation 保留使用者描述順序與完整 task_brief。
- 若 `calendar_draft` 存在，且本回合看起來是在補 `missing_fields`、修正該 draft，或選擇其 candidates，必須路由一個 calendar task；只負責路由，不合併 draft，也不自行計算日期。
- explicit match start/retry/search 才建立 match task；孤單或負面情緒本身不是搜尋。
- 詢問「我認識哪些人」、「目前聯絡人中適合約誰」或「這個活動適合約誰」時，建立 relationship task，要求讀取 accepted contacts；不要因為存在 active proposal 就改派 match task。
- 只有詢問 pending proposal 的接受狀態、目前配對進度、等待誰決定或明確開始／重新搜尋時，才建立 match task。accepted contact 的公開名稱優先用於 Synthesizer 回覆；pending proposal 沒有公開名稱時才使用「對方」。
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


_PLANNER_SYSTEM += """
Places/Web collaboration contract:
- Route Places only for nearby discovery, category, distance, address, map, or other typed place facts.
- Route Places followed by Web when the user asks for current, dated, public, event, promotion, menu, opening, or other evidence that Places cannot provide.
- For Places -> Web, emit t1=places, t2=web depends_on=[t1], and terminal t3=synthesizer depends_on=[t2]. The Web task brief must preserve the original unresolved criterion, date/location constraint, evidence class, and say it may research only candidates from t1.
- In that chain, the Places task brief asks only for the location/category candidate pool and must not claim the candidates already satisfy the unresolved current/public criterion. The Web task owns that verification.
- Do not route every place request to Web. Do not use keyword or regex routing. Do not let Web invent new place candidates.
- The Synthesizer may compare and curate only from verified observations; missing Web evidence is an explicit limitation, not permission to infer.
- For a general district day trip without a current/new activity criterion, emit `t1=places` and terminal `t2=synthesizer depends_on=[t1]`, with `presentation_mode="itinerary"`. `t1` searches a balanced pool of restaurants, cafes, and attractions in the requested district.
- For a request that asks for one new public activity in a district and a full-day plan around it, emit exactly `t1=web`, `t2=places depends_on=[t1]`, `t3=web depends_on=[t1,t2]`, and terminal `t4=synthesizer depends_on=[t3]`, with `presentation_mode="itinerary"`. `t1` finds one direct-supported activity and preserves typed title/date/start/end/venue; `t2` uses that venue as its anchor to find a bounded pool of restaurants, cafes, and attractions; `t3` verifies the selected place candidates for the activity date. This is still at most three domain tasks. Do not route this request to Calendar unless the user explicitly asks to save the finished plan.
- In that itinerary chain, `t1` must exclude activities already present in recent_messages or a recent calendar mutation. The first Places task must consume only the typed upstream activity observation, and the second Web task must use only server-issued place candidate refs from `t2` while retaining the activity date/location constraint.
"""

# ProductInfo is a normal DAG capability. Keep the Planner at the capability
# boundary; its internal section IDs belong exclusively to ProductInfoAgent.
_PLANNER_SYSTEM += """
- ProductInfo handles questions about Ayue or the App itself: capabilities,
  visible product behavior and flows, limitations, privacy boundaries,
  matching, calendar, assessment, and where a product question belongs.
  Route it as a normal `product_info` task with a task_brief containing the
  user's proposition. Do not select internal knowledge sections or write the
  final answer.
"""
_PLANNER_SYSTEM += "\n- `product_info`：回答阿月／App 的產品能力、可見流程、限制、隱私、媒合、行事曆與測驗行為。\n"


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


def _product_info_plan(task_brief: str) -> Plan:
    """Build the normal DAG shape for legacy provider product routing."""
    brief = str(task_brief or "產品功能問題").strip()[:500] or "產品功能問題"
    return Plan(
        mode="tasks",
        tasks=[
            SubTask(id="product_info", agent="product_info", depends_on=[], task_brief=brief),
            SubTask(id="synthesizer", agent="synthesizer", depends_on=["product_info"], task_brief="整合已驗證的產品資訊觀察"),
        ],
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
                # Older providers used a task-free product_info mode.  Repair
                # only the routing envelope; ProductInfoAgent still owns all
                # interpretation and retrieval.
                metrics.decision_mode = "tasks"
                metrics.product_info_fallback_reason = "legacy_product_info_mode"
                return _product_info_plan(turn_ctx.message), metrics
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
                presentation_mode=validated.presentation_mode,
                tasks=validated.tasks,
                direct_reply=validated.direct_reply,
                direct_messages=validated.direct_messages,
                opportunity=opportunity,
            )
        except Exception:
            # Preserve a valid domain DAG if the provider incorrectly adds a
            # direct reply alongside it.  Otherwise use a task-free semantic
            # fallback; neither branch executes a tool on its own.
            try:
                if validated.tasks:
                    plan = Plan(mode="tasks", presentation_mode=validated.presentation_mode, tasks=validated.tasks, opportunity=opportunity)
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
