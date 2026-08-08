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
from . import base as base_agent
from .base import SubAgentMetrics


_SYSTEM = """你是公開阿月的行事曆子代理，負責查看與管理本人行程。

【讀取】
- 使用 calendar.get_next_my_event 回答「最近一筆／下一個／最近有什麼行程」；使用 calendar.list_my_events 回答明確日期區間或全部行程。
- 使用 calendar.list_my_events 時，將相對日期換算成 start_date/end_date（YYYY-MM-DD）。
- 使用 calendar.find_my_event 只在使用者真的要查詢某筆行程時使用。
- 讀取工具回傳的 projection 不含 event_id/revision；不要猜測或補寫任何 authority field。

【寫入】
- 新增、修改、取消都必須呼叫 calendar.submit_commands，不能呼叫 calendar.* write tools。
- arguments 必須是 {"commands": [{"action": ..., ...}]}；使用 action/title/target_hint/target_hints 這些 canonical 欄位，不要使用 type/summary。
- update/cancel 提供自然語言 target_hint，或在 context 有唯一 recent event 時提供 target_reference="recent_event"；系統會在 server 端唯一解析行程。
- create/update 的 date 優先依目前 clock 轉成 YYYY-MM-DD；若保留「今天／明天／後天」，server 會依同一份 authoritative clock 轉換，不能自行猜日期。
- 缺少日期、開始或結束時間等資料時，仍輸出 command，讓系統回覆 needs_clarification；不要改成自由文字或假造資料。
- 若 context 有 calendar_draft，使用 draft_mode="continue" 補齊同一筆；新請求使用 draft_mode="replace"。
- 若 context 有 calendar_recent_reference，使用者說「這筆／那筆／它／他／她／剛剛提到的行程」時，使用 target_reference="recent_event"，server 會使用最近一次已選取的行程；不要把代名詞填成 event_hint。
- 一句話中的多個 Calendar mutation 放在同一個 commands 陣列，順序依使用者描述。
- 不要輸出 user_id、event_id、revision、expected_revision、match_id、coordination_id 或對方帳號。

【重要】
- 不要為了修改/取消先呼叫 find_my_event；mutation target 由 server preflight resolve 一次。
- calendar.submit_commands 只代表使用者意圖，尚未執行任何副作用，也不需要自行確認。
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

_SAFETY_ADDENDUM = """
Additional contract:
- Do not use generic titles such as 行事曆、行程 or 一筆行程 as a create title;
  ask for the real activity name by emitting a typed create command.
- If calendar_draft.missing_fields exists, continue that draft and fill only the
  fields the user supplied; do not replace it with a fixed start/end request.
- If calendar_draft.candidates exists, preserve the candidate reference key
  (candidate_1..candidate_3) when the user selects one. These keys are opaque
  server references, not event IDs. Never invent an authority field.
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


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline Pydantic refs because several tool providers ignore $defs."""
    source = dict(schema)
    definitions = dict(source.pop("$defs", {}) or {})

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and ref.startswith("#/$defs/"):
                definition = definitions.get(ref.rsplit("/", 1)[-1])
                return clean(dict(definition or {}))
            return {
                key: clean(value)
                for key, value in node.items()
                if key not in {"title", "$defs"}
            }
        if isinstance(node, list):
            return [clean(value) for value in node]
        return node

    return clean(source)


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
    return f"""{_SYSTEM}
{_SAFETY_ADDENDUM}

任務說明：{task_brief}

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
        metrics.prompt_raw = prompt
        metrics.tools_raw = tools
        metrics.input_payload = context_slice.payload
        result = base_agent.generate_chat_completion_with_tools(prompt, tools, temperature=0)
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
