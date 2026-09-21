"""Bounded conversational working state; never an authority to execute a write."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .contracts import _calendar_update_clocks, _calendar_update_date, _small_number, validate_proposal

DRAFT_FIELDS = {
    "chat.request_send": {"contact_name": 40, "message": 500},
    "calendar.create": {"title": 80, "date": 10, "start_time": 5,
                        "end_time": 5, "location": 120, "notes": 500},
    "calendar.update": {"target": 80, "title": 80, "date": 10,
                        "start_time": 5, "end_time": 5, "location": 120, "notes": 500},
}
LABELS = {"contact_name": "對象", "message": "訊息內容", "target": "要修改的行程",
          "title": "行程名稱", "date": "日期", "start_time": "開始時間",
          "end_time": "結束時間", "location": "地點", "notes": "備註"}
SAFE_DRAFT_STAGES = frozenset({
    "draft_collecting", "draft_ready", "draft_paused", "waiting_confirmation",
    "confirmation_expired", "context_stale", "session_closed",
    "confirmation_context_lost", "live_session_restarted", "permission_denied", "quota_exhausted",
    "quota_unavailable", "quota_paused", "needs_input", "validation_failed",
})


def draft_editable(row: dict[str, Any]) -> bool:
    return (row.get("capability_id") in DRAFT_FIELDS
            and row.get("status") in {"waiting_input", "waiting_confirmation", "failed"}
            and row.get("stage") in SAFE_DRAFT_STAGES)


def clean_draft(intent: str, values: Any) -> dict[str, str]:
    if intent not in DRAFT_FIELDS or not isinstance(values, dict):
        raise ValueError("draft_fields_invalid")
    allowed = {**DRAFT_FIELDS[intent], "target_ref": 100}
    if set(values) - set(allowed):
        raise ValueError("draft_fields_invalid")
    result = {}
    for key, value in values.items():
        if value is None:
            continue
        if not isinstance(value, str) or len(value.strip()) > allowed[key]:
            raise ValueError("draft_fields_invalid")
        text = re.sub(r"\s+", " ", value).strip()
        if key == "date" and text:
            try:
                if date.fromisoformat(text).isoformat() != text:
                    raise ValueError()
            except ValueError as error:
                raise ValueError("draft_date_invalid") from error
        if key in {"start_time", "end_time"} and text and not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", text):
            raise ValueError("draft_time_invalid")
        result[key] = text
    return result


def missing_fields(intent: str, values: dict[str, Any]) -> list[str]:
    required = {"chat.request_send": ["contact_name", "message"],
                "calendar.create": ["title", "date", "start_time"],
                "calendar.update": ["target"]}[intent]
    missing = [key for key in required if not values.get(key)
               and not (key in {"contact_name", "target"} and values.get("target_ref"))]
    if intent == "calendar.update" and not any(key in values for key in ("title", "date", "start_time", "end_time", "location", "notes")):
        missing.append("start_time")
    return missing


def draft_question(intent: str, values: dict[str, Any]) -> str:
    missing = missing_fields(intent, values)
    if missing:
        return {"contact_name": "要傳給哪一位？", "message": "想傳什麼內容？",
                "title": "這個行程要叫什麼名稱？", "date": "安排在哪一天？",
                "start_time": "幾點開始？", "target": "要修改哪個行程？"}[missing[0]]
    if values.get("start_time") and values.get("end_time") and values["end_time"] <= values["start_time"]:
        return "結束時間需要晚於開始時間，想改成幾點結束？"
    if intent == "calendar.create" and values.get("date", "") < datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat():
        return "原本的日期已經過了，要改到哪一天？"
    return ""


def draft_summary(intent: str, values: dict[str, Any]) -> str:
    if intent == "chat.request_send":
        return f"傳給{values.get('contact_name') or '目前選擇的對象'}：{values.get('message') or '尚未填寫內容'}"
    if intent == "calendar.update":
        target = values.get("target") or "目前選擇的行程"
        changes = [f"{LABELS[key]}：{values[key] or '清除'}"
                   for key in DRAFT_FIELDS[intent] if key != "target" and key in values]
        return f"修改「{target}」：{'；'.join(changes) or '尚未指定修改內容'}"
    parts = [values.get("title"), values.get("date"), values.get("start_time")]
    if values.get("end_time"):
        parts.append(f"到 {values['end_time']}")
    if values.get("location"):
        parts.append(values["location"])
    if values.get("notes"):
        parts.append(f"備註：{values['notes']}")
    return " · ".join(str(part) for part in parts if part) or "尚未填寫行程內容"


def draft_projection(row: dict[str, Any], *, include_values: bool = True) -> dict[str, Any]:
    intent = row["capability_id"]
    values = row.get("arguments") or {}
    result = {"summary": draft_summary(intent, values) if include_values else row.get("title", ""),
              "missing_fields": missing_fields(intent, values),
              "changed_fields": row.get("draft_changed_fields") or [],
              "editable": draft_editable(row),
              "prompt": draft_question(intent, values) or row.get("result_summary") or "草稿已保留，繼續後會重新確認。"}
    if include_values:
        result["values"] = {key: values[key] for key in DRAFT_FIELDS[intent] if key in values}
    return result


def validate_complete_draft(intent: str, values: dict[str, Any]) -> None:
    if draft_question(intent, values) or validate_proposal({"intent": intent, "arguments": values}, base_revision=0) is None:
        raise ValueError("draft_needs_input")


def spoken_draft_patch(text: str, intent: str, values: dict[str, Any]) -> dict[str, str] | None:
    """Only consume complete, narrow corrections; leave other utterances to Live."""
    if intent == "chat.request_send":
        content = re.fullmatch(r"(?:訊息|內容)\s*(?:改成|換成|改為|是|：|:)\s*(.+)", text.strip(), re.DOTALL)
        if content:
            return {"message": content[1].strip("「」『』")}
    raw = text.strip().rstrip("。！？!?")
    patch: dict[str, str] = {}
    for part in re.split(r"[，,；;]", raw):
        part = part.strip()
        if re.fullmatch(r'(?:其他|其餘|其他條件|其餘條件|地點|對象|日期|備註|名稱)(?:都)?(?:不變|保留|不用改)',part):
            continue
        relative = re.fullmatch(r'(?:整個行程|時間|行程)?\s*(往後|延後|往前|提早|提前)\s*(半小時|[一二兩三四五六七八九十\d]+(?:分鐘|分|小時))',part)
        if relative and intent.startswith('calendar.') and values.get('start_time'):
            unit = relative[2]
            amount = 30 if unit == '半小時' else _small_number(re.sub(r'分鐘|小時|分','',unit))
            if amount is None:
                return None
            if unit.endswith('小時') and unit != '半小時':
                amount *= 60
            if relative[1] in {'往前','提早','提前'}:
                amount *= -1
            moved = {}
            for key in ('start_time','end_time'):
                if not values.get(key):
                    continue
                hour, minute = map(int, values[key].split(':'))
                total = hour*60+minute+amount
                # Multi-day mutations need a new explicit date confirmation.
                if not 0 <= total < 1440:
                    return None
                moved[key] = f'{total//60:02d}:{total%60:02d}'
            patch.update(moved)
            continue
        labelled = re.fullmatch(r"(對象|收件人|訊息|內容|標題|名稱|地點|備註|行程)\s*(?:改成|換成|改為|是|：|:)\s*(.+)", part)
        if labelled:
            key = {"對象": "contact_name", "收件人": "contact_name", "訊息": "message", "內容": "message",
                   "標題": "title", "名稱": "title", "地點": "location", "備註": "notes", "行程": "target"}[labelled[1]]
            if key not in DRAFT_FIELDS[intent]:
                return None
            patch[key] = labelled[2].strip("「」『』")
            continue
        if intent == "chat.request_send":
            contact = re.fullmatch(r"(?:改傳給|換成|改成|傳給)\s*([^，。！？!?]{1,40})", part)
            if contact and not any(w in contact[1] for w in ("點", "改", "傳", "查", "取消")):
                patch["contact_name"] = contact[1]
                continue
            # Free-form messages require the explicit content label, so "查天氣"
            # and "取消" can never accidentally become a message to send.
            return None
        clock_text = re.sub(r"^(?:不對[，,]?|時間|開始時間|結束時間)?\s*(?:改成|改到|改為|換成|改)?\s*", "", part)
        if re.fullmatch(r"(?:上午|早上|下午|晚上|傍晚|中午|凌晨)?\s*(?:\d{1,2}:\d{2}|[零〇一二兩三四五六七八九十\d]{1,3}[點点時时](?:半|[零〇一二兩三四五六七八九十\d]{1,3}分)?)", clock_text):
            clocks = _calendar_update_clocks(clock_text)
            if clocks:
                needs_end = bool(values.get("end_time") and values.get("start_time")
                                 and values["end_time"] <= values["start_time"])
                key = "end_time" if part.startswith("結束") or (needs_end and not part.startswith("開始")) else "start_time"
                value = clocks[0]
                # "改七點" keeps the existing evening period; don't turn 18:00 into 07:00.
                if not re.search(r"上午|早上|下午|晚上|傍晚|中午|凌晨|:", clock_text) and int(value[:2]) < 12 and int(str(values.get(key) or "00:00")[:2]) >= 12:
                    value = f"{int(value[:2]) + 12:02d}{value[2:]}"
                patch[key] = value
                continue
        day_text = re.sub(r"^(?:日期)?\s*(?:改成|改到|換成|改為)?\s*", "", part)
        if re.fullmatch(r"今天|明天|後天|后天|(?:下|這|本)?(?:週|周|星期)[一二三四五六日天]|\d{4}-\d{2}-\d{2}|\d{1,2}月\d{1,2}[日號]?", day_text):
            day = _calendar_update_date(day_text)
            week = re.fullmatch(r"(下|這|本)?(?:週|周|星期)([一二三四五六日天])", day_text)
            if week:
                today = datetime.now(ZoneInfo("Asia/Taipei")).date()
                weekday = "一二三四五六日".index(week[2].replace("天", "日"))
                offset = weekday - today.weekday()
                if week[1] == "下" or (week[1] is None and offset < 0):
                    offset += 7
                day = (today + timedelta(days=offset)).isoformat()
            if day:
                patch["date"] = day
                continue
        return None
    return patch or None


def draft_tool(types: Any) -> Any:
    properties = {key: {"type": "string", "maxLength": limit}
                  for fields in DRAFT_FIELDS.values() for key, limit in fields.items()}
    return types.FunctionDeclaration(
        name="manage_voice_draft",
        description=("Prepare or correct a message/calendar draft without executing it. "
                     "Use partial fields and ask only the returned missing question. "
                     "For corrections send ONLY changed fields, keeping all others. "
                     "Use list/resume for unfinished drafts, pause for detours, cancel to discard. "
                     "Never invent contacts, dates, message text, or task refs. A fresh confirmation is required."),
        parameters_json_schema={"type": "object", "additionalProperties": False,
            "properties": {"action": {"type": "string", "enum": ["start", "update", "list", "resume", "pause", "cancel"]},
                           "intent": {"type": "string", "enum": list(DRAFT_FIELDS)},
                           "task_ref": {"type": "string", "maxLength": 80},
                           "fields": {"type": "object", "additionalProperties": False, "properties": properties}},
            "required": ["action"]},
    )
