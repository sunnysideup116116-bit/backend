"""Bounded Web research runtime owned by the Web domain."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import config
from services.ayue_agent.web_tools import web_enabled

from .contracts import AgentContextSlice, SubTask, SubTaskResult, SubTaskStatus, ToolProposal
from .guarded_execution import GuardedReadExecutor
from .place_projection import public_place_cards
from .sub_agents import web_agent
from .runtime_registry import TaskRunnerResult
from .sub_agents.base import SubAgentMetrics
from .web_research import (
    MAX_WEB_EXTRACT_CALLS,
    MAX_WEB_EXTRACT_URLS,
    MAX_WEB_INITIAL_SEARCH_QUERIES,
    MAX_WEB_REFINED_SEARCH_QUERIES,
    MAX_WEB_FINISH_DECISION_ATTEMPTS,
    MAX_WEB_RESEARCH_DECISION_ITERATIONS,
    MAX_WEB_SEARCH_CALLS,
    MAX_WEB_TOTAL_TOOL_CALLS,
    WebResearchResultV1,
    anchor_place_search_query,
    anchor_web_search_query,
    build_research_result,
)


def _observation_dict(result: SubTaskResult) -> dict[str, Any]:
    return {
        "task_id": result.task_id,
        "status": result.status.value,
        "tool": result.tool_name,
        "result": result.observation,
        "error_code": result.error_code,
        "skip_reason": result.skip_reason,
    }


def _completed_result(task: SubTask, result: WebResearchResultV1) -> TaskRunnerResult:
    return TaskRunnerResult.from_completed([SubTaskResult(
        task_id=task.id,
        status=SubTaskStatus.OK,
        observation=result.model_dump(mode="json"),
    )])


def run(
    context_slice: AgentContextSlice,
    *,
    task: SubTask,
    services: GuardedReadExecutor,
) -> tuple[TaskRunnerResult, SubAgentMetrics]:
    """Run bounded research/tool work followed by one finish-only phase.

    Web owns all research state and policy. ``services`` is only the guarded
    execution boundary; this runtime never calls a provider adapter directly.
    """
    aggregate = SubAgentMetrics()
    aggregate.requested_model_tier = "main"

    def accumulate_metrics(metrics: SubAgentMetrics) -> None:
        aggregate.input_tokens += metrics.input_tokens
        aggregate.output_tokens += metrics.output_tokens
        aggregate.duration_ms += metrics.duration_ms
        aggregate.llm_call_count += metrics.llm_call_count
        aggregate.tool_calls_raw.extend(metrics.tool_calls_raw or [])
        aggregate.rejected_calls.extend(metrics.rejected_calls or [])
        aggregate.content_raw = metrics.content_raw
        aggregate.prompt_raw = metrics.prompt_raw
        aggregate.tools_raw = metrics.tools_raw
        aggregate.input_payload = metrics.input_payload
        aggregate.llm_requests.extend(metrics.llm_requests or [])
        if metrics.error:
            aggregate.error = metrics.error

    evidence_policy = task.evidence_policy or "casual_discovery"
    place_cards = public_place_cards(
        context_slice.payload.get("prior_observations") or [],
        run_id=services.run_id,
        include_internal=True,
    )[:5]
    place_candidates = [
        {
            "candidate_ref": item.get("candidate_ref"),
            "name": item.get("name"),
            "category": item.get("category"),
            "address_summary": item.get("address_summary"),
            "distance_m": item.get("distance_m"),
        }
        for item in place_cards
    ]
    candidate_by_ref = {
        str(item.get("candidate_ref")): item
        for item in place_candidates
        if item.get("candidate_ref")
    }
    allowed_subject_refs = set(candidate_by_ref)
    if not web_enabled():
        aggregate.error = "web_not_configured"
        result = build_research_result(
            research_question=context_slice.payload.get("message", ""),
            answer_target=task.task_brief,
            decision=None,
            observations=[],
            execution_status="unavailable",
            stop_reason="tool_unavailable",
            allowed_subject_refs=allowed_subject_refs if place_candidates else None,
            evidence_policy=evidence_policy,
        )
        return _completed_result(task, result), aggregate

    observations: list[dict[str, Any]] = []
    failures: list[str] = []
    tool_calls_used = 0
    search_calls_used = 0
    extract_calls_used = 0
    last_decision = None
    stop_reason = "budget_exhausted"

    def execute_proposals(
        proposals: list[ToolProposal],
        proposal_subject_refs: dict[int, str | None],
    ) -> None:
        """Execute bounded Web proposals through the shared Guard boundary."""
        nonlocal tool_calls_used, search_calls_used, extract_calls_used
        if len(proposals) > 1:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ayue-web") as pool:
                futures = {
                    pool.submit(
                        services.execute,
                        proposal,
                        allowed_tools=frozenset({"web.search", "web.extract"}),
                        step_count=tool_calls_used + index,
                        max_reads=MAX_WEB_TOTAL_TOOL_CALLS,
                        prior_observations=observations,
                        call_index=tool_calls_used + index,
                    ): proposal
                    for index, proposal in enumerate(proposals)
                }
                executed_results = [(future.result(), proposal) for future, proposal in futures.items()]
        else:
            executed_results = [(
                services.execute(
                    proposals[0],
                    allowed_tools=frozenset({"web.search", "web.extract"}),
                    step_count=tool_calls_used,
                    max_reads=MAX_WEB_TOTAL_TOOL_CALLS,
                    prior_observations=observations,
                    call_index=tool_calls_used,
                ),
                proposals[0],
            )]

        for outcome, proposal in executed_results:
            result_item = outcome.result
            attempted = outcome.attempted
            if attempted:
                tool_calls_used += 1
                if result_item.tool_name == "web.search":
                    search_calls_used += 1
                elif result_item.tool_name == "web.extract":
                    extract_calls_used += 1
            if result_item.status is SubTaskStatus.OK and result_item.observation:
                observation = _observation_dict(result_item)
                subject_ref = proposal_subject_refs.get(id(proposal))
                if subject_ref:
                    observation["subject_ref"] = subject_ref
                observations.append(observation)
            elif result_item.error_code:
                failures.append(str(result_item.error_code))

    if (
        config.AYUE_V3_WEB_PLACE_BOOTSTRAP_FAST_PATH
        and evidence_policy == "casual_discovery"
        and place_candidates
        and search_calls_used == 0
        and tool_calls_used + min(len(place_candidates), MAX_WEB_INITIAL_SEARCH_QUERIES) <= MAX_WEB_TOTAL_TOOL_CALLS
    ):
        bootstrap_proposals: list[ToolProposal] = []
        bootstrap_subject_refs: dict[int, str | None] = {}
        for candidate in place_candidates[:MAX_WEB_INITIAL_SEARCH_QUERIES]:
            proposal = ToolProposal(
                tool_name="web.search",
                arguments={
                    "query": anchor_place_search_query(
                        candidate_name=str(candidate.get("name") or ""),
                        address_summary=str(candidate.get("address_summary") or ""),
                        answer_target=task.task_brief,
                        suggested_query="",
                    ),
                    "recency": "none",
                    "use_saved_location": False,
                },
            )
            bootstrap_proposals.append(proposal)
            bootstrap_subject_refs[id(proposal)] = str(candidate.get("candidate_ref") or "") or None
        execute_proposals(bootstrap_proposals, bootstrap_subject_refs)

    for round_index in range(1, MAX_WEB_RESEARCH_DECISION_ITERATIONS + 1):
        decision, metrics = web_agent.decide(
            context_slice,
            task_brief=task.task_brief,
            round_index=round_index,
            observations=observations,
            tool_calls_used=tool_calls_used,
            search_calls_used=search_calls_used,
            extract_calls_used=extract_calls_used,
            place_candidates=place_candidates,
            evidence_policy=evidence_policy,
            finish_only=False,
        )
        accumulate_metrics(metrics)
        if decision is None:
            if observations and round_index < MAX_WEB_RESEARCH_DECISION_ITERATIONS:
                continue
            if observations:
                # A failed research decision does not consume tool budget. End
                # the research phase and give the separate finish phase its
                # guaranteed bounded opportunity.
                break
            stop_reason = "model_failure"
            result = build_research_result(
                research_question=context_slice.payload.get("message", ""),
                answer_target=task.task_brief,
                decision=None,
                observations=observations,
                execution_status="degraded" if observations else "unavailable",
                stop_reason=stop_reason,
                allowed_subject_refs=allowed_subject_refs if place_candidates else None,
                evidence_policy=evidence_policy,
            )
            return _completed_result(task, result), aggregate
        if aggregate.error and not metrics.error:
            aggregate.error = ""
        last_decision = decision

        if decision.assessment is not None and decision.assessment.target_alignment == "conflict":
            stop_reason = "target_conflict"
            result = build_research_result(
                research_question=context_slice.payload.get("message", ""),
                answer_target=task.task_brief,
                decision=decision,
                observations=observations,
                execution_status="degraded",
                stop_reason=stop_reason,
                allowed_subject_refs=allowed_subject_refs if place_candidates else None,
                evidence_policy=evidence_policy,
            )
            return _completed_result(task, result), aggregate

        if decision.action == "finish":
            if not observations:
                aggregate.error = "web_finish_missing_evidence_assessment"
                stop_reason = "model_failure"
                result = build_research_result(
                    research_question=context_slice.payload.get("message", ""),
                    answer_target=task.task_brief,
                    decision=None,
                    observations=observations,
                    execution_status="unavailable",
                    stop_reason=stop_reason,
                    allowed_subject_refs=allowed_subject_refs if place_candidates else None,
                    evidence_policy=evidence_policy,
                )
            elif decision.assessment is None:
                aggregate.error = "web_finish_missing_evidence_assessment"
                # Preserve observations and retry through the finish-only
                # phase; this failure consumes no Web tool budget.
                break
            else:
                stop_reason = "tool_failure" if failures and not observations else "evidence_sufficient"
                result = build_research_result(
                    research_question=context_slice.payload.get("message", ""),
                    answer_target=task.task_brief,
                    decision=decision,
                    observations=observations,
                    execution_status="degraded" if failures else "completed",
                    stop_reason=stop_reason,
                    allowed_subject_refs=allowed_subject_refs if place_candidates else None,
                    evidence_policy=evidence_policy,
                )
            return _completed_result(task, result), aggregate

        proposals: list[ToolProposal] = []
        proposal_subject_refs: dict[int, str | None] = {}
        if decision.action == "search":
            max_queries = MAX_WEB_INITIAL_SEARCH_QUERIES if search_calls_used == 0 else MAX_WEB_REFINED_SEARCH_QUERIES
            queries: list[str] = []
            query_subject_refs: list[str | None] = []
            for index, query in enumerate(decision.queries):
                if not str(query).strip():
                    continue
                subject_ref = decision.subject_refs[index] if index < len(decision.subject_refs) else None
                candidate = candidate_by_ref.get(subject_ref or "")
                if candidate is not None:
                    anchored = anchor_place_search_query(
                        candidate_name=str(candidate.get("name") or ""),
                        address_summary=str(candidate.get("address_summary") or ""),
                        answer_target=task.task_brief,
                        suggested_query=str(query),
                    )
                else:
                    anchored = anchor_web_search_query(task.task_brief, str(query))
                queries.append(anchored)
                query_subject_refs.append(subject_ref)
            if not queries or len(queries) > max_queries:
                failures.append("invalid_search_queries")
                aggregate.error = "web_search_query_budget_invalid"
                continue
            if search_calls_used + len(queries) > MAX_WEB_SEARCH_CALLS:
                failures.append("search_budget_exhausted")
                continue
            if tool_calls_used + len(queries) > MAX_WEB_TOTAL_TOOL_CALLS:
                failures.append("tool_budget_exhausted")
                continue
            proposals = [ToolProposal(
                tool_name="web.search",
                arguments={
                    "query": query,
                    "recency": decision.recency,
                    "use_saved_location": decision.use_saved_location,
                },
            ) for query in queries]
            for proposal, subject_ref in zip(proposals, query_subject_refs):
                proposal_subject_refs[id(proposal)] = subject_ref
        elif decision.action == "extract":
            urls = [str(url).strip() for url in decision.urls if str(url).strip()]
            if not urls or len(urls) > MAX_WEB_EXTRACT_URLS or extract_calls_used >= MAX_WEB_EXTRACT_CALLS:
                failures.append("extract_budget_invalid")
                continue
            if tool_calls_used >= MAX_WEB_TOTAL_TOOL_CALLS:
                failures.append("tool_budget_exhausted")
                continue
            proposals = [ToolProposal(
                tool_name="web.extract",
                arguments={"urls": urls, "query": decision.extract_query},
            )]
            proposal_subject_refs[id(proposals[0])] = decision.subject_ref
        else:
            failures.append("unknown_web_action")
            aggregate.error = "web_action_invalid"
            continue

        execute_proposals(proposals, proposal_subject_refs)

    finish_phase_failed = not observations
    if observations:
        for finish_attempt_index in range(MAX_WEB_FINISH_DECISION_ATTEMPTS):
            finish_decision, finish_metrics = web_agent.decide(
                context_slice,
                task_brief=task.task_brief,
                round_index=MAX_WEB_RESEARCH_DECISION_ITERATIONS + finish_attempt_index + 1,
                observations=observations,
                tool_calls_used=tool_calls_used,
                search_calls_used=search_calls_used,
                extract_calls_used=extract_calls_used,
                place_candidates=place_candidates,
                evidence_policy=evidence_policy,
                finish_only=True,
            )
            accumulate_metrics(finish_metrics)
            if aggregate.error and not finish_metrics.error:
                aggregate.error = ""
            if finish_decision is None:
                finish_phase_failed = True
                continue
            last_decision = finish_decision
            if (
                finish_decision.assessment is not None
                and finish_decision.assessment.target_alignment == "conflict"
            ):
                result = build_research_result(
                    research_question=context_slice.payload.get("message", ""),
                    answer_target=task.task_brief,
                    decision=finish_decision,
                    observations=observations,
                    execution_status="degraded",
                    stop_reason="target_conflict",
                    allowed_subject_refs=allowed_subject_refs if place_candidates else None,
                    evidence_policy=evidence_policy,
                )
                return _completed_result(task, result), aggregate
            if finish_decision.action != "finish" or finish_decision.assessment is None:
                finish_phase_failed = True
                continue
            stop_reason = "tool_failure" if failures and not observations else "evidence_sufficient"
            result = build_research_result(
                research_question=context_slice.payload.get("message", ""),
                answer_target=task.task_brief,
                decision=finish_decision,
                observations=observations,
                execution_status="degraded" if failures else "completed",
                stop_reason=stop_reason,
                allowed_subject_refs=allowed_subject_refs if place_candidates else None,
                evidence_policy=evidence_policy,
            )
            return _completed_result(task, result), aggregate

    result = build_research_result(
        research_question=context_slice.payload.get("message", ""),
        answer_target=task.task_brief,
        decision=last_decision,
        observations=observations,
        execution_status="degraded" if failures else "completed",
        stop_reason=(
            "model_failure"
            if finish_phase_failed or tool_calls_used < MAX_WEB_TOTAL_TOOL_CALLS
            else "budget_exhausted"
        ),
        allowed_subject_refs=allowed_subject_refs if place_candidates else None,
        evidence_policy=evidence_policy,
    )
    return _completed_result(task, result), aggregate
