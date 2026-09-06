"""Relationship runtime dispatch for read and typed date-card tasks."""

from __future__ import annotations

from typing import Any

from .contracts import (
    DATE_INVITATION_WRITE_INTENT,
    AgentContextSlice,
    SubTask,
    SubTaskResult,
    SubTaskStatus,
    ToolProposal,
)
from .guarded_execution import GuardedReadExecutor
from .runtime_registry import TaskRunnerResult
from .sub_agents import relationship_agent
from .sub_agents.base import SubAgentMetrics


MAX_RELATIONSHIP_READS = 3
MAX_RELATIONSHIP_LLM_CALLS = 4


def _observation_dict(result: SubTaskResult) -> dict[str, Any]:
    return {
        "task_id": result.task_id,
        "status": result.status.value,
        "tool": result.tool_name,
        "result": result.observation,
        "error_code": result.error_code,
        "skip_reason": result.skip_reason,
    }


def _accumulate_metrics(total: SubAgentMetrics, current: SubAgentMetrics) -> None:
    total.input_tokens += current.input_tokens
    total.output_tokens += current.output_tokens
    total.duration_ms += current.duration_ms
    total.llm_call_count += current.llm_call_count
    total.tool_calls_raw.extend(current.tool_calls_raw or [])
    total.rejected_calls.extend(current.rejected_calls or [])
    total.llm_requests.extend(current.llm_requests or [])
    total.content_raw = current.content_raw
    total.prompt_raw = current.prompt_raw
    total.tools_raw = current.tools_raw
    total.input_payload = current.input_payload
    if current.error:
        total.error = current.error


def _contact_refs(observations: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for item in observations:
        result = item.get("result") if isinstance(item, dict) else None
        if not isinstance(result, dict):
            continue
        for contact in result.get("contacts") or []:
            if isinstance(contact, dict) and contact.get("contact_ref"):
                refs.add(str(contact["contact_ref"]))
    return refs


def _recommendation_observation(
    *, task: SubTask, observations: list[dict[str, Any]], stop_reason: str,
    decision: Any | None,
) -> dict[str, Any]:
    """Build the typed evidence envelope consumed by Synthesizer."""
    contacts: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    unavailable: list[str] = []
    seen_candidates: set[str] = set()
    seen_evidence: set[str] = set()
    for item in observations:
        result = item.get("result") if isinstance(item, dict) else None
        if not isinstance(result, dict):
            continue
        for contact in result.get("contacts") or []:
            if not isinstance(contact, dict):
                continue
            ref = str(contact.get("contact_ref") or "")
            target = evidence if item.get("tool") == "relationship.get_contact_evidence" else contacts
            seen = seen_evidence if target is evidence else seen_candidates
            if ref and ref not in seen:
                target.append(contact)
                seen.add(ref)
        unavailable.extend(str(ref) for ref in result.get("unavailable_refs") or [] if str(ref))
    evidence_by_ref = {
        str(item.get("contact_ref")): item
        for item in [*contacts, *evidence]
        if item.get("contact_ref")
    }
    recommendations: list[dict[str, Any]] = []
    recommended_refs: set[str] = set()
    decision_unknowns: list[str] = []
    activity = ""
    decision_status = "insufficient"
    if decision is not None:
        payload = decision.model_dump(mode="json") if hasattr(decision, "model_dump") else dict(decision)
        activity = str(payload.get("activity") or "")[:180]
        decision_status = str(payload.get("status") or "insufficient")
        decision_unknowns = [str(value)[:240] for value in (payload.get("unknowns") or [])[:6]]
        for item in payload.get("recommendations") or []:
            if not isinstance(item, dict):
                continue
            ref = str(item.get("contact_ref") or "")
            source = evidence_by_ref.get(ref)
            if source is None or ref in recommended_refs:
                continue
            recommended_refs.add(ref)
            fields = [
                str(field)
                for field in (item.get("evidence_fields") or [])
                if str(field) in source and bool(source.get(str(field)))
            ][:6]
            classification = str(item.get("classification") or "exploratory")
            direct_capable_fields = {
                "recent_context", "initial_interest", "verified_common_ground", "distinctive_tags",
            }
            if classification == "direct" and not direct_capable_fields.intersection(fields):
                classification = "exploratory"
            recommendations.append({
                "contact_ref": ref,
                "display_name": str(source.get("display_name") or "對方")[:30],
                "classification": classification,
                "evidence_fields": fields,
                "reason": str(item.get("reason") or "")[:240],
                "unknowns": [str(value)[:240] for value in (item.get("unknowns") or [])[:4]],
            })
    direct_evidence = [item for item in recommendations if item["classification"] == "direct"]
    exploratory_evidence = [item for item in recommendations if item["classification"] != "direct"]
    if not recommendations:
        decision_status = "insufficient"
    elif decision_status == "grounded" and not direct_evidence:
        decision_status = "exploratory"
    unknowns = list(dict.fromkeys([
        *decision_unknowns,
        *[
            value
            for item in recommendations
            for value in item.get("unknowns") or []
        ],
    ]))[:6]
    if not recommendations and not unknowns:
        unknowns = ["沒有足夠資料判斷誰適合目前活動"]
    return {
        "schema_version": "relationship_recommendation.v1",
        "intent": task.relationship_intent or "recommend",
        "request": task.task_brief[:500],
        "activity": activity,
        "candidate_refs": [str(item.get("contact_ref")) for item in contacts if item.get("contact_ref")],
        "candidate_pool": contacts[:8],
        "evidence": evidence[:8],
        "recommendations": recommendations[:3],
        "recommended_candidate_refs": [item["contact_ref"] for item in recommendations[:3]],
        "direct_evidence": direct_evidence[:3],
        "exploratory_evidence": exploratory_evidence[:3],
        "unavailable_refs": list(dict.fromkeys(unavailable))[:3],
        "unknowns": unknowns,
        "status": decision_status if contacts else "unavailable",
        "stop_reason": stop_reason,
    }


def _run_recommendation(
    context_slice: AgentContextSlice,
    *, task: SubTask,
    services: GuardedReadExecutor,
) -> tuple[TaskRunnerResult, SubAgentMetrics]:
    aggregate = SubAgentMetrics()
    aggregate.requested_model_tier = "fast"
    observations: list[dict[str, Any]] = []
    # A prior recommendation snapshot is evidence only when it is explicitly
    # marked as a current, server-owned observation. Ordinary chat history is
    # never treated as proof.
    prior = context_slice.payload.get("prior_observations") or []
    read_count = 0
    stop_reason = "budget_exhausted"
    list_seen = False
    evidence_seen = False

    for round_index in range(MAX_RELATIONSHIP_LLM_CALLS):
        if read_count >= MAX_RELATIONSHIP_READS:
            stop_reason = "read_budget_exhausted"
            break
        round_slice = context_slice.model_copy(update={
            "payload": {
                **context_slice.payload,
                "prior_observations": [*prior, *observations],
                "relationship_round": round_index + 1,
                "relationship_read_count": read_count,
                "relationship_intent": task.relationship_intent or "recommend",
            },
        })
        proposals, metrics = relationship_agent.run(
            round_slice, task_brief=task.task_brief,
        )
        _accumulate_metrics(aggregate, metrics)
        if not proposals:
            stop_reason = "model_finished" if observations else "model_no_read"
            break

        allowed: set[str]
        if not list_seen:
            allowed = {"relationship.list_accepted_contacts"}
        elif not evidence_seen:
            allowed = {"relationship.get_contact_evidence"}
        else:
            stop_reason = "evidence_collected"
            break
        filtered: list[ToolProposal] = []
        valid_refs = _contact_refs(observations)
        for proposal in proposals:
            if proposal.tool_name not in allowed:
                aggregate.rejected_calls.append("relationship_phase_mismatch")
                continue
            if proposal.tool_name == "relationship.get_contact_evidence":
                refs = [str(item) for item in proposal.arguments.get("contact_refs") or []]
                refs = [ref for ref in refs if ref in valid_refs][:3]
                if not refs:
                    aggregate.rejected_calls.append("contact_ref_not_grounded")
                    continue
                filtered.append(proposal.model_copy(update={"arguments": {"contact_refs": refs}}))
            else:
                filtered.append(proposal)
        if not filtered:
            stop_reason = "no_grounded_followup"
            break
        for index, proposal in enumerate(filtered):
            outcome = services.execute(
                proposal,
                allowed_tools=frozenset({
                    "relationship.list_accepted_contacts",
                    "relationship.get_contact_evidence",
                }),
                step_count=read_count,
                max_reads=MAX_RELATIONSHIP_READS,
                prior_observations=[*prior, *observations],
                call_index=read_count + index,
            )
            if outcome.attempted:
                read_count += 1
            result = outcome.result
            if result.status is SubTaskStatus.OK and result.observation:
                observations.append(_observation_dict(result))
                if result.tool_name == "relationship.list_accepted_contacts":
                    list_seen = True
                if result.tool_name == "relationship.get_contact_evidence":
                    evidence_seen = True
            elif result.error_code:
                aggregate.rejected_calls.append(str(result.error_code))
        if read_count >= MAX_RELATIONSHIP_READS:
            stop_reason = "read_budget_exhausted"
            break
        if evidence_seen:
            stop_reason = "evidence_collected"
            break

    decision = None
    if observations and aggregate.llm_call_count < MAX_RELATIONSHIP_LLM_CALLS:
        decision, metrics = relationship_agent.finish_recommendation(
            context_slice,
            task_brief=task.task_brief,
            observations=[*prior, *observations],
        )
        _accumulate_metrics(aggregate, metrics)
        if decision is None:
            stop_reason = "finish_protocol_failed"
    envelope = _recommendation_observation(
        task=task, observations=observations, stop_reason=stop_reason,
        decision=decision,
    )
    return TaskRunnerResult.from_completed([SubTaskResult(
        task_id=task.id,
        status=SubTaskStatus.OK if observations else SubTaskStatus.FAILED,
        observation=envelope,
        error_code=None if observations else "relationship_evidence_unavailable",
    )]), aggregate


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
    elif task.relationship_intent in {"recommend", "review"}:
        return _run_recommendation(context_slice, task=task, services=services)
    else:
        proposals, metrics = relationship_agent.run(
            context_slice, task_brief=task.task_brief,
        )
    return TaskRunnerResult.from_proposals(proposals), metrics
