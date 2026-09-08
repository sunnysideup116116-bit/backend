"""Lightweight V3 Planner: only decomposes the request into a static sub-task DAG.

Uses native function calling: the model emits a `decompose_tasks` tool call
whose arguments ARE the typed Plan. No free-text JSON parsing.
"""

from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config import OLLAMA_REQUEST_TIMEOUT_SECONDS
from services.ai_service import generate_chat_completion_with_tools
from services.ayue_agent.contracts import PublicAgentTurnContext
from services.ayue_agent.product_identity import (
    AYUE_CORE_IDENTITY,
    AYUE_MISSION_SHORT,
    AYUE_VOICE_SHORT,
)
from .contracts import (
    DATE_COORDINATION_CANCEL_WRITE_INTENT,
    DATE_INVITATION_WRITE_INTENT,
    OpportunitySignal,
    Plan,
    PlaceSelection,
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
    "missing_tool_call", "wrong_function_name", "invalid_arguments", "provider_error",
})
_KNOWN_PLANNER_AGENTS = frozenset({
    "calendar", "places", "web", "match", "relationship", "profile",
    "product_info", "synthesizer",
})


def _planner_provider_failure(exc: Exception) -> tuple[str, bool]:
    """Retry only transient transport/server failures, never a spent timeout."""
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return "provider_timeout", False
    status = getattr(exc, "status_code", None)
    if status is None and isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
    if status in {401, 403}:
        return "provider_auth_error", False
    if status == 429:
        # Immediate retry amplifies throttling; let a later turn retry rather
        # than sleeping in the Planner or ignoring the provider's cooldown.
        return "provider_rate_limited", False
    if status in {500, 502, 503, 504}:
        return "provider_error", True
    if isinstance(exc, (ConnectionError, httpx.NetworkError)):
        return "provider_error", True
    return "provider_error", False


_REPAIR_CODES = frozenset({
    "non_web_evidence_policy_removed",
    "non_calendar_outcome_contract_removed",
    "empty_optional_task_fields_removed",
    "misplaced_date_invitation_intent_recovered",
    "match_write_intent_normalized",
    "match_observation_contract_removed",
    "match_intent_object_normalized",
    "non_match_match_intent_removed",
    "known_counterparty_removed",
    "unsupported_presentation_mode_defaulted",
    "date_write_presentation_mode_defaulted",
    "resolved_place_task_reference_removed",
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
    place_mode_message = any(
        "place_mode is required for Places tasks" in message
        or "place_mode is only valid for Places tasks" in message
        for message in messages
    )
    web_mode_message = any(
        "web_mode is required for Web tasks" in message
        or "web_mode is only valid for Web tasks" in message
        or "public_lookup Web task cannot depend on Places" in message
        or "Web task requires a Places dependency" in message
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
            "relationship.date_coordination_cancel.v1 requires",
            "relationship date write intents require",
        )
    )
    missing_synthesizer = any(
        "plan must contain exactly one synthesizer" in message
        for message in messages
    )
    hints: list[str] = []
    if "write_intent" in locations or write_intent_message:
        hints.append(
            "write_intent is required. Use relationship.date_invitation.v1 only when the user explicitly "
            "asks Ayue to create or send a date invitation now; wanting or planning to invite someone, "
            "naming or @-mentioning a companion, and asking only for places or advice use none. "
            "Use relationship.date_coordination_cancel.v1 only for an explicit cancellation. "
            "A date write requires one Relationship write task and one terminal Synthesizer; independent "
            "typed read-only tasks may remain, but Match is never a precheck."
        )
    if "match_intent" in locations or any("Match task requires match_intent" in msg for msg in messages):
        hints.append(
            "Every Match task requires match_intent: status, counterparty, start_search, "
            "restart_search, cancel_search or clarify. Accepting, declining and withdrawing an invitation "
            "are Hub card actions, not chat tools. Classify the "
            'current request. Use a string, e.g. "match_intent":"start_search", not an object or flags. '
            "Wanting a new match never means accepting an existing proposal."
        )
    if "task_brief" in locations:
        hints.append("Every task, including Synthesizer, requires a non-empty task_brief string.")
    if "evidence_policy" in locations or evidence_message:
        hints.append(
            "evidence_policy is only for Web tasks and is exactly "
            "casual_discovery or strict_verification; omit the key, not an empty string, for every other agent."
        )
    if "web_mode" in locations or web_mode_message:
        hints.append(
            "Every Web task requires web_mode: public_lookup for a public topic with no Places dependency, "
            "place_verification for a bound place claim, or place_hours_fallback after a Places details task; "
            "omit it for every other agent."
        )
    if place_mode_message or "place_mode" in locations:
        hints.append(
            "Every Places task requires place_mode: discover for a new recommendation list, "
            "details for one bound place's structured data, or reviews for one bound place's public opinions; "
            "omit it for every other agent."
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
    if "place_reference" in locations:
        hints.append(
            "place_reference never belongs inside a task. For an ordinal place follow-up, "
            "use a Places details or reviews task and put only the user's selection phrase in "
            "top-level place_selection; the server owns and injects the resolved place identity."
        )
    if missing_synthesizer:
        hints.append(
            "Keep the required domain tasks and include exactly one terminal "
            "agent=synthesizer task whose depends_on lists the domain leaves. "
            "A Match action still needs Match then Synthesizer to present the "
            "server-owned confirmation preview, not a claim that the write completed."
        )
    if not hints and "tasks" in locations:
        hints.append(
            "A domain request uses mode=tasks with the required domain agents and exactly "
            "one terminal synthesizer; direct_chat has no tasks or domain claims."
        )
    return " ".join(hints)[:1200]


def _planner_validation_fields(exc: Exception) -> list[str]:
    """Return allowlisted schema locations without retaining rejected values."""
    errors_fn = getattr(exc, "errors", None)
    try:
        errors = errors_fn() if callable(errors_fn) else []
    except Exception:
        errors = []
    fields: list[str] = []
    for error in errors:
        if not isinstance(error, dict):
            continue
        location = ".".join(str(part)[:40] for part in (error.get("loc") or ()))
        message = str(error.get("msg") or "")
        if not location and "relationship date write intents" in message:
            location = "plan.write_intent"
        elif not location and "plan must contain exactly one synthesizer" in message:
            location = "plan.tasks"
        if location and location not in fields:
            fields.append(location)
    return fields[:8]


def _normalize_provider_plan_arguments(
    arguments: Any,
    *,
    resolved_place_reference: bool = False,
) -> tuple[Any, list[str]]:
    """Repair only known agent-scoped provider compatibility drift.

    The returned payload is a deep copy. Canonical Pydantic contracts remain
    strict; unknown agents, malformed field types, and all graph fields are
    untouched. Match does not declare outcome contracts: textual provider
    result labels in that Calendar-only field never grant execution authority.
    """
    normalized = deepcopy(arguments)
    if not isinstance(normalized, dict) or not isinstance(normalized.get("tasks"), list):
        return normalized, []
    repair_codes: list[str] = []
    if (
        "presentation_mode" in normalized
        and normalized.get("presentation_mode") not in {"default", "itinerary"}
    ):
        # Presentation mode is an editorial hint, not execution authority.
        # Provider enum drift must not discard an otherwise valid domain DAG.
        normalized["presentation_mode"] = "default"
        repair_codes.append("unsupported_presentation_mode_defaulted")
    write_intent = normalized.get("write_intent")
    if (
        write_intent in {
            DATE_INVITATION_WRITE_INTENT,
            DATE_COORDINATION_CANCEL_WRITE_INTENT,
        }
        and normalized.get("presentation_mode", "default") != "default"
    ):
        # Date writes always render their confirmation with the locked server
        # contract. An itinerary hint may accompany a Places sibling, but it
        # must not invalidate or authorize the DAG.
        normalized["presentation_mode"] = "default"
        repair_codes.append("date_write_presentation_mode_defaulted")
    if isinstance(write_intent, str) and (
        write_intent.startswith("match.")
        or write_intent in {"start_search", "cancel_search", "accept", "decline", "cancel"}
    ):
        # Match writes are proposed by the Match specialist and still pass the
        # Guard + confirmation boundary. They never belong in PlannerWriteIntent.
        normalized["write_intent"] = "none"
        repair_codes.append("match_write_intent_normalized")
    for task in normalized["tasks"]:
        if not isinstance(task, dict):
            continue
        agent = task.get("agent")
        if not isinstance(agent, str) or agent not in _KNOWN_PLANNER_AGENTS:
            continue
        misplaced_place_reference = task.get("place_reference")
        if (
            resolved_place_reference
            and agent == "places"
            and task.get("place_mode") in {"details", "reviews"}
            and "place_reference" in task
            and (
                misplaced_place_reference is None
                or isinstance(misplaced_place_reference, str)
            )
        ):
            # The Planner may mirror the opaque reference it saw in bounded
            # context into the task. Its value is never authoritative: the
            # Scheduler already resolved the current utterance against the
            # owner/room snapshot and supplies that trusted projection to the
            # Places runtime.
            task.pop("place_reference", None)
            if "resolved_place_task_reference_removed" not in repair_codes:
                repair_codes.append("resolved_place_task_reference_removed")
        # Some providers attach a harmless public-label hint to a task. It is
        # never an authority field and the executor resolves the real target,
        # so remove only this known compatibility drift. Other unknown fields
        # still fail closed under the strict task schema.
        if "counterparty" in task:
            task.pop("counterparty", None)
            if "known_counterparty_removed" not in repair_codes:
                repair_codes.append("known_counterparty_removed")
        intent = task.get("match_intent")
        if agent == "match" and isinstance(intent, dict) and len(intent) == 1:
            key, enabled = next(iter(intent.items()))
            if key in {"status", "counterparty", "start_search", "restart_search", "cancel_search", "accept_proposal", "dismiss_proposal", "clarify"} and enabled is True:
                task["match_intent"] = key
                if "match_intent_object_normalized" not in repair_codes:
                    repair_codes.append("match_intent_object_normalized")
        elif agent != "match" and isinstance(intent, str):
            # A task outside the Match domain cannot acquire Match authority.
            # Dropping provider-invented string labels is therefore a
            # compatibility repair, while malformed object/list shapes still
            # reach strict validation and fail closed.
            task.pop("match_intent", None)
            if "non_match_match_intent_removed" not in repair_codes:
                repair_codes.append("non_match_match_intent_removed")
        empty_placeholder_removed = False
        if task.get("evidence_policy") == "":
            task.pop("evidence_policy", None)
            empty_placeholder_removed = True
        if task.get("web_mode") == "":
            task.pop("web_mode", None)
            empty_placeholder_removed = True
        if task.get("outcome_contract") == "":
            task.pop("outcome_contract", None)
            empty_placeholder_removed = True
        if task.get("run_if") in ("", {}):
            task.pop("run_if", None)
            empty_placeholder_removed = True
        if empty_placeholder_removed and "empty_optional_task_fields_removed" not in repair_codes:
            repair_codes.append("empty_optional_task_fields_removed")
        evidence_policy = task.get("evidence_policy")
        if agent != "web" and isinstance(evidence_policy, str) and evidence_policy in {
            "casual_discovery", "strict_verification",
        }:
            task.pop("evidence_policy", None)
            if "non_web_evidence_policy_removed" not in repair_codes:
                repair_codes.append("non_web_evidence_policy_removed")
        web_mode = task.get("web_mode")
        if agent != "web" and isinstance(web_mode, str) and web_mode in {
            "public_lookup", "place_verification", "place_hours_fallback",
        }:
            task.pop("web_mode", None)
            if "non_web_web_mode_removed" not in repair_codes:
                repair_codes.append("non_web_web_mode_removed")
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
        elif agent != "calendar" and isinstance(task.get("outcome_contract"), str):
            # Only Calendar owns outcome_contract. Removing a string placed on
            # another known agent cannot grant a capability; malformed
            # container values remain untouched for strict validation.
            task.pop("outcome_contract", None)
            code = (
                "match_observation_contract_removed"
                if agent == "match"
                else "non_calendar_outcome_contract_removed"
            )
            if code not in repair_codes:
                repair_codes.append(code)
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
    validation_fields: list[str] | None = None,
    ttft_ms: int = 0,
    tps: float = 0.0,
    model_name: str = "",
) -> None:
    """Keep a bounded, local-debug-only summary for each provider attempt."""
    metrics.attempts.append({
        "attempt": attempt,
        "status": status,
        "stage": (
            "safe_normalization" if status == "repaired"
            else "llm_plan" if status == "ok"
            else "retry"
        ),
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
        "validation_fields": [str(field)[:80] for field in (validation_fields or [])[:8]],
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
            "Use relationship.date_invitation.v1 only when the user explicitly asks Ayue to create or send "
            "a date invitation now. A stated wish/plan to invite someone, a companion mention, or a request "
            "for places/advice alone uses none."
        ),
    )
    presentation_mode: Literal["default", "itinerary"] = "default"
    tasks: list[SubTask] = Field(default_factory=list, max_length=5)
    direct_reply: str | None = Field(default=None, max_length=160)
    direct_messages: list[str] = Field(default_factory=list, max_length=3)
    opportunity: _OpportunityArguments | None = None
    place_selection: str | None = Field(
        default=None,
        description=(
            "Only when the current message continues a presented place list; "
            "copy only the user's selection phrase. Never put place_reference or candidate_ref in a task; "
            "refs are server-owned and are not authoritative Planner input"
        ),
    )

    @model_validator(mode="after")
    def _reject_unjustified_synthesizer_only_output(self) -> "_DecomposeTasksArguments":
        """Keep provider-authored task mode from silently bypassing all domains."""
        for task in self.tasks:
            if task.agent == "places" and task.place_mode is None:
                raise ValueError("place_mode is required for Places tasks")
            if task.agent != "places" and task.place_mode is not None:
                raise ValueError("place_mode is only valid for Places tasks")
            if task.agent == "match" and task.match_intent is None and self.write_intent != DATE_INVITATION_WRITE_INTENT:
                raise ValueError("Match task requires match_intent")
            if task.agent != "match" and task.match_intent is not None:
                raise ValueError("match_intent is only valid for Match tasks")
            if task.agent != "web" and task.web_mode is not None:
                raise ValueError("web_mode is only valid for Web tasks")
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
        # Keep this internal classification out of the compact provider schema;
        # the canonical SubTask contract infers it from the task brief.
        task_schema.get("properties", {}).pop("relationship_intent", None)
        for field_name in ("evidence_policy", "web_mode", "outcome_contract", "run_if", "match_intent", "match_search_request"):
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
        # The system prompt carries the full routing policy.  Keep the
        # provider schema compact so native tool calling remains within the
        # planner's request budget; canonical Pydantic validation is unchanged.
        for field_name in ("relationship_intent", "match_intent", "match_search_request", "evidence_policy", "web_mode", "outcome_contract", "run_if"):
            field_schema = task_schema.get("properties", {}).get(field_name)
            if isinstance(field_schema, dict):
                field_schema.pop("description", None)
                if field_name in {"run_if", "match_search_request"}:
                    for nested in field_schema.get("properties", {}).values():
                        if isinstance(nested, dict):
                            nested.pop("description", None)
    write_intent_schema = schema.get("properties", {}).get("write_intent")
    if isinstance(write_intent_schema, dict):
        write_intent_schema.pop("description", None)
    # Keep the optional provider hint compact.  The full internal Plan contract
    # wraps the phrase in PlaceSelection after provider validation.
    place_selection_schema = schema.get("properties", {}).get("place_selection")
    if isinstance(place_selection_schema, dict):
        schema["properties"]["place_selection"] = {"type": "string"}
    return {
        "type": "function",
        "function": {
            "name": "decompose_tasks",
            "description": "把使用者請求拆解成一張靜態子任務 DAG。",
            "parameters": schema,
        },
    }


_PLANNER_PROMPT_VERSION = "compact_v3_semantic_match_v5"
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
只呼叫 decompose_tasks 一次，不輸出普通文字；遵守 schema。

輸出規則：
- 偏好回想／建議用 profile 補查；使用者未確認的推測不是事實。
- mode=direct_chat 只適用於不需要 App、domain、private、external truth 或 workflow 的聊天；tasks 為空，direct_reply 不超過160字。
- 任一子需求需要 state、產品能力、特定對方聊天內容、行事曆、配對、profile、relationship、places 或外部資料，就用 mode=tasks；不得只答聊天部分或只輸出 synthesizer。
- tasks 最多 4 個 domain + 1 個 synth。
- Relationship 活動→recommend；理由追問→review；名單→lookup。
- task 只填 id、agent、depends_on、task_brief 與 schema 內欄位；Relationship 語意放 task_brief。約會邀請用頂層 write_intent；不填 observation schema；不要使用 type/task_agent。
- write_intent 必填；明確命令阿月現在建／送空白邀請才用 relationship.date_invitation.v1；想約背景、人名／@、找店／建議均用 none；取消才用 relationship.date_coordination_cancel.v1。date write 使用 Relationship+Synthesizer，可加明確唯讀 task；Match 絕不作前置檢查。
- depends_on 只表示下游會消費上游 typed observation、candidate ref 或其他明確 contract；run_if 是不傳遞 observation 的控制條件。獨立查詢放同一層，不為了排序而串接。

Agent ownership：
calendar=本人行程、空檔、建立／修改／取消、共同日期、calendar draft 與 recent mutation 驗證。
places=附近地點、餐廳、景點、地址、地圖、距離，以及 hours／price／rating／walking 等結構化地點資料。
web=外部／近期／公開資訊、活動、新聞、文章、論壇、社群、URL，以及 Places 無法證明的公開主張。
match=搜尋、狀態彙總與多卡片牽線收件匣；Hub 可同時有多張人物、主題與活動卡
relationship=accepted contacts aggregate／@ 對象與公開互動。
profile=本人 profile、memory、近期情境與 assessment start／restart。
product_info=阿月／App 的能力、流程、限制、隱私與 Public／Private 入口邊界。
synthesizer=只根據本回合 verified observations 與 bounded context 組最終回覆。

關鍵 routing：
- 依語意判斷，不使用關鍵字或 regex router。
- web_mode：public_lookup=獨立公開查詢；place_verification=綁定店家查證；place_hours_fallback=Places 營業時間不足才補查。
- 結構化 hours、price、rating、walking 使用 places -> synthesizer；不自動加 web，資訊不足才補查。
- 區域／場館／品牌活動走 web(public_lookup) -> synthesizer；延續問句沿用 recent_messages。
- 店家優惠、特殊菜單、活動、臨時歇業、社群公告使用 places -> web -> synthesizer；只研究 server-issued candidate refs。
- 一般區域半日／一日遊使用 t1=places、terminal t2=synthesizer、presentation_mode=itinerary。
- 新活動整天用 Web、Places、Synth；具體日期依 Calendar policy 唯讀；除非要求保存，排除 recent_messages／recent mutation 已出現的活動。
- 外部探索使用 casual_discovery；明確官方查證或醫療／法律／金融／安全風險使用 strict_verification。
- calendar_draft 的 missing_fields、candidates 補充、修正或選擇用 calendar。`calendar_recent_mutation` 的成功與否只交 calendar 做唯讀驗證，不自行猜測。
- Calendar 寫入依完整語意：明確「幫我安排／幫我排一下／幫我記進行程」才走 calendar -> synthesizer mutation flow；「我明天五點想去健身」不必然授權，幫我排明天五點去健身才是明確 create。
- 「想約小凱吃冰，幫我找店」只建 Places；@ 只綁定人，不授權讀寫。
- Places 必填 place_mode：discover=清單；details=單店；reviews=口碑。discover 才 search_nearby；details/reviews 不重新搜尋 Places；server resolution 綁定，task 不填 place_reference／candidate_ref。
- 簡短肯定語接唯讀地點重試提議時，依語意建立 Places read -> synthesizer；不得當 Calendar confirmation；無提議則 direct_chat／澄清。
- 明確／重做 assessment 用 profile；「更認識我／更了解我／多了解我一點」走 profile -> synthesizer，提出 profile.start_assessment(kind=basic) 確認；不可 direct_chat。正常 product_info -> synthesizer DAG。
- 「怎麼配對／如何配到人」→ product_info；明確「幫我配對／開始找人」→ match。
- 「幫我約人」若找新人走 Match；在活動語境詢問現有聯絡人中誰適合同行，或追問上一個推薦理由，走 Relationship recommend/review。撤回約會邀請走 Relationship date-card cancellation。
- 有 recent_recommendation 且追問上一個建議時判斷 review；活動改變時重新 recommend，不把快照當成新事實。
- 配對→ match -> synthesizer，write_intent=none。match_intent 必填：status/counterparty/start_search/restart_search/cancel_search/clarify。接受／婉拒／撤回邀請只在阿月牽線卡片操作。
- 找新人 start_search；再找一位 restart_search（保留等待邀請）；換卡到 Hub；取消搜尋 cancel_search 只停 queued/running。一次搜尋一個 task。
- 搜尋填 match_search_request：kind=general（適合認識等找人條件）或 activity；topic 填本句活動原文。invitation_evidence 只填本句明確代送邀請的原文，否則空；找伴不等於送邀請。
- 「約會卡可以取消嗎」是 ProductInfo 能力問句，不建立操作；只有「幫我取消／撤回約會卡」等命令才用 write_intent=relationship.date_coordination_cancel.v1。Relationship 依 recent_action_reference 或 date_coordination_summary（零張說明、單張確認、多張指定）提出 cancel；過期／不唯一回 clarification，不查 Match。
- opportunity.signal="social_opening" 只用於間接表達想找人一起參與、尚未要求從既有聯絡人挑選或開始找新人的情況；evidence_span 必須是 current message 的連續原文，confidence >= 0.8。從既有聯絡人挑選用 relationship；找新的人用 match；單純寒暄、孤單或負面情緒使用 signal="none"。
- Web task brief 保留原始命題、地點／日期與 evidence class。活動名稱、場地有直接來源但時間不完整時保留 partial 並標示待確認，不湊數。

"""

_PLANNER_SYSTEM += """

Calendar availability policy (must follow for every concrete date):
- This policy is for advice/discovery without persistence. An explicit Calendar create/update/cancel
  request takes precedence: use a normal mutation task,
  not `calendar.availability.v1`; preflight checks conflicts.
- For a personal outing/date/itinerary on a concrete or resolvable date,
  precheck Calendar even if the user says not to save. Only explicit save/create/update/cancel requests authorize mutation.
- Precheck: `agent=calendar`, `outcome_contract=calendar.availability.v1`,
  brief requests one `calendar.list_my_events` read for the requested window.
- Ordinary prechecks use a control-only edge:
  `run_if={source_task_id:<calendar-id>,required_outcome:"task.finished"}`.
  This means later recommendations wait for Calendar but still proceed when
  the calendar read is busy or unavailable; Synthesizer explains the warning.
- Only explicit conditional wording such as "有事就算了／沒事才繼續／有空才找" may
  use `required_outcome="calendar.no_scheduled_events"`. A busy Calendar result
  then skips all gated downstream reads. Never mark a valid busy answer FAILED.
- Keep Calendar gates in `run_if`, separate from `depends_on`; no raw events in
  downstream inputs. Terminal Synthesizer receives Calendar observations.
- Pure public activity lookups need no Calendar precheck; advice never authorizes Calendar mutations.
- Exact example 「這週六，從目前認識的人挑一位，找新活動，再排附近晚餐，只給建議」:
  c1=calendar(outcome_contract=calendar.availability.v1);
  r1=relationship(run_if c1:task.finished) and w1=web(run_if c1:task.finished)(web_mode=public_lookup) in parallel;
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


def _prompt_json_value(value: Any, *, depth: int = 0) -> Any:
    """Keep optional context JSON-safe without serializing mock/object reprs."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if depth > 5:
        return None
    if isinstance(value, dict):
        return {
            str(key): safe
            for key, item in value.items()
            if (safe := _prompt_json_value(item, depth=depth + 1)) is not None
        }
    if isinstance(value, (list, tuple)):
        return [
            safe for item in value
            if (safe := _prompt_json_value(item, depth=depth + 1)) is not None
        ]
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _prompt_json_value(model_dump(), depth=depth + 1)
        except Exception:
            return None
    return None


def _planner_prompt(turn_ctx: PublicAgentTurnContext) -> str:
    """Build a compact, privacy-safe user/context message for the Planner."""
    payload: dict[str, Any] = {
        "message": _prompt_json_value(turn_ctx.message) or "",
        "clock": _planner_clock(turn_ctx),
    }
    recent_messages = _planner_recent_messages(turn_ctx)
    # Only the owner projector's explicit polarity protocol is admitted;
    # untyped legacy memory strings are not routing context.
    preferences = [value for value in turn_ctx.relevant_memories
                   if isinstance(value, str) and value.startswith(("喜歡：", "不喜歡：", "避免：", "需要："))][:8]
    if preferences:
        payload["user_preferences"] = [_prompt_json_value(value[:80]) for value in preferences]
    if recent_messages:
        payload["recent_messages"] = recent_messages
    if turn_ctx.conversation_continuity is not None:
        payload["conversation_continuity"] = turn_ctx.conversation_continuity.model_dump(
            mode="json", exclude_none=True,
        )

    active = turn_ctx.active_proposal
    if isinstance(active, dict):
        active_projection = {
            key: active[key]
            for key in ("status", "stage", "counterparty", "user_can_decide", "allowed_actions", "created_at", "source")
            if active.get(key) not in (None, "")
        }
        if active_projection:
            payload["active_proposal"] = active_projection

    search = turn_ctx.match_search
    if isinstance(search, dict):
        search_projection = {
            key: search[key]
            for key in (
                "status", "cancellable", "reason_code", "active_proposal_count",
                "pending_action_count", "waiting_other_count",
            )
            if search.get(key) not in (None, "")
        }
        if search_projection:
            payload["match_search"] = search_projection

    event_active = turn_ctx.active_event_invitation
    if isinstance(event_active, dict):
        event_projection = {
            key: event_active[key]
            for key in ("status", "event_title", "user_can_decide")
            if event_active.get(key) not in (None, "")
        }
        if event_projection:
            payload["active_event_invitation"] = event_projection

    optional_fields = (
        "focused_match",
        "calendar_draft",
        "calendar_recent_reference",
        "calendar_recent_mutation",
        "mentioned_contacts",
        "recent_contact_reference",
        "recent_action_reference",
        "date_coordination_summary",
        "recent_recommendation",
        "recent_place_candidates",
        "recent_place_reference",
        "place_reference_resolution",
        "place_followup",
    )
    for field_name in optional_fields:
        value = getattr(turn_ctx, field_name, None)
        safe_value = _prompt_json_value(value)
        if safe_value not in (None, "", [], {}):
            payload[field_name] = safe_value

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
_MIXED_DATE_WRITE_SYNTHESIZER_BRIEF = (
    "Compose every verified read-only observation naturally, then preserve the server-owned "
    "date-card confirmation preview or verified write result exactly. Do not claim the write completed."
)
_DATE_COORDINATION_CANCEL_RELATIONSHIP_BRIEF = (
    "Propose relationship.cancel_date_coordination exactly once for an explicit date-card "
    "cancellation request. Choose only a target_source grounded by the current message or "
    "the prompt-safe recent_action_reference; use summary_singleton when its count is one; "
    "never provide IDs, status, or revision."
)
_DATE_COORDINATION_CANCEL_SYNTHESIZER_BRIEF = (
    "Present only the server-owned date-card cancellation confirmation or verified result; "
    "do not invent a target, status, or calendar effect."
)


def _canonicalize_write_intent_briefs(plan: Plan) -> Plan:
    """Replace provider prose with bounded server-owned briefs for typed writes."""
    if plan.write_intent not in {
        DATE_INVITATION_WRITE_INTENT,
        DATE_COORDINATION_CANCEL_WRITE_INTENT,
    }:
        return plan
    relationship_brief = (
        _DATE_COORDINATION_CANCEL_RELATIONSHIP_BRIEF
        if plan.write_intent == DATE_COORDINATION_CANCEL_WRITE_INTENT
        else _DATE_INVITATION_RELATIONSHIP_BRIEF
    )
    synthesizer_brief = (
        _DATE_COORDINATION_CANCEL_SYNTHESIZER_BRIEF
        if plan.write_intent == DATE_COORDINATION_CANCEL_WRITE_INTENT
        else _DATE_INVITATION_SYNTHESIZER_BRIEF
    )
    if sum(task.agent != "synthesizer" for task in plan.tasks) > 1:
        synthesizer_brief = _MIXED_DATE_WRITE_SYNTHESIZER_BRIEF
    tasks = [
        task.model_copy(update={"task_brief": relationship_brief})
        if task.agent == "relationship"
        else task.model_copy(update={"task_brief": synthesizer_brief})
        if task.agent == "synthesizer"
        else task
        for task in plan.tasks
    ]
    return plan.model_copy(update={"tasks": tasks})


def _explicit_match_request_intent(message: Any) -> str | None:
    """Legacy inspection helper kept for callers during the prompt rollout.

    It is deliberately not used to mutate a Planner result. Exact phrases are
    retained for old diagnostics/tests; all live routing remains provider
    semantic output plus deterministic server validation.
    """
    compact = re.sub(r"\s+", "", str(message or "")).casefold()
    return {
        "我想配對": "start_search",
        "我要配對": "start_search",
        "幫我配對": "start_search",
        "幫我找人": "start_search",
        "開始配對": "start_search",
        "開始找人": "start_search",
        "我想認識其他人": "start_search",
        "重新配對": "restart_search",
        "重新找人": "restart_search",
        "再找一位": "restart_search",
    }.get(compact)


def _planner_context_conflict(
    plan: Plan,
    turn_ctx: PublicAgentTurnContext,
) -> str:
    """Reject a domain plan that contradicts server-owned active context."""
    followup = turn_ctx.place_followup
    resolved = followup.get("resolved_place") if isinstance(followup, dict) else None
    if not isinstance(resolved, dict):
        resolution = turn_ctx.place_reference_resolution
        resolved = resolution if isinstance(resolution, dict) and resolution.get("status") == "resolved" else None
    if not isinstance(resolved, dict) or not str(resolved.get("label") or "").strip():
        return ""
    has_match_task = any(task.agent == "match" for task in plan.tasks)
    has_place_task = any(task.agent == "places" for task in plan.tasks)
    explicit_match_reference = bool(re.search(
        r"配對|媒合|牽線|對方|人選",
        str(turn_ctx.message or ""),
    ))
    if has_match_task and not explicit_match_reference:
        return (
            "The selected referent is the resolved place from place_followup. "
            "Do not use Match; route place details through Places or Web."
        )
    if getattr(plan, "place_selection", None) is not None and not has_place_task:
        return (
            "The current message explicitly selects a server-resolved place. Include a Places "
            "details task for that place while preserving any independent Relationship request."
        )
    if plan.mode == "direct_chat" and not has_place_task:
        return (
            "The current message has a server-resolved place referent. "
            "Use a Places task with details or reviews rather than direct_chat."
        )
    return ""


def plan_turn(turn_ctx: PublicAgentTurnContext) -> tuple[Plan | None, PlannerMetrics]:
    """Call LLM (function calling) to decompose the request into a static Plan.

    Returns (plan_or_none, metrics). The tool-call arguments ARE the Plan.
    """
    metrics = PlannerMetrics()
    metrics.prompt_version = _PLANNER_PROMPT_VERSION
    started = time.perf_counter()
    deadline = time.monotonic() + OLLAMA_REQUEST_TIMEOUT_SECONDS
    prompt = _planner_prompt(turn_ctx)
    metrics.prompt_raw = f"SYSTEM:\n{_PLANNER_SYSTEM}\nUSER:\n{prompt}"
    metrics.tools_raw = [_decompose_tool_schema()]
    attempt_prompt = prompt

    for attempt in range(1, _PLANNER_MAX_ATTEMPTS + 1):
        if time.monotonic() >= deadline:
            metrics.failure_code = "planner_deadline_exceeded"
            break
        validation_hint = ""
        attempt_started = time.perf_counter()
        metrics.llm_call_count += 1
        if attempt > 1:
            metrics.retry_count += 1
        try:
            result = generate_chat_completion_with_tools(
                attempt_prompt, metrics.tools_raw, temperature=0,
                system_prompt=_PLANNER_SYSTEM, prefer_fast_model=True,
                deadline_monotonic=deadline,
            )
            if time.monotonic() >= deadline:
                raise TimeoutError("Planner deadline exhausted")
        except Exception as exc:
            duration_ms = round((time.perf_counter() - attempt_started) * 1000)
            metrics.duration_ms += duration_ms
            metrics.error = str(exc)
            failure_code, retryable = _planner_provider_failure(exc)
            metrics.failure_code = failure_code
            _record_planner_attempt(
                metrics, attempt=attempt, status="provider_error",
                failure_code=metrics.failure_code, duration_ms=duration_ms,
                error=metrics.error,
            )
            if not retryable or attempt >= _PLANNER_MAX_ATTEMPTS:
                break
            metrics.retry_reason = failure_code
            attempt_prompt = _planner_retry_prompt(prompt, failure_code)
            continue
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
                resolved_place_reference = bool(
                    isinstance(turn_ctx.place_reference_resolution, dict)
                    and turn_ctx.place_reference_resolution.get("status") == "resolved"
                )
                normalized_arguments, repair_codes = _normalize_provider_plan_arguments(
                    arguments,
                    resolved_place_reference=resolved_place_reference,
                )
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
                        validation_fields=_planner_validation_fields(exc),
                        ttft_ms=int(getattr(result, "ttft_ms", 0) or 0),
                        tps=round(float(getattr(result, "tps", 0) or 0), 3),
                        model_name=str(getattr(result, "model_name", "") or ""),
                    )
                else:
                    opportunity = _opportunity_from_arguments(validated)
                    selection_text = str(validated.place_selection or "").strip()[:160]
                    place_selection = (
                        PlaceSelection(selection_text=selection_text)
                        if selection_text else None
                    )
                    try:
                        plan = Plan(
                            mode=validated.mode,
                            write_intent=validated.write_intent,
                            presentation_mode=validated.presentation_mode,
                            tasks=validated.tasks,
                            direct_reply=validated.direct_reply,
                            direct_messages=validated.direct_messages,
                            opportunity=opportunity,
                            place_selection=place_selection,
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
                                    place_selection=place_selection,
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
                                validation_fields=_planner_validation_fields(repair_exc),
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
                        validation_hint = _planner_context_conflict(plan, turn_ctx)
                        if validation_hint:
                            failure_code = "invalid_arguments"
                            metrics.error = "planner_context_conflict"
                            _record_planner_attempt(
                                metrics, attempt=attempt, status="protocol_error",
                                failure_code=failure_code,
                                raw_content=metrics.raw_content,
                                tool_calls=metrics.tool_calls_raw,
                                input_tokens=input_tokens,
                                output_tokens=output_tokens,
                                duration_ms=duration_ms,
                                error=metrics.error,
                                validation_fields=["place_followup"],
                                repair_codes=repair_codes,
                                ttft_ms=int(getattr(result, "ttft_ms", 0) or 0),
                                tps=round(float(getattr(result, "tps", 0) or 0), 3),
                                model_name=str(getattr(result, "model_name", "") or ""),
                            )
                            plan = None
                        else:
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
        metrics.retry_reason = failure_code
        attempt_prompt = _planner_retry_prompt(
            prompt,
            failure_code,
            validation_hint,
        )

    metrics.duration_ms = max(metrics.duration_ms, round((time.perf_counter() - started) * 1000))
    return None, metrics
