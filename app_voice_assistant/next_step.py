"""One optional, owner-scoped next step after a verified place recommendation."""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any


OFFER_TTL_SECONDS = 90


def _compact(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[\s，,。.!！?？、]+", "", text)


def _calendar_range(question: str) -> tuple[str, str, str]:
    compact = _compact(question)
    if any(word in compact for word in ("下週", "下周", "nextweek")):
        return "next_week", "下週", "next week"
    if any(word in compact for word in ("週末", "周末", "weekend")):
        return "weekend", "這週末", "this weekend"
    if any(word in compact for word in ("明天", "tomorrow")):
        return "tomorrow", "明天", "tomorrow"
    if any(word in compact for word in ("今天", "today")):
        return "today", "今天", "today"
    if any(word in compact for word in ("這週", "这周", "thisweek")):
        return "week", "這週", "this week"
    return "week", "這週", "this week"


def build_place_next_step_offer(
    proposal: Any,
    result: dict[str, Any],
    permissions: dict[str, Any],
    *,
    response_language: str = "zh-TW",
    now: float | None = None,
) -> dict[str, Any] | None:
    """Offer only available reads after a successful, structured places result."""
    if (
        getattr(proposal, "intent", "") != "ayue.public_query"
        or getattr(proposal, "arguments", {}).get("domain") != "places"
        or result.get("status") not in {"success", "ok"}
    ):
        return None
    recommendations = (result.get("data") or {}).get("recommendations") or []
    if not isinstance(recommendations, list) or not any(
        isinstance(item, dict) and str(item.get("name") or "").strip()
        for item in recommendations
    ):
        return None
    calendar_enabled = permissions.get("calendar_read") is True
    companion_enabled = permissions.get("match_ayue") is True
    if not calendar_enabled and not companion_enabled:
        return None
    calendar_range, zh_range, en_range = _calendar_range(
        str(getattr(proposal, "arguments", {}).get("question") or "")
    )
    if response_language == "en-US":
        if calendar_enabled and companion_enabled:
            prompt = (
                f"Want me to check your calendar for {en_range}, "
                "or ask Matching Ayue who might join you?"
            )
        elif calendar_enabled:
            prompt = f"Want me to check your calendar for {en_range}?"
        else:
            prompt = "Want me to ask Matching Ayue who might join you?"
    elif response_language == "zh-CN":
        if calendar_enabled and companion_enabled:
            prompt = f"要我查{zh_range}的日历，或请配对阿月推荐同行的人吗？"
        elif calendar_enabled:
            prompt = f"要我查{zh_range}的日历吗？"
        else:
            prompt = "要我请配对阿月推荐同行的人吗？"
    else:
        if calendar_enabled and companion_enabled:
            prompt = f"要我查{zh_range}的行事曆，或請配對阿月推薦同行人嗎？"
        elif calendar_enabled:
            prompt = f"要我查{zh_range}的行事曆嗎？"
        else:
            prompt = "要我請配對阿月推薦同行人嗎？"
    return {
        "prompt": prompt,
        "response_language": response_language,
        "calendar_range": calendar_range,
        "calendar_enabled": calendar_enabled,
        "companion_enabled": companion_enabled,
        "expires_at": (time.time() if now is None else now) + OFFER_TTL_SECONDS,
    }


def select_place_next_step(
    text: str, offer: dict[str, Any] | None, *, now: float | None = None,
) -> str | None:
    """Return an explicit choice; an ambiguous yes never starts a search."""
    if not isinstance(offer, dict):
        return None
    current = time.time() if now is None else now
    try:
        expires_at = float(offer.get("expires_at") or 0)
    except (TypeError, ValueError):
        return None
    if current > expires_at:
        return None
    compact = _compact(text)
    if not compact:
        return None
    if compact in {
        "不要", "不用", "不用了", "先不用", "算了", "取消", "沒事",
        "不要了", "不用謝", "no", "notnow", "nevermind", "cancel",
    }:
        return "dismiss"
    # A calendar edit is a new request, never consent to the offered read.
    if any(word in compact for word in (
        "新增", "建立", "加入", "修改", "編輯", "编辑", "更改", "調整",
        "刪除", "删除", "取消行程", "移除", "reschedule", "createevent",
        "editevent", "deleteevent", "cancelevent", "updateevent",
        "changecalendar", "movecalendar", "removeevent",
    )):
        return None
    if any(word in compact for word in (
        "配對進度", "配对进度", "配對狀態", "配对状态",
        "媒合進度", "媒合状态", "matchstatus", "matchingprogress",
        "matchingstatus",
    )):
        return None
    if any(word in compact for word in (
        "這個月", "这个月", "下個月", "下个月", "上個月", "上个月",
        "本月", "月底", "nextmonth", "lastmonth",
    )) or re.search(r"\d{1,4}[/\-年]\d{1,2}", compact):
        return None
    calendar = offer.get("calendar_enabled") is True and any(word in compact for word in (
        "行事曆", "日曆", "日历", "空檔", "空档", "有空", "行程",
        "calendar", "schedule", "freetime", "availability", "第一個",
        "第一个", "前者", "firstone",
    ))
    companion = offer.get("companion_enabled") is True and any(word in compact for word in (
        "同行", "和誰", "跟誰", "與誰", "和谁", "跟谁", "与谁",
        "配對阿月", "找人", "朋友一起", "旅伴", "companion", "joinme",
        "gowith", "matchingayue", "第二個", "第二个", "後者", "后者",
        "secondone",
    ))
    calendar_negated = bool(re.search(
        r"(?:不要|不用|別|别|先不)(?:幫我|帮我)?"
        r"(?:查|看|行事曆|日曆|日历|空檔|空档|calendar)",
        compact,
    ))
    companion_negated = bool(re.search(
        r"(?:不要|不用|別|别|先不)(?:幫我|帮我)?"
        r"(?:找同行|找人|同行|配對|配对|companion)",
        compact,
    ))
    if calendar_negated:
        calendar = False
    if companion_negated:
        companion = False
    if calendar and companion:
        return "clarify"
    if calendar:
        return "calendar"
    if companion:
        return "companion"
    if calendar_negated or companion_negated:
        return "dismiss"
    if compact in {"好", "好的", "可以", "好啊", "yes", "sure", "okay"}:
        if offer.get("calendar_enabled") is True and offer.get("companion_enabled") is True:
            return "clarify"
        return "calendar" if offer.get("calendar_enabled") is True else "companion"
    return None


def place_next_step_action(
    choice: str, offer: dict[str, Any], *, selected_text: str = "",
) -> tuple[str, dict[str, str]] | None:
    if choice == "calendar" and offer.get("calendar_enabled") is True:
        compact = _compact(selected_text)
        selected_range = (
            _calendar_range(selected_text)[0]
            if any(word in compact for word in (
                "今天", "明天", "週末", "周末", "下週", "下周",
                "這週", "这周", "today", "tomorrow", "weekend",
                "nextweek", "thisweek",
            )) else str(offer.get("calendar_range") or "week")
        )
        return "calendar.query", {
            "source": "google" if "google" in compact else "all",
            "range": selected_range,
        }
    if choice == "companion" and offer.get("companion_enabled") is True:
        return "match.ayue_query", {
            "question": (
                "我想知道剛才推薦的地點適合和哪位目前可聊天的聯絡人一起去。"
                "請依真實線索建議；若有多個地點需要選擇，請先問我。"
            ),
        }
    return None
