"""Relationship runtime dispatch for read and typed date-card tasks."""

from __future__ import annotations

from .contracts import DATE_INVITATION_WRITE_INTENT, AgentContextSlice, SubTask
from .runtime_registry import TaskRunnerResult
from .sub_agents import relationship_agent
from .sub_agents.base import SubAgentMetrics


DATE_INVITATION_PROTOCOL_FAILURE_CODE = (
    "relationship_date_invitation_provider_protocol_failed"
)
DATE_INVITATION_PROTOCOL_FAILURE_REPLY = (
    "我知道你要建立邀請卡，但我剛才沒能安全確認邀請對象。"
    "請直接說名字或 @ 對方再試一次。"
)


def run(
    context_slice: AgentContextSlice,
    *,
    task: SubTask,
    services: object,
) -> tuple[TaskRunnerResult, SubAgentMetrics]:
    write_intent = str(
        getattr(services, "runtime_state", {}).get("planner_write_intent") or "none"
    )
    if write_intent == DATE_INVITATION_WRITE_INTENT:
        proposals, metrics = relationship_agent.run_date_invitation(
            context_slice, task_brief=task.task_brief,
        )
    else:
        proposals, metrics = relationship_agent.run(
            context_slice, task_brief=task.task_brief,
        )
    return TaskRunnerResult.from_proposals(proposals), metrics
