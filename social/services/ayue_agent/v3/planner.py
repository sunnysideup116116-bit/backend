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
from datetime import datetime
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from config import OLLAMA_REQUEST_TIMEOUT_SECONDS
from services.ai_service import generate_chat_completion_with_tools
from services.ayue_agent.contracts import PublicAgentTurnContext
from services.ayue_agent.time_context import resolve_temporal_candidates
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
    ResolvedTemporalTarget,
    RunCondition,
    SubTask,
    TemporalClarification,
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
    "temporal_candidate_used_as_interpretation",
    "temporal_candidate_defaulted_from_interpretation",
    "non_temporal_task_ids_removed",
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
    exception_text = str(exc)
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
    availability_message = any(
        "availability_policy" in message
        for message in messages
    ) or "availability_policy" in exception_text
    temporal_message = any(
        "temporal" in message
        for message in messages
    ) or "temporal" in exception_text
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
    if "availability_policy" in locations or availability_message:
        hints.append(
            "availability_policy is only for a Calendar availability read and must be one object with exactly "
            "calendar_task_id, controlled_task_ids, mode, and optional evidence_span. mode is "
            "fit_around_events or abort_if_any_event. Omit the whole field for Calendar mutations. "
            "Do not output type or policy keys."
        )
    if "temporal_bindings" in locations or temporal_message:
        hints.append(
            "temporal_bindings is an array whose items use exactly source_text, task_ids, interpretation, "
            "and candidate_id. Select candidate_id from clock.temporal_candidates; never copy date or relation "
            "and never use source_task_id or target_task_id."
        )
    if "run_if" in locations or "required_outcome" in locations or run_if_message:
        hints.append(
            "run_if is only a control edge with source_task_id and required_outcome; "
            "required_outcome is task.finished or an allowlisted calendar outcome; omit run_if rather than sending {}."
        )
    if "place_reference" in locations:
        hints.append(
            "place_reference never belongs inside a task. For an ordinal place follow-up, "
            "resolve the visible chat order into the concrete public place name and known area, "
            "then put those facts in the Places details/reviews task_brief."
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
        elif not location and "availability_policy" in message:
            location = "availability_policy"
        elif not location and "temporal" in message:
            location = "temporal_bindings"
        if location and location not in fields:
            fields.append(location)
    if not fields:
        message = str(exc)
        if "availability_policy" in message:
            fields.append("availability_policy")
        if "temporal" in message:
            fields.append("temporal_bindings")
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
        # A task-scoped value must never elevate the top-level write intent.
        # Remove this known provider drift and let the next Planner attempt
        # supply an explicit top-level authorization when one was intended.
        if (
            agent == "relationship"
            and task.get("outcome_contract") == DATE_INVITATION_WRITE_INTENT
        ):
            task.pop("outcome_contract", None)
            if "misplaced_date_invitation_intent_removed" not in repair_codes:
                repair_codes.append("misplaced_date_invitation_intent_removed")
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
    task_agents = {
        str(task.get("id") or ""): str(task.get("agent") or "")
        for task in normalized["tasks"]
        if isinstance(task, dict) and str(task.get("id") or "")
    }
    temporal_bindings = normalized.get("temporal_bindings")
    if isinstance(temporal_bindings, list):
        for binding in temporal_bindings:
            if not isinstance(binding, dict):
                continue
            interpretation = binding.get("interpretation")
            candidate_id = binding.get("candidate_id")
            if interpretation == "next_occurrence" and candidate_id in {None, "next_occurrence"}:
                # This is a closed enum-position swap, not date interpretation:
                # next_occurrence is itself the server candidate handle and
                # necessarily denotes the future-planning choice.
                binding["candidate_id"] = "next_occurrence"
                binding["interpretation"] = "future_planning"
                if "temporal_candidate_used_as_interpretation" not in repair_codes:
                    repair_codes.append("temporal_candidate_used_as_interpretation")
            elif interpretation == "stated_period" and candidate_id is None:
                binding["candidate_id"] = "stated_period"
                if "temporal_candidate_defaulted_from_interpretation" not in repair_codes:
                    repair_codes.append("temporal_candidate_defaulted_from_interpretation")
            task_ids = binding.get("task_ids")
            if isinstance(task_ids, list):
                filtered_task_ids = [
                    task_id for task_id in task_ids
                    if not isinstance(task_id, str)
                    or task_agents.get(task_id) is None
                    or task_agents.get(task_id) in {"calendar", "web"}
                ]
                if filtered_task_ids != task_ids:
                    # ResolvedTemporalTarget is intentionally unavailable to
                    # Places/other agents. Dropping only those known task IDs
                    # cannot grant authority or alter the chosen date.
                    binding["task_ids"] = filtered_task_ids
                    if "non_temporal_task_ids_removed" not in repair_codes:
                        repair_codes.append("non_temporal_task_ids_removed")
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


class _AvailabilityPolicyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar_task_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    controlled_task_ids: list[str] = Field(min_length=1, max_length=4)
    mode: Literal["fit_around_events", "abort_if_any_event"]
    evidence_span: str = Field(default="", max_length=120)


class _TemporalBindingArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_text: str = Field(min_length=1, max_length=40)
    task_ids: list[str] = Field(default_factory=list, max_length=4)
    interpretation: Literal["future_planning", "stated_period", "needs_clarification"]
    candidate_id: Literal["stated_period", "next_occurrence"] | None = None


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
    availability_policy: _AvailabilityPolicyArguments | None = None
    temporal_bindings: list[_TemporalBindingArguments] = Field(default_factory=list, max_length=4)
    place_selection: str | None = Field(
        default=None,
        description=(
            "Only when the current message continues a presented place list; "
            "copy only the user's selection phrase. Never put place_reference or candidate_ref in a task; "
            "refs are server-owned and are not authoritative Planner input"
        ),
    )

    @model_validator(mode="before")
    @classmethod
    def _require_provider_relationship_intent(cls, value: Any) -> Any:
        """Require public Planner output to classify Relationship explicitly."""
        if not isinstance(value, dict):
            return value
        tasks = value.get("tasks")
        if not isinstance(tasks, list):
            return value
        for task in tasks:
            if (
                isinstance(task, dict)
                and task.get("agent") == "relationship"
                and task.get("relationship_intent") not in {"lookup", "recommend", "review"}
            ):
                raise ValueError(
                    "Relationship tasks require relationship_intent=lookup, recommend, or review"
                )
        return value

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
        policy = self.availability_policy
        legacy_consumed_availability_ids = {
            task.run_if.source_task_id
            for task in self.tasks
            if task.run_if is not None
            and task.run_if.source_task_id in availability_ids
        }
        has_downstream_domain = any(
            task.agent not in {"calendar", "synthesizer"}
            for task in self.tasks
        )
        if availability_ids and policy is None and (
            legacy_consumed_availability_ids or has_downstream_domain
        ):
            raise ValueError("calendar availability tasks require availability_policy")
        if policy is not None and not availability_ids:
            raise ValueError("availability_policy requires a Calendar availability task")
        if policy is not None:
            if policy.calendar_task_id not in availability_ids:
                raise ValueError("availability_policy requires a Calendar availability task")
            if len(set(policy.controlled_task_ids)) != len(policy.controlled_task_ids):
                raise ValueError("availability_policy has duplicate controlled tasks")
            ids = {task.id for task in self.tasks}
            if any(task_id not in ids for task_id in policy.controlled_task_ids):
                raise ValueError("availability_policy references an unknown controlled task")
            if policy.calendar_task_id in policy.controlled_task_ids:
                raise ValueError("availability_policy cannot control its Calendar source")
        consumed_availability_ids = set(legacy_consumed_availability_ids)
        if policy is not None:
            consumed_availability_ids.add(policy.calendar_task_id)
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
        # These are server-owned after Planner validation. The provider emits
        # availability_policy/temporal_bindings at the top level instead.
        task_schema.get("properties", {}).pop("resolved_time_target", None)
        task_schema.get("properties", {}).pop("run_if", None)
        for field_name in ("evidence_policy", "web_mode", "outcome_contract", "match_intent", "match_search_request"):
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
        for field_name in ("relationship_intent", "match_intent", "match_search_request", "evidence_policy", "web_mode", "outcome_contract"):
            field_schema = task_schema.get("properties", {}).get(field_name)
            if isinstance(field_schema, dict):
                field_schema.pop("description", None)
                if field_name == "match_search_request":
                    for nested in field_schema.get("properties", {}).values():
                        if isinstance(nested, dict):
                            nested.pop("description", None)
    write_intent_schema = schema.get("properties", {}).get("write_intent")
    if isinstance(write_intent_schema, dict):
        write_intent_schema.pop("description", None)
    # Keep the optional provider hint compact.  The full internal Plan contract
    # wraps the phrase in PlaceSelection after provider validation.
    # Legacy place_selection remains on the internal compatibility model, but
    # is no longer part of the public Planner tool contract. The Planner must
    # resolve a visible ordinal/pronoun into each task's concrete task_brief.
    schema.get("properties", {}).pop("place_selection", None)
    availability_schema = schema.get("properties", {}).get("availability_policy")
    if isinstance(availability_schema, dict):
        options = [
            option for option in availability_schema.get("anyOf", [])
            if isinstance(option, dict) and option.get("type") == "object"
        ]
        if len(options) == 1:
            schema["properties"]["availability_policy"] = {
                **options[0],
                "description": (
                    "Availability: {calendar_task_id,controlled_task_ids,mode,evidence_span}; "
                    "omit for mutations."
                ),
            }
    temporal_schema = schema.get("properties", {}).get("temporal_bindings")
    if isinstance(temporal_schema, dict):
        temporal_schema["description"] = (
            "Items: {source_text,task_ids,interpretation,candidate_id}; choose candidate_id, not date/relation."
        )
        item_schema = temporal_schema.get("items")
        candidate_schema = (
            item_schema.get("properties", {}).get("candidate_id")
            if isinstance(item_schema, dict) else None
        )
        if isinstance(candidate_schema, dict):
            options = [
                option for option in candidate_schema.get("anyOf", [])
                if isinstance(option, dict) and option.get("type") == "string"
            ]
            if len(options) == 1:
                item_schema["properties"]["candidate_id"] = options[0]
    return {
        "type": "function",
        "function": {
            "name": "decompose_tasks",
            "description": "把使用者請求拆解成一張靜態子任務 DAG。",
            "parameters": schema,
        },
    }


_PLANNER_PROMPT_VERSION = "compact_v3_semantic_match_v5"
_PLANNER_MAX_RECENT_MESSAGES = 12
_PLANNER_MAX_RECENT_CHARS = 6000
_PLANNER_MAX_SYSTEM_CHARS = 6500
_PLANNER_MAX_SCHEMA_CHARS = 4000
_PLANNER_MAX_PROVIDER_CHARS = 10500

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
- Relationship 活動同行者／誰適合→recommend；質疑或要求重評上一個建議→review；名單或總數→lookup。每個 Relationship task 必須明填 relationship_intent，不依 task_brief 關鍵字猜。
- task 只填 id、agent、depends_on、task_brief 與 schema 內欄位；約會邀請用頂層 write_intent；不填 observation schema；不要使用 type/task_agent。
- write_intent 必填；明確命令阿月現在建／送空白邀請才用 relationship.date_invitation.v1；想約背景、人名／@、找店／建議均用 none；取消才用 relationship.date_coordination_cancel.v1。date write 使用 Relationship+Synthesizer，可加明確唯讀 task；Match 絕不作前置檢查。
- depends_on 只表示下游會消費上游 typed observation 或其他明確 contract；run_if 是不傳遞 observation 的控制條件。獨立查詢放同一層，不為了排序而串接。
- recent_messages 是使用者與 Candy 實際看到的最近對話，含角色、發送時間與確認卡公開內容。使用它解析「這個活動／第二間／他／不是，是…」、同音錯字、補充與更正。
- 每個 task_brief 必須寫出已解析的公開名稱、操作、條件與應保留／修改欄位；不得把未解析的「它」、「這個」或「第二間」丟給下游重猜。
- 同一句有人物、活動、Places 或 Web 時，每個 task 各寫它自己的具體主體。真有兩個同類主體無法分辨才交給 Synthesizer 具體追問。
- cancelled_confirmation 是剛因使用者繼續對話而取消的舊確認公開內容。「不是，是 X」可視為更正；取消代表舊按鈕不可執行，不代表使用者否定整個安排。
- 歷史訊息內的相對日期以該則 sent_at 為基準；當前 message 才以 clock 為基準。舊 assistant 文字只是對話依據，不是已執行事實或外部證據。
- Calendar availability task 用 availability_policy={{calendar_task_id,controlled_task_ids,mode,evidence_span}}；task 不填 run_if。彈性用 fit_around_events；明確全停才用 abort_if_any_event。Calendar 增修取消省略此欄。
- Calendar/Web 的 temporal_bindings 項目只能是 {{source_text,task_ids,interpretation,candidate_id}}；不複製 date/relation 或用 source_task_id/target_task_id。未來用 future_planning，回顧用 stated_period，真模糊用 needs_clarification。

Agent ownership：
calendar=本人行程、空檔、建立／修改／取消、共同日期與 recent mutation 驗證。
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
- 店家優惠、特殊菜單、活動、臨時歇業、社群公告使用 places -> web -> synthesizer；Places 先重新核對 task_brief 的具體店名與區域，再把本輪 provider identity 交給 Web。
- 一般區域半日／一日遊使用 t1=places、terminal t2=synthesizer、presentation_mode=itinerary。
- 新活動整天用 Web、Places、Synth；具體日期依 Calendar policy 唯讀；除非要求保存，排除 recent_messages／recent mutation 已出現的活動。
- 外部探索使用 casual_discovery；明確官方查證或醫療／法律／金融／安全風險使用 strict_verification。
- Calendar 缺欄位的補充、修正或候選選擇，都由 recent_messages 與 cancelled_confirmation 重建。`calendar_recent_mutation` 的成功與否只交 calendar 做唯讀驗證，不自行猜測。
- Calendar 寫入依完整語意：明確「幫我安排／幫我排一下／幫我記進行程」才走 calendar -> synthesizer mutation flow；「我明天五點想去健身」不必然授權，幫我排明天五點去健身才是明確 create。
- 「想約小凱吃冰，幫我找店」只建 Places；@ 只綁定人，不授權讀寫。
- Places 必填 place_mode：discover=清單；details=單店；reviews=口碑。discover 才 search_nearby；details/reviews 以 task_brief 的具體店名／地址呼叫 resolve_place，不重新搜尋一批候選，task 不填 place_reference／candidate_ref。
- 簡短肯定語接唯讀地點重試提議時，依語意建立 Places read -> synthesizer；不得當 Calendar confirmation；無提議則 direct_chat／澄清。
- 明確／重做 assessment 用 profile；「更認識我／更了解我／多了解我一點」走 profile -> synthesizer，提出 profile.start_assessment(kind=basic) 確認；不可 direct_chat。正常 product_info -> synthesizer DAG。
- 「怎麼配對／如何配到人」→ product_info；明確「幫我配對／開始找人」→ match。
- 「幫我約人」若找新人走 Match；在活動語境詢問現有聯絡人中誰適合同行，或追問上一個推薦理由，走 Relationship recommend/review。撤回約會邀請走 Relationship date-card cancellation。
- 問「你剛推薦誰」可依 recent_messages 回想；重新比較、問誰適合或活動改變時建立 Relationship recommend/review task 並讀取目前聯絡人。
- 配對→ match -> synthesizer，write_intent=none。match_intent 必填：status/counterparty/start_search/restart_search/cancel_search/clarify。接受／婉拒／撤回邀請只在阿月牽線卡片操作。
- 找新人 start_search；再找一位 restart_search（保留等待邀請）；換卡到 Hub；取消搜尋 cancel_search 只停 queued/running。一次搜尋一個 task。
- 搜尋填 match_search_request：kind=general（適合認識等找人條件）或 activity；topic 填本句活動原文。invitation_evidence 只填本句明確代送邀請的原文，否則空；找伴不等於送邀請。
- 「約會卡可以取消嗎」是 ProductInfo 能力問句，不建立操作；只有「幫我取消／撤回約會卡」等命令才用 write_intent=relationship.date_coordination_cancel.v1。Relationship 依已解析的人名或 date_coordination_summary（零張說明、單張確認、多張指定）提出 cancel；過期／不唯一回 clarification，不查 Match。
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
- Flexible availability wording uses availability_policy=fit_around_events so
  recommendations continue and fit around verified intervals. Only an explicit
  abort-on-any-event instruction uses abort_if_any_event. Never mark a valid
  condition stop as a failed Web execution.
- Keep Calendar gates in `run_if`, separate from `depends_on`; no raw events in
  downstream inputs. Terminal Synthesizer receives Calendar observations.
- Pure public activity lookups need no Calendar precheck; advice never authorizes Calendar mutations.
- Exact example 「這週六，從目前認識的人挑一位，找新活動，再排附近晚餐，只給建議」:
  c1=calendar(outcome_contract=calendar.availability.v1);
  availability_policy=fit_around_events controls r1/w1; r1=relationship and
  w1=web(web_mode=public_lookup) in parallel;
  p1=places(depends_on=[w1]); s1=synthesizer(depends_on=[c1,r1,p1]). Places uses
  the typed Web activity venue.
- With explicit 「有任何行程就不要繼續」, use abort_if_any_event and
  control r1/w1. Never replace this graph with direct_chat or synth-only.
"""

def _planner_recent_messages(turn_ctx: PublicAgentTurnContext) -> list[dict[str, Any]]:
    """Keep the Planner history bounded and remove the saved current message duplicate."""
    current = str(turn_ctx.message or "").strip()
    source = list(turn_ctx.recent_messages or [])
    if source and isinstance(source[-1], dict):
        last_role = str(source[-1].get("role") or "")
        last_content = str(source[-1].get("content") or "").strip()
        if last_role == "user" and last_content == current:
            source.pop()

    selected: list[dict[str, Any]] = []
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
        selected.append({
            "role": role,
            "content": clipped,
            "sent_at": str(item.get("sent_at") or "unknown")[:40],
            "timezone": str(item.get("timezone") or turn_ctx.clock.timezone)[:64],
            "truncated": bool(item.get("truncated")) or len(clipped) < len(content),
        })
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
    try:
        local_now = datetime.fromisoformat(turn_ctx.clock.local_iso)
        candidates = resolve_temporal_candidates(turn_ctx.message, local_now)
    except (TypeError, ValueError):
        candidates = []
    if candidates:
        projected["temporal_candidates"] = candidates
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
    cancelled_confirmation = getattr(turn_ctx, "_cancelled_confirmation_context", None)
    if isinstance(cancelled_confirmation, dict):
        payload["cancelled_confirmation"] = _prompt_json_value(
            cancelled_confirmation,
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
        "calendar_recent_mutation",
        "mentioned_contacts",
        "date_coordination_summary",
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


def _apply_planner_semantic_contracts(
    validated: _DecomposeTasksArguments,
    turn_ctx: PublicAgentTurnContext,
) -> tuple[list[SubTask], TemporalClarification | None]:
    """Validate provider semantics and attach only server-derived controls."""
    tasks = [task.model_copy(deep=True) for task in validated.tasks]
    task_by_id = {task.id: task for task in tasks}

    policy = validated.availability_policy
    if policy is not None:
        evidence = str(policy.evidence_span or "").strip()
        if policy.mode == "abort_if_any_event" and (
            not evidence or evidence not in str(turn_ctx.message or "")
        ):
            raise ValueError("abort_if_any_event requires an exact evidence_span")
        required_outcome = (
            "calendar.no_scheduled_events"
            if policy.mode == "abort_if_any_event"
            else "task.finished"
        )
        for task_id in policy.controlled_task_ids:
            task = task_by_id[task_id]
            task.run_if = RunCondition(
                source_task_id=policy.calendar_task_id,
                required_outcome=required_outcome,
            )

    try:
        local_now = datetime.fromisoformat(turn_ctx.clock.local_iso)
    except (TypeError, ValueError) as exc:
        raise ValueError("turn clock local_iso is invalid") from exc
    candidate_sets = {
        str(item.get("source_text") or ""): item
        for item in resolve_temporal_candidates(turn_ctx.message, local_now)
        if isinstance(item, dict) and str(item.get("source_text") or "")
    }
    ambiguous_sources = {
        source
        for source, item in candidate_sets.items()
        if len(item.get("candidates") or []) > 1
    }
    bindings_by_source: dict[str, _TemporalBindingArguments] = {}
    for binding in validated.temporal_bindings:
        if binding.source_text in bindings_by_source:
            raise ValueError("temporal_bindings contains a duplicate source_text")
        if binding.source_text not in str(turn_ctx.message or ""):
            raise ValueError("temporal binding source_text is not in the current message")
        if binding.source_text not in candidate_sets:
            raise ValueError("temporal binding source_text has no server candidate set")
        bindings_by_source[binding.source_text] = binding

    if ambiguous_sources and validated.mode == "tasks":
        missing = ambiguous_sources - set(bindings_by_source)
        if missing and any(task.agent in {"calendar", "web"} for task in tasks):
            raise ValueError("ambiguous task dates require temporal_bindings")

    for source_text, binding in bindings_by_source.items():
        candidate_rows = [
            row for row in (candidate_sets[source_text].get("candidates") or [])
            if isinstance(row, dict)
        ]
        if binding.interpretation == "needs_clarification":
            if binding.candidate_id is not None:
                raise ValueError("needs_clarification cannot select a date candidate")
            return [SubTask(
                id="temporal_clarification",
                agent="synthesizer",
                depends_on=[],
                task_brief="Ask one natural question that disambiguates the server-provided date candidates.",
            )], TemporalClarification(
                source_text=source_text,
                candidates=[
                    {
                        "id": str(row.get("id") or ""),
                        "date": str(row.get("date") or ""),
                        "relation": str(row.get("relation") or ""),
                    }
                    for row in candidate_rows
                ],
            )
        if binding.candidate_id is None or not binding.task_ids:
            raise ValueError("resolved temporal binding requires a candidate and task_ids")
        selected = next(
            (row for row in candidate_rows if row.get("id") == binding.candidate_id),
            None,
        )
        if selected is None:
            raise ValueError("temporal binding selected an unknown server candidate")
        if (
            binding.interpretation == "future_planning"
            and str(selected.get("relation") or "") == "past"
        ):
            raise ValueError("future_planning cannot select a past date")
        if binding.interpretation == "stated_period" and binding.candidate_id != "stated_period":
            raise ValueError("stated_period must select the stated_period candidate")
        if len(set(binding.task_ids)) != len(binding.task_ids):
            raise ValueError("temporal binding has duplicate task_ids")
        target = ResolvedTemporalTarget(
            source_text=source_text,
            date=str(selected.get("date") or ""),
            timezone=turn_ctx.clock.timezone,
        )
        for task_id in binding.task_ids:
            task = task_by_id.get(task_id)
            if task is None or task.agent not in {"calendar", "web"}:
                raise ValueError("temporal binding task must be Calendar or Web")
            task.resolved_time_target = target

    return tasks, None


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
                model_owner="planner",
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
                normalized_arguments, repair_codes = _normalize_provider_plan_arguments(
                    arguments,
                    resolved_place_reference=False,
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
                    place_selection = None
                    semantic_tasks: list[SubTask] | None = None
                    temporal_clarification: TemporalClarification | None = None
                    try:
                        semantic_tasks, temporal_clarification = _apply_planner_semantic_contracts(
                            validated, turn_ctx,
                        )
                        if temporal_clarification is not None:
                            plan = Plan(
                                mode="tasks",
                                tasks=semantic_tasks,
                                temporal_clarification=temporal_clarification,
                            )
                        else:
                            plan = Plan(
                                mode=validated.mode,
                                write_intent=validated.write_intent,
                                presentation_mode=validated.presentation_mode,
                                tasks=semantic_tasks,
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
                            if semantic_tasks:
                                plan = Plan(
                                    mode="tasks",
                                    write_intent=validated.write_intent,
                                    presentation_mode=validated.presentation_mode,
                                    tasks=semantic_tasks,
                                    opportunity=opportunity,
                                    place_selection=place_selection,
                                    temporal_clarification=temporal_clarification,
                                )
                            else:
                                raise exc
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
                        validation_hint = ""
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
