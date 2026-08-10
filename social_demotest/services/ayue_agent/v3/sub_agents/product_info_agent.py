"""Bounded ProductInfo specialist for the public V3 DAG.

ProductInfo is intentionally not a FAQ responder.  It understands a task
brief, selects a small set of allowlisted product-knowledge sections, and
returns a typed observation for the Synthesizer.  Retrieval is deterministic
and read-only; no raw manifest, database document, prompt, or internal ID is
ever exposed.
"""

from __future__ import annotations

import json
import time
from typing import Any

from services.ayue_agent.capabilities import (
    CAPABILITY_MANIFEST_VERSION,
    PRODUCT_KNOWLEDGE_VERSION,
    get_product_knowledge,
)
from ..contracts import AgentContextSlice
from .base import StructuredAgentResult, SubAgentMetrics


MAX_RETRIEVAL_ROUNDS = 2
MAX_KNOWLEDGE_SECTIONS = 6


# This is an internal retrieval hint table, not the Planner's intent router.
# It only maps a ProductInfo task to safe section IDs; all truth still comes
# from ``get_product_knowledge``.  Terms are grouped by product concept so a
# semantic question can retrieve more than one section in one bounded pass.
_SECTION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("capabilities.overview", ("能做什麼", "可以幹嘛", "你是做什麼", "功能", "capabilit", "what can you do", "capabilities")),
    ("identity.overview", ("阿月是誰", "你是誰", "身份", "同一個", "另一個阿月", "same ayue", "same_identity", "identity")),
    ("surfaces.public", ("主聊天室", "公開阿月", "public", "這裡", "surface_scope")),
    ("surfaces.private", ("悄悄話", "雙人聊天室", "private", "私訊", "where_to_ask")),
    ("surfaces.context_boundary", ("跨入口", "跨聊天室", "完整對話", "互相看到", "context", "看得到", "cross_surface_context")),
    ("privacy.relationship", ("對方看到", "隱私", "可見", "visibility", "private message", "private_message_visibility", "relationship_chat_access")),
    ("matching.overview", ("配對", "媒合", "找人", "人選", "match", "matching")),
    ("matching.selection", ("怎麼挑", "怎麼選", "排序", "原理", "抽籤", "隨機", "rank", "select", "matching_principles")),
    ("matching.profile_usage", ("偏好", "喜好", "記得我", "我的資料", "profile", "preference", "already knows", "近況")),
    ("matching.confirmation", ("確認", "核准", "開始搜尋", "開始找", "confirmation", "confirm")),
    ("matching.search_flow", ("搜尋流程", "search flow", "怎麼開始找", "找得到")),
    ("matching.limitations", ("沒有合適", "找不到人", "限制", "limitations", "不一定")),
    ("calendar.confirmation", ("行事曆", "日曆", "calendar", "行程", "新增活動", "修改活動", "刪除活動")),
    ("assessment.overview", ("性格", "測驗", "探索", "assessment", "basic", "deep")),
    ("assessment.lifecycle", ("取消測驗", "重新開始", "提交測驗", "commit", "restart", "cancel")),
)

# Keep the user-language aliases as escaped Unicode so this module remains
# stable even when a Windows console is using a legacy code page.
_SECTION_HINTS += (
    ("capabilities.overview", ("\u80fd\u505a\u4ec0\u9ebc", "\u53ef\u4ee5\u5e79\u561b", "\u529f\u80fd")),
    ("identity.overview", ("\u963f\u6708\u662f\u8ab0", "\u4f60\u662f\u8ab0", "\u8eab\u4efd", "\u540c\u4e00\u500b", "\u53e6\u4e00\u500b\u963f\u6708")),
    ("surfaces.public", ("\u4e3b\u804a\u5929\u5ba4", "\u516c\u958b\u963f\u6708")),
    ("surfaces.private", ("\u6084\u6084\u8a71", "\u96d9\u4eba\u804a\u5929\u5ba4", "\u79c1\u8a0a")),
    ("surfaces.context_boundary", ("\u8de8\u5165\u53e3", "\u8de8\u804a\u5929\u5ba4", "\u5b8c\u6574\u5c0d\u8a71", "\u4e92\u76f8\u770b\u5230", "\u770b\u5f97\u5230")),
    ("privacy.relationship", ("\u5c0d\u65b9\u770b\u5230", "\u96b1\u79c1", "\u53ef\u898b")),
    ("matching.overview", ("\u914d\u5c0d", "\u5a92\u5408", "\u627e\u4eba", "\u4eba\u9078")),
    ("matching.selection", ("\u600e\u9ebc\u6311", "\u600e\u9ebc\u9078", "\u6392\u5e8f", "\u539f\u7406", "\u62bd\u7c64", "\u96a8\u6a5f")),
    ("matching.profile_usage", ("\u504f\u597d", "\u559c\u597d", "\u8a18\u5f97\u6211", "\u6211\u7684\u8cc7\u6599", "\u8fd1\u6cc1")),
    ("matching.confirmation", ("\u78ba\u8a8d", "\u6838\u51c6", "\u958b\u59cb\u641c\u5c0b", "\u958b\u59cb\u627e")),
    ("matching.search_flow", ("\u641c\u5c0b\u6d41\u7a0b", "\u600e\u9ebc\u958b\u59cb\u627e")),
    ("matching.limitations", ("\u6c92\u6709\u5408\u9069", "\u627e\u4e0d\u5230\u4eba", "\u9650\u5236")),
    ("calendar.confirmation", ("\u884c\u4e8b\u66c6", "\u65e5\u66c6", "\u884c\u7a0b", "\u65b0\u589e\u6d3b\u52d5", "\u4fee\u6539\u6d3b\u52d5", "\u522a\u9664\u6d3b\u52d5")),
    ("assessment.overview", ("\u6027\u683c", "\u6e2c\u9a57", "\u63a2\u7d22")),
    ("assessment.lifecycle", ("\u53d6\u6d88\u6e2c\u9a57", "\u91cd\u65b0\u958b\u59cb", "\u63d0\u4ea4\u6e2c\u9a57")),
)


def _brief_text(context_slice: AgentContextSlice, task_brief: str) -> str:
    payload = context_slice.payload if isinstance(context_slice.payload, dict) else {}
    # The task brief is the Planner-owned proposition.  The current message is
    # useful only as a bounded fallback for older provider trajectories.
    return str(task_brief or payload.get("message") or "").strip()[:800]


def _select_sections(text: str) -> list[str]:
    lowered = text.casefold()
    selected: list[str] = []
    for section_id, hints in _SECTION_HINTS:
        if any(hint.casefold() in lowered for hint in hints):
            selected.append(section_id)

    # Questions about why confirmation is needed despite known preferences
    # require both facts even when the provider paraphrases one half.
    profile_terms = ("偏好", "喜好", "profile", "preference", "記得", "known")
    confirmation_terms = ("確認", "confirm", "核准", "開始")
    if any(term in lowered for term in profile_terms) and any(term in lowered for term in confirmation_terms):
        selected.extend(("matching.profile_usage", "matching.confirmation"))

    return list(dict.fromkeys(selected))[:MAX_KNOWLEDGE_SECTIONS]


def _supplement_sections(text: str, selected: list[str]) -> list[str]:
    """Choose one bounded second-round supplement for incomplete coverage."""
    lowered = text.casefold()
    supplements: list[str] = []
    if any(term in lowered for term in ("配對", "媒合", "match", "找人")):
        supplements.extend(("matching.overview", "matching.limitations"))
    if any(term in lowered for term in ("悄悄話", "聊天室", "對方", "private", "visibility")):
        supplements.extend(("surfaces.context_boundary", "privacy.relationship"))
    if any(term in lowered for term in ("行事曆", "行程", "calendar")):
        supplements.append("calendar.confirmation")
    if any(term in lowered for term in ("測驗", "探索", "assessment")):
        supplements.append("assessment.lifecycle")
    return [section_id for section_id in dict.fromkeys(supplements) if section_id not in selected]


def retrieve_product_info(task_brief: str) -> dict[str, Any]:
    """Understand and retrieve a ProductInfo task in at most two rounds."""
    text = str(task_brief or "").strip()[:800]
    selected = _select_sections(text)
    rounds: list[dict[str, Any]] = []
    retrieved: dict[str, Any] = {
        "schema_version": PRODUCT_KNOWLEDGE_VERSION,
        "requested_sections": [],
        "knowledge_sections": [],
        "unknown_sections": [],
        "coverage": "insufficient",
    }
    for round_index in range(MAX_RETRIEVAL_ROUNDS):
        if not selected:
            break
        retrieved = get_product_knowledge(selected, max_sections=MAX_KNOWLEDGE_SECTIONS)
        rounds.append({
            "round": round_index + 1,
            "requested_sections": list(retrieved.get("requested_sections") or []),
            "coverage": retrieved.get("coverage"),
        })
        if retrieved.get("coverage") == "sufficient":
            break
        extra = _supplement_sections(text, selected)
        if not extra:
            break
        selected = (selected + extra)[:MAX_KNOWLEDGE_SECTIONS]

    sections = list(retrieved.get("knowledge_sections") or [])
    facts = {
        str(item.get("section_id")): item.get("facts")
        for item in sections
        if isinstance(item, dict) and item.get("section_id")
    }
    legacy_topics: list[str] = []
    if "matching_principles" in text.casefold():
        legacy_topics = ["matching_principles"]
        matching_projection = get_product_knowledge(
            ["matching.overview", "matching.selection"], max_sections=2,
        )
        matching_facts = matching_projection.get("knowledge_sections") or []
        facts["matching"] = {
            key: value
            for item in matching_facts
            if isinstance(item, dict)
            for key, value in (item.get("facts") or {}).items()
        }
    domains = list(dict.fromkeys(
        str(item.get("domain"))
        for item in sections
        if isinstance(item, dict) and item.get("domain")
    ))
    coverage = str(retrieved.get("coverage") or "insufficient")
    return {
        "schema_version": PRODUCT_KNOWLEDGE_VERSION,
        # Kept only for old Synthesizer trajectories during rollout.  New code
        # uses knowledge_sections and section-keyed facts below.
        "manifest_version": CAPABILITY_MANIFEST_VERSION,
        "topics": legacy_topics,
        "question_understanding": {
            "request_shape": "product_behavior_question",
            "domains": domains,
            "retrieval_rounds": len(rounds),
        },
        "facts": facts,
        "knowledge_sections": [str(item.get("section_id")) for item in sections],
        "unknown_sections": list(retrieved.get("unknown_sections") or []),
        "coverage": coverage,
        "failure_code": None if coverage == "sufficient" else "product_knowledge_insufficient",
        "retrieval": {
            "rounds": rounds,
            "max_rounds": MAX_RETRIEVAL_ROUNDS,
        },
    }


def run(context_slice: AgentContextSlice, *, task_brief: str) -> tuple[StructuredAgentResult, SubAgentMetrics]:
    """Return a structured observation; never return user-facing prose."""
    started = time.perf_counter()
    metrics = SubAgentMetrics()
    text = _brief_text(context_slice, task_brief)
    metrics.input_payload = {
        "message": str(context_slice.payload.get("message") or "")[:800],
        "task_brief": text,
    }
    observation = retrieve_product_info(text)
    metrics.prompt_raw = "PRODUCT_INFO_INTERNAL_RETRIEVAL\n" + json.dumps(
        {"task_brief": text, "max_rounds": MAX_RETRIEVAL_ROUNDS},
        ensure_ascii=False,
    )
    metrics.duration_ms = round((time.perf_counter() - started) * 1000)
    return StructuredAgentResult(observation={"product_info": observation}), metrics
