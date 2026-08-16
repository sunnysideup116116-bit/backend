"""Pure display projection for profile state.

This module intentionally has no database or model-provider imports.  It is
safe for public-agent context construction and only removes unsafe legacy
state; it does not decide whether a new owner message is meaningful.
"""

from __future__ import annotations

import re
from typing import Any

from services.language_service import normalize_zh_tw


INTERNAL_ID_RE = re.compile(r"(?:@?(?:seed_user(?:_[\w-]+)?|demo_user|user[_-]?\d+))", re.IGNORECASE)
PROTECTED_CONTENT_RE = re.compile(
    r"(?:黑人|白人|黃種人|種族|族裔|宗教|信仰|穆斯林|基督教|同性戀|性傾向|性別認同|跨性別|殘障|身心障礙|疾病|政治立場|國籍|公民身分)",
    re.IGNORECASE,
)
# This is a legacy-display contamination check, not a profile-extraction gate.
LEGACY_CONTEXT_CONTAMINATION_RE = re.compile(
    r"(?:約會邀請|進行中的提案|等待(?:對方)?回覆|媒合(?:提案|進度)?|配對|翻名單|瞭解配對(?:物件|對象)|幫我找人|找下一(?:個|位)|行事曆|calendar|match(?:ing)?|pending|accepted)",
    re.IGNORECASE,
)


def clean_profile_text(value: Any, limit: int = 60) -> str:
    return normalize_zh_tw(str(value or ""), max_length=limit).strip(" \"'：:，,。")


def contains_internal_identifier(value: Any) -> bool:
    return bool(INTERNAL_ID_RE.search(str(value or "")))


def contains_protected_content(value: Any) -> bool:
    return bool(PROTECTED_CONTENT_RE.search(str(value or "")))


def safe_recent_context(value: Any, fallback: str = "") -> str:
    """Return a user-safe recent-context display string or the fallback."""
    text = clean_profile_text(value, 48)
    if (
        not text
        or contains_internal_identifier(text)
        or contains_protected_content(text)
        or LEGACY_CONTEXT_CONTAMINATION_RE.search(text)
    ):
        return fallback
    duplicate = re.fullmatch(
        r"(?P<prefix>近期|本週|下週|本月|下個月)規劃前往(?P<destination>.{1,16}?)(?:去|前往)(?P=destination)",
        text,
    )
    if duplicate:
        text = f"{duplicate.group('prefix')}想去{duplicate.group('destination')}"
    # Old records had no tense field, so an activity-only summary was always
    # rendered as a future plan.  Keep legacy reads neutral rather than making
    # an unsupported claim about the owner's intent.
    legacy_activity = re.fullmatch(r"近期規劃(?!前往)(?P<activity>.{1,36})", text)
    if legacy_activity:
        activity = re.sub(
            r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])",
            "、",
            legacy_activity.group("activity"),
        )
        text = f"近期活動：{activity}"
    return text or fallback


def render_recent_context(fields: dict[str, Any]) -> str:
    """Deterministic Traditional-Chinese fallback for verified typed fields."""
    def value(name: str, limit: int) -> str:
        raw = fields.get(name) or {}
        raw = raw.get("value") if isinstance(raw, dict) else raw
        return clean_profile_text(raw, limit)

    activity = value("activity", 28)
    destination = value("destination", 28)
    timing = value("timing", 8)
    companion = value("companion_intent", 20)
    temporal_status = value("temporal_status", 12)
    if not activity and not destination:
        return ""
    prefix = timing if timing in {"昨天", "前天", "今天", "明天", "後天", "最近", "近期", "本週", "下週", "本月", "下個月"} else "近期"
    if destination:
        if temporal_status == "past":
            if activity and destination in activity:
                summary = f"{prefix}有{activity}"
            elif not activity:
                summary = f"{prefix}有去{destination}"
            else:
                summary = f"{prefix}有去{destination}{activity}"
        elif temporal_status == "current":
            summary = f"{prefix}在{activity or f'去{destination}'}"
        elif temporal_status == "planned":
            if activity and destination in activity:
                summary = f"{prefix}想{activity}"
            elif not activity:
                summary = f"{prefix}想去{destination}"
            else:
                suffix = "旅行" if activity in {"旅行", "旅遊", "玩"} else activity
                summary = f"{prefix}規劃前往{destination}{suffix}"
        else:
            summary = f"近期活動：{activity or destination}"
    else:
        if temporal_status == "past":
            summary = f"{prefix if timing else '最近'}有在{activity}"
        elif temporal_status == "current":
            summary = f"{prefix if timing else '最近'}在{activity}"
        elif temporal_status == "planned":
            summary = f"{prefix}想{activity}"
        else:
            summary = f"近期活動：{activity}"
    if companion == "自己":
        summary += "，想自己去"
    elif companion == "找人同行":
        summary += "，想找人同行"
    return summary[:48]
