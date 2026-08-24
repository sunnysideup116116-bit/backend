"""Lightweight V3 Planner: only decomposes the request into a static sub-task DAG.

Uses native function calling: the model emits a `decompose_tasks` tool call
whose arguments ARE the typed Plan. No free-text JSON parsing.
"""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.ai_service import generate_chat_completion_with_tools
from services.ayue_agent.contracts import PublicAgentTurnContext
from services.ayue_agent.product_identity import (
    AYUE_CORE_IDENTITY,
    AYUE_MISSION_SHORT,
    AYUE_VOICE_SHORT,
)
from .contracts import (
    DATE_INVITATION_WRITE_INTENT,
    OpportunitySignal,
    Plan,
    PlannerWriteIntent,
    SubTask,
)
from .schema_utils import inline_json_schema_refs


@dataclass
class PlannerMetrics:
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    llm_call_count: int = 0
    retry_count: int = 0
    retry_reason: str = ""
    failure_code: str = ""
    attempts: list[dict[str, Any]] = field(default_factory=list)
    requested_model_tier: Literal["none", "main", "fast"] = "fast"
    prompt_version: str = ""
    raw_content: str = ""
    prompt_raw: str = ""
    tool_calls_raw: list[dict] | None = None
    tools_raw: list[dict] | None = None
    error: str = ""
    llm_requests: list[dict[str, Any]] = field(default_factory=list)
    decision_mode: Literal["tasks", "direct_chat", "product_info"] | None = None
    direct_chat_fallback_reason: str = ""
    product_info_fallback_reason: str = ""


_PLANNER_MAX_ATTEMPTS = 2
_PLANNER_RETRYABLE_FAILURES = frozenset({
    "missing_tool_call", "wrong_function_name", "invalid_arguments",
})
_KNOWN_PLANNER_AGENTS = frozenset({
    "calendar", "places", "web", "match", "relationship", "profile",
    "product_info", "synthesizer",
})
_REPAIR_CODES = frozenset({
    "non_web_evidence_policy_removed",
    "non_calendar_outcome_contract_removed",
    "empty_optional_task_fields_removed",
    "misplaced_date_invitation_intent_recovered",
})


def _planner_retry_prompt(
    prompt: str,
    failure_code: str,
    validation_hint: str = "",
) -> str:
    """Ask the same Planner request to obey the native tool-call protocol."""
    retry = (
        f"{prompt}\n\n"
        "Planner protocol retry: the previous attempt failed with "
        f"{failure_code}. Re-evaluate the same request. Call `decompose_tasks` "
        "exactly once, output no ordinary text, and make the arguments conform "
        "to the provided schema."
    )
    if validation_hint:
        retry += f"\nContract correction: {validation_hint}"
    return retry


def _planner_validation_retry_hint(exc: Exception) -> str:
    """Return bounded field-specific repair guidance without echoing bad values."""
    errors_fn = getattr(exc, "errors", None)
    try:
        errors = errors_fn() if callable(errors_fn) else []
    except Exception:
        errors = []
    locations = {str(part) for error in errors for part in (error.get("loc") or ())}
    messages = {
        str(error.get("msg") or "")
        for error in errors
        if isinstance(error, dict)
    }
    # Pydantic can collapse a SubTask model-level error to tasks.0. Only these
    # fixed, allowlisted fragments may recover the scoped field name; never
    # reflect the provider's value or the exception itself into the retry.
    evidence_message = any(
        "evidence_policy is only valid for Web tasks" in message
        for message in messages
    )
    outcome_message = any(
        "outcome_contract is only valid for Calendar tasks" in message
        for message in messages
    )
    run_if_message = any(
        fragment in message
        for message in messages
        for fragment in (
            "run_if is only a control edge",
            "required_outcome must be",
            "required_outcome is only",
            "run_if cannot",
            "run_if references",
            "run_if source must be",
            "calendar outcomes require a Calendar source task",
            "Calendar source must declare availability outcome_contract",
        )
    )
    write_intent_message = any(
        fragment in message
        for message in messages
        for fragment in (
            "relationship.date_invitation.v1 requires",
            "relationship.date_invitation.v1 Relationship task cannot",
            "relationship.date_invitation.v1 Synthesizer must",
            "relationship.date_invitation.v1 uses default presentation",
            "relationship.date_invitation.v1 cannot contain an opportunity",
        )
    )
    hints: list[str] = []
    if "write_intent" in locations or write_intent_message:
        hints.append(
            "write_intent is required: use relationship.date_invitation.v1 only for an "
            "explicit request to create a date invitation card, with exactly Relationship "
            "then Synthesizer; Match is never a precheck. Use none for every other request."
        )
    if "evidence_policy" in locations or evidence_message:
        hints.append(
            "evidence_policy is only for Web tasks and is exactly "
            "casual_discovery or strict_verification; omit the key, not an empty string, for every other agent."
        )
    if "outcome_contract" in locations or outcome_message:
        hints.append(
            "outcome_contract is only for a Calendar availability task and, when used, "
            "is exactly calendar.availability.v1; observation schema names never belong here; "
            "relationship.date_invitation.v1 belongs only in the top-level write_intent; "
            "otherwise omit the key, not an empty string."
        )
    if "run_if" in locations or "required_outcome" in locations or run_if_message:
        hints.append(
            "run_if is only a control edge with source_task_id and required_outcome; "
            "required_outcome is task.finished or an allowlisted calendar outcome; omit run_if rather than sending {}."
        )
    if not hints and "tasks" in locations:
        hints.append(
            "A domain request uses mode=tasks with the required domain agents and exactly "
            "one terminal synthesizer; direct_chat has no tasks or domain claims."
        )
    return " ".join(hints)[:1200]


def _normalize_provider_plan_arguments(
    arguments: Any,
) -> tuple[Any, list[str]]:
    """Repair only known agent-scoped provider compatibility drift.

    The returned payload is a deep copy. Canonical Pydantic contracts remain
    strict; unknown agents, invalid values, and all graph fields are untouched.
    """
    normalized = deepcopy(arguments)
    if not isinstance(normalized, dict) or not isinstance(normalized.get("tasks"), list):
        return normalized, []
    repair_codes: list[str] = []
    for task in normalized["tasks"]:
        if not isinstance(task, dict):
            continue
        agent = task.get("agent")
        if agent not in _KNOWN_PLANNER_AGENTS:
            continue
        empty_placeholder_removed = False
        if task.get("evidence_policy") == "":
            task.pop("evidence_policy", None)
            empty_placeholder_removed = True
        if task.get("outcome_contract") == "":
            task.pop("outcome_contract", None)
            empty_placeholder_removed = True
        if task.get("run_if") in ("", {}):
            task.pop("run_if", None)
            empty_placeholder_removed = True
        if empty_placeholder_removed and "empty_optional_task_fields_removed" not in repair_codes:
            repair_codes.append("empty_optional_task_fields_removed")
        if agent != "web" and task.get("evidence_policy") in {
            "casual_discovery", "strict_verification",
        }:
            task.pop("evidence_policy", None)
            if "non_web_evidence_policy_removed" not in repair_codes:
                repair_codes.append("non_web_evidence_policy_removed")
        if agent != "calendar" and task.get("outcome_contract") == "calendar.availability.v1":
            task.pop("outcome_contract", None)
            if "non_calendar_outcome_contract_removed" not in repair_codes:
                repair_codes.append("non_calendar_outcome_contract_removed")
        # DeepSeek occasionally places the known Relationship write capability
        # in the task-scoped Calendar outcome field. Recover only this exact,
        # known field relocation; all other invalid values remain fail-closed.
        if (
            agent == "relationship"
            and task.get("outcome_contract") == DATE_INVITATION_WRITE_INTENT
            and normalized.get("write_intent") in (None, "none", DATE_INVITATION_WRITE_INTENT)
        ):
            task.pop("outcome_contract", None)
            normalized["write_intent"] = DATE_INVITATION_WRITE_INTENT
            if "misplaced_date_invitation_intent_recovered" not in repair_codes:
                repair_codes.append("misplaced_date_invitation_intent_recovered")
    return normalized, repair_codes[:2]


def _record_planner_attempt(
    metrics: PlannerMetrics,
    *,
    attempt: int,
    status: str,
    failure_code: str = "",
    raw_content: str = "",
    tool_calls: list[dict] | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    duration_ms: int = 0,
    error: str = "",
    repair_codes: list[str] | None = None,
    ttft_ms: int = 0,
    tps: float = 0.0,
    model_name: str = "",
) -> None:
    """Keep a bounded, local-debug-only summary for each provider attempt."""
    metrics.attempts.append({
        "attempt": attempt,
        "status": status,
        "failure_code": failure_code,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "ttft_ms": ttft_ms,
        "tps": tps,
        "model_name": model_name,
        "raw_content": raw_content,
        "tool_calls": tool_calls or [],
        "error": error,
        "repair_codes": [code for code in (repair_codes or []) if code in _REPAIR_CODES][:2],
    })


def _record_planner_request(metrics: PlannerMetrics, result: Any) -> None:
    """Append one provider request summary to the Planner debug envelope."""
    try:
        metrics.llm_requests.append({
            "input_tokens": int(getattr(result, "input_tokens", 0) or 0),
            "output_tokens": int(getattr(result, "output_tokens", 0) or 0),
            "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
            "ttft_ms": int(getattr(result, "ttft_ms", 0) or 0),
            "tps": round(float(getattr(result, "tps", 0) or 0), 3),
            "model_name": str(getattr(result, "model_name", "") or ""),
        })
    except Exception:
        pass


class _OpportunityArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal: Literal["none", "social_opening"] = "none"
    evidence_span: str = ""
    confidence: float = 0.0


class _DecomposeTasksArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal["tasks", "direct_chat"] = "tasks"
    write_intent: PlannerWriteIntent = Field(
        description=(
            "Required semantic capability decision. Use relationship.date_invitation.v1 "
            "when the user asks Ayue to actually create an empty date invitation card "
            "for an existing contact. Use none for capability questions, advice, contact "
            "lookup, or match search."
        ),
    )
    presentation_mode: Literal["default", "itinerary"] = "default"
    tasks: list[SubTask] = Field(default_factory=list, max_length=5)
    direct_reply: str | None = Field(default=None, max_length=160)
    direct_messages: list[str] = Field(default_factory=list, max_length=3)
    opportunity: _OpportunityArguments | None = None

    @model_validator(mode="after")
    def _reject_unjustified_synthesizer_only_output(self) -> "_DecomposeTasksArguments":
        """Keep provider-authored task mode from silently bypassing all domains."""
        if self.mode != "tasks" or not self.tasks:
            return self
        has_domain = any(task.agent != "synthesizer" for task in self.tasks)
        has_social_opening = (
            self.opportunity is not None
            and self.opportunity.signal == "social_opening"
        )
        if not has_domain and not has_social_opening:
            raise ValueError(
                "provider task mode cannot contain only a synthesizer; use direct_chat "
                "for ordinary conversation or include every required domain task"
            )
        availability_ids = {
            task.id
            for task in self.tasks
            if task.agent == "calendar"
            and task.outcome_contract == "calendar.availability.v1"
        }
        consumed_availability_ids = {
            task.run_if.source_task_id
            for task in self.tasks
            if task.run_if is not None
            and task.run_if.source_task_id in availability_ids
        }
        orphan_availability_ids = availability_ids - consumed_availability_ids
        for task in self.tasks:
            if task.id in orphan_availability_ids:
                # An unconsumed outcome cannot control the graph. Treat it as
                # provider compatibility noise so the Calendar Agent retains
                # normal READ/WRITE ownership instead of failing the whole turn.
                task.outcome_contract = None
        return self


def _decompose_tool_schema() -> dict[str, Any]:
    """Build the Ollama tool definition for the single decompose_tasks function."""
    schema = inline_json_schema_refs(_DecomposeTasksArguments.model_json_schema())
    schema.pop("title", None)
    # Some providers turn nullable optional properties with ``default: null``
    # into empty-string placeholders. Keep these keys optional in the
    # provider-facing schema, but remove the nullable/default branch; the
    # canonical Pydantic contract remains unchanged and still accepts null.
    task_schema = (
        schema.get("properties", {})
        .get("tasks", {})
        .get("items", {})
    )
    if isinstance(task_schema, dict):
        for field_name in ("evidence_policy", "outcome_contract", "run_if"):
            field_schema = task_schema.get("properties", {}).get(field_name)
            if not isinstance(field_schema, dict):
                continue
            options = [
                option for option in field_schema.get("anyOf", [])
                if isinstance(option, dict) and option.get("type") != "null"
            ]
            if len(options) != 1:
                continue
            description = field_schema.get("description")
            field_schema.clear()
            field_schema.update(options[0])
            if description:
                field_schema["description"] = description
    return {
        "type": "function",
        "function": {
            "name": "decompose_tasks",
            "description": "把使用者請求拆解成一張靜態子任務 DAG。",
            "parameters": schema,
        },
    }


_PLANNER_PROMPT_VERSION = "compact_v3_write_intent_v1"
_PLANNER_MAX_RECENT_MESSAGES = 4
_PLANNER_MAX_RECENT_CHARS = 2000
_PLANNER_MAX_SYSTEM_CHARS = 6000
_PLANNER_MAX_SCHEMA_CHARS = 3500
_PLANNER_MAX_PROVIDER_CHARS = 9500

_PLANNER_SYSTEM = f"""{AYUE_CORE_IDENTITY}
{AYUE_MISSION_SHORT}
{AYUE_VOICE_SHORT}

你是公開阿月 V3 Planner，只做語意 routing 與靜態 sub-task DAG。
不執行工具、不回答 domain／產品事實、不產生 user、proposal、event ID、revision、confirmation 或 tool arguments。
只呼叫 decompose_tasks 一次，不輸出普通文字；輸出必須符合提供的 schema。

輸出規則：
- mode=direct_chat 只適用於不需要 App、domain、private、external truth 或 workflow 的一般聊天；tasks 為空，direct_reply 不超過 160 字。
- direct_reply 短自然、像熟朋友；先接住內容再給看法或下一步；不得宣稱副作用已完成。
- 只要任一子需求需要 state、產品能力、特定對方聊天內容、行事曆、配對、profile、relationship、places 或外部資料，整個回合就使用 mode=tasks；不得只回答可聊天的部分，也不得輸出只有 synthesizer 的 tasks。
- tasks 最多 4 個 domain 加 1 個 terminal synthesizer，總數最多 5；只有複合需求才拆多個。
- task 可使用 id、agent、depends_on、task_brief、evidence_policy、outcome_contract、run_if；Web 以外省略 evidence_policy，Calendar availability 以外省略 outcome_contract；outcome_contract 只能是 calendar.availability.v1，約會邀請 capability 絕對不要放進 task，只屬於頂層 write_intent。沒有 Calendar control edge 就省略 run_if。絕對不要把 match.current_proposal.v1 等 observation schema 名稱填入這三個欄位。不要使用 type 或 task_agent。
- write_intent 必填。當使用者是在要求阿月實際建立一張給既有聯絡人的空白約會邀請卡，而不是只詢問功能或討論怎麼約時，使用 relationship.date_invitation.v1，且只輸出 relationship -> synthesizer；其他情況用 none。對象是否為 accepted contact、錯字與拼音由 Relationship runtime 驗證，Match 絕不作前置檢查。
- depends_on 只表示下游會消費上游 typed observation、candidate ref 或其他明確 contract；run_if 是不傳遞 observation 的控制條件。獨立查詢放同一層，不為了排序而串接。
- presentation_mode 只有 default／itinerary。itinerary 是普通 composition 的 editorial hint，不是固定 headings、server schema、卡片或特殊 renderer。

Agent ownership：
calendar=本人行程、空檔、建立／修改／取消、共同日期、calendar draft 與 recent mutation 驗證。
places=附近地點、餐廳、景點、地址、地圖、距離，以及 hours／price／rating／walking 等結構化地點資料。
web=外部／近期／公開資訊、活動、新聞、文章、論壇、社群、URL，以及 Places 無法證明的公開主張。
match=單筆 active proposal/search lifecycle。
relationship=accepted contacts aggregate／@ 對象與公開互動。
profile=本人 profile、memory、近期情境與 assessment start／restart。
product_info=阿月／App 的能力、流程、限制、隱私與 Public／Private 入口邊界。
synthesizer=只根據本回合 verified observations 與 bounded context 組最終回覆。

關鍵 routing：
- 以上規則以完整語意判斷，不使用關鍵字或 regex router；不確定時保留 tasks 讓既有 domain flow 處理。
- 結構化 hours、price、rating、walking 即使包含今晚／目前，使用 places -> synthesizer，不自動加 web。
- 優惠、特殊菜單、活動、臨時歇業、社群公告等非結構化或目前公開主張，使用 places -> web -> synthesizer。Places task 只建立候選池；Web 只能研究 t1 的 server-issued candidate refs，不得發明新地點。
- 一般區域半日／一日遊使用 t1=places、terminal t2=synthesizer、presentation_mode=itinerary。
- 找一個有直接證據的新活動並排整天，固定使用 Web、Places 與 terminal Synthesizer；若同時有具體日期的個人外出安排，依 Calendar availability policy 先做唯讀檢查。除非使用者要求保存，不建立 calendar mutation；活動研究排除 recent_messages 與 recent calendar mutation 已出現的活動。
- 外部探索使用 casual_discovery；明確官方查證或醫療／法律／金融／安全風險使用 strict_verification。
- calendar_draft 的 missing_fields、candidates 補充、修正或選擇用 calendar。`calendar_recent_mutation` 的成功與否只交 calendar 做唯讀驗證，不自行猜測。
- 明確開始／重做 assessment 用 profile；產品問題用正常 product_info -> synthesizer DAG，不選內部 knowledge section，也不產生舊的 task-free ProductInfo envelope。
- opportunity.signal="social_opening" 只用於間接表達想找人一起參與、尚未要求從既有聯絡人挑選或開始找新人的情況；evidence_span 必須是 current message 的連續原文，confidence >= 0.8。從既有聯絡人挑選用 relationship；找新的人用 match；單純寒暄、孤單或負面情緒使用 signal="none"。
- Web task brief 必須保留原始 proposition、地點／日期限制與 evidence class；不可把背景資料改成新的答案目標。

所有 state、authority、ID、revision、confirmation 與真實工具參數由 server/runtime 負責。"""

_PLANNER_SYSTEM += """

Calendar availability policy (must follow for every concrete date):
- If the user asks for a personal outing/date/itinerary on a concrete or
  resolvable date, create a read-only Calendar task first, even when the user
  explicitly says not to add an event.  Do not create a mutation task unless
  the user asks to save/create/update/cancel an event.
- The Calendar precheck task must be `agent=calendar` with
  `outcome_contract=calendar.availability.v1` and a brief that asks for one
  `calendar.list_my_events` read covering the requested date/window.
- Ordinary prechecks use a control-only edge:
  `run_if={source_task_id:<calendar-id>,required_outcome:"task.finished"}`.
  This means later recommendations wait for Calendar but still proceed when
  the calendar read is busy or unavailable; Synthesizer explains the warning.
- Only explicit conditional wording such as "有事就算了／沒事才繼續／有空才找" may
  use `required_outcome="calendar.no_scheduled_events"`. A busy Calendar result
  then skips all gated downstream reads. Never mark a valid busy answer FAILED.
- Keep the Calendar control edge separate from `depends_on`; do not pass raw
  calendar events into Web, Places, Relationship, or another domain just to
  implement a branch. Synthesizer remains terminal and receives the Calendar
  observation.
- A pure public activity lookup without a personal date/outing does not need
  an automatic Calendar precheck. Advice-only requests must never emit a
  Calendar mutation task.
- Exact example 「這週六，從目前認識的人挑一位，找新活動，再排附近晚餐，只給建議」:
  c1=calendar(outcome_contract=calendar.availability.v1);
  r1=relationship(run_if c1:task.finished) and w1=web(run_if c1:task.finished) in parallel;
  p1=places(depends_on=[w1]); s1=synthesizer(depends_on=[c1,r1,p1]). Places uses
  the typed Web activity venue.
- With explicit 「有事就算了／沒事才繼續」, change r1 and w1 to
  calendar.no_scheduled_events. Never replace this graph with direct_chat or synth-only.
"""

def _planner_recent_messages(turn_ctx: PublicAgentTurnContext) -> list[dict[str, str]]:
    """Keep the Planner history bounded and remove the saved current message duplicate."""
    current = str(turn_ctx.message or "").strip()
    source = list(turn_ctx.recent_messages or [])
    if source and isinstance(source[-1], dict):
        last_role = str(source[-1].get("role") or "")
        last_content = str(source[-1].get("content") or "").strip()
        if last_role == "user" and last_content == current:
            source.pop()

    selected: list[dict[str, str]] = []
    used = 0
    for item in reversed(source):
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        remaining = _PLANNER_MAX_RECENT_CHARS - used
        if remaining <= 0:
            break
        clipped = content[:remaining]
        selected.append({"role": role, "content": clipped})
        used += len(clipped)
        if len(selected) >= _PLANNER_MAX_RECENT_MESSAGES:
            break
    selected.reverse()
    return selected

def _planner_clock(turn_ctx: PublicAgentTurnContext) -> dict[str, Any]:
    """Project only clock fields useful to Planner routing."""
    clock = turn_ctx.clock.model_dump()
    projected = {
        key: clock[key]
        for key in ("timezone", "local_date", "local_time", "weekday_zh_tw")
        if clock.get(key)
    }
    references = clock.get("temporal_references") or {}
    if references:
        projected["temporal_references"] = references
    return projected

def _planner_prompt(turn_ctx: PublicAgentTurnContext) -> str:
    """Build a compact, privacy-safe user/context message for the Planner."""
    payload: dict[str, Any] = {
        "message": turn_ctx.message,
        "clock": _planner_clock(turn_ctx),
    }
    recent_messages = _planner_recent_messages(turn_ctx)
    if recent_messages:
        payload["recent_messages"] = recent_messages

    active = turn_ctx.active_proposal
    if isinstance(active, dict):
        active_projection = {
            key: active[key]
            for key in ("status", "counterparty", "user_can_decide")
            if active.get(key) not in (None, "")
        }
        if active_projection:
            payload["active_proposal"] = active_projection

    optional_fields = (
        "calendar_draft",
        "calendar_recent_reference",
        "calendar_recent_mutation",
        "mentioned_contacts",
        "recent_contact_reference",
    )
    for field_name in optional_fields:
        value = getattr(turn_ctx, field_name, None)
        if value not in (None, "", [], {}):
            payload[field_name] = value

    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

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


_DATE_INVITATION_RELATIONSHIP_BRIEF = (
    "Propose relationship.start_date_coordination exactly once for the explicit "
    "empty date-card request. Resolve the target from the current name span, one "
    "validated mention, or the prompt-safe recent contact; do not perform a read precheck."
)
_DATE_INVITATION_SYNTHESIZER_BRIEF = (
    "Present only the server-owned confirmation preview or verified write result; "
    "do not ask for date, time, place, activity, budget, or notes."
)


def _canonicalize_write_intent_briefs(plan: Plan) -> Plan:
    """Replace provider prose with bounded server-owned briefs for typed writes."""
    if plan.write_intent != DATE_INVITATION_WRITE_INTENT:
        return plan
    tasks = [
        task.model_copy(update={
            "task_brief": (
                _DATE_INVITATION_RELATIONSHIP_BRIEF
                if task.agent == "relationship"
                else _DATE_INVITATION_SYNTHESIZER_BRIEF
            ),
        })
        for task in plan.tasks
    ]
    return plan.model_copy(update={"tasks": tasks})


def plan_turn(turn_ctx: PublicAgentTurnContext) -> tuple[Plan | None, PlannerMetrics]:
    """Call LLM (function calling) to decompose the request into a static Plan.

    Returns (plan_or_none, metrics). The tool-call arguments ARE the Plan.
    """
    metrics = PlannerMetrics()
    metrics.prompt_version = _PLANNER_PROMPT_VERSION
    started = time.perf_counter()
    prompt = _planner_prompt(turn_ctx)
    metrics.prompt_raw = f"SYSTEM:\n{_PLANNER_SYSTEM}\nUSER:\n{prompt}"
    metrics.tools_raw = [_decompose_tool_schema()]
    attempt_prompt = prompt

    for attempt in range(1, _PLANNER_MAX_ATTEMPTS + 1):
        validation_hint = ""
        attempt_started = time.perf_counter()
        metrics.llm_call_count += 1
        try:
            result = generate_chat_completion_with_tools(
                attempt_prompt, metrics.tools_raw, temperature=0,
                system_prompt=_PLANNER_SYSTEM, prefer_fast_model=True,
            )
        except Exception as exc:
            duration_ms = round((time.perf_counter() - attempt_started) * 1000)
            metrics.duration_ms += duration_ms
            metrics.error = str(exc)
            metrics.failure_code = "provider_error"
            _record_planner_attempt(
                metrics, attempt=attempt, status="provider_error",
                failure_code=metrics.failure_code, duration_ms=duration_ms,
                error=metrics.error,
            )
            break
        input_tokens = int(result.input_tokens or 0)
        output_tokens = int(result.output_tokens or 0)
        duration_ms = int(result.duration_ms or 0)
        metrics.input_tokens += input_tokens
        metrics.output_tokens += output_tokens
        metrics.duration_ms += duration_ms
        metrics.raw_content = str(result.content or "")
        metrics.tool_calls_raw = result.tool_calls or []
        metrics.error = ""
        _record_planner_request(metrics, result)

        if not result.tool_calls:
            failure_code = "missing_tool_call"
            _record_planner_attempt(
                metrics, attempt=attempt, status="protocol_error",
                failure_code=failure_code, raw_content=metrics.raw_content,
                tool_calls=metrics.tool_calls_raw, input_tokens=input_tokens,
                output_tokens=output_tokens, duration_ms=duration_ms,
                ttft_ms=int(getattr(result, "ttft_ms", 0) or 0),
                tps=round(float(getattr(result, "tps", 0) or 0), 3),
                model_name=str(getattr(result, "model_name", "") or ""),
            )
        else:
            tc = result.tool_calls[0]
            if tc.get("name") != "decompose_tasks":
                failure_code = "wrong_function_name"
                _record_planner_attempt(
                    metrics, attempt=attempt, status="protocol_error",
                    failure_code=failure_code, raw_content=metrics.raw_content,
                    tool_calls=metrics.tool_calls_raw, input_tokens=input_tokens,
                    output_tokens=output_tokens, duration_ms=duration_ms,
                    ttft_ms=int(getattr(result, "ttft_ms", 0) or 0),
                    tps=round(float(getattr(result, "tps", 0) or 0), 3),
                    model_name=str(getattr(result, "model_name", "") or ""),
                )
            else:
                arguments = tc.get("arguments") or {}
                normalized_arguments, repair_codes = _normalize_provider_plan_arguments(arguments)
                try:
                    validated = _DecomposeTasksArguments.model_validate(normalized_arguments)
                except Exception as exc:
                    # Product-info is a read-only presentation mode. If the model
                    # correctly selected that mode but paraphrased the enum values,
                    # discard every untrusted field and return a safe, generic product
                    # projection instead of failing the whole turn. This is protocol
                    # repair, not natural-language intent classification.
                    if isinstance(arguments, dict) and arguments.get("mode") == "product_info":
                        metrics.decision_mode = "tasks"
                        metrics.product_info_fallback_reason = "legacy_product_info_mode"
                        _record_planner_attempt(
                            metrics, attempt=attempt, status="repaired",
                            failure_code="legacy_product_info_mode",
                            raw_content=metrics.raw_content, tool_calls=metrics.tool_calls_raw,
                            input_tokens=input_tokens, output_tokens=output_tokens,
                            duration_ms=duration_ms,
                            repair_codes=repair_codes,
                            ttft_ms=int(getattr(result, "ttft_ms", 0) or 0),
                            tps=round(float(getattr(result, "tps", 0) or 0), 3),
                            model_name=str(getattr(result, "model_name", "") or ""),
                        )
                        return _product_info_plan(turn_ctx.message), metrics
                    failure_code = "invalid_arguments"
                    metrics.error = str(exc)
                    validation_hint = _planner_validation_retry_hint(exc)
                    _record_planner_attempt(
                        metrics, attempt=attempt, status="protocol_error",
                        failure_code=failure_code, raw_content=metrics.raw_content,
                        tool_calls=metrics.tool_calls_raw, input_tokens=input_tokens,
                        output_tokens=output_tokens, duration_ms=duration_ms,
                        error=metrics.error,
                        ttft_ms=int(getattr(result, "ttft_ms", 0) or 0),
                        tps=round(float(getattr(result, "tps", 0) or 0), 3),
                        model_name=str(getattr(result, "model_name", "") or ""),
                    )
                else:
                    opportunity = _opportunity_from_arguments(validated)
                    try:
                        plan = Plan(
                            mode=validated.mode,
                            write_intent=validated.write_intent,
                            presentation_mode=validated.presentation_mode,
                            tasks=validated.tasks,
                            direct_reply=validated.direct_reply,
                            direct_messages=validated.direct_messages,
                            opportunity=opportunity,
                        )
                    except Exception as exc:
                        # Preserve a valid domain DAG if the provider incorrectly adds a
                        # direct reply alongside it. An invalid/empty DAG must be retried;
                        # silently replacing it with Synthesizer-only would bypass every
                        # requested domain capability.
                        try:
                            if validated.tasks:
                                plan = Plan(
                                    mode="tasks",
                                    write_intent=validated.write_intent,
                                    presentation_mode=validated.presentation_mode,
                                    tasks=validated.tasks,
                                    opportunity=opportunity,
                                )
                            else:
                                raise ValueError("invalid plan has no executable domain DAG")
                        except Exception as repair_exc:
                            failure_code = "invalid_arguments"
                            metrics.error = str(repair_exc)
                            validation_hint = _planner_validation_retry_hint(repair_exc)
                            _record_planner_attempt(
                                metrics, attempt=attempt, status="protocol_error",
                                failure_code=failure_code, raw_content=metrics.raw_content,
                                tool_calls=metrics.tool_calls_raw, input_tokens=input_tokens,
                                output_tokens=output_tokens, duration_ms=duration_ms,
                                error=metrics.error,
                                ttft_ms=int(getattr(result, "ttft_ms", 0) or 0),
                                tps=round(float(getattr(result, "tps", 0) or 0), 3),
                                model_name=str(getattr(result, "model_name", "") or ""),
                            )
                            plan = None
                        else:
                            metrics.direct_chat_fallback_reason = "incompatible_direct_chat_payload"
                    if plan is None:
                        pass
                    else:
                        plan = _canonicalize_write_intent_briefs(plan)
                        _record_planner_attempt(
                            metrics, attempt=attempt,
                            status="repaired" if repair_codes else "ok",
                            raw_content=metrics.raw_content, tool_calls=metrics.tool_calls_raw,
                            input_tokens=input_tokens, output_tokens=output_tokens,
                            duration_ms=duration_ms,
                            repair_codes=repair_codes,
                            ttft_ms=int(getattr(result, "ttft_ms", 0) or 0),
                            tps=round(float(getattr(result, "tps", 0) or 0), 3),
                            model_name=str(getattr(result, "model_name", "") or ""),
                        )
                        metrics.failure_code = ""
                        metrics.error = ""
                        metrics.decision_mode = plan.mode
                        return plan, metrics

        if failure_code not in _PLANNER_RETRYABLE_FAILURES:
            metrics.failure_code = failure_code
            break
        if attempt >= _PLANNER_MAX_ATTEMPTS:
            metrics.failure_code = failure_code
            break
        metrics.retry_count += 1
        metrics.retry_reason = failure_code
        attempt_prompt = _planner_retry_prompt(
            prompt,
            failure_code,
            validation_hint,
        )

    metrics.duration_ms = max(metrics.duration_ms, round((time.perf_counter() - started) * 1000))
    return None, metrics
