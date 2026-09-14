"""Engine-neutral deterministic product-knowledge retrieval."""
from __future__ import annotations

from typing import Any

from services.ayue_agent.capabilities import (
    CAPABILITY_MANIFEST_VERSION,
    PRODUCT_KNOWLEDGE_VERSION,
    get_product_knowledge,
)


MAX_RETRIEVAL_ROUNDS = 2
MAX_KNOWLEDGE_SECTIONS = 6
_SECTION_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("capabilities.overview", ("能做什麼", "可以幹嘛", "可以幹啥", "功能", "capabilit", "what can you do")),
    ("relationship.date_invitation", ("邀請卡", "約會邀請", "空白卡", "幫我約", "date invitation")),
    ("relationship.date_coordination_cancel", ("取消約會", "撤回約會", "取消邀請卡", "約會卡取消")),
    ("identity.overview", ("阿月是誰", "你是誰", "身份", "另一個阿月", "identity")),
    ("surfaces.public", ("主聊天室", "公開阿月", "public", "這裡")),
    ("surfaces.private", ("悄悄話", "雙人聊天室", "private", "私訊")),
    ("surfaces.context_boundary", ("跨入口", "跨聊天室", "互相看到", "看得到", "context")),
    ("privacy.relationship", ("對方看到", "隱私", "可見", "visibility")),
    ("matching.overview", ("配對", "媒合", "找人", "人選", "matching")),
    ("matching.methods", ("配對方式", "怎麼配對", "如何配到人", "matching methods")),
    ("matching.selection", ("怎麼挑", "怎麼選", "排序", "原理", "隨機", "rank")),
    ("matching.profile_usage", ("偏好", "喜好", "記得我", "我的資料", "近況", "profile")),
    ("matching.confirmation", ("確認", "核准", "開始搜尋", "開始找", "confirmation")),
    ("matching.search_flow", ("搜尋流程", "怎麼開始找", "search flow")),
    ("matching.limitations", ("沒有合適", "找不到人", "限制", "limitations")),
    ("calendar.confirmation", ("行事曆", "日曆", "行程", "新增活動", "修改活動", "刪除活動")),
    ("assessment.overview", ("性格", "測驗", "探索", "assessment", "basic", "deep")),
    ("assessment.lifecycle", ("取消測驗", "重新開始", "提交測驗", "commit", "restart")),
)


def _select_sections(text: str) -> list[str]:
    lowered = text.casefold()
    selected = [
        section_id for section_id, hints in _SECTION_HINTS
        if any(hint.casefold() in lowered for hint in hints)
    ]
    if any(term in lowered for term in ("約會卡", "約會邀請", "邀請卡", "date invitation")):
        selected.extend(("relationship.date_invitation", "relationship.date_coordination_cancel"))
    if (
        any(term in lowered for term in ("偏好", "喜好", "profile", "preference", "記得"))
        and any(term in lowered for term in ("確認", "confirm", "核准", "開始"))
    ):
        selected.extend(("matching.profile_usage", "matching.confirmation"))
    return list(dict.fromkeys(selected))[:MAX_KNOWLEDGE_SECTIONS]


def _supplement_sections(text: str, selected: list[str]) -> list[str]:
    lowered = text.casefold()
    supplements: list[str] = []
    if any(term in lowered for term in ("配對", "媒合", "match", "找人")):
        supplements.extend(("matching.overview", "matching.methods", "matching.limitations"))
    if any(term in lowered for term in ("悄悄話", "聊天室", "對方", "private", "visibility")):
        supplements.extend(("surfaces.context_boundary", "privacy.relationship"))
    if any(term in lowered for term in ("行事曆", "行程", "calendar")):
        supplements.append("calendar.confirmation")
    if any(term in lowered for term in ("測驗", "探索", "assessment")):
        supplements.append("assessment.lifecycle")
    return [item for item in dict.fromkeys(supplements) if item not in selected]


def retrieve_product_info(query: str) -> dict[str, Any]:
    """Retrieve bounded product facts without a Planner, subagent or model."""
    text = str(query or "").strip()[:800]
    selected = _select_sections(text)
    rounds: list[dict[str, Any]] = []
    retrieved: dict[str, Any] = {
        "schema_version": PRODUCT_KNOWLEDGE_VERSION,
        "requested_sections": [], "knowledge_sections": [],
        "unknown_sections": [], "coverage": "insufficient",
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
        for item in sections if isinstance(item, dict) and item.get("section_id")
    }
    legacy_topics: list[str] = []
    if "matching_principles" in text.casefold():
        legacy_topics = ["matching_principles"]
        matching_projection = get_product_knowledge(
            ["matching.overview", "matching.selection"], max_sections=2,
        )
        facts["matching"] = {
            key: value
            for item in (matching_projection.get("knowledge_sections") or [])
            if isinstance(item, dict)
            for key, value in (item.get("facts") or {}).items()
        }
    domains = list(dict.fromkeys(
        str(item.get("domain")) for item in sections
        if isinstance(item, dict) and item.get("domain")
    ))
    coverage = str(retrieved.get("coverage") or "insufficient")
    return {
        "schema_version": PRODUCT_KNOWLEDGE_VERSION,
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
        "retrieval": {"rounds": rounds, "max_rounds": MAX_RETRIEVAL_ROUNDS},
    }


__all__ = ["retrieve_product_info"]
