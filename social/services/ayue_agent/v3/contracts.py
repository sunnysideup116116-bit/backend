"""Typed contracts for the V3 sub-agent runtime."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


# Forbidden ID/revision fields a sub-agent must never fill.
FORBIDDEN_ARG_FIELDS = frozenset({
    "user_id", "match_id", "event_id", "coordination_id", "calendar_event_id",
    "other_id", "revision", "expected_revision", "expected_status",
    "expected_coordination_revision", "expected_event_revision",
})

VALID_AGENTS = frozenset({
    "calendar", "places", "web", "match", "relationship", "profile", "product_info", "synthesizer",
})
DATE_INVITATION_WRITE_INTENT = "relationship.date_invitation.v1"
DATE_COORDINATION_CANCEL_WRITE_INTENT = "relationship.date_coordination_cancel.v1"
PlannerWriteIntent = Literal[
    "none",
    "relationship.date_invitation.v1",
    "relationship.date_coordination_cancel.v1",
]
MatchIntent = Literal[
    "status", "counterparty", "start_search", "restart_search", "cancel_search",
    "accept_proposal", "dismiss_proposal", "clarify",
]
PlaceMode = Literal["discover", "details", "reviews"]
WebMode = Literal["public_lookup", "place_verification", "place_hours_fallback"]


class SubTaskStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class GuardResultCode(str, Enum):
    PASSED = "passed"
    SCHEMA_INVALID = "schema_invalid"
    FORBIDDEN_ARG_FIELD = "forbidden_arg_field"
    DUPLICATE_CALL = "duplicate_call"
    STEP_LIMIT_EXCEEDED = "step_limit_exceeded"
    WRITE_REQUIRES_CONFIRMATION = "write_requires_confirmation"
    TOOL_NOT_REGISTERED = "tool_not_registered"


class OpportunitySignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal: Literal["none", "social_opening"] = "none"
    evidence_span: str = ""
    confidence: float = 0.0


class RunCondition(BaseModel):
    """A bounded, server-evaluated execution condition.

    ``depends_on`` remains the data edge.  ``run_if`` is a control edge and
    therefore never causes the source observation to be copied into the
    downstream agent's prompt.
    """

    model_config = ConfigDict(extra="forbid")

    source_task_id: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="Task id that owns the server-issued control outcome; not a data dependency",
    )
    required_outcome: Literal[
        "task.finished",
        "calendar.no_scheduled_events",
        "calendar.has_scheduled_events",
    ] = Field(
        description=(
            "Exact control outcome. Use task.finished for ordinary Calendar prechecks; "
            "use calendar.no_scheduled_events only for an explicit stop-when-busy request"
        ),
    )


class MatchSearchRequest(BaseModel):
    """Semantic request only; an invitation still requires a bound confirmation."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["general", "activity"] = "general"
    topic: str = Field(default="", max_length=80)
    invitation_evidence: str = Field(default="", max_length=120)


class ResolvedTemporalTarget(BaseModel):
    """Server-resolved date shared by every task bound to one time phrase."""

    model_config = ConfigDict(extra="forbid")

    source_text: str = Field(min_length=1, max_length=40)
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    timezone: str = Field(default="Asia/Taipei", min_length=1, max_length=64)


class TemporalClarification(BaseModel):
    """Prompt-safe date alternatives that require a natural LLM question."""

    model_config = ConfigDict(extra="forbid")

    source_text: str = Field(min_length=1, max_length=40)
    candidates: list[dict[str, str]] = Field(min_length=2, max_length=3)


class SubTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    agent: Literal["calendar", "places", "web", "match", "relationship", "profile", "product_info", "synthesizer"] = Field(
        description=(
            "Domain owner. relationship owns the aggregate of accepted/established contacts "
            "(list, count, compare, or choose among them), date invitations, and cancellation "
            "of a server-referenced date card; match owns search state and the Hub's multi-card inbox."
        ),
    )
    place_mode: PlaceMode | None = None
    depends_on: list[str] = Field(default_factory=list, max_length=4)
    task_brief: str = Field(min_length=1, max_length=500)
    relationship_intent: Literal["lookup", "recommend", "review"] | None = Field(
        default=None,
        description=(
            "Relationship only. lookup reads the accepted-contact aggregate; "
            "recommend evaluates who fits the current activity; review rechecks "
            "a prior recommendation or its reasoning."
        ),
    )
    match_intent: MatchIntent | None = Field(
        default=None,
        description="Match only. User intent, never state permission.",
    )
    match_search_request: MatchSearchRequest | None = Field(
        default=None,
        description="Search only: general or activity; topic and invitation_evidence are exact spans from the current user message. Empty invitation_evidence means preview only.",
    )
    evidence_policy: Literal["casual_discovery", "strict_verification"] | None = Field(
        default=None,
        description="Web tasks only; omit for Calendar, Places, Match, Relationship and every other agent",
    )
    web_mode: WebMode | None = Field(
        default=None,
        description=(
            "Web only: independent lookup, bound-place verification, or hours fallback."
        ),
    )
    outcome_contract: Literal["calendar.availability.v1"] | None = Field(
        default=None,
        description=(
            "Calendar availability tasks only; use exactly calendar.availability.v1. "
            "Never put relationship.date_invitation.v1 here; that belongs only in "
            "the top-level write_intent. Omit for every other task."
        ),
    )
    run_if: RunCondition | None = Field(
        default=None,
        description="Optional Calendar control edge; omit unless a downstream task must wait for a Calendar outcome",
    )
    resolved_time_target: ResolvedTemporalTarget | None = Field(
        default=None,
        description="Internal-only server-resolved date; never authored by a provider",
    )

    @model_validator(mode="after")
    def _normalize_evidence_policy(self) -> "SubTask":
        if self.agent != "relationship" and self.relationship_intent is not None:
            raise ValueError("relationship_intent is only valid for Relationship tasks")
        if self.agent == "relationship" and self.relationship_intent is None:
            brief = str(self.task_brief or "")
            if any(token in brief for token in ("為什麼", "為啥", "理由", "換一個", "不喜歡", "上一個", "剛才")):
                self.relationship_intent = "review"
            elif any(token in brief for token in ("適合", "約誰", "同行", "一起去", "推薦", "挑一位", "活動")):
                self.relationship_intent = "recommend"
            else:
                self.relationship_intent = "lookup"
        if self.agent == "web":
            if self.evidence_policy is None:
                self.evidence_policy = "casual_discovery"
        elif self.evidence_policy is not None:
            raise ValueError("evidence_policy is only valid for Web tasks")
        if self.agent != "web" and self.web_mode is not None:
            raise ValueError("web_mode is only valid for Web tasks")
        if self.outcome_contract is not None and self.agent != "calendar":
            raise ValueError("outcome_contract is only valid for Calendar tasks")
        if self.resolved_time_target is not None and self.agent not in {"calendar", "web"}:
            raise ValueError("resolved_time_target is only valid for Calendar or Web tasks")
        return self


class PlaceSelection(BaseModel):
    """Planner evidence that the current turn continues a place list.

    This is an intent hint only.  The Scheduler resolves the selection against
    the server-owned presentation snapshot before Calendar can use it.
    """

    model_config = ConfigDict(extra="forbid")

    selection_text: str = Field(
        min_length=1,
        max_length=160,
        description="A contiguous selection phrase copied from the current user message",
    )
    source_references: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Optional opaque references copied from the server-projected place list",
    )

    @field_validator("selection_text")
    @classmethod
    def _trim_selection_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("source_references")
    @classmethod
    def _trim_source_references(cls, value: list[str]) -> list[str]:
        return [item.strip()[:80] for item in value if isinstance(item, str) and item.strip()]


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # ``tasks`` remains the default for compatibility with older provider
    # payloads that predate the direct-chat optimization.
    mode: Literal["tasks", "direct_chat", "product_info"] = Field(
        default="tasks",
        description="tasks runs the existing DAG; direct_chat is a task-free conversational reply",
    )
    write_intent: PlannerWriteIntent = Field(
        default="none",
        description=(
            "Typed write capability declared by Planner. Date invitation and date-card cancellation "
            "are owned exclusively by a Relationship-to-Synthesizer DAG."
        ),
    )
    presentation_mode: Literal["default", "itinerary"] = "default"
    # At most four domain specialists plus one terminal synthesizer.
    tasks: list[SubTask] = Field(default_factory=list, max_length=5)
    direct_reply: str | None = Field(
        default=None,
        max_length=160,
        description="Only for direct_chat; a short plain-text reply with no App/domain claims",
    )
    direct_messages: list[str] = Field(default_factory=list, max_length=3)
    # Deprecated provider-compatibility field.  New plans route a normal
    # ``product_info`` SubTask and do not expose a section/topic taxonomy.
    product_info_topics: list[str] = Field(default_factory=list, max_length=3)
    opportunity: OpportunitySignal | None = None
    place_selection: PlaceSelection | None = Field(
        default=None,
        description=(
            "Optional intent hint for continuing a server-presented place list; "
            "the reference is never authoritative by itself"
        ),
    )
    temporal_clarification: TemporalClarification | None = Field(
        default=None,
        description="Internal-only unresolved date alternatives for Synthesizer composition",
    )

    @field_validator("direct_reply")
    @classmethod
    def _trim_direct_reply(cls, value: str | None) -> str | None:
        return value.strip() if isinstance(value, str) else value

    @field_validator("direct_messages")
    @classmethod
    def _trim_direct_messages(cls, value: list[str]) -> list[str]:
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]

    @model_validator(mode="after")
    def _validate_dag(self) -> "Plan":
        if self.write_intent in {
            DATE_INVITATION_WRITE_INTENT,
            DATE_COORDINATION_CANCEL_WRITE_INTENT,
        } and self.mode != "tasks":
            raise ValueError(
                "relationship date write intents require tasks mode with exactly one "
                "Relationship task and one terminal Synthesizer"
            )
        if self.mode == "direct_chat":
            if self.tasks:
                raise ValueError("direct_chat plan cannot contain tasks")
            if bool(self.direct_reply) == bool(self.direct_messages):
                raise ValueError("direct_chat plan requires exactly one reply shape")
            if self.product_info_topics:
                raise ValueError("direct_chat plan cannot contain product_info_topics")
            if self.opportunity is not None and self.opportunity.signal != "none":
                raise ValueError("direct_chat plan cannot contain an opportunity")
            if self.presentation_mode != "default":
                raise ValueError("direct_chat plan cannot use itinerary presentation")
            if self.temporal_clarification is not None:
                raise ValueError("direct_chat cannot carry temporal clarification")
            return self

        if self.mode == "product_info":
            if self.tasks or self.opportunity is not None:
                raise ValueError("product_info plan cannot contain tasks or opportunity")
            if self.direct_reply is not None or self.direct_messages:
                raise ValueError("product_info plan cannot contain direct reply")
            if self.presentation_mode != "default":
                raise ValueError("product_info plan cannot use itinerary presentation")
            if not self.product_info_topics:
                raise ValueError("product_info plan requires topics")
            if len(set(self.product_info_topics)) != len(self.product_info_topics):
                raise ValueError("duplicate product_info topic")
            if self.temporal_clarification is not None:
                raise ValueError("product_info cannot carry temporal clarification")
            return self

        if self.direct_reply is not None or self.direct_messages or self.product_info_topics:
            raise ValueError("tasks plan cannot contain direct reply or product_info topics")
        if not self.tasks:
            raise ValueError("tasks plan requires at least one task")
        if self.presentation_mode == "itinerary" and not any(t.agent == "places" for t in self.tasks):
            raise ValueError("itinerary plan requires a places task")

        if self.write_intent in {
            DATE_INVITATION_WRITE_INTENT,
            DATE_COORDINATION_CANCEL_WRITE_INTENT,
        }:
            relationship_tasks = [task for task in self.tasks if task.agent == "relationship"]
            synthesizer_tasks = [task for task in self.tasks if task.agent == "synthesizer"]
            if len(relationship_tasks) != 1 or len(synthesizer_tasks) != 1:
                raise ValueError(
                    "relationship date write intents require exactly one Relationship "
                    "write task and one terminal Synthesizer"
                )
            relationship_task = relationship_tasks[0]
            synthesizer_task = synthesizer_tasks[0]
            if relationship_task.depends_on or relationship_task.run_if is not None:
                raise ValueError(
                    "relationship date write intents Relationship task cannot depend on a precheck"
            )
            for task in self.tasks:
                if task is relationship_task or task is synthesizer_task:
                    continue
                read_only_sibling = (
                    task.agent in {"places", "web", "product_info"}
                    or (
                        task.agent == "calendar"
                        and task.outcome_contract == "calendar.availability.v1"
                    )
                    or (
                        task.agent == "match"
                        and task.match_intent in {"status", "counterparty", "clarify"}
                    )
                )
                if not read_only_sibling:
                    raise ValueError(
                        "relationship date write intents allow only typed read-only sibling tasks"
                    )
            if self.presentation_mode != "default":
                raise ValueError("relationship date write intents use default presentation")
            if self.opportunity is not None and self.opportunity.signal != "none":
                raise ValueError("relationship date write intents cannot contain an opportunity")

        ids = {t.id for t in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("duplicate SubTask id")
        for t in self.tasks:
            if len(set(t.depends_on)) != len(t.depends_on):
                raise ValueError(f"SubTask {t.id} has duplicate dependencies")
            if t.id in t.depends_on:
                raise ValueError(f"SubTask {t.id} cannot depend on itself")
            for dep in t.depends_on:
                if dep not in ids:
                    raise ValueError(f"SubTask {t.id} depends on unknown {dep}")
            if t.agent == "web" and t.web_mode is not None:
                dependency_agents = {
                    next(item.agent for item in self.tasks if item.id == dep)
                    for dep in t.depends_on
                }
                if t.web_mode == "public_lookup" and "places" in dependency_agents:
                    raise ValueError("public_lookup Web task cannot depend on Places")
                if (
                    t.web_mode in {"place_verification", "place_hours_fallback"}
                    and "places" not in dependency_agents
                ):
                    raise ValueError(f"{t.web_mode} Web task requires a Places dependency")
            if t.run_if is not None:
                if t.agent == "synthesizer":
                    raise ValueError("synthesizer cannot have run_if")
                source = t.run_if.source_task_id
                if source not in ids:
                    raise ValueError(f"SubTask {t.id} run_if references unknown {source}")
                if source == t.id:
                    raise ValueError(f"SubTask {t.id} run_if cannot reference itself")
                source_task = next(item for item in self.tasks if item.id == source)
                if source_task.agent == "synthesizer":
                    raise ValueError("run_if cannot reference the terminal synthesizer")
                if source in t.depends_on:
                    raise ValueError("run_if source must be a control edge, not a data dependency")
                if t.run_if.required_outcome.startswith("calendar."):
                    if source_task.agent != "calendar":
                        raise ValueError("calendar outcomes require a Calendar source task")
                    if source_task.outcome_contract != "calendar.availability.v1":
                        raise ValueError("Calendar source must declare availability outcome_contract")
        synthesizers = [t for t in self.tasks if t.agent == "synthesizer"]
        if len(synthesizers) != 1:
            raise ValueError("plan must contain exactly one synthesizer")
        if self.temporal_clarification is not None:
            if self.write_intent != "none":
                raise ValueError("temporal clarification cannot authorize a write")
            if len(self.tasks) != 1:
                raise ValueError("temporal clarification runs only the synthesizer")
            return self
        synthesizer = synthesizers[0]
        domain_ids = ids - {synthesizer.id}
        referenced_domains = {
            dep
            for task in self.tasks
            if task.agent != "synthesizer"
            for dep in task.depends_on
        }
        referenced_domains.update(
            task.run_if.source_task_id
            for task in self.tasks
            if task.agent != "synthesizer" and task.run_if is not None
        )
        terminal_domain_ids = domain_ids - referenced_domains
        if not terminal_domain_ids <= set(synthesizer.depends_on):
            raise ValueError("synthesizer must depend on every terminal domain task")
        for other in self.tasks:
            if other.id != synthesizer.id and synthesizer.id in other.depends_on:
                raise ValueError(f"synthesizer {synthesizer.id} must be terminal")

        # A valid reference set can still contain a cycle.  Walk the whole DAG
        # so the Scheduler never receives an unbounded/unexecutable plan.
        completed: set[str] = set()
        remaining = list(self.tasks)
        while remaining:
            ready = [
                t for t in remaining
                if set(t.depends_on)
                | ({t.run_if.source_task_id} if t.run_if is not None else set())
                <= completed
            ]
            if not ready:
                raise ValueError("plan contains a dependency cycle")
            completed.update(t.id for t in ready)
            remaining = [t for t in remaining if t not in ready]
        return self


def normalize_plan_for_execution(plan: Plan, fallback_task_brief: str = "") -> Plan:
    """Normalize the retired task-free ProductInfo envelope into a DAG.

    This compatibility shim is intentionally outside the Scheduler's dispatch
    logic.  New Planner payloads already contain a normal ``product_info`` task;
    this only keeps old provider trajectories executable while deployments
    roll forward.
    """
    if plan.mode != "product_info" or plan.tasks:
        return plan
    brief = " ".join(str(item).strip() for item in plan.product_info_topics if str(item).strip())
    brief = (brief or str(fallback_task_brief or "產品功能問題")).strip()[:500]
    return Plan(
        mode="tasks",
        tasks=[
            SubTask(id="product_info", agent="product_info", depends_on=[], task_brief=brief),
            SubTask(id="synthesizer", agent="synthesizer", depends_on=["product_info"], task_brief="整合已驗證的產品資訊觀察"),
        ],
    )


class AgentContextSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["calendar", "places", "web", "match", "relationship", "profile", "product_info", "synthesizer"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolProposal(BaseModel):
    """A sub-agent's tool-call proposal. Execution stays with the Scheduler/Guard."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_forbidden_fields(self) -> "ToolProposal":
        present = FORBIDDEN_ARG_FIELDS & set(self.arguments.keys())
        if present:
            raise ValueError(f"forbidden arg fields: {sorted(present)}")
        return self


class GuardDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    code: GuardResultCode
    reason: str = ""


class SubTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: SubTaskStatus
    tool_name: str | None = None
    observation: dict[str, Any] | None = None
    error_code: str | None = None
    skip_reason: str | None = None
    guard_code: GuardResultCode | None = None
    outcome_codes: list[Literal[
        "calendar.no_scheduled_events",
        "calendar.has_scheduled_events",
    ]] = Field(default_factory=list, max_length=2)
