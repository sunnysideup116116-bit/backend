"""V3 Calendar sub-agent.

Calendar reads remain ordinary registered read-tool proposals.  Calendar
mutations are emitted as a typed command batch and are not executable tool
calls; the Scheduler performs the deterministic preflight and owns all
authority fields.
"""

from __future__ import annotations

import json
from typing import Any

from services.ayue_agent.tool_registry import (
    get_tool_spec,
    planner_arguments_allowed,
    planner_arguments_schema,
    planner_tool_names,
)

from ..calendar_commands import CalendarCommandBatch, normalize_calendar_batch_payload
from ..contracts import AgentContextSlice, ToolProposal
from ..schema_utils import inline_json_schema_refs
from . import base as base_agent
from .base import SubAgentMetrics


# Keep the old private seam for local callers while sharing one implementation.
_clean_schema = inline_json_schema_refs


_SYSTEM = """你是公開阿月的行事曆子代理，負責提出本人行程的 read proposal 或 typed mutation intent。

行為規則：
- 真正查詢行程時才使用 read tools；新增、修改、取消一律使用 calendar.submit_commands。
- 不要為 mutation 先呼叫 calendar.find_my_event；target 由 server preflight 唯一 resolve。
- 只能填入 current user message、明確延續的最近對話，或 server-owned context 明確提供的值；不得自行補齊看似合理的日期、時間、時長、標題、地點或對象。
- command 欄位必須使用 canonical 名稱：action（不要用 type）、target_reference（不要用 target）、target_hint（自然語言 identity clue）。只有 server 提供的 recent_event 或 candidate_1..candidate_3 才能放入 target_reference。
- target_hint 只保留活動／地點等辨識線索，例如「牙醫」或「睡覺」；不要把取消／修改、禮貌、情緒、代名詞或完整對話句塞進去。
- create/update 的 title 只保留活動本身（例如「下下周四我要去駁二玩」應拆成 date=下下周四、title=去駁二玩），不要把日期、時間或操作詞塞進 title。
- 使用者明確說出「半小時／一小時／一個半小時／兩小時」等持續時間時，填 duration_minutes；不要自行從開始時間猜 duration 或計算 end_time。server 會在 preflight 產生結束時間；若同時有 end_time，兩者不一致時交由 server 追問。
- 「延後／提前／整段往後或往前」既有行程時，填 update 的 time_shift_minutes signed integer；這不是 duration_minutes，也不要自行計算平移後的 start/end。time_shift_minutes 不要與新的 date/start_time/end_time/duration_minutes 同時提供。
- 缺欄位仍提交 typed command，讓 server 回 needs_clarification；不要自行改寫成固定的追問或自由文字。
- 有 calendar_draft 且 resolved_target.bound=true 時，已選定的行程是 server-owned continuation target；只提交本回合新的 changes，不能要求使用者重複標題、原日期或原時間。除非本回合明確提出另一個 target_hint/target_reference，否則使用 draft_mode=continue；draft_mode 是提示，不是丟棄 server draft 的權限。
- 使用者以「這筆／那筆／它／他／她／剛剛提到的行程」指涉 server context 的最近唯一行程時，使用 recent_event。
- ambiguity clarification 中的 candidate_1..candidate_3 只能原樣回傳 calendar_draft.candidates 已提供的 reference，不得發明 token。
- 同一回合多個 mutation 放在同一批 commands，維持使用者描述順序。
- submit_commands 只描述意圖，不執行副作用，也不自行決定 confirmation。
- 不得提供 user_id、event_id、revision、expected_revision、match_id、coordination_id 或其他 authority field。
"""


_READ_TOOLS = planner_tool_names(
    can_start_search=False,
    can_decide_active_proposal=False,
    can_edit_calendar=False,
    can_read_mentioned_contacts=False,
    can_use_web=False,
    can_use_places=False,
    can_start_assessments=False,
)
_READ_TOOLS = frozenset(name for name in _READ_TOOLS if name.startswith("calendar."))
_COMMAND_TOOL_NAME = "calendar.submit_commands"

_SAFETY_ADDENDUM = """Calendar schema and preflight own field names, enums, missing-field calculation,
date normalization, permission checks, target resolution, revision/CAS and
confirmation. Follow those typed contracts instead of recreating them in
natural language."""

_SAFETY_ADDENDUM += """
若 context 明確提供 `calendar_recent_mutation`，且使用者是在確認上一筆行事曆寫入是否成功，請只提出唯讀的 `calendar.verify_recent_mutation`；不要再次提出 create/update/cancel。若使用者描述的是新的變更，才使用 `calendar.submit_commands`。
"""


class CalendarAgentResult(list):
    """Backward-compatible read proposal list plus typed mutation commands."""

    def __init__(
        self,
        reads: list[ToolProposal] | None = None,
        commands: list[Any] | None = None,
        command_errors: list[dict[str, str]] | None = None,
    ):
        super().__init__(reads or [])
        self.commands = list(commands or [])
        self.command_errors = list(command_errors or [])


def _tools_schema() -> list[dict[str, Any]]:
    tools: list[dict[str, Any]] = []
    for name in sorted(_READ_TOOLS):
        spec = get_tool_spec(name)
        if spec is None:
            continue
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": spec.description,
                "parameters": _clean_schema(planner_arguments_schema(spec)),
            },
        })
    command_spec = get_tool_spec(_COMMAND_TOOL_NAME)
    command_schema = _clean_schema(
        planner_arguments_schema(command_spec)
        if command_spec is not None else CalendarCommandBatch.model_json_schema()
    )
    tools.append({
        "type": "function",
        "function": {
            "name": _COMMAND_TOOL_NAME,
            "description": command_spec.description if command_spec is not None else "提交 typed Calendar mutation command；只解析意圖，不執行寫入。",
            "parameters": command_schema,
        },
    })
    return tools


def _prompt(context_slice: AgentContextSlice, task_brief: str) -> str:
    return f"""任務說明：{task_brief}

目前可公開的 context：
{json.dumps(context_slice.payload, ensure_ascii=False)}

只呼叫一個或多個上述 function，不要輸出其他文字。"""


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[CalendarAgentResult, SubAgentMetrics]:
    metrics = SubAgentMetrics()
    reads: list[ToolProposal] = []
    commands: list[Any] = []
    command_errors: list[dict[str, str]] = []
    try:
        tools = _tools_schema()
        prompt = _prompt(context_slice, task_brief)
        system_prompt = f"{_SYSTEM}\n{_SAFETY_ADDENDUM}"
        metrics.prompt_raw = f"SYSTEM:\n{system_prompt}\nUSER:\n{prompt}"
        metrics.tools_raw = tools
        metrics.input_payload = context_slice.payload
        result = base_agent.generate_chat_completion_with_tools(
            prompt, tools, temperature=0, system_prompt=system_prompt,
        )
        metrics.input_tokens = result.input_tokens
        metrics.output_tokens = result.output_tokens
        metrics.duration_ms = result.duration_ms
        metrics.tool_calls_raw = result.tool_calls or []
        metrics.content_raw = result.content or ""
        for tool_call in result.tool_calls or []:
            name = str(tool_call.get("name") or "")
            arguments = tool_call.get("arguments") or {}
            if name == _COMMAND_TOOL_NAME:
                try:
                    validated = CalendarCommandBatch.model_validate(
                        normalize_calendar_batch_payload(arguments)
                    )
                except Exception:
                    metrics.rejected_calls.append("calendar_command_schema_invalid")
                    command_errors.append({
                        "code": "invalid_command",
                        "message": "這筆行程資訊格式不完整，請再說一次日期、時間與行程名稱。",
                    })
                    continue
                commands.extend(validated.commands)
                continue
            if name not in _READ_TOOLS:
                metrics.rejected_calls.append("tool_not_visible")
                continue
            spec = get_tool_spec(name)
            if spec is None or not planner_arguments_allowed(spec, arguments):
                metrics.rejected_calls.append("schema_invalid")
                continue
            try:
                reads.append(ToolProposal(tool_name=name, arguments=arguments))
            except Exception:
                metrics.rejected_calls.append("forbidden_or_invalid_arguments")
        if result.tool_calls and not reads and not commands and metrics.rejected_calls:
            metrics.error = "no_valid_proposal"
        return CalendarAgentResult(reads, commands, command_errors), metrics
    except Exception as exc:
        metrics.error = str(exc)
        return CalendarAgentResult(reads, commands, command_errors), metrics
