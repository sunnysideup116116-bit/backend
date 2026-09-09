import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from services.ai_service import ToolCallResult, generate_chat_completion_with_tools
from ..contracts import AgentContextSlice
from ..schema_utils import inline_json_schema_refs
from .base import run_required_sub_agent, run_sub_agents, SubAgentMetrics

_SYSTEM = """你是公開阿月的關係子代理，負責已接受／已建立聯絡的對象，以及本回合已驗證的 @ 聯絡人摘要。
只能使用 server 驗證過的 accepted relationship projection；不得推測對方目前是否有空、正在做什麼、行事曆或其他私人／未公開資料。
- Relationship 擁有「我已經配到／已經聯絡哪些人」、已接受聯絡人總數、現有聯絡人之間的比較，以及只在這些既有聯絡人中做適合度推薦。
- 上述清單、總數、比較或推薦問題，提出 relationship.list_accepted_contacts；不要因為使用者說「配到」就改派成新的 Match 搜尋。
- 推薦活動同行者時，先取得清單，再根據目前活動判斷證據是否直接相關；必要時只能用清單回傳的 opaque contact_ref 提出 get_contact_evidence。
- 性格或一般共同話題只能標成探索性依據，不可直接說成「最適合這個活動」；沒有足夠證據時可以不選首選，並把未知條件交給 Synthesizer。
- review 代表使用者質疑上一個推薦或要求換人；重新核對當前活動與推薦依據，不要替上一句硬辯。
- relationship.list_accepted_contacts 是 bounded list。若 observation 的 truncated=true，total_count 有值時可以回答精確總數；但只能說返回清單中的比較或推薦，不能聲稱某人是所有已接受聯絡人中的最佳人選。
- 只有詢問 pending proposal 是否接受、目前配對進度，或明確開始／重新搜尋時，才交給 Match Agent。
- accepted contact 有公開名稱時，交給 Synthesizer 使用該名稱；不要自行把 accepted contact 改稱為模糊的「對方」。
- 約會卡的建立與取消是兩個獨立 workflow；取消可使用 server 提供的 recent_action_reference、明確名字／@ 對象或 Hub 指定卡片。若沒有 reference、名字或 @，只有一張有效約會卡時選 `summary_singleton`；零張或多張時不要猜。不要把約會卡當成 Match 邀請。"""
_READ_TOOLS = frozenset({
    "relationship.get_verified_evidence",
    "relationship.get_mentioned_contact_summary",
    "relationship.list_accepted_contacts",
})
_RECOMMENDATION_TOOLS = frozenset({
    *_READ_TOOLS,
    "relationship.get_contact_evidence",
})
_DATE_INVITATION_TOOL = "relationship.start_date_coordination"
_DATE_COORDINATION_CANCEL_TOOL = "relationship.cancel_date_coordination"
_DATE_INVITATION_SYSTEM = """你是公開阿月的 Relationship write specialist。
這是已由 Planner 確認的空白約會邀請卡建立任務。只可呼叫
`relationship.start_date_coordination` 一次，不可先查聯絡人清單。
使用一位已驗證的 @ 對象時選 `mention`；使用目前訊息中的名字時選
`name`，並把連續原文名字放進 `target_evidence_span`；只有 context 明確
提供 recent contact reference 時才選 `recent_contact`。不要填入 ID、日期、
時間、地點、活動或備註。沒有可 grounding 的對象時不要猜。
"""
_DATE_INVITATION_RETRY_HINT = (
    "Protocol correction: call relationship.start_date_coordination exactly once "
    "with one grounded target reference. Do not call a read function, emit multiple "
    "calls, or output ordinary text."
)
_DATE_COORDINATION_CANCEL_SYSTEM = """你是公開阿月的 Relationship date-card cancellation specialist。
這是已由 Planner 確認的約會卡取消任務。只可呼叫
`relationship.cancel_date_coordination` 一次，不可先查聯絡人或 Match 狀態。
若 current message 明確 @ 一位對象，選 `mention`；若 current message 明確寫出
對象名字，選 `name` 並把連續原文名字放進 `target_evidence_span`；若使用者說
「可以取消嗎」且 context 有 recent_action_reference，選 `recent_action`；若使用者
從 Hub 指定了卡片，選 `focused_card`；若沒有上述指涉且
`date_coordination_summary.count=1`，選 `summary_singleton`。不要填入 ID、status 或 revision；無法安全
判斷時不要猜。
"""
_DATE_COORDINATION_CANCEL_RETRY_HINT = (
    "Protocol correction: call relationship.cancel_date_coordination exactly once with one "
    "grounded target_source (recent_action, mention, name, focused_card, or summary_singleton). Do not call a "
    "read function, emit multiple calls, or output ordinary text."
)


class RecommendationCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contact_ref: str = Field(min_length=1, max_length=64)
    classification: Literal["direct", "exploratory"]
    evidence_fields: list[Literal[
        "recent_context", "initial_interest", "personality_summary",
        "safe_match_reason", "verified_common_ground", "distinctive_tags",
    ]] = Field(default_factory=list, max_length=6)
    reason: str = Field(min_length=1, max_length=240)
    unknowns: list[str] = Field(default_factory=list, max_length=4)


class RelationshipRecommendationDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["grounded", "exploratory", "insufficient"]
    activity: str = Field(default="", max_length=180)
    recommendations: list[RecommendationCandidate] = Field(default_factory=list, max_length=3)
    unknowns: list[str] = Field(default_factory=list, max_length=6)


def finish_recommendation(
    context_slice: AgentContextSlice,
    *,
    task_brief: str,
    observations: list[dict],
) -> tuple[RelationshipRecommendationDecision | None, SubAgentMetrics]:
    """Produce one typed recommendation assessment from verified observations."""
    metrics = SubAgentMetrics()
    schema = inline_json_schema_refs(RelationshipRecommendationDecision.model_json_schema())
    schema.pop("title", None)
    tool = {
        "type": "function",
        "function": {
            "name": "finish_relationship_recommendation",
            "description": "完成既有聯絡人的活動適合度評估。",
            "parameters": schema,
        },
    }
    system_prompt = f"""{_SYSTEM}
你正在完成活動同行者評估，只能呼叫 finish_relationship_recommendation 一次。
- contact_ref 與 evidence_fields 必須逐字取自 verified observations。
- 只有資料直接提到當前活動、活動類型或明確相符興趣，才可用 direct。
- 個性、一般聊得來、歷史配對理由或不相干共同點只能用 exploratory。
- 證據不夠時 status=insufficient，recommendations 可以是空陣列；不要為了回答而硬選一人。
- unknowns 要寫出仍不知道的活動興趣、時間或意願；不得推測對方有空。
"""
    payload = {
        "request": str(context_slice.payload.get("message") or "")[:1200],
        "task_brief": task_brief[:500],
        "owner_recent_context": str(context_slice.payload.get("recent_context") or "")[:300],
        "owner_preferences": list(context_slice.payload.get("relevant_memories") or [])[:8],
        "verified_observations": observations[:4],
        "recent_recommendation": context_slice.payload.get("recent_recommendation"),
    }
    prompt = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    metrics.prompt_raw = f"SYSTEM:\n{system_prompt}\nUSER:\n{prompt}"
    metrics.tools_raw = [tool]
    metrics.input_payload = payload
    metrics.llm_call_count = 1
    try:
        result = generate_chat_completion_with_tools(
            prompt,
            [tool],
            temperature=0,
            system_prompt=system_prompt,
            prefer_fast_model=True,
            model_owner="relationship",
        )
    except Exception as exc:
        metrics.error = str(exc)
        return None, metrics
    if not isinstance(result, ToolCallResult):
        metrics.error = "recommendation_invalid_provider_result"
        return None, metrics
    metrics.input_tokens = int(result.input_tokens or 0)
    metrics.output_tokens = int(result.output_tokens or 0)
    metrics.duration_ms = int(result.duration_ms or 0)
    metrics.tool_calls_raw = list(result.tool_calls or [])
    metrics.content_raw = str(result.content or "")
    metrics.llm_requests.append({
        "input_tokens": metrics.input_tokens,
        "output_tokens": metrics.output_tokens,
        "duration_ms": metrics.duration_ms,
        "ttft_ms": int(getattr(result, "ttft_ms", 0) or 0),
        "tps": round(float(getattr(result, "tps", 0) or 0), 3),
        "model_name": str(getattr(result, "model_name", "") or ""),
    })
    calls = [
        call for call in (result.tool_calls or [])
        if call.get("name") == "finish_relationship_recommendation"
    ]
    if len(calls) != 1:
        metrics.error = "recommendation_finish_protocol_failed"
        return None, metrics
    try:
        return RelationshipRecommendationDecision.model_validate(
            calls[0].get("arguments") or {},
        ), metrics
    except Exception:
        metrics.error = "recommendation_finish_schema_invalid"
        return None, metrics


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[list, SubAgentMetrics]:
    intent = str(context_slice.payload.get("relationship_intent") or "lookup")
    tools = _RECOMMENDATION_TOOLS if intent in {"recommend", "review"} else _READ_TOOLS
    system_line = _SYSTEM
    if intent in {"recommend", "review"}:
        system_line += (
            "\n本回合是活動同行者推薦。先用 list_accepted_contacts 建立候選池；"
            "看完結果後再決定是否需要 get_contact_evidence，最多補查三位。"
            "補查只傳回清單中的 contact_ref，不得猜 ID。"
        )
    return run_sub_agents(
        tool_names=tools, system_line=system_line,
        context_slice=context_slice, task_brief=task_brief,
        model_owner="relationship",
    )


def run_date_invitation(
    context_slice: AgentContextSlice, *, task_brief: str,
) -> tuple[list, SubAgentMetrics]:
    return run_required_sub_agent(
        tool_name=_DATE_INVITATION_TOOL,
        system_line=_DATE_INVITATION_SYSTEM,
        context_slice=context_slice,
        task_brief=task_brief,
        retry_hint=_DATE_INVITATION_RETRY_HINT,
        max_attempts=2,
        model_owner="relationship",
    )


def run_date_coordination_cancel(
    context_slice: AgentContextSlice, *, task_brief: str,
) -> tuple[list, SubAgentMetrics]:
    return run_required_sub_agent(
        tool_name=_DATE_COORDINATION_CANCEL_TOOL,
        system_line=_DATE_COORDINATION_CANCEL_SYSTEM,
        context_slice=context_slice,
        task_brief=task_brief,
        retry_hint=_DATE_COORDINATION_CANCEL_RETRY_HINT,
        max_attempts=2,
        model_owner="relationship",
    )
