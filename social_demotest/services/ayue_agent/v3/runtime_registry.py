"""Domain-neutral runner contracts used by the V3 Scheduler.

The Scheduler only needs to know whether a registered runtime returned tool
proposals (which still go through the central guard) or completed sub-task
results (which are already typed and safe to project).  Domain runtimes own
the interpretation of their provider output before it reaches this boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from .contracts import AgentContextSlice, SubTask, SubTaskResult, ToolProposal
from .guarded_execution import GuardedReadExecutor
from .sub_agents.base import SubAgentMetrics


@dataclass(frozen=True)
class TaskRunnerResult:
    """Mutually exclusive result of one registered task runner.

    Proposal runners return ``proposals`` and leave guarding/execution to the
    Scheduler.  Specialist runtimes return ``completed_results`` after owning
    their bounded interpretation and execution.  Keeping these alternatives
    explicit prevents a domain result from being mistaken for a proposal.
    """

    proposals: list[ToolProposal] | None = None
    completed_results: list[SubTaskResult] | None = None

    def __post_init__(self) -> None:
        if (self.proposals is None) == (self.completed_results is None):
            raise ValueError("TaskRunnerResult requires exactly one result kind")

    @classmethod
    def from_proposals(cls, proposals: list[ToolProposal] | None) -> "TaskRunnerResult":
        return cls(proposals=list(proposals or []))

    @classmethod
    def from_completed(cls, results: list[SubTaskResult]) -> "TaskRunnerResult":
        return cls(completed_results=list(results))

    @property
    def completed(self) -> list[SubTaskResult] | None:
        """Short compatibility alias for callers describing terminal results."""
        return self.completed_results


RunnerCallable = Callable[..., tuple[TaskRunnerResult, SubAgentMetrics | None]]
Blocker = Callable[[Any], str | None]
ResultProjector = Callable[[list[dict[str, Any]]], dict[str, Any]]
BeforeRun = Callable[[SubTask, GuardedReadExecutor], Any]
AfterRun = Callable[[SubTask, GuardedReadExecutor, TaskRunnerResult, SubAgentMetrics | None], Any]


@dataclass(frozen=True)
class RuntimeRegistration:
    """Registered capability surface consumed by the Scheduler.

    Optional hooks are deliberately projections only: they cannot execute a
    domain write or change the runner result.  They keep direct-chat gates,
    confirmation-result presentation, and bounded progress envelopes beside
    the runtime that owns those semantics.
    """

    runner: RunnerCallable
    direct_chat_blocker: Blocker | None = None
    confirmed_result_projector: ResultProjector | None = None
    before_run: BeforeRun | None = None
    after_run: AfterRun | None = None
    step_prefix: str = "web"
    legacy_signature: bool = False


def proposal_runner(run_fn: Callable[..., Any]) -> RunnerCallable:
    """Adapt the existing proposal agents to the uniform runner signature."""

    def run(
        context_slice: AgentContextSlice,
        *,
        task: SubTask,
        services: GuardedReadExecutor,
    ) -> tuple[TaskRunnerResult, SubAgentMetrics | None]:
        del services
        raw_result = run_fn(context_slice, task_brief=task.task_brief)
        if not isinstance(raw_result, tuple) or len(raw_result) != 2:
            raise TypeError("proposal runner must return (proposals, metrics)")
        proposals, metrics = raw_result
        if isinstance(proposals, TaskRunnerResult):
            return proposals, metrics
        return TaskRunnerResult.from_proposals(list(proposals or [])), metrics

    return run


def normalize_runner_output(
    output: Any,
) -> tuple[TaskRunnerResult, SubAgentMetrics | None]:
    """Normalize a legacy test/provider tuple at the compatibility boundary.

    Production registrations all return ``TaskRunnerResult`` directly.  The
    tuple/list fallback keeps older focused tests and local provider stubs
    useful during the staged migration without adding signature inspection.
    """

    if not isinstance(output, tuple) or len(output) != 2:
        raise TypeError("runner must return (TaskRunnerResult, metrics)")
    result, metrics = output
    if isinstance(result, TaskRunnerResult):
        return result, metrics
    if result is None:
        return TaskRunnerResult.from_proposals([]), metrics
    if isinstance(result, list):
        return TaskRunnerResult.from_proposals(result), metrics
    raise TypeError("runner returned an unsupported result")


def registration_for(value: Any) -> RuntimeRegistration | None:
    """Return a registration for production records and legacy test callables."""

    if value is None:
        return None
    if isinstance(value, RuntimeRegistration):
        return value
    if callable(value):
        # A short-lived compatibility path for callers that still inject the
        # semantic Calendar function directly.  The adapter itself lives at
        # the domain boundary; Scheduler never interprets Calendar objects.
        if str(getattr(value, "__module__", "")).endswith("sub_agents.calendar_agent"):
            from . import calendar_runtime

            def calendar_legacy_runtime(
                context_slice: AgentContextSlice,
                *,
                task: SubTask,
                services: GuardedReadExecutor,
            ) -> Any:
                services.semantic_runner_override = value
                return calendar_runtime.run(context_slice, task=task, services=services)

            return RuntimeRegistration(runner=calendar_legacy_runtime)
        return RuntimeRegistration(runner=value, legacy_signature=True)
    return None


def completed_projection(result: TaskRunnerResult) -> list[SubTaskResult]:
    """Return completed results, raising if a proposal result is misused."""

    if result.completed_results is None:
        raise ValueError("proposal result cannot be projected as completed")
    return result.completed_results
