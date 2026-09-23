from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any
from matchmaker_agent.concept_identity import PreferenceTextError, normalize_preference_text

from .capabilities import ACTIONS
from .contextual import safe_screen, REF


ALLOWED_INTENTS = frozenset(ACTIONS)
PROFILE_FIELDS = frozenset({
    "name", "phone", "age", "region", "city", "district", "userinfo",
})
SETTING_KEYS = frozenset({
    "notifications.global",
    "location.enabled",
    "ai.proactive_care",
    "ui.liquid_glass",
    "ui.dark_mode",
})
CONFIRMED_SETTING_KEYS = frozenset({
    "notifications.global", "location.enabled", "ai.proactive_care",
})
VOICE_PERMISSION_KEYS = frozenset({
    "navigation", "screen_read", "profile", "settings", "post_draft",
    "post_publish", "gallery", "match_read", "match_actions", "match_ayue",
    "public_ayue", "private_ayue", "chat_list", "chat_content", "chat_send",
    "calendar_read", "calendar_write", "web_search", "places",
    "location_precise", "memory_read", "memory_write", "status_read",
    "safety_actions",
})
SENSITIVE_PERMISSION_DEFAULTS = frozenset({
    "match_actions", "private_ayue", "chat_content", "chat_send",
    "calendar_write", "location_precise", "memory_write",
    "safety_actions",
})
NAVIGATION_DESTINATIONS = frozenset({
    "chat", "matching", "profile", "settings", "profile_edit",
    "voice_settings", "calendar", "matching_ayue", "match_hub",
    "memory", "create_post",
    "blocked_users",
})
PUBLIC_AYUE_DOMAINS = frozenset({
    "matching", "web", "places", "memory", "profile",
})
CALENDAR_QUERY_RANGES = frozenset({
    "today", "tomorrow", "week", "weekend", "next_week", "upcoming",
})
CALENDAR_SOURCES = frozenset({"all", "personal", "google"})
VOICE_NAMES = frozenset({
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus",
    "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel",
    "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi",
    "Laomedeia", "Achernar", "Alnilam", "Schedar", "Gacrux",
    "Pulcherrima", "Achird", "Zubenelgenubi", "Vindemiatrix",
    "Sadachbia", "Sadaltager", "Sulafat",
})
VOICE_SPEEDS = frozenset({"slow", "normal", "fast"})
VOICE_LANGUAGES = frozenset({"zh-TW", "zh-CN", "en-US"})
APP_SEARCH_DOMAINS = frozenset({
    "contacts", "calendar", "shared_dates", "memory", "matching",
})
ROUTINE_TEMPLATES = frozenset({
    "daily_briefing", "open_match_hub", "open_calendar",
    "search_app", "search_memory",
})
FEATURE_STATUS_KEYS = frozenset({
    "voice_experience",
    "conversation_drafts",
    "operational_status",
    "assistant_enabled", "wake_word_enabled", "location_enabled",
    "notifications_enabled", "proactive_care_enabled", "liquid_glass_enabled",
    "dark_mode_enabled", "visible_choice_pending",
})
TAIWAN_CITIES = (
    "台北市", "新北市", "桃園市", "台中市", "台南市", "高雄市", "基隆市",
    "新竹市", "嘉義市", "新竹縣", "苗栗縣", "彰化縣", "南投縣", "雲林縣",
    "嘉義縣", "屏東縣", "宜蘭縣", "花蓮縣", "台東縣", "澎湖縣", "金門縣",
    "連江縣",
)
_EMAIL_RE = re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")
_PHONE_RE = re.compile(r"(?<!\d)09\d{8}(?!\d)")
_ROUTINE_NAME_RE = re.compile(r"[A-Za-z0-9_\- \u4e00-\u9fff]{1,30}")
_PASSWORD_RE = re.compile(
    r"((?:密碼|密码|password)\s*(?:是|為|为|=|:|：)?\s*)[^\s，,。；;]{1,128}",
    re.IGNORECASE,
)
_CALENDAR_EXPLICIT_DATE_RE = re.compile(
    r"(?<!\d)(\d{4})\s*(?:年|[-/.])\s*(\d{1,2})\s*"
    r"(?:月|[-/.])\s*(\d{1,2})\s*日?",
)


@dataclass(frozen=True)
class VoiceProposal:
    intent: str
    arguments: dict[str, Any]
    reply: str
    base_revision: int


def normalized_phrase(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"[\s，,。.!！?？、]+", "", text)


def _weather_location(value: str) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    lower = text.lower()
    weather_terms = (
        "天氣", "天气", "氣象", "气象", "氣溫", "气温", "溫度", "温度",
        "會下雨", "会下雨", "空氣品質", "空气质量", "aqi",
        "weather", "air quality",
    )
    if not any(term in lower for term in weather_terms):
        return None
    compact = normalized_phrase(text)
    lookup_suffixes = (
        "天氣", "天气", "氣象", "气象", "氣溫", "气温", "溫度", "温度",
        "空氣品質", "空气质量", "aqi", "weather", "airquality",
    )
    query_markers = (
        "如何", "怎麼樣", "怎么样", "多少", "查", "看", "想知道",
        "告訴", "告诉", "會不會", "会不会", "嗎", "吗", "?", "？",
    )
    if (
        not any(marker in lower for marker in query_markers)
        and not any(compact.endswith(suffix) for suffix in lookup_suffixes)
        and not lower.startswith(("weather", "air quality"))
    ):
        return None
    if (
        any(term in text for term in ("貼文", "发文", "發文", "寫一篇", "写一篇"))
        or any(term in lower for term in ("喜歡這種天氣", "喜欢这种天气"))
    ):
        return None
    location = re.sub(
        r"(?:幫我|帮我|請|请|可以|能不能|想知道|告訴我|告诉我|"
        r"查詢|查询|查一下|看一下|查|看)",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    location = re.sub(
        r"(?:今天|今日|現在|现在|目前|此刻|當地|当地|即時|实时|的)",
        " ",
        location,
        flags=re.IGNORECASE,
    )
    location = re.sub(
        r"(?:天氣|天气|氣象|气象|氣溫|气温|溫度|温度|空氣品質|空气质量|"
        r"aqi|weather|air\s*quality|會不會下雨|会不会下雨|會下雨|会下雨|"
        r"怎麼樣|怎么样|如何|多少|好不好|很好|好嗎|好吗|嗎|吗|\bin\b)",
        " ",
        location,
        flags=re.IGNORECASE,
    )
    # Removing two requested weather subjects can leave a standalone joiner
    # (for example, "台北天氣與空氣品質" -> "台北 與").  Strip only
    # whitespace-delimited joiners so real place names such as 中和區 remain intact.
    location = re.sub(
        r"(?:^|\s)(?:與|与|和|及|以及|跟|還有|还有|and)(?=\s|$)",
        " ",
        location,
        flags=re.IGNORECASE,
    )
    return re.sub(r"\s+", " ", location).strip(" ，,。.!！?？、")[:120]


def _explicit_calendar_range(value: str) -> tuple[str, str] | None:
    matches = _CALENDAR_EXPLICIT_DATE_RE.findall(str(value or ""))
    parsed: list[date] = []
    for year, month, day_value in matches[:2]:
        try:
            parsed.append(date(int(year), int(month), int(day_value)))
        except ValueError:
            return None
    if not parsed:
        return None
    if len(parsed) == 1:
        parsed.append(parsed[0])
    if parsed[1] < parsed[0]:
        return None
    return parsed[0].isoformat(), parsed[1].isoformat()


def _calendar_source(value: str) -> str:
    compact = normalized_phrase(value)
    if any(marker in compact for marker in (
        "google日曆", "google行事曆", "googlecalendar", "googlecalender",
    )):
        return "google"
    if any(marker in compact for marker in (
        "app行事曆", "阿月行事曆", "個人行事曆", "个人日历",
        "自己的行事曆", "自己的日曆", "自己行事曆", "自己日曆",
        "我的行事曆", "我的日曆", "本機行事曆", "本機日曆",
        "personal行事曆", "personal日曆", "personalcalendar",
        "mycalendar", "myappcalendar",
    )):
        return "personal"
    return "all"


def _is_match_start_request(value: str) -> bool:
    compact = normalized_phrase(value)
    if re.search(r'(?:不要|不用|不想)(?:再|幫我|替我)?(?:找|配對)', compact):
        return False
    if any(phrase in compact for phrase in (
        '不要配對', '不想配對', '不要找', '不用找', '取消配對',
        "don'tfind", 'donotfind', 'stopmatching', 'cancelmatching',
    )):
        return False
    if '配對' in compact and any(term in compact for term in ('新的人', '新人', '新對象')):
        return True
    if compact in {
        "新的配對", "新配對", "新的配對建議", "找個新配對",
        "newmatch", "newmatching", "newmatchsuggestion",
    }:
        return True
    return any(phrase in compact for phrase in (
        "幫我找新的配對", "找新的配對", "找新配對",
        "尋找新的配對", "尋找新配對", "尋找配對對象",
        "幫我找新對象", "找新對象", "尋找新對象",
        "開始新的配對", "開始新一輪配對", "開始配對",
        "開始配對搜尋", "開始找對象", "開始尋找對象",
        "幫我找配對", "找配對", "重新配對", "重新找對象",
        "媒合新對象", "幫我配對", "我要配對",
        "想找人一起", "找人一起", "找個人一起", "找對象一起",
        "想找旅伴", "找旅伴", "尋找旅伴",
        "findnewmatch", "findanewmatch", "findmeanewmatch",
        "searchforanewmatch", "startmatching", "startnewmatch",
        "startanewmatch", "startnewmatchsearch", "startanewmatchsearch",
        "findmatchingpartner", "findnewpartner", "findmeanewpartner",
        "matchmewithsomeone",
    ))


def _current_contact(context: dict[str, Any]) -> tuple[str, str]:
    screen = context.get("screen") if isinstance(context.get("screen"), dict) else {}
    items = [
        item for item in (screen.get("items") or [])
        if isinstance(item, dict) and item.get("kind") == "contact"
    ]
    selected_ref = str(screen.get("selected_ref") or "")
    selected = next(
        (item for item in items if item.get("ref") == selected_ref),
        items[0] if len(items) == 1 else None,
    )
    if selected is not None:
        return (
            str(selected.get("label") or "")[:40],
            str(selected.get("ref") or "")[:80],
        )
    feature_status = context.get("feature_status")
    if isinstance(feature_status, dict):
        return str(feature_status.get("contact_name") or "")[:40], ""
    return "", ""


def visible_choice_action(value: Any) -> str | None:
    compact = normalized_phrase(value)
    if compact in {
        "確認", "確定", "同意", "好", "好的", "可以", "沒問題",
        "就這樣", "幫我確認", "請確認", "確認吧", "好啊",
        "開始", "開始吧", "那就開始", "那就開始吧", "我們開始吧",
        "可以開始", "可以開始吧", "可以開始了", "幫我開始", "請開始", "直接開始",
        "好喔", "好哦", "行", "來吧",
        "那我們開始吧", "好那開始吧", "好那我們開始吧",
        "重新開始", "重新開始探索", "開始探索",
        "繼續", "繼續吧", "繼續探索",
        "confirm", "confirmed", "yes", "ok", "okay",
        "start", "goahead", "begin", "proceed",
    }:
        return "confirm"
    if compact in {
        "取消", "不要", "不用", "不同意", "算了", "先不要",
        "這次先不用", "這次不用", "先不用",
        "幫我取消", "取消吧",
        "cancel", "cancelled", "no",
    }:
        return "cancel"
    # Whole-utterance grammar accepts conversational fillers, not arbitrary
    # sentences containing a confirmation word (e.g. 開始前我想問 or 不要開始).
    if re.fullmatch(
        r"(?:嗯+|好(?:的|啊)?|那(?:就)?|我們|請|麻煩(?:你)?|幫我)*"
        r"(?:可以|直接)?(?:確認|確定|同意|開始(?:個性探索|性格探索|探索)?|"
        r"重新開始(?:探索)?|繼續(?:探索)?|套用(?:探索結果|結果)?|儲存|就這樣)"
        r"(?:吧|啊|喔|哦|囉|了)*", compact,
    ):
        return "confirm"
    if re.fullmatch(
        r"(?:嗯+|那(?:就)?|請|麻煩(?:你)?|幫我|先)*"
        r"(?:取消|不要(?:開始|套用|儲存)?|不用(?:開始|套用|儲存)?|不同意|算了)"
        r"(?:吧|啊|喔|哦|囉|了)*", compact,
    ):
        return "cancel"
    return None


def safe_reply(value: Any) -> str:
    text = str(value or "").strip()[:240]
    text = _PASSWORD_RE.sub(r"\1[已隱藏]", text)
    text = _EMAIL_RE.sub("[Email 已隱藏]", text)
    return _PHONE_RE.sub("[電話已隱藏]", text)


def _routine_name(value: Any) -> str:
    name = re.sub(r"\s+", " ", str(value or "")).strip()[:30]
    return name if _ROUTINE_NAME_RE.fullmatch(name) else ""


def _small_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "兩": 2, "三": 3,
              "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        left, right = value.split("十", 1)
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    return digits.get(value)


def _current_calendar_target(context: dict[str, Any]) -> tuple[str, str]:
    """Return a single safe personal-calendar target from the current screen."""
    screen = context.get("screen") if isinstance(context.get("screen"), dict) else {}
    if screen.get("ready") is not True:
        return "", ""
    items = []
    for item in screen.get("items") or []:
        if not isinstance(item, dict) or item.get("kind") != "calendar_event":
            continue
        if not {
            "calendar.update",
            "calendar.cancel",
        }.intersection(item.get("actions") or []):
            continue
        attributes = item.get("attributes") if isinstance(item.get("attributes"), dict) else {}
        # Shared dates and Google events have their own write boundary. They
        # must never be silently converted into a personal-calendar update.
        if str(attributes.get("source_type") or "") in {"date", "google"}:
            continue
        items.append(item)
    selected_ref = str(screen.get("selected_ref") or "")
    selected = next((item for item in items if item.get("ref") == selected_ref), None)
    if selected is None and len(items) == 1:
        selected = items[0]
    if selected is None:
        return "", ""
    return (
        str(selected.get("label") or "")[:80],
        str(selected.get("ref") or "")[:80],
    )


def _calendar_update_date(raw: str) -> str | None:
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    if "後天" in raw or "后天" in raw:
        return (today + timedelta(days=2)).isoformat()
    if "明天" in raw:
        return (today + timedelta(days=1)).isoformat()
    if "今天" in raw or "今日" in raw:
        return today.isoformat()
    explicit = _CALENDAR_EXPLICIT_DATE_RE.search(raw)
    if explicit is None:
        explicit = re.search(r"(?<!\d)(\d{4})[-/](\d{1,2})[-/](\d{1,2})(?!\d)", raw)
    if explicit is None:
        return None
    try:
        return date(
            int(explicit.group(1)),
            int(explicit.group(2)),
            int(explicit.group(3)),
        ).isoformat()
    except ValueError:
        return None


def _calendar_update_clocks(raw: str) -> list[str]:
    values: list[tuple[int, str]] = []
    chinese_clock = re.compile(
        r"(上午|下午|晚上|傍晚|中午|凌晨)?\s*"
        r"([零〇一二兩三四五六七八九十\d]{1,3})\s*[點点時时]"
        r"(?:(半)|([零〇一二兩三四五六七八九十\d]{1,3})\s*分)?"
    )
    for match in chinese_clock.finditer(raw):
        hour = _small_number(match.group(2))
        minute = 30 if match.group(3) else _small_number(match.group(4) or "零")
        if hour is None or minute is None or hour > 23 or minute > 59:
            continue
        period = match.group(1) or ""
        if period in {"下午", "晚上", "傍晚"} and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        values.append((match.start(), f"{hour:02d}:{minute:02d}"))
    numeric_clock = re.compile(
        r"(?<!\d)(上午|下午|晚上|傍晚|中午|凌晨)?\s*"
        r"(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)"
    )
    for match in numeric_clock.finditer(raw):
        hour, minute = int(match.group(2)), int(match.group(3))
        if hour > 23 or minute > 59:
            continue
        period = match.group(1) or ""
        if period in {"下午", "晚上", "傍晚"} and hour < 12:
            hour += 12
        elif period == "中午" and hour < 11:
            hour += 12
        elif period == "凌晨" and hour == 12:
            hour = 0
        values.append((match.start(), f"{hour:02d}:{minute:02d}"))
    values.sort(key=lambda item: item[0])
    return [value for _, value in values]


def _calendar_update_text(raw: str, labels: tuple[str, ...], limit: int) -> str | None:
    label_pattern = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:{label_pattern})\s*(?:改成|改到|換成|換到|設為|設定為|是|為|为)\s*"
        r"[「『]?(.+?)[」』]?(?=$|[，,。；;])",
        raw,
    )
    if match is None:
        return None
    value = re.sub(r"\s+", " ", match.group(1)).strip(" ，,。；;「」『』")
    return value[:limit] if value else None


def _deterministic_calendar_update(
    raw: str, *, context: dict[str, Any], revision: int,
) -> VoiceProposal | None:
    if not any(word in raw for word in ("修改", "編輯", "更改", "調整", "改到", "改成", "換成")):
        return None
    target, target_ref = _current_calendar_target(context)
    explicit_target = re.search(
        r"(?:把|將|将|修改|編輯|更改|調整)\s*[「『]?(.+?)[」』]?\s*"
        r"(?:的)?(?:日期|時間|時段|地點|位置|備註|標題|名稱)?\s*"
        r"(?:改到|改成|調整到|換成|換到|設為|設定為)",
        raw,
    )
    if explicit_target is not None:
        candidate = explicit_target.group(1).strip(" 的「」『』")
        if normalized_phrase(candidate) not in {
            "這個行程", "这个行程", "目前行程", "目前這個行程", "目前这个行程",
            "行程", "日曆", "行事曆",
        } and candidate:
            target = candidate[:80]

    changes: dict[str, str] = {}
    event_date = _calendar_update_date(raw)
    if event_date is not None:
        changes["date"] = event_date
    clocks = _calendar_update_clocks(raw)
    if len(clocks) >= 2:
        changes["start_time"], changes["end_time"] = clocks[:2]
    elif len(clocks) == 1:
        if re.search(r"結束(?:時間)?|到幾點|到几点", raw):
            changes["end_time"] = clocks[0]
        else:
            changes["start_time"] = clocks[0]
    location = _calendar_update_text(raw, ("地點", "位置"), 120)
    if location is not None:
        changes["location"] = location
    notes = _calendar_update_text(raw, ("備註", "备注"), 500)
    if notes is not None:
        changes["notes"] = notes
    title = _calendar_update_text(raw, ("標題", "名稱", "行程名稱"), 80)
    if title is not None:
        changes["title"] = title
    if not target and not target_ref:
        return None
    if not changes:
        return None
    arguments: dict[str, Any] = {"target": target, **changes}
    if target_ref:
        arguments["target_ref"] = target_ref
    return validate_proposal({
        "intent": "calendar.update",
        "arguments": arguments,
        "reply": "我已準備修改行程，確認後會直接更新。",
    }, base_revision=revision)


def _deterministic_calendar_cancel(
    raw: str, *, context: dict[str, Any], revision: int,
) -> VoiceProposal | None:
    """Build a safe cancel proposal without accepting a database event id.

    Calendar cancellation is intentionally conservative: an explicit quoted
    title, a cleaned natural-language title, or the one selected personal
    event on the current calendar surface is required. Dates and times are
    only hints for the user-facing utterance; the Flutter executor resolves
    the final personal event and its current revision before writing.
    """
    if not any(word in raw.lower() for word in (
        "取消", "刪除", "移除", "刪掉", "cancel", "delete", "remove",
    )):
        return None

    target, target_ref = _current_calendar_target(context)
    event_date = _calendar_update_date(raw)
    if event_date is None:
        month_day = re.search(
            r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*(?:日|號|号)?(?!\d)",
            raw,
        )
        if month_day is not None:
            today = datetime.now(ZoneInfo("Asia/Taipei")).date()
            try:
                event_date = date(
                    today.year,
                    int(month_day.group(1)),
                    int(month_day.group(2)),
                ).isoformat()
            except ValueError:
                event_date = None
    quoted = re.search(r"[「『]([^」』]{1,80})[」』]", raw)
    if quoted is not None:
        candidate = quoted.group(1).strip()
    else:
        candidate = re.sub(
            r"^(?:幫我|請|我要|可以)?\s*"
            r"(?:取消|刪除|移除|刪掉|cancel|delete|remove)\s*",
            "",
            raw,
            flags=re.IGNORECASE,
        )
        candidate = re.sub(
            r"^(?:我的|我自己的)?(?:個人)?"
            r"(?:行事曆|日曆|行程表|行程|日程|事件)"
            r"(?:中|裡|里面|裡面|裏面|中的|裡的|裡面的)?\s*",
            "",
            candidate,
        )
        candidate = re.sub(
            r"^(?:今天|今日|明天|後天|后天|這週|這周|下週|下周)\s*(?:的)?",
            "",
            candidate,
        )
        candidate = re.sub(
            r"(?:的)?(?:行程|日程|事件)$",
            "",
            candidate,
        )
        candidate = candidate.strip(" \t\r\n，,。；;：:的")

    # Dates are a disambiguating hint, not part of the event title. Keep the
    # canonical date in the proposal so the device can resolve duplicate
    # titles safely instead of trying to match the whole spoken sentence.
    candidate = re.sub(
        r"^\d{4}\s*(?:年|[-/.])\s*\d{1,2}\s*(?:月|[-/.])\s*"
        r"\d{1,2}\s*(?:日)?\s*(?:的)?",
        "",
        candidate,
    )
    candidate = re.sub(
        r"^\d{1,2}\s*月\s*\d{1,2}\s*(?:日|號|号)?\s*(?:的)?",
        "",
        candidate,
    )
    candidate = re.sub(r"^(?:今天|今日|明天|後天|后天)\s*(?:的)?", "", candidate)
    candidate = re.sub(r"(?:的)?(?:行程|日程|事件)$", "", candidate).strip(
        " \t\r\n，,。；;：:的"
    )

    if normalized_phrase(candidate) in {
        "這個", "這項", "目前", "目前這個", "這個行程", "目前行程",
        "行程", "日曆", "行事曆", "事件",
    }:
        candidate = ""
    if candidate:
        target = candidate[:80]
    if not target and not target_ref:
        return None
    arguments: dict[str, Any] = {"target": target}
    if event_date is not None:
        arguments["date"] = event_date
    if target_ref:
        arguments["target_ref"] = target_ref
    return validate_proposal({
        "intent": "calendar.cancel",
        "arguments": arguments,
        "reply": "我已準備取消這個行程，確認後才會刪除。",
    }, base_revision=revision)


def _deterministic_calendar_create(raw: str, revision: int) -> VoiceProposal | None:
    if not any(word in raw for word in ("新增", "建立", "加入")):
        return None
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    if "後天" in raw or "后天" in raw:
        event_date = today + timedelta(days=2)
    elif "明天" in raw:
        event_date = today + timedelta(days=1)
    elif "今天" in raw:
        event_date = today
    else:
        explicit = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", raw)
        if explicit is None:
            return None
        try:
            event_date = date(*(int(explicit.group(index)) for index in (1, 2, 3)))
        except ValueError:
            return None
    clock = re.search(
        r"(上午|下午|晚上|傍晚|中午|凌晨)?\s*"
        r"([零〇一二兩三四五六七八九十\d]{1,3})\s*[點点時时]"
        r"(?:(半)|([零〇一二兩三四五六七八九十\d]{1,3})\s*分)?",
        raw,
    )
    if clock is None:
        return None
    hour = _small_number(clock.group(2))
    minute = 30 if clock.group(3) else _small_number(clock.group(4) or "零")
    if hour is None or minute is None or hour > 23 or minute > 59:
        return None
    period = clock.group(1) or ""
    if period in {"下午", "晚上", "傍晚"} and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period == "凌晨" and hour == 12:
        hour = 0
    start_minutes = hour * 60 + minute
    if start_minutes >= 23 * 60 + 59:
        return None
    end_minutes = min(start_minutes + 60, 23 * 60 + 59)
    title = raw[clock.end():].strip(" 的，,。")
    title = re.sub(r"(?:行程|活動|活动)$", "", title).strip()
    if not title:
        return None
    return validate_proposal({
        "intent": "calendar.create",
        "arguments": {
            "title": title,
            "date": event_date.isoformat(),
            "start_time": f"{hour:02d}:{minute:02d}",
            "end_time": f"{end_minutes // 60:02d}:{end_minutes % 60:02d}",
        },
        "reply": "我已準備新增行程，確認後會直接寫入行事曆。",
    }, base_revision=revision)


def safe_context(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    def integer(key: str, default: int = 0) -> int:
        try:
            return int(raw.get(key) or default)
        except (TypeError, ValueError):
            return default
    permissions_value = raw.get("permissions")
    permissions_raw = permissions_value if isinstance(permissions_value, dict) else None
    permissions = {
        key: (
            permissions_raw.get(key) is True
            if permissions_raw is not None
            else key not in SENSITIVE_PERMISSION_DEFAULTS
        )
        for key in VOICE_PERMISSION_KEYS
    }
    status_value = raw.get("feature_status")
    status_raw = status_value if isinstance(status_value, dict) else {}
    feature_status = {
        key: status_raw.get(key) is True
        for key in FEATURE_STATUS_KEYS
        if isinstance(status_raw.get(key), bool)
    }
    if permissions.get("chat_list") is True:
        contact_name = safe_reply(status_raw.get("contact_name"))[:40]
        if contact_name:
            feature_status["contact_name"] = contact_name
    config_value = raw.get("voice_config")
    config_raw = config_value if isinstance(config_value, dict) else {}
    voice_name = str(config_raw.get("voice_name") or "Achird")
    speech_speed = str(config_raw.get("speech_speed") or "normal")
    response_language = str(config_raw.get("response_language") or "zh-TW")
    input_language = str(config_raw.get("input_language") or "zh-en")
    raw_self_name = re.sub(
        r"\s+", " ", str(config_raw.get("self_name") or ""),
    ).strip()[:40]
    self_name = "" if _EMAIL_RE.search(raw_self_name) else raw_self_name
    routines: list[dict[str, str]] = []
    raw_routines = config_raw.get("routines")
    for raw_routine in (
        raw_routines[:8] if isinstance(raw_routines, list) else []
    ):
        if not isinstance(raw_routine, dict):
            continue
        name = _routine_name(raw_routine.get("name"))
        template_id = str(raw_routine.get("template_id") or "").strip()
        if (
            not name or template_id not in ROUTINE_TEMPLATES
        ):
            continue
        routines.append({"name": name, "template_id": template_id})
    return {
        "scope": str(raw.get("scope") or "global")[:40],
        "screen": safe_screen(raw.get("screen"), permissions),
        "revision": max(0, integer("revision")),
        "media_count": max(0, min(integer("media_count"), 5)),
        "can_publish": raw.get("can_publish") is True,
        "permissions": permissions,
        "feature_status": feature_status,
        "voice_config": {
            "voice_name": voice_name if voice_name in VOICE_NAMES else "Achird",
            "speech_speed": speech_speed if speech_speed in VOICE_SPEEDS else "normal",
            "response_language": (
                response_language if response_language in VOICE_LANGUAGES else "zh-TW"
            ),
            "input_language": input_language if input_language in {"zh-en", "zh-TW", "en-US"} else "zh-en",
            **({"self_name": self_name} if self_name else {}),
            **({"routines": routines} if routines else {}),
        },
    }


def permission_for_intent(intent: str) -> str | None:
    return ACTIONS.get(intent, {}).get("permission")


def context_allows_intent(context: dict[str, Any], intent: str) -> bool:
    permission = permission_for_intent(intent)
    if permission is None:
        return True
    permissions = context.get("permissions")
    return isinstance(permissions, dict) and permissions.get(permission) is True


def _direct_matching_access(
    context: dict[str, Any], proposal: VoiceProposal,
) -> tuple[bool, bool]:
    if proposal.intent == "match.query":
        return True, False
    if proposal.intent not in {"match.ayue_query", "ayue.public_query"}:
        return False, False
    if (
        proposal.intent == "ayue.public_query"
        and str(proposal.arguments.get("domain") or "") != "matching"
    ):
        return False, False
    question = str(proposal.arguments.get("question") or "")
    lowered = question.lower()
    scope = str(context.get("scope") or "")
    progress_or_hub = any(word in lowered for word in (
        "配對進度", "媒合進度", "搜尋進度", "配對狀態", "媒合狀態",
        "配對結果", "配到誰", "配對到誰", "找到人了嗎", "找到對象了嗎",
        "有要確認", "待確認的配對",
        "阿月牽線", "牽線頁", "牽線邀請", "牽線卡", "邀請專區",
        "邀請內容", "有哪些邀請",
        "match progress", "matching progress", "match status", "matching status",
        "match result", "matching result", "match hub", "introduction", "invitation",
    ))
    decision = any(word in lowered for word in (
        "接受", "同意", "願意認識", "有興趣", "認識看看",
        "拒絕", "婉拒", "先不用", "這次不要", "撤回", "收回邀請",
        "accept", "decline", "reject", "withdraw",
    ))
    confirmation = normalized_phrase(question) in {
        "確認", "確定", "取消", "confirm", "confirmed", "cancel", "yes", "no",
    }
    direct = progress_or_hub or (scope == "match_hub" and (decision or confirmation))
    return direct, decision or (scope == "match_hub" and confirmation)


def context_allows_proposal(context: dict[str, Any], proposal: VoiceProposal) -> bool:
    permissions = context.get("permissions")
    if proposal.intent == "ui.choice.activate":
        status = context.get("feature_status")
        return (
            isinstance(status, dict)
            and status.get("visible_choice_pending") is True
        )
    direct_matching, direct_matching_write = _direct_matching_access(
        context, proposal,
    )
    if direct_matching:
        return (
            isinstance(permissions, dict)
            and permissions.get("match_read") is True
            and (
                not direct_matching_write
                or permissions.get("match_actions") is True
            )
        )
    if not context_allows_intent(context, proposal.intent):
        return False
    if not isinstance(permissions, dict):
        return False
    if proposal.intent in {"calendar.update", "calendar.cancel", "date.respond", "date.update", "date.confirm"}:
        return permissions.get("calendar_read") is True
    if proposal.intent == "ayue.private_query":
        if permissions.get("chat_content") is not True:
            return False
        question = str(proposal.arguments.get("question") or "")
        if any(word in question for word in ("通知對方", "傳給", "送出", "安排約會", "發起約會")):
            return permissions.get("match_actions") is True
        return True
    if proposal.intent == "ayue.private_open":
        return permissions.get("chat_list") is True
    if proposal.intent == "safety.block_user":
        return permissions.get("chat_list") is True
    if proposal.intent == "personality.explore":
        return permissions.get("memory_read") is True
    if proposal.intent == "match.ayue_query":
        question = str(proposal.arguments.get("question") or "")
        if (
            _is_match_start_request(question)
            or any(word in question for word in (
                "取消搜尋", "接受", "婉拒", "拒絕", "撤回",
            ))
            or normalized_phrase(question) in {
                "確認", "確定", "取消", "confirm", "confirmed", "cancel", "yes", "no",
            }
        ):
            return permissions.get("match_actions") is True
        return True
    if proposal.intent == "ayue.public_query":
        question = str(proposal.arguments.get("question") or "")
        domain = str(proposal.arguments.get("domain") or "")
        domain_permission = {
            "matching": "match_read",
            "calendar": "calendar_read",
            "web": "web_search",
            "places": "places",
            "memory": "memory_read",
            "profile": "memory_read",
        }.get(domain)
        if domain_permission is None or permissions.get(domain_permission) is not True:
            return False
        delegated_confirmation = normalized_phrase(question) in {
            "確認", "確定", "取消", "confirm", "confirmed", "cancel", "yes", "no",
        }
        if domain == "matching" and (
            _is_match_start_request(question)
            or any(word in question for word in (
                "取消搜尋", "接受", "婉拒", "拒絕", "撤回",
            ))
            or delegated_confirmation
        ):
            return permissions.get("match_actions") is True
        return True
    return True


def requires_confirmation(proposal: VoiceProposal) -> bool:
    if ACTIONS.get(proposal.intent, {}).get("confirmation"):
        return True
    # This intent only hands the request to Matching Ayue. Its existing
    # server-owned choice card confirms the actual search write.
    return (
        proposal.intent == "settings.set"
        and proposal.arguments.get("key") in CONFIRMED_SETTING_KEYS
    )


def confirmation_phrase(proposal: VoiceProposal) -> str:
    if (
        proposal.intent == "match.ayue_query"
        and _is_match_start_request(proposal.arguments.get("question") or "")
    ):
        return "確認開始配對"
    if proposal.intent == "routine.save":
        return "確認儲存捷徑"
    if proposal.intent == "routine.delete":
        return "確認刪除捷徑"
    if proposal.intent == "date.confirm":
        return "確認安排"
    if proposal.intent == "date.update":
        return "確認修改共同約會"
    if proposal.intent == "date.respond":
        return (
            "確認接受約會邀請"
            if proposal.arguments.get("accepted") is True
            else "確認拒絕約會邀請"
        )
    if proposal.intent.startswith("date."):
        return "確認共同約會操作"
    if proposal.intent == "calendar.create":
        return "確認新增行程"
    if proposal.intent == "calendar.update":
        return "確認修改行程"
    if proposal.intent == "calendar.cancel":
        return "確認取消行程"
    if proposal.intent == "ayue.private_query":
        return "確認讀取私人聊天"
    if proposal.intent == "safety.block_user":
        return "確認封鎖使用者"
    if proposal.intent == "safety.unblock_user":
        return "確認解除封鎖使用者"
    if proposal.intent == "chat.request_send":
        return "確認傳送訊息"
    if proposal.intent == "memory.add":
        return "確認新增阿月記憶"
    if proposal.intent == "memory.disable":
        return "確認停用偏好"
    if proposal.intent == "post.request_publish":
        return "確認發布"
    if proposal.intent == "profile.request_commit":
        return "確認儲存個人資料"
    key = proposal.arguments.get("key")
    enabled = proposal.arguments.get("enabled") is True
    label = {
        "notifications.global": "訊息通知",
        "location.enabled": "定位",
        "ai.proactive_care": "AI 主動關心",
    }.get(key, "設定")
    return f"確認{'開啟' if enabled else '關閉'}{label}"


def confirmation_matches(text: str, proposal: VoiceProposal) -> bool:
    """Match a spoken confirmation without accepting a bare action phrase."""
    actual = normalized_phrase(text)
    expected = normalized_phrase(confirmation_phrase(proposal))
    if expected == "確認開始配對":
        if actual in {
            "可以", "可以啊", "可以的", "沒問題", "沒問題啊",
            "好啊", "好喔", "好哦", "行", "行啊", "來吧",
            "對", "對啊", "對的", "當然", "當然可以", "繼續", "繼續吧",
            "開始", "開始吧", "開始了", "開始配對", "開始配對吧",
            "開始找", "開始找人", "開始找對象", "就開始吧",
            "start", "startnow", "goahead", "proceed", "letsgo",
            "letsstart", "let'sstart", "sure", "ok", "okay",
        }:
            return True
        if re.fullmatch(
            r"(?:嗯(?:嗯)?|對(?:的|啊)?|當然|好(?:的|啊|喔|哦)?|可以|"
            r"沒問題|行(?:啊)?|那(?:就)?|"
            r"好那(?:就)?|我們|那我們|我想|我要|現在|先|直接|請|幫我){0,3}"
            r"(?:開始|開始配對|開始找|開始找人|開始找對象)"
            r"(?:吧|了|喔|哦|啊|看看|看看吧|試試|試試看|試試吧)?",
            actual,
        ):
            return True
    if actual in {
        "確認", "確定", "同意", "好", "好的", "好確認", "好確定",
        "confirm", "confirmed", "yes", "yesconfirm",
    }:
        return True
    if actual == expected:
        return True
    prefix = next(
        (item for item in ("確認", "確定") if actual.startswith(item)),
        None,
    )
    if prefix is None:
        return False
    action = actual[len(prefix):]
    expected_action = expected.removeprefix("確認")
    if action == expected_action:
        return True
    if proposal.intent == "settings.set":
        key = proposal.arguments.get("key")
        enabled = proposal.arguments.get("enabled") is True
        verb = "開啟" if enabled else "關閉"
        aliases = {
            "notifications.global": {f"{verb}訊息通知", f"{verb}通知"},
            "location.enabled": {f"{verb}定位", f"{verb}位置"},
            "ai.proactive_care": {f"{verb}AI主動關心", f"{verb}主動關心"},
        }.get(key, set())
        return action in aliases
    if proposal.intent == "date.confirm":
        return action in {"安排", "約會安排", "共同約會", "共同約會安排"}
    if proposal.intent == "date.update":
        return action in {
            "安排", "修改安排", "更新安排", "修改共同約會", "更新共同約會",
        }
    if proposal.intent == "date.respond":
        aliases = (
            {"接受邀請", "接受約會邀請"}
            if proposal.arguments.get("accepted") is True
            else {"拒絕邀請", "拒絕約會邀請"}
        )
        return action in aliases
    return False


def validate_proposal(value: Any, *, base_revision: int) -> VoiceProposal | None:
    if not isinstance(value, dict):
        return None
    intent = str(value.get("intent") or "").strip()
    if intent not in ALLOWED_INTENTS:
        return None
    raw_args = value.get("arguments") if isinstance(value.get("arguments"), dict) else {}
    from .voice_experience import EXPERIENCE_INTENTS, experience_arguments
    if intent in EXPERIENCE_INTENTS:
        args = experience_arguments(intent, raw_args)
        return None if args is None else VoiceProposal(intent, args, safe_reply(value.get('reply')), base_revision)
    args: dict[str, Any] = {}
    target_ref = raw_args.get("target_ref")
    if target_ref is not None and (not isinstance(target_ref, str) or not REF.fullmatch(target_ref)
                                   or not ACTIONS[intent]["target_kinds"]):
        return None
    if intent == "ui.target.select":
        if not target_ref:
            return None
    elif intent == "post.open":
        if not target_ref:
            return None
    elif intent in {"app.digest.query", "workflow.daily_briefing", "quota.query"}:
        args = {}
    elif intent == "chat.status.query":
        name = re.sub(r"\s+", " ", str(raw_args.get("contact_name") or "")).strip()[:40]
        args = {"contact_name": name}
    elif intent == "app.search":
        query = re.sub(r"\s+", " ", str(raw_args.get("query") or "")).strip()[:120]
        domains = raw_args.get("domains")
        if (
            not query
            or not isinstance(domains, list)
            or not 1 <= len(domains) <= 5
        ):
            return None
        cleaned_domains = list(dict.fromkeys(str(item) for item in domains))
        if len(cleaned_domains) != len(domains) or any(
            item not in APP_SEARCH_DOMAINS for item in cleaned_domains
        ):
            return None
        args = {"query": query, "domains": cleaned_domains}
    elif intent in {"routine.run", "routine.delete"}:
        name = _routine_name(raw_args.get("name"))
        if not name:
            return None
        args = {"name": name}
    elif intent == "routine.save":
        name = _routine_name(raw_args.get("name"))
        template_id = str(raw_args.get("template_id") or "").strip()
        supplied = raw_args.get("arguments")
        if (
            not name
            or template_id not in ROUTINE_TEMPLATES
            or not isinstance(supplied, dict)
        ):
            return None
        routine_arguments: dict[str, Any] = {}
        if template_id in {"search_app", "search_memory"}:
            query = re.sub(r"\s+", " ", str(supplied.get("query") or "")).strip()[:120]
            if not query:
                return None
            routine_arguments["query"] = query
        if template_id == "search_app":
            domains = supplied.get("domains")
            if not isinstance(domains, list) or not 1 <= len(domains) <= 5:
                return None
            cleaned_domains = list(dict.fromkeys(str(item) for item in domains))
            if len(cleaned_domains) != len(domains) or any(
                item not in APP_SEARCH_DOMAINS for item in cleaned_domains
            ):
                return None
            routine_arguments["domains"] = cleaned_domains
        allowed_routine_keys = (
            {"query", "domains"} if template_id == "search_app"
            else {"query"} if template_id == "search_memory"
            else set()
        )
        if set(supplied) - allowed_routine_keys:
            return None
        args = {
            "name": name,
            "template_id": template_id,
            "arguments": routine_arguments,
        }
    elif intent == "workflow.prepare_post":
        caption = str(raw_args.get("caption") or "").strip()[:2000]
        try:
            count = int(raw_args.get("count"))
        except (TypeError, ValueError):
            return None
        if not caption or not 1 <= count <= 5:
            return None
        args = {"caption": caption, "count": count}
    elif intent == "workflow.plan_date":
        contact_name = re.sub(
            r"\s+", " ", str(raw_args.get("contact_name") or ""),
        ).strip()[:80]
        changes = raw_args.get("changes")
        allowed = {
            "date", "start_time", "end_time", "activity",
            "location", "notes", "budget",
        }
        if not contact_name or not isinstance(changes, dict) or not changes or set(changes) - allowed:
            return None
        clean_changes: dict[str, str] = {}
        for key, item in changes.items():
            if not isinstance(item, str) or len(item) > (500 if key == "notes" else 120):
                return None
            clean_changes[key] = item.strip()
        try:
            if "date" in clean_changes:
                date.fromisoformat(clean_changes["date"])
        except ValueError:
            return None
        for key in ("start_time", "end_time"):
            if key in clean_changes and not re.fullmatch(
                r"(?:[01]\d|2[0-3]):[0-5]\d", clean_changes[key],
            ):
                return None
        args = {"contact_name": contact_name, "changes": clean_changes}
    elif intent == "workflow.update_profile":
        changes = raw_args.get("changes") if isinstance(raw_args.get("changes"), dict) else {}
        clean_profile: dict[str, Any] = {}
        for key, item in changes.items():
            if key not in PROFILE_FIELDS:
                continue
            if key == "age":
                try:
                    age = int(item)
                except (TypeError, ValueError):
                    continue
                if 18 <= age <= 120:
                    clean_profile[key] = age
            elif key == "phone":
                phone = re.sub(r"[\s\-().]", "", unicodedata.normalize("NFKC", str(item)))
                if re.fullmatch(r"09\d{8}", phone):
                    clean_profile[key] = phone
            elif key == "region":
                region = str(item).replace("臺", "台").strip()
                if region in TAIWAN_CITIES:
                    clean_profile[key] = region
            else:
                limit = 500 if key == "userinfo" else 40
                text = re.sub(r"\s+", " ", str(item or "")).strip()[:limit]
                if text:
                    clean_profile[key] = text
        if not clean_profile:
            return None
        args = {"changes": clean_profile}
    elif intent in {"date.query", "date.respond", "date.update", "date.confirm"}:
        target = re.sub(r"\s+", " ", str(raw_args.get("contact_name") or "")).strip()[:80]
        args = {"contact_name": target}
        if intent == "date.respond":
            if raw_args.get("accepted") not in (True, False) or not isinstance(raw_args.get("accepted"), bool):
                return None
            args["accepted"] = raw_args["accepted"]
        if intent == "date.update":
            changes = raw_args.get("changes")
            allowed = {"date", "start_time", "end_time", "activity", "location", "notes", "budget"}
            if not isinstance(changes, dict) or not changes or set(changes) - allowed:
                return None
            clean = {}
            for key, item in changes.items():
                if not isinstance(item, str) or len(item) > (500 if key == "notes" else 120):
                    return None
                clean[key] = item.strip()
            try:
                if "date" in clean:
                    date.fromisoformat(clean["date"])
            except ValueError:
                return None
            for key in ("start_time", "end_time"):
                if key in clean and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", clean[key]):
                    return None
            args["changes"] = clean
    elif intent == "weather.query":
        location = re.sub(
            r"\s+", " ", str(raw_args.get("location") or ""),
        ).strip()[:120]
        args = {"location": location}
    elif intent == "app.navigate":
        destination = str(raw_args.get("destination") or "").strip()
        if destination not in NAVIGATION_DESTINATIONS:
            return None
        args["destination"] = destination
    elif intent == "calendar.query":
        source = str(raw_args.get("source") or "").strip()
        if source and source not in CALENDAR_SOURCES:
            return None
        start_date = str(raw_args.get("start_date") or "").strip()
        end_date = str(raw_args.get("end_date") or "").strip()
        if bool(start_date) != bool(end_date):
            return None
        if start_date and end_date:
            try:
                parsed_start = date.fromisoformat(start_date)
                parsed_end = date.fromisoformat(end_date)
            except ValueError:
                return None
            if parsed_end < parsed_start:
                return None
            args = {
                "start_date": parsed_start.isoformat(),
                "end_date": parsed_end.isoformat(),
            }
        else:
            requested_range = str(raw_args.get("range") or "upcoming").strip()
            if requested_range not in CALENDAR_QUERY_RANGES:
                return None
            args["range"] = requested_range
        if source:
            args["source"] = source
    elif intent == "calendar.create":
        title = re.sub(r"\s+", " ", str(raw_args.get("title") or "")).strip()[:80]
        event_date = str(raw_args.get("date") or "").strip()
        start_time = str(raw_args.get("start_time") or "").strip()
        end_time = str(raw_args.get("end_time") or "").strip()
        try:
            date.fromisoformat(event_date)
        except ValueError:
            return None
        if not title or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", start_time):
            return None
        if end_time and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", end_time):
            return None
        args = {
            "title": title,
            "date": event_date,
            "start_time": start_time,
            "end_time": end_time,
            "location": re.sub(r"\s+", " ", str(raw_args.get("location") or "")).strip()[:120],
            "notes": re.sub(r"\s+", " ", str(raw_args.get("notes") or "")).strip()[:500],
        }
    elif intent == "calendar.update":
        target = re.sub(r"\s+", " ", str(raw_args.get("target") or "")).strip()[:80]
        if not target and not target_ref:
            return None
        clean: dict[str, str] = {"target": target}
        event_date = str(raw_args.get("date") or "").strip()
        if event_date:
            try:
                date.fromisoformat(event_date)
            except ValueError:
                return None
            clean["date"] = event_date
        for key in ("start_time", "end_time"):
            value_text = str(raw_args.get(key) or "").strip()
            if value_text:
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value_text):
                    return None
                clean[key] = value_text
        for key, limit in (("title", 80), ("location", 120), ("notes", 500)):
            if key in raw_args:
                clean[key] = re.sub(r"\s+", " ", str(raw_args.get(key) or "")).strip()[:limit]
        if len(clean) == 1:
            return None
        args = clean
    elif intent == "calendar.cancel":
        target = re.sub(r"\s+", " ", str(raw_args.get("target") or "")).strip()[:80]
        if not target and not target_ref:
            return None
        args["target"] = target
        event_date = str(raw_args.get("date") or "").strip()
        if event_date:
            try:
                parsed_date = date.fromisoformat(event_date)
            except ValueError:
                return None
            if parsed_date.isoformat() != event_date:
                return None
            args["date"] = event_date
    elif intent == "personality.explore":
        message = re.sub(r"\s+", " ", str(raw_args.get("message") or "")).strip()[:1000]
        if not message:
            return None
        args["message"] = message
    elif intent == "ayue.public_query":
        domain = str(raw_args.get("domain") or "").strip()
        question = re.sub(r"\s+", " ", str(raw_args.get("question") or "")).strip()[:1000]
        if domain not in PUBLIC_AYUE_DOMAINS or not question:
            return None
        args = {"domain": domain, "question": question}
    elif intent == "ayue.private_query":
        contact_name = re.sub(r"\s+", " ", str(raw_args.get("contact_name") or "")).strip()[:40]
        question = re.sub(r"\s+", " ", str(raw_args.get("question") or "")).strip()[:1000]
        if not question:
            return None
        args = {"contact_name": contact_name, "question": question}
    elif intent in {"ayue.private_open", "safety.block_user", "safety.unblock_user"}:
        contact_name = re.sub(
            r"\s+", " ", str(raw_args.get("contact_name") or ""),
        ).strip()[:40]
        if not contact_name and not target_ref:
            return None
        args = {"contact_name": contact_name} if contact_name else {}
    elif intent == "safety.blocked_users_query":
        args = {}
    elif intent == "contacts.query":
        args = {}
    elif intent == "self.query":
        detail = str(raw_args.get("detail") or "summary").strip()
        if detail not in {"name", "summary"}:
            return None
        args["detail"] = detail
    elif intent == "memory.query":
        args["query"] = re.sub(
            r"\s+", " ", str(raw_args.get("query") or ""),
        ).strip()[:120]
    elif intent == "memory.add":
        try:
            label = normalize_preference_text(raw_args.get("label"))
        except PreferenceTextError:
            return None
        stance = str(raw_args.get("stance") or "like").strip()
        if not label or stance not in {"like", "dislike", "require", "avoid"}:
            return None
        args = {"label": label, "stance": stance}
    elif intent == "match.query":
        view = str(raw_args.get("view") or "").strip()
        if view not in {"status", "hub"}:
            return None
        args = {"view": view}
    elif intent == "ui.choice.activate":
        action = str(raw_args.get("action") or "").strip()
        if action not in {"confirm", "cancel"}:
            return None
        args["action"] = action
    elif intent == "chat.open":
        contact_name = re.sub(r"\s+", " ", str(raw_args.get("contact_name") or "")).strip()[:40]
        if not contact_name and not target_ref:
            return None
        args["contact_name"] = contact_name
    elif intent == "chat.request_send":
        contact_name = re.sub(r"\s+", " ", str(raw_args.get("contact_name") or "")).strip()[:40]
        message = re.sub(r"\s+", " ", str(raw_args.get("message") or "")).strip()[:500]
        if (not contact_name and not target_ref) or not message:
            return None
        args = {"contact_name": contact_name, "message": message}
    elif intent == "profile.patch":
        changes = raw_args.get("changes") if isinstance(raw_args.get("changes"), dict) else {}
        clean: dict[str, Any] = {}
        for key, item in changes.items():
            if key not in PROFILE_FIELDS:
                continue
            if key == "age":
                try:
                    age = int(item)
                except (TypeError, ValueError):
                    continue
                if 18 <= age <= 120:
                    clean[key] = age
            elif key == "phone":
                phone = re.sub(r"[\s\-().]", "", unicodedata.normalize("NFKC", str(item)))
                if re.fullmatch(r"09\d{8}", phone):
                    clean[key] = phone
            elif key == "region":
                region = str(item).replace("臺", "台").strip()
                if region in TAIWAN_CITIES:
                    clean[key] = region
            else:
                limit = 500 if key == "userinfo" else 40
                text = re.sub(r"\s+", " ", str(item or "")).strip()[:limit]
                if text:
                    clean[key] = text
        if not clean:
            return None
        args["changes"] = clean
    elif intent == "settings.set":
        key = str(raw_args.get("key") or "")
        if key not in SETTING_KEYS or not isinstance(raw_args.get("enabled"), bool):
            return None
        args = {"key": key, "enabled": raw_args["enabled"]}
    elif intent in {"post.open_draft", "post.replace_caption", "post.append_caption"}:
        caption = str(raw_args.get("caption") or "").strip()[:2000]
        if not caption:
            return None
        args["caption"] = caption
    elif intent == "post.select_recent_photos":
        raw_positions = raw_args.get("positions")
        if raw_positions is not None:
            if raw_args.get("count") not in (None, ""):
                return None
            if not isinstance(raw_positions, list) or not 1 <= len(raw_positions) <= 5:
                return None
            positions: list[int] = []
            for item in raw_positions:
                if not isinstance(item, int) or isinstance(item, bool):
                    return None
                position = item
                if not 1 <= position <= 20:
                    return None
                positions.append(position)
            if len(set(positions)) != len(positions):
                return None
            args["positions"] = positions
        else:
            try:
                count = int(raw_args.get("count") or 3)
            except (TypeError, ValueError):
                return None
            if not 1 <= count <= 5:
                return None
            args["count"] = count
    elif intent == "match.ayue_query":
        question = re.sub(r"\s+", " ", str(raw_args.get("question") or "")).strip()[:1000]
        if not question:
            return None
        args["question"] = question
    if target_ref:
        args["target_ref"] = target_ref
    reply = safe_reply(value.get("reply"))
    if not reply:
        reply = "好的，我已整理好這個操作。"
    return VoiceProposal(intent, args, reply, max(0, int(base_revision)))


def deterministic_proposal(
    text: str, *, context: dict[str, Any], generated_caption: str | None = None,
) -> VoiceProposal | None:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    raw_lower = raw.lower()
    compact = normalized_phrase(raw)
    revision = int(context.get("revision") or 0)
    response_language = str(
        (context.get("voice_config") or {}).get("response_language") or "zh-TW"
    )
    working_reply = {
        "zh-TW": "我找一下，稍等一下。",
        "zh-CN": "我查一下，请稍等。",
        "en-US": "Let me check. One moment.",
    }.get(response_language, "我找一下，稍等一下。")
    if not raw:
        return None
    if any(phrase in compact for phrase in ('剩多少額度', '剩餘額度', '額度還有', '額度用完', '額度恢復', '什麼時候恢復', '語音和配對共用', 'remainingquota', 'quotabalance')):
        return VoiceProposal('quota.query', {}, '', revision)
    if any(phrase in compact for phrase in ('冷卻還', '還要等多久', '幾點可以再傳', '可以傳了嗎', '剛剛那則有送出', '剛才有送出', '查詢傳送狀態', 'cooldown', 'diditsend')):
        return VoiceProposal('chat.status.query', {'contact_name': ''}, '', revision)
    if compact in {
        "關閉語音模式", "关闭语音模式", "關閉語音助理", "关闭语音助理",
        "停止語音模式", "停止语音模式", "結束語音模式", "结束语音模式",
        "退出語音模式", "退出语音模式",
        "休息一下", "你休息一下", "你先休息", "先休息", "可以休息了", "休息吧",
        "先不要聽", "先不要听", "不要再聽", "不要再听", "不要聽了", "不要听了",
        "別聽了", "别听了",
        "不用聽了", "不用听了", "停止聆聽", "停止聆听",
        "安靜一下", "安静一下", "先安靜", "先安静",
        "暫停語音", "暂停语音", "先別聽", "先别听",
        "takeabreak", "stoplistening", "dontlisten", "don'tlisten", "bequiet",
    }:
        return VoiceProposal(
            "assistant.close", {}, "好，語音模式已關閉。", revision,
        )
    visible_action = visible_choice_action(raw)
    if (
        visible_action is not None
        and (context.get("feature_status") or {}).get("visible_choice_pending")
        is True
    ):
        return VoiceProposal(
            "ui.choice.activate",
            {"action": visible_action},
            "我會按下畫面上目前的按鈕。", revision,
        )
    named_date_confirmation = re.search(
        r"(?:幫我|請)?(?:確認|確定)(?:一下)?(?:和|跟)\s*"
        r"([^，,。！？!?]{1,40}?)\s*(?:的)?(?:共同約會|約會)?安排$",
        raw,
    ) or re.search(
        r"(?:幫我|請)?(?:確認|確定)(?:一下)?\s*"
        r"([^，,。！？!?]{1,40}?)\s*的(?:共同約會|約會)?安排$",
        raw,
    )
    date_confirmation_phrases = {
        "確認安排", "確定安排", "確認這個安排", "確定這個安排",
        "確認約會安排", "確定約會安排", "確認共同約會", "確定共同約會",
        "確認共同約會安排", "確定共同約會安排",
        "confirmshareddate", "confirmthearrangement",
    }
    if named_date_confirmation is not None or compact in date_confirmation_phrases:
        target = (
            named_date_confirmation.group(1).strip()
            if named_date_confirmation is not None
            else "目前對象"
        )
        if normalized_phrase(target) in {
            "這個", "目前", "目前這個", "共同約會", "約會",
            "這個共同約會", "目前共同約會",
        }:
            target = "目前對象"
        return VoiceProposal(
            "date.confirm",
            {"contact_name": target},
            "我先核對這份共同約會，再確認你這一方。",
            revision,
        )
    weather_location = _weather_location(raw)
    if weather_location is not None:
        return VoiceProposal(
            "weather.query",
            {"location": weather_location},
            "我同時查看目前天氣與空氣品質。",
            revision,
        )
    if compact in {
        "你是誰", "你是誰啊", "請問你是誰", "你叫什麼", "你的名字是什麼",
        "你是谁", "你是谁啊", "请问你是谁", "你叫什么", "你的名字是什么",
    }:
        return VoiceProposal(
            "assistant.reply", {},
            "我是阿月，Folks 裡的語音助理。我可以陪你聊聊，也能幫你操作個資、設定和貼文草稿。",
            revision,
        )
    if compact in {
        "我是誰", "你知道我是誰嗎", "你知道我叫什麼嗎", "我叫什麼",
        "我是谁", "你知道我是谁吗", "你知道我叫什么吗", "我叫什么",
        "whoami", "doyouknowmyname", "whatismyname",
    }:
        return VoiceProposal(
            "self.query", {"detail": "name"},
            "我查看你的本人資料。", revision,
        )
    if any(phrase in compact for phrase in (
        "你對我了解多少", "你了解我嗎", "說說你對我的了解",
        "你知道我的什麼", "描述我",
        "你对我了解多少", "你了解我吗", "说说你对我的了解",
        "whatdoyouknowaboutme",
    )):
        return VoiceProposal(
            "self.query", {"detail": "summary"},
            "我整理目前對你的了解。", revision,
        )
    if compact in {"取消", "停止", "算了", "不要了"}:
        return VoiceProposal("assistant.cancel", {}, "好的，這次操作已取消。", revision)
    if any(term in compact for term in ("約會邀請", "共同約會", "dateinvitation", "dateinvites", "shareddate")) and not any(
        term in compact for term in ("接受", "拒絕", "改", "調整", "確認", "accept", "decline", "update", "confirm", "change")
    ):
        named_shared_date = re.search(
            r"(?:查|查看|讀取|看看)?(?:我)?(?:和|跟)\s*"
            r"([^，,。！？!?]{1,40}?)(?:的)?(?:共同約會|約會安排)",
            raw,
        )
        contact_name = (
            named_shared_date.group(1).strip(" 的「」『』“”\"'")
            if named_shared_date is not None else ""
        )
        return VoiceProposal(
            "date.query", {"contact_name": contact_name},
            "我查看你的約會邀請與共同安排。", revision,
        )
    if any(phrase in compact for phrase in (
        "配對進度", "媒合進度", "搜尋進度", "配對狀態", "媒合狀態",
        "配對結果", "配對好了嗎", "媒合好了嗎", "配到誰", "配對到誰",
        "有要確認", "待確認的配對", "找到人了嗎", "找到對象了嗎",
    )):
        return VoiceProposal(
            "match.query", {"view": "status"},
            "我直接查看目前配對狀態。", revision,
        )
    if _is_match_start_request(raw):
        return VoiceProposal(
            "match.ayue_query", {"question": raw}, working_reply, revision,
        )
    if any(phrase in compact for phrase in (
        "打開阿月牽線", "開啟阿月牽線", "查看阿月牽線", "阿月牽線內容",
        "朗讀阿月牽線", "牽線邀請內容", "有哪些牽線", "有哪些邀請",
    )):
        return VoiceProposal(
            "match.query", {"view": "hub"},
            "我直接打開並讀取阿月牽線。", revision,
        )
    generic_chat_open = {
        "聊天室", "聊天列表", "開聊天室", "打開聊天室", "開啟聊天室",
        "進入聊天室", "前往聊天室", "去聊天室", "去聊天",
        "幫我開聊天室", "幫我打開聊天室", "幫我開啟聊天室",
        "我要開聊天室", "打開聊天列表", "開啟聊天列表",
        "openchat", "openchatlist", "openmessages",
    }
    if compact in generic_chat_open:
        return VoiceProposal(
            "app.navigate", {"destination": "chat"}, "好，我幫你開啟聊天列表。", revision,
        )
    named_chat_open = re.search(
        r"(?:(?:幫我|請|我要)?(?:打開|開啟|開|進入|前往))(?:和|跟)?\s*"
        r"([^，,。！？!?]{1,40}?)(?:的)?(?:聊天室|聊天頁面|對話)$",
        raw,
    ) or re.fullmatch(
        r"(?:和|跟)?\s*([^，,。！？!?]{1,40}?)的"
        r"(?:聊天室|聊天頁面|對話)",
        raw.strip(),
    )
    if named_chat_open:
        contact_name = named_chat_open.group(1).strip(" 的「」『』“”\"'")
        if contact_name:
            return VoiceProposal(
                "chat.open", {"contact_name": contact_name},
                f"我開啟{contact_name}的聊天室。", revision,
            )
    chat_read_requested = (
        any(marker in compact for marker in (
            "讀取", "查看", "看看", "念出", "摘要", "總結", "分析",
            "read", "summarize", "analyse", "analyze",
        ))
        and any(marker in compact for marker in (
            "聊天", "對話", "訊息", "裡面的內容", "里面的内容", "messages", "chat",
        ))
    )
    if chat_read_requested:
        named_chat_read = re.search(
            r"(?:讀取|查看|看看|摘要|總結|分析)(?:一下)?"
            r"(?:和|跟)?\s*([^，,。！？!?]{1,40}?)的"
            r"(?:聊天室|聊天|對話)(?:內容|記錄|紀錄|訊息)?",
            raw,
        )
        contact_name = (
            named_chat_read.group(1).strip(" 的「」『』“”\"'")
            if named_chat_read is not None else ""
        )
        target_ref = ""
        if not contact_name and str(context.get("scope") or "") in {
            "chat", "ayue_private",
        }:
            contact_name, target_ref = _current_contact(context)
        if contact_name or target_ref:
            arguments = {"contact_name": contact_name, "question": raw}
            if target_ref:
                arguments["target_ref"] = target_ref
            return VoiceProposal(
                "ayue.private_query", arguments, working_reply, revision,
            )
    private_ayue_requested = any(
        marker in compact
        for marker in (
            "阿月悄悄話", "阿月悄悄话", "悄悄話", "悄悄话",
            "privateayue", "privatechat",
        )
    )
    if private_ayue_requested:
        contact_name, target_ref = ("", "")
        if str(context.get("scope") or "") in {"chat", "ayue_private"}:
            contact_name, target_ref = _current_contact(context)
        named_private = re.search(
            r"(?:和|跟|對|对)\s*([^，,。！？!?]{1,40}?)\s*(?:的)?"
            r"(?:阿月)?(?:悄悄話|悄悄话)",
            raw,
        )
        if named_private is not None:
            contact_name = named_private.group(1).strip(" 的「」『』“”\"'")
        if contact_name or target_ref:
            arguments = {"contact_name": contact_name, "question": raw}
            if target_ref:
                arguments["target_ref"] = target_ref
            return VoiceProposal(
                "ayue.private_query", arguments, working_reply, revision,
            )
    navigation = {
        "打開聊天": "chat", "開啟聊天": "chat", "去聊天頁面": "chat",
        "打開配對": "matching", "開啟配對": "matching", "去配對頁面": "matching",
        "打開配對頁面": "matching", "開啟配對頁面": "matching",
        "前往配對頁面": "matching", "跳到配對頁面": "matching",
        "打開個人頁面": "profile", "開啟個人頁面": "profile", "去個人頁面": "profile",
        "打開設定": "settings", "開啟設定": "settings", "去設定頁面": "settings",
        "編輯個人資料": "profile_edit", "打開編輯個人資料": "profile_edit",
        "打開語音助理設定": "voice_settings", "阿月語音助理設定": "voice_settings",
        "打開行事曆": "calendar", "開啟行事曆": "calendar",
        "打開行事曆頁面": "calendar", "開啟行事曆頁面": "calendar",
        "前往行事曆": "calendar", "跳到行事曆": "calendar",
        "打開配對阿月": "matching_ayue", "開啟配對阿月": "matching_ayue",
        "打開阿月記住的事": "memory", "查看阿月記憶": "memory",
        "打開發文頁": "create_post", "我要發文": "create_post",
    }
    destination = navigation.get(compact)
    if destination is None and any(
        verb in compact
        for verb in (
            "打開", "開啟", "前往", "跳到", "切換到", "帶我到", "带我到", "進入",
            "open", "goto", "navigateto", "switchto", "takemeto", "enterthe",
        )
    ):
        navigation_targets = (
            (("語音助理設定",), "voice_settings"),
            (("配對阿月",), "matching_ayue"),
            (("阿月記住的事", "阿月記憶", "記憶頁"), "memory"),
            (("編輯個人資料", "個人資料編輯"), "profile_edit"),
            (("行事曆", "行程頁"), "calendar"),
            (("發文頁", "貼文草稿"), "create_post"),
            (("配對頁", "配對主頁"), "matching"),
            (("聊天頁", "訊息頁"), "chat"),
            (("個人頁", "個人主頁"), "profile"),
            (("設定頁",), "settings"),
            (("calendar",), "calendar"),
            (("matchingpage", "matchpage"), "matching"),
            (("chatpage", "messagespage"), "chat"),
            (("profilepage",), "profile"),
            (("settingspage",), "settings"),
        )
        destination = next((
            target
            for phrases, target in navigation_targets
            if any(phrase in compact for phrase in phrases)
        ), None)
    if destination:
        return VoiceProposal(
            "app.navigate", {"destination": destination}, "好，我幫你打開。", revision,
        )
    if "戀愛顧問" in raw:
        return VoiceProposal("settings.open", {}, "AI 戀愛顧問目前尚未提供可調整設定。", revision)
    exploration_requested = any(phrase in compact for phrase in (
        "開始個性探索", "個性探索", "人格探索", "性格探索",
        "開始性格測驗", "開始人格測驗",
    ))
    get_to_know_request = bool(re.search(
        r"(?:我(?:希望|想要|想)(?:你|妳)?(?:可以|能夠|能)?|"
        r"(?:你|妳)(?:可以|能不能|能夠|能)|讓(?:你|妳))"
        r"(?:再)?(?:更|多|更加|更深入)(?:地|一點)?(?:認識|了解|瞭解)我",
        compact,
    ))
    exploration_declined = any(phrase in compact for phrase in (
        "不要", "不想", "不用", "不需要", "停止", "結束", "取消", "先別",
    ))
    if (exploration_requested or get_to_know_request) and not exploration_declined:
        return VoiceProposal(
            "personality.explore", {"message": (
                raw if exploration_requested else f"我想開始性格探索。{raw}"
            )},
            working_reply, revision,
        )
    private_date = re.search(
        r"(?:我)?(?:想要|想)?(?:和|跟)\s*([^，。！？!?]{1,40}?)\s*"
        r"(?:安排|約|约)(?:一場|一场|一個|一个)?(?:約會|约会|見面|见面)",
        raw,
    )
    if private_date:
        contact_name = private_date.group(1).strip()
        if contact_name:
            return VoiceProposal(
                "ayue.private_query",
                {"contact_name": contact_name, "question": raw},
                working_reply,
                revision,
            )
    if "相簿" in raw and any(word in raw for word in ("最近", "最新", "前", "前三張")):
        count = 3
        match = re.search(r"([一二兩三四五1-5])張", raw)
        if match:
            count = {
                "一": 1, "二": 2, "兩": 2, "三": 3, "四": 4, "五": 5,
            }.get(match.group(1), int(match.group(1)) if match.group(1).isdigit() else 3)
        return VoiceProposal(
            "post.select_recent_photos", {"count": count},
            f"我會選取相簿最近的 {count} 張照片；發布仍需要再次確認。", revision,
        )
    if any(word in raw for word in ("幫我發布", "發布貼文", "圖片選好了")):
        count = int(context.get("media_count") or 0)
        reply = (
            "你還沒有選擇圖片，請先自行選擇一到五張圖片。"
            if count == 0
            else f"目前有 {count} 張圖片。要發布請說「確認發布」。"
        )
        return VoiceProposal("post.request_publish", {}, reply, revision)
    if any(word in raw for word in ("幫我儲存", "儲存個人資料", "保存個人資料")):
        return VoiceProposal(
            "profile.request_commit", {},
            "我已準備儲存目前變更。請說「確認儲存個人資料」。", revision,
        )
    post_open_verbs = ("看", "查看", "檢視", "打開", "開啟", "開", "open", "show", "view")
    post_open_markers = ("貼文", "文章", "動態", "post", "story")
    if (
        str(context.get("scope") or "") == "profile"
        and any(word in raw_lower for word in post_open_verbs)
        and any(marker.lower() in raw_lower for marker in post_open_markers)
    ):
        screen = context.get("screen") if isinstance(context.get("screen"), dict) else {}
        post_items = [
            item for item in screen.get("items") or []
            if isinstance(item, dict) and item.get("kind") == "post"
        ]
        if post_items:
            target_index = 0  # The profile page deliberately keeps newest first.
            if not any(word in raw_lower for word in ("最新", "最近", "第一", "first", "latest", "newest")):
                ordinal_match = re.search(
                    r"第\s*([一二兩三四五六七八九十\d]{1,3})\s*(?:篇|則|個)?",
                    raw,
                    flags=re.IGNORECASE,
                )
                if ordinal_match is not None:
                    ordinal = _small_number(ordinal_match.group(1))
                    if ordinal is None or not 1 <= ordinal <= len(post_items):
                        return None
                    target_index = ordinal - 1
            target_ref = str(post_items[target_index].get("ref") or "").strip()
            if target_ref:
                return VoiceProposal(
                    "post.open",
                    {"target_ref": target_ref},
                    "我打開個人頁面上的那篇貼文。",
                    revision,
                )
    post_markers = ("貼文", "發文", "文章", "文案", "文字內容", "故事", "po", "story")
    post_verbs = (
        "幫我", "寫", "撰寫", "編", "邊", "編輯", "修改", "重寫", "發一個", "po",
        "write", "compose", "draft",
    )
    if any(marker.lower() in raw_lower for marker in post_markers) and any(
        word.lower() in raw_lower for word in post_verbs
    ):
        topic = raw
        match = re.search(r"關於(.+?)(?:的(?:故事|貼文)|$)", raw)
        if match:
            topic = match.group(1).strip(" ，。")
        elif "故事" in raw or "story" in raw_lower:
            topic = re.sub(
                r"^(?:請|請幫我|幫我|可以)?\s*"
                r"(?:編|寫|撰寫|創作|write|compose|draft)\s*"
                r"(?:一點|一個|一段|一則|a|an|the)?\s*",
                "",
                raw,
                flags=re.IGNORECASE,
            )
            topic = re.sub(r"(?:的)?(?:故事|story)$", "", topic, flags=re.IGNORECASE)
            topic = topic.strip(" ，。的") or "日常生活"
        caption = generated_caption or (
            f"今天想記錄一段關於{topic}的故事。\n\n"
            "有些片刻不需要特別安排，回想起來仍然會讓人微笑。"
        )
        post_intent = (
            "post.replace_caption"
            if str(context.get("scope") or "") == "post"
            and any(word in raw_lower for word in (
                "編", "邊", "編輯", "修改", "重寫", "文字內容",
                "write", "compose", "draft",
            ))
            else "post.open_draft"
        )
        return VoiceProposal(
            post_intent, {"caption": caption[:2000]},
            "好的，我幫你整理成貼文草稿。", revision,
        )
    if any(phrase in compact for phrase in (
        "我的好友有誰", "我有哪些好友", "好友有誰", "好友名單",
        "聊天裡有誰", "聊天室裡有誰", "聯絡人有誰", "聯絡人名單",
        "我可以傳訊息給誰", "配對對象有誰", "有哪些配對對象",
        "whoaremycontacts", "listmycontacts", "whocanimessage",
        "whocanichatwith",
    )):
        return VoiceProposal(
            "contacts.query", {},
            "我查看目前可以聊天的配對對象。", revision,
        )
    message_request = re.search(
        r"(?:幫我)?(?:傳(?:送)?(?:一則|一個)?(?:訊息|消息)?|"
        r"發(?:一則|一個)?(?:訊息|消息)?|告訴|回覆|問)\s*(?:給)?\s*"
        r"([^，,：:。！？!?\s]{1,40})[，,：:\s]+"
        r"(?:內容(?:是|為|为)?|說|说|問|问)?\s*(.+)$",
        raw,
    )
    if message_request:
        contact_name = message_request.group(1).strip()
        message = message_request.group(2).strip(" ，,：:")
        if contact_name and message:
            return VoiceProposal(
                "chat.request_send",
                {"contact_name": contact_name, "message": message},
                "我先確認收件人與內容。", revision,
            )
    if any(phrase in compact for phrase in (
        "你記得我", "你記住我", "阿月記住什麼",
        "阿月記住的事", "我的阿月記憶", "我的偏好",
        "你记得我", "你记住我", "阿月记住的事",
        "whatdoyourememberaboutme", "readmymemories",
    )):
        return VoiceProposal(
            "memory.query", {"query": ""},
            "我查看阿月記住的事。", revision,
        )
    memory_add = re.search(
        r"(?:請|请|幫我|帮我)?(?:記住|记住|記得|记得)(?:我)?\s*"
        r"(喜歡|喜欢|偏好|不喜歡|不喜欢|討厭|讨厌|需要|避免)\s*(.+)$",
        raw,
    )
    if memory_add:
        stance_text = memory_add.group(1)
        stance = (
            "dislike"
            if stance_text in {"不喜歡", "不喜欢", "討厭", "讨厌"}
            else "avoid"
            if stance_text == "避免"
            else "require"
            if stance_text == "需要"
            else "like"
        )
        try:
            label = normalize_preference_text(memory_add.group(2).strip(" ，,。"))
        except PreferenceTextError:
            return None
        if label:
            return VoiceProposal(
                "memory.add", {"label": label, "stance": stance},
                "我可以把這件事存進阿月記憶。", revision,
            )
    external_event = (
        re.search(r"活動|展覽|演唱會|音樂會|市集|講座|\bevents?\b|\bconcerts?\b|\bexhibitions?\b|\bfestivals?\b", raw_lower)
        and re.search(r"找|查|推薦|有沒有|有哪些|哪裡|什麼|\bfind\b|\bsearch\b|\blook\s*up\b|\bwhat\b|\bany\b", raw_lower)
        and not re.search(r"我的活動|我的行事曆|我的日曆|我(?:今天|明天|後天|當天|那天)?(?:有什麼|有哪些|有沒有)活動|行事曆裡|日曆裡|已安排|新增|加入|建立|修改|取消|刪除|不要|不用|不想|\bmy\s+(?:calendar|events)\b|\b(?:add|create|cancel|delete)\b", raw_lower)
    )
    if external_event:
        question = raw
        if re.search(r"當天|那天|同一天|那一天|that day|same day|then", raw_lower):
            reference = str(context.get("_calendar_reference") or "").strip()
            if not reference:
                # Let the conversational model resolve the date or ask for it.
                return None
            question = f"日期沿用先前的行事曆查詢：{reference}。請查公開活動：{raw}"
        return VoiceProposal("ayue.public_query", {"domain": "web", "question": question}, working_reply, revision)
    public_domain = None
    if (
        any(word in raw for word in (
            "行事曆", "日曆", "日历", "行程", "空檔", "有空",
        ))
        or "calendar" in compact
        or "calender" in compact
        or (
            any(word in raw.lower() for word in (
                "取消", "刪除", "移除", "刪掉", "cancel", "delete", "remove",
            ))
            and (
                _CALENDAR_EXPLICIT_DATE_RE.search(raw) is not None
                or re.search(
                    r"(?<!\d)\d{1,2}\s*月\s*\d{1,2}\s*(?:日|號|号)?(?!\d)",
                    raw,
                ) is not None
            )
        )
        or (
            str(context.get("scope") or "") == "calendar"
            and any(word in raw for word in (
                "修改", "編輯", "更改", "調整", "改到", "改成", "換成",
                "取消", "刪除", "移除", "刪掉", "cancel", "delete", "remove",
            ))
        )
    ):
        calendar_write_words = (
            "新增", "建立", "加入", "修改", "編輯", "更改", "調整",
            "改到", "改成", "換成", "取消", "刪除", "移除", "刪掉",
            "cancel", "delete", "remove",
        )
        if any(word in raw for word in calendar_write_words):
            if any(word in raw for word in ("新增", "建立", "加入")):
                return _deterministic_calendar_create(raw, revision)
            if any(word in raw for word in ("修改", "編輯", "更改", "調整", "改到", "改成", "換成")):
                return _deterministic_calendar_update(
                    raw, context=context, revision=revision,
                )
            return _deterministic_calendar_cancel(
                raw, context=context, revision=revision,
            )
        else:
            source = _calendar_source(raw)
            explicit_range = _explicit_calendar_range(raw)
            if _CALENDAR_EXPLICIT_DATE_RE.search(raw) and explicit_range is None:
                return None
            if explicit_range is not None:
                return VoiceProposal(
                    "calendar.query",
                    {
                        "source": source,
                        "start_date": explicit_range[0],
                        "end_date": explicit_range[1],
                    },
                    working_reply,
                    revision,
                )
            calendar_range = "upcoming"
            if "明天" in raw:
                calendar_range = "tomorrow"
            elif "今天" in raw:
                calendar_range = "today"
            elif "週末" in raw or "周末" in raw:
                calendar_range = "weekend"
            elif "下週" in raw or "下周" in raw:
                calendar_range = "next_week"
            elif any(word in raw for word in ("這週", "这周", "七天")):
                calendar_range = "week"
            return VoiceProposal(
                "calendar.query", {"source": source, "range": calendar_range},
                working_reply, revision,
            )
    elif any(word in raw for word in (
        "附近", "餐廳", "咖啡廳", "景點", "距離", "營業",
        "哪裡好玩", "哪裡有好玩", "好玩的", "可以去哪裡",
        "去哪玩", "推薦去處", "景點推薦", "好吃的",
    )):
        public_domain = "places"
    elif any(word in raw for word in ("上網查", "網路查", "最新消息", "公開資訊")):
        public_domain = "web"
    elif any(word in raw for word in (
        "配對進度", "媒合進度", "搜尋進度", "配對狀態", "媒合狀態",
        "配對結果", "配到誰", "配對到誰", "配對對象", "找到人了嗎", "找到對象了嗎",
        "有要確認", "待確認的配對",
        "阿月牽線", "牽線頁", "牽線邀請", "牽線卡", "邀請專區",
        "邀請內容", "有哪些邀請",
    )) or (
        str(context.get("scope") or "") == "match_hub"
        and any(word in raw for word in (
            "接受", "同意", "願意認識", "有興趣", "認識看看",
            "拒絕", "婉拒", "先不用", "撤回",
        ))
    ):
        public_domain = "matching"
    elif any(word in raw for word in ("你記得我", "阿月記憶", "我的偏好")):
        public_domain = "memory"
    if public_domain:
        return VoiceProposal(
            "ayue.public_query", {"domain": public_domain, "question": raw},
            working_reply, revision,
        )
    enabled = not any(word in raw for word in ("關閉", "停用", "不要", "取消"))
    setting_key = None
    if "定位" in raw:
        setting_key = "location.enabled"
    elif "訊息通知" in raw or "消息通知" in raw or "通知" in raw:
        setting_key = "notifications.global"
    elif "主動關心" in raw:
        setting_key = "ai.proactive_care"
    elif "玻璃" in raw or "毛玻璃" in raw:
        setting_key = "ui.liquid_glass"
    elif "深色" in raw or "暗色" in raw:
        setting_key = "ui.dark_mode"
    if setting_key:
        action = "開啟" if enabled else "關閉"
        return VoiceProposal(
            "settings.set", {"key": setting_key, "enabled": enabled},
            f"好的，我準備{action}這項設定。", revision,
        )
    changes: dict[str, Any] = {}
    nickname = re.search(r"(?:暱稱|名字)(?:改成|改為|是|叫)?\s*([^，。]{1,40})", raw)
    phone = re.search(r"(?:電話|手機)(?:改成|改為|是)?\s*([0-9０-９\s\-()]{8,20})", raw)
    age = re.search(r"(?:年齡|我)(?:改成|是)?\s*(\d{2,3})\s*歲", raw)
    intro = re.search(r"自介(?:改成|改為|是)?\s*(.+)$", raw)
    if nickname:
        changes["name"] = nickname.group(1).strip()
    if phone:
        changes["phone"] = phone.group(1)
    if age:
        changes["age"] = age.group(1)
    if intro:
        changes["userinfo"] = intro.group(1).strip()
    normalized_location = raw.replace("臺", "台")
    for city in TAIWAN_CITIES:
        if city in normalized_location:
            changes["region"] = city
            changes["city"] = city
            remainder = normalized_location.split(city, 1)[1]
            district = re.match(r"([\u4e00-\u9fff]{1,6}(?:區|鄉|鎮|市))", remainder)
            if district:
                changes["district"] = district.group(1)
            break
    if changes:
        return validate_proposal({
            "intent": "profile.patch",
            "arguments": {"changes": changes},
            "reply": "好的，我會開啟個人資料並填入變更。",
        }, base_revision=revision)
    if "個人資料" in raw or "編輯資料" in raw:
        return VoiceProposal("profile.open", {}, "好的，我幫你開啟個人資料。", revision)
    if "設定" in raw:
        return VoiceProposal("settings.open", {}, "好的，我幫你開啟設定。", revision)
    return None
