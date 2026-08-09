"""Typed, bounded research state for Public Ayue's Web specialist.

The Web tools remain ordinary Tool Registry capabilities.  This module owns
the research-workflow contract: answer-target preservation, evidence grading,
bounded source projection, and the explicit insufficient-evidence outcome.
"""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from services.ayue_agent.web_tools import is_safe_public_url


WebSourceType = Literal[
    "official", "news", "article", "forum", "community", "social", "other",
]
WebCoverage = Literal["direct_sufficient", "direct_partial", "adjacent_only", "none"]

MAX_WEB_ROUNDS = 3
MAX_WEB_INITIAL_SEARCH_QUERIES = 2
MAX_WEB_REFINED_SEARCH_QUERIES = 1
MAX_WEB_SEARCH_CALLS = 3
MAX_WEB_PARALLEL_SEARCHES = 2
MAX_WEB_EXTRACT_CALLS = 1
MAX_WEB_EXTRACT_URLS = 2
MAX_WEB_TOTAL_TOOL_CALLS = 3
MAX_WEB_SEARCH_RESULTS_PER_CALL = 5
MAX_WEB_PROMPT_SEARCH_RESULTS = 8
MAX_WEB_PROMPT_SNIPPET_CHARS = 600
MAX_WEB_EXTRACT_CHARS_PER_PAGE = 4000
MAX_WEB_RESEARCH_CONTEXT_CHARS = 12000
MAX_WEB_FINDINGS = 5
MAX_WEB_SOURCES = 8
MAX_WEB_SOURCES_PER_FINDING = 3
MAX_WEB_LIMITATIONS = 3
MAX_WEB_TOOL_QUERY_CHARS = 300
MAX_WEB_QUERY_ANCHOR_CHARS = 220


class WebEvidenceAssessmentV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_alignment: Literal["aligned", "conflict"] = "aligned"
    coverage: WebCoverage
    missing_evidence: str = Field(default="", max_length=300)


class WebResearchFindingDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=500)
    relation: Literal["direct", "adjacent_context"]
    source_urls: list[str] = Field(default_factory=list, max_length=MAX_WEB_SOURCES_PER_FINDING)
    source_types: list[WebSourceType] = Field(default_factory=list, max_length=MAX_WEB_SOURCES_PER_FINDING)


class WebResearchSourceV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    title: str = Field(min_length=1, max_length=200)
    source_type: WebSourceType


class WebResearchFindingV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=500)
    relation: Literal["direct", "adjacent_context"]
    source_urls: list[str] = Field(min_length=1, max_length=MAX_WEB_SOURCES_PER_FINDING)


class WebResearchResultV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["web_research.v1"] = "web_research.v1"
    research_question: str = Field(min_length=1, max_length=6000)
    answer_target: str = Field(min_length=1, max_length=500)
    status: Literal["answered", "partial", "insufficient_evidence"]
    execution_status: Literal["completed", "degraded", "unavailable"]
    coverage: WebCoverage
    findings: list[WebResearchFindingV1] = Field(default_factory=list, max_length=MAX_WEB_FINDINGS)
    sources: list[WebResearchSourceV1] = Field(default_factory=list, max_length=MAX_WEB_SOURCES)
    limitations: list[str] = Field(default_factory=list, max_length=MAX_WEB_LIMITATIONS)
    stop_reason: Literal[
        "evidence_sufficient", "partial_coverage", "no_direct_evidence",
        "budget_exhausted", "tool_unavailable", "tool_failure",
        "model_failure", "target_conflict",
    ]


def _clean(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def anchor_web_search_query(answer_target: str, suggested_query: str) -> str:
    """Keep every model-suggested query bound to the Planner's answer target."""
    anchor = _clean(answer_target, MAX_WEB_QUERY_ANCHOR_CHARS)
    suggestion = _clean(suggested_query, MAX_WEB_TOOL_QUERY_CHARS)
    if not anchor:
        return suggestion
    if not suggestion or suggestion.casefold() in anchor.casefold():
        return anchor[:MAX_WEB_TOOL_QUERY_CHARS]
    remaining = MAX_WEB_TOOL_QUERY_CHARS - len(anchor) - 1
    if remaining <= 0:
        return anchor[:MAX_WEB_TOOL_QUERY_CHARS]
    return f"{anchor} {suggestion[:remaining]}".strip()


def _safe_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if is_safe_public_url(url) else ""


def project_web_observations(observations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only bounded Web data that can be shown to the next model call."""
    projected: list[dict[str, Any]] = []
    result_count = 0
    char_count = 0
    for item in observations:
        if not isinstance(item, dict):
            continue
        tool = str(item.get("tool") or "")
        data = item.get("result") or {}
        if tool == "web.search":
            rows: list[dict[str, Any]] = []
            for row in (data.get("results") or [])[:MAX_WEB_SEARCH_RESULTS_PER_CALL]:
                url = _safe_url((row or {}).get("url"))
                if not url:
                    continue
                rows.append({
                    "title": _clean((row or {}).get("title"), 140) or urlsplit(url).hostname,
                    "url": url,
                    "snippet": _clean((row or {}).get("snippet"), MAX_WEB_PROMPT_SNIPPET_CHARS),
                    "published_date": _clean((row or {}).get("published_date"), 32),
                })
                result_count += 1
                char_count += len(rows[-1]["snippet"])
                if result_count >= MAX_WEB_PROMPT_SEARCH_RESULTS:
                    break
            if rows:
                projected.append({"tool": tool, "result": {"results": rows}})
        elif tool == "web.extract":
            pages: list[dict[str, Any]] = []
            for page in (data.get("pages") or [])[:MAX_WEB_EXTRACT_URLS]:
                url = _safe_url((page or {}).get("url"))
                if not url:
                    continue
                content = str((page or {}).get("content") or "")[:MAX_WEB_EXTRACT_CHARS_PER_PAGE]
                pages.append({"url": url, "content": content, "truncated": bool((page or {}).get("truncated"))})
                char_count += len(content)
            if pages:
                projected.append({"tool": tool, "result": {"pages": pages}})
        if result_count >= MAX_WEB_PROMPT_SEARCH_RESULTS:
            break
        if char_count >= MAX_WEB_RESEARCH_CONTEXT_CHARS:
            break
    return projected


def observed_source_catalog(observations: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Build a server-owned URL/title catalog from successful Web projections."""
    catalog: dict[str, dict[str, str]] = {}
    for item in project_web_observations(observations):
        tool = item.get("tool")
        data = item.get("result") or {}
        rows = data.get("results") if tool == "web.search" else data.get("pages")
        for row in rows or []:
            url = _safe_url((row or {}).get("url"))
            if not url or url in catalog:
                continue
            title = _clean((row or {}).get("title"), 200)
            if not title:
                title = _clean(urlsplit(url).hostname, 200) or url
            catalog[url] = {"url": url, "title": title}
    return catalog


def _default_limitation(coverage: WebCoverage) -> str:
    if coverage == "adjacent_only":
        return "找到的是相關背景資料，但沒有找到能直接支持使用者原始問題的指定證據。"
    if coverage == "none":
        return "目前沒有找到能直接支持使用者原始問題的公開證據。"
    return "目前證據只覆蓋問題的一部分，仍有重要內容未被直接證實。"


def build_research_result(
    *,
    research_question: str,
    answer_target: str,
    decision: Any | None,
    observations: list[dict[str, Any]],
    execution_status: Literal["completed", "degraded", "unavailable"],
    stop_reason: str,
    fallback_coverage: WebCoverage = "none",
) -> WebResearchResultV1:
    """Create and structurally validate the final server-owned result."""
    assessment = getattr(decision, "assessment", None)
    coverage: WebCoverage = getattr(assessment, "coverage", None) or fallback_coverage
    target_alignment = getattr(assessment, "target_alignment", "aligned")
    requested_status = getattr(decision, "status", None)
    findings_draft = list(getattr(decision, "findings", []) or [])
    limitation_values = [_clean(value, 300) for value in (getattr(decision, "limitations", []) or []) if _clean(value, 300)]
    catalog = observed_source_catalog(observations)
    sources_by_url: dict[str, WebResearchSourceV1] = {}
    findings: list[WebResearchFindingV1] = []

    for draft in findings_draft[:MAX_WEB_FINDINGS]:
        claim = _clean(getattr(draft, "claim", ""), 500)
        if not claim:
            continue
        direct_urls: list[str] = []
        source_types = list(getattr(draft, "source_types", []) or [])
        for index, raw_url in enumerate(list(getattr(draft, "source_urls", []) or [])[:MAX_WEB_SOURCES_PER_FINDING]):
            url = _safe_url(raw_url)
            if not url or url not in catalog or url in direct_urls:
                continue
            direct_urls.append(url)
            source_type = source_types[index] if index < len(source_types) else "other"
            source = WebResearchSourceV1(
                url=url,
                title=catalog[url]["title"],
                source_type=source_type,
            )
            sources_by_url.setdefault(url, source)
        if direct_urls:
            findings.append(WebResearchFindingV1(
                claim=claim,
                relation=getattr(draft, "relation", "adjacent_context"),
                source_urls=direct_urls,
            ))

    # A finish-decision parser failure must not erase the safe source catalog
    # from Web calls that already succeeded.  Claims still require a validated
    # finding; only observed URL/title projections survive this degraded path.
    if stop_reason == "model_failure" and catalog and not sources_by_url:
        for url, item in list(catalog.items())[:MAX_WEB_SOURCES]:
            sources_by_url[url] = WebResearchSourceV1(
                url=url,
                title=item["title"],
                source_type="other",
            )

    direct_findings = [item for item in findings if item.relation == "direct"]
    if target_alignment == "conflict":
        coverage = "none"
        requested_status = "insufficient_evidence"
        execution_status = "degraded"
        stop_reason = "target_conflict"
        findings = [item for item in findings if item.relation == "adjacent_context"]
        direct_findings = []
    elif requested_status == "answered" and coverage == "direct_sufficient" and direct_findings:
        requested_status = "answered"
    elif requested_status == "partial" and coverage == "direct_partial" and direct_findings:
        requested_status = "partial"
    elif requested_status == "insufficient_evidence" and coverage in {"adjacent_only", "none"}:
        requested_status = "insufficient_evidence"
    elif direct_findings and coverage == "direct_partial":
        requested_status = "partial"
    else:
        requested_status = "insufficient_evidence"
        coverage = "adjacent_only" if findings else "none"

    if requested_status == "answered":
        limitation_values = limitation_values[:MAX_WEB_LIMITATIONS]
        final_reason = "evidence_sufficient"
    elif requested_status == "partial":
        coverage = "direct_partial"
        if not limitation_values:
            limitation_values.append(_default_limitation(coverage))
        final_reason = "partial_coverage"
    else:
        if coverage not in {"adjacent_only", "none"}:
            coverage = "adjacent_only" if findings else "none"
        if not limitation_values:
            limitation_values.append(_default_limitation(coverage))
        final_reason = stop_reason if stop_reason in {
            "tool_unavailable", "tool_failure", "model_failure", "target_conflict", "budget_exhausted",
        } else "no_direct_evidence"

    if stop_reason == "tool_unavailable":
        final_reason = stop_reason
        execution_status = "unavailable"
    elif stop_reason == "model_failure":
        final_reason = stop_reason
        execution_status = "degraded" if catalog else "unavailable"
    elif stop_reason == "tool_failure" and execution_status == "unavailable":
        final_reason = stop_reason

    return WebResearchResultV1(
        research_question=_clean(research_question, 6000),
        answer_target=_clean(answer_target, 500),
        status=requested_status,
        execution_status=execution_status,
        coverage=coverage,
        findings=findings[:MAX_WEB_FINDINGS],
        sources=list(sources_by_url.values())[:MAX_WEB_SOURCES],
        limitations=limitation_values[:MAX_WEB_LIMITATIONS],
        stop_reason=final_reason,
    )
