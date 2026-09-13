from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from typing import Any

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
})
SENSITIVE_PERMISSION_DEFAULTS = frozenset({
    "match_actions", "private_ayue", "chat_content", "chat_send",
    "calendar_write", "location_precise", "memory_write",
})
NAVIGATION_DESTINATIONS = frozenset({
    "chat", "matching", "profile", "settings", "profile_edit",
    "voice_settings", "calendar", "matching_ayue", "match_hub",
    "memory", "create_post",
})
PUBLIC_AYUE_DOMAINS = frozenset({
    "matching", "web", "places", "memory", "profile",
})
CALENDAR_QUERY_RANGES = frozenset({
    "today", "tomorrow", "week", "weekend", "next_week", "upcoming",
})
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
FEATURE_STATUS_KEYS = frozenset({
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


def visible_choice_action(value: Any) -> str | None:
    compact = normalized_phrase(value)
    if compact in {
        "確認", "確定", "同意", "好", "好的", "可以", "沒問題",
        "就這樣", "幫我確認", "請確認", "確認吧", "好啊",
        "confirm", "confirmed", "yes", "ok", "okay",
    }:
        return "confirm"
    if compact in {
        "取消", "不要", "不用", "不同意", "算了", "先不要",
        "幫我取消", "取消吧",
        "cancel", "cancelled", "no",
    }:
        return "cancel"
    return None


def safe_reply(value: Any) -> str:
    text = str(value or "").strip()[:240]
    text = _PASSWORD_RE.sub(r"\1[已隱藏]", text)
    text = _EMAIL_RE.sub("[Email 已隱藏]", text)
    return _PHONE_RE.sub("[電話已隱藏]", text)


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
    if proposal.intent == "personality.explore":
        return permissions.get("memory_read") is True
    if proposal.intent == "match.ayue_query":
        question = str(proposal.arguments.get("question") or "")
        if (
            any(word in question for word in (
                "開始找", "幫我找", "取消搜尋", "接受", "婉拒", "拒絕", "撤回",
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
            any(word in question for word in ("開始找", "幫我找", "取消搜尋", "接受", "婉拒", "拒絕", "撤回"))
            or delegated_confirmation
        ):
            return permissions.get("match_actions") is True
        return True
    return True


def requires_confirmation(proposal: VoiceProposal) -> bool:
    if ACTIONS.get(proposal.intent, {}).get("confirmation"):
        return True
    return (
        proposal.intent == "settings.set"
        and proposal.arguments.get("key") in CONFIRMED_SETTING_KEYS
    )


def confirmation_phrase(proposal: VoiceProposal) -> str:
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
    if proposal.intent == "chat.request_send":
        return "確認傳送訊息"
    if proposal.intent == "memory.add":
        return "確認新增阿月記憶"
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
    args: dict[str, Any] = {}
    target_ref = raw_args.get("target_ref")
    if target_ref is not None and (not isinstance(target_ref, str) or not REF.fullmatch(target_ref)
                                   or not ACTIONS[intent]["target_kinds"]):
        return None
    if intent == "ui.target.select":
        if not target_ref:
            return None
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
        for key, limit in (("location", 120), ("notes", 500)):
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
        label = re.sub(
            r"\s+", " ", str(raw_args.get("label") or ""),
        ).strip()[:40]
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
        return VoiceProposal("date.query", {"contact_name": ""}, "我查看你的約會邀請與共同安排。", revision)
    if any(phrase in compact for phrase in (
        "配對進度", "媒合進度", "搜尋進度", "配對狀態", "媒合狀態",
        "配對結果", "配對好了嗎", "媒合好了嗎", "配到誰", "配對到誰",
        "有要確認", "待確認的配對", "找到人了嗎", "找到對象了嗎",
    )):
        return VoiceProposal(
            "match.query", {"view": "status"},
            "我直接查看目前配對狀態。", revision,
        )
    if any(phrase in compact for phrase in (
        "打開阿月牽線", "開啟阿月牽線", "查看阿月牽線", "阿月牽線內容",
        "朗讀阿月牽線", "牽線邀請內容", "有哪些牽線", "有哪些邀請",
    )):
        return VoiceProposal(
            "match.query", {"view": "hub"},
            "我直接打開並讀取阿月牽線。", revision,
        )
    navigation = {
        "打開聊天": "chat", "開啟聊天": "chat", "去聊天頁面": "chat",
        "打開配對": "matching", "開啟配對": "matching", "去配對頁面": "matching",
        "打開個人頁面": "profile", "開啟個人頁面": "profile", "去個人頁面": "profile",
        "打開設定": "settings", "開啟設定": "settings", "去設定頁面": "settings",
        "編輯個人資料": "profile_edit", "打開編輯個人資料": "profile_edit",
        "打開語音助理設定": "voice_settings", "阿月語音助理設定": "voice_settings",
        "打開行事曆": "calendar", "開啟行事曆": "calendar",
        "打開配對阿月": "matching_ayue", "開啟配對阿月": "matching_ayue",
        "打開阿月記住的事": "memory", "查看阿月記憶": "memory",
        "打開發文頁": "create_post", "我要發文": "create_post",
    }
    destination = navigation.get(compact)
    if destination:
        return VoiceProposal(
            "app.navigate", {"destination": destination}, "好，我幫你打開。", revision,
        )
    if "戀愛顧問" in raw:
        return VoiceProposal("settings.open", {}, "AI 戀愛顧問目前尚未提供可調整設定。", revision)
    if any(phrase in raw for phrase in (
        "開始個性探索", "個性探索", "人格探索", "性格探索",
        "開始性格測驗", "開始人格測驗",
    )):
        return VoiceProposal(
            "personality.explore", {"message": raw},
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
    if ("貼文" in raw or "po" in raw.lower()) and any(
        word in raw for word in ("幫我", "寫", "撰寫", "發一個", "po")
    ):
        topic = raw
        match = re.search(r"關於(.+?)(?:的(?:故事|貼文)|$)", raw)
        if match:
            topic = match.group(1).strip(" ，。")
        caption = generated_caption or (
            f"今天想記錄一段關於{topic}的故事。\n\n"
            "有些片刻不需要特別安排，回想起來仍然會讓人微笑。"
        )
        return VoiceProposal(
            "post.open_draft", {"caption": caption[:2000]},
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
        label = memory_add.group(2).strip(" ，,。")
        if label:
            return VoiceProposal(
                "memory.add", {"label": label[:40], "stance": stance},
                "我可以把這件事存進阿月記憶。", revision,
            )
    public_domain = None
    if any(word in raw for word in ("行事曆", "行程", "空檔", "有空")):
        if any(word in raw for word in (
            "新增", "建立", "加入", "修改", "改到", "改成", "取消", "刪除",
        )):
            return _deterministic_calendar_create(raw, revision)
        else:
            explicit_range = _explicit_calendar_range(raw)
            if _CALENDAR_EXPLICIT_DATE_RE.search(raw) and explicit_range is None:
                return None
            if explicit_range is not None:
                return VoiceProposal(
                    "calendar.query",
                    {
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
                "calendar.query", {"range": calendar_range},
                working_reply, revision,
            )
    elif any(word in raw for word in ("附近", "餐廳", "咖啡廳", "景點", "距離", "營業")):
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
