"""Bounded agent runtime for private mediator conversations.

The planner may use a partner's private profile to choose a conversation
strategy, but the renderer never receives that raw profile.  This keeps the
mediator useful without turning private profile data into chat disclosures.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any

from database import db, messages_coll, profiles_coll
from services.ai_service import generate_chat_completion
from services.calendar_service import calendar_access_enabled, get_calendar_context, get_timezone
from services.chat_service import generate_room_id

from .contracts import AgentResult


RUNS = db["agent_runs"]
PRIVATE_CONFIRM_TTL = 15 * 60


@dataclass(frozen=True)
class PrivateAgentTurnContext:
    user_id: str
    other_id: str
    room_id: str
    message: str
    match_doc: dict[str, Any]
    user_profile: dict[str, Any]


def private_agent_mode_for_user(user_id: str) -> str:
    mode = os.getenv("AYUE_PRIVATE_AGENTIC_MODE", "off").strip().lower()
    if mode not in {"off", "shadow", "on"}:
        mode = "off"
    allowlist = {
        item.strip()
        for item in os.getenv("AYUE_PRIVATE_AGENTIC_USER_ALLOWLIST", "").split(",")
        if item.strip()
    }
    return mode if not allowlist or user_id in allowlist else "off"


def _trace(run_id: str, ctx: PrivateAgentTurnContext, mode: str, payload: dict[str, Any]) -> None:
    """Keep diagnostics metadata-only; never persist private text or evidence."""
    try:
        RUNS.insert_one({
            "run_id": run_id,
            "surface": "private_mediator",
            "user_id": ctx.user_id,
            "other_id": ctx.other_id,
            "match_id": str(ctx.match_doc.get("_id", "")),
            "mode": mode,
            "created_at": time.time(),
            **payload,
        })
    except Exception as exc:
        print(f"Private agent trace skipped: {exc}")


def _compact(message: str) -> str:
    return re.sub(r"\s+", "", message or "").lower()


_DATE_MARKER_RE = re.compile(r"(?:\d{1,4}[/-])?\d{1,2}月\d{1,2}日?|\d{4}[/-]\d{1,2}[/-]\d{1,2}")


def is_private_calendar_query(message: str) -> bool:
    compact = _compact(message)
    calendar_words = ("行事曆", "日曆", "行程", "時間表")
    range_words = ("本週", "這週", "下週", "這個月", "本月", "下個月", "下月", "哪天", "哪一天", "哪幾天", "哪些日期", "幾月幾號", "什麼時候")
    availability_words = ("有空", "沒空", "不行", "不能", "不方便", "忙", "有事", "有安排", "可不可以", "方便")
    question_words = ("嗎", "？", "?", "看", "有沒有", "哪天", "什麼時候", "幾點", "哪些")
    has_date_scope = bool(_DATE_MARKER_RE.search(compact)) or any(word in compact for word in range_words)
    has_availability = any(word in compact for word in availability_words)
    return (any(word in compact for word in calendar_words) and (has_availability or any(word in compact for word in question_words))) or (has_date_scope and has_availability)


def _month_start(value: date) -> date:
    return value.replace(day=1)


def _next_month(value: date) -> date:
    return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)


def _calendar_range_for_message(message: str, now: datetime | None = None) -> tuple[datetime, datetime, bool]:
    """Resolve a Taiwan-local availability range and cap it to 31 days."""
    zone = get_timezone("Asia/Taipei")
    local_now = (now or datetime.now(timezone.utc)).astimezone(zone)
    today = local_now.date()
    compact = _compact(message)
    start_day = today
    end_day = today + timedelta(days=31)

    if "本週" in compact or "這週" in compact:
        start_day = today - timedelta(days=today.weekday())
        end_day = start_day + timedelta(days=7)
    elif "下週" in compact:
        start_day = today - timedelta(days=today.weekday()) + timedelta(days=7)
        end_day = start_day + timedelta(days=7)
    elif "這個月" in compact or "本月" in compact:
        start_day = _month_start(today)
        end_day = _next_month(start_day)
    elif "下個月" in compact or "下月" in compact:
        start_day = _next_month(_month_start(today))
        end_day = _next_month(start_day)
    else:
        match = re.search(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})", compact)
        month_day = re.search(r"(\d{1,2})月(\d{1,2})日?", compact)
        try:
            if match:
                start_day = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
                end_day = start_day + timedelta(days=1)
            elif month_day:
                start_day = date(today.year, int(month_day.group(1)), int(month_day.group(2)))
                if start_day < today:
                    start_day = start_day.replace(year=start_day.year + 1)
                end_day = start_day + timedelta(days=1)
        except ValueError:
            start_day = today
            end_day = today + timedelta(days=31)

    truncated = end_day - start_day > timedelta(days=31)
    if truncated:
        end_day = start_day + timedelta(days=31)
    start = datetime.combine(start_day, datetime.min.time(), zone).astimezone(timezone.utc)
    end = datetime.combine(end_day, datetime.min.time(), zone).astimezone(timezone.utc)
    return start, end, truncated


def is_private_fun_fact_request(message: str) -> bool:
    compact = _compact(message)
    return "有趣小事" in compact or "問趣事" in compact or ("幫我問" in compact and "趣事" in compact)


def _partner_advisory_profile(other_id: str) -> dict[str, Any]:
    """Private-only material for the strategy planner, never the renderer."""
    doc = profiles_coll.find_one({"user_id": other_id}, {
        "_id": 0,
        "big_five.summary": 1,
        "deep_profile.values": 1,
        "deep_profile.life_goals": 1,
        "deep_profile.relationship_needs": 1,
        "deep_profile.stress_coping": 1,
        "deep_profile.ideal_future": 1,
        "deep_profile.summary": 1,
        "current_context": 1,
        "profile_memory_preview": 1,
    }) or {}
    deep = doc.get("deep_profile") or {}
    memories = []
    for item in (doc.get("profile_memory_preview") or [])[:8]:
        if isinstance(item, dict) and item.get("label"):
            memories.append({
                "label": str(item.get("label"))[:60],
                "stance": str(item.get("stance", "like"))[:12],
                "category": str(item.get("category", ""))[:30],
            })
    return {
        "big_five_summary": (doc.get("big_five") or {}).get("summary", ""),
        "deep_profile": {
            key: deep.get(key)
            for key in ("values", "life_goals", "relationship_needs", "stress_coping", "ideal_future", "summary")
            if deep.get(key)
        },
        "current_context": doc.get("current_context", ""),
        "memories": memories,
    }


def _shared_and_consented_facts(ctx: PrivateAgentTurnContext) -> list[dict[str, str]]:
    facts: list[dict[str, str]] = []
    relationship_memory = ctx.match_doc.get("relationship_memory") or {}
    summary = str(relationship_memory.get("shared_summary") or "").strip()
    if summary:
        facts.append({"evidence_id": "shared:summary", "visibility": "shared_fact", "value": summary[:500]})

    pair_room_id = generate_room_id(ctx.match_doc["from_user"], ctx.match_doc["to_user"])
    messages = list(messages_coll.find(
        {"room_id": pair_room_id}, {"_id": 0, "sender_id": 1, "content": 1}
    ).sort("timestamp", -1).limit(12))[::-1]
    for index, item in enumerate(messages):
        content = str(item.get("content") or "").strip()
        if content:
            facts.append({
                "evidence_id": f"shared:message:{index}",
                "visibility": "shared_fact",
                "value": content[:300],
            })

    probe_results = ((ctx.match_doc.get("mediator_state") or {}).get("probe_results") or {})
    for probe_id, result in probe_results.items():
        if not isinstance(result, dict):
            continue
        if result.get("status") != "completed" or not result.get("shareable"):
            continue
        if result.get("answered_by") != ctx.other_id:
            continue
        answer = str(result.get("answer") or "").strip()
        if answer:
            facts.append({
                "evidence_id": f"consented:probe:{probe_id}",
                "visibility": "consented_fact",
                "value": answer[:300],
            })
    return facts[:18]


def _partner_busy(ctx: PrivateAgentTurnContext, start: datetime, end: datetime) -> tuple[bool, list[dict[str, str]]]:
    """Return only busy intervals. No IDs or event metadata leave this facade."""
    if not calendar_access_enabled(ctx.other_id):
        return False, []
    events = get_calendar_context(ctx.user_id, ctx.other_id, start, end)
    busy = []
    for event in events.get("partner_busy", []):
        start_at, end_at = event.get("start_at"), event.get("end_at")
        if start_at and end_at:
            busy.append({"start_at": str(start_at), "end_at": str(end_at), "busy": "true"})
    return True, busy[:16]


def _calendar_reply(access_enabled: bool, busy: list[dict[str, str]], *, truncated: bool = False) -> str:
    if not access_enabled:
        return "對方沒有開放阿月使用行事曆，我不能替他判斷時間。"
    if not busy:
        suffix = "；查詢範圍已先截成 31 天" if truncated else ""
        return f"這段期間目前沒看到對方行事曆上的衝突{suffix}，但不代表他一定有空，最好還是直接問他。"
    slots = []
    for event in busy[:5]:
        try:
            start = datetime.fromisoformat(event["start_at"].replace("Z", "+00:00")).astimezone(get_timezone("Asia/Taipei"))
            end = datetime.fromisoformat(event["end_at"].replace("Z", "+00:00")).astimezone(get_timezone("Asia/Taipei"))
            slots.append(f"{start.month}/{start.day} {start:%H:%M}–{end:%H:%M}")
        except (KeyError, ValueError):
            continue
    if not slots:
        return "我看得出對方近期有幾段已安排的時間，但不能透露內容。"
    suffix = "（查詢範圍已先截成 31 天）" if truncated else ""
    return "我只能看出對方這幾段時間已有安排：" + "、".join(slots) + "。活動內容我不能透露。" + suffix


def _asks_event_detail(message: str) -> bool:
    compact = _compact(message)
    return any(word in compact for word in ("做什麼", "什麼事", "活動", "去哪", "跟誰", "內容"))


def _is_partner_event_detail_query(message: str) -> bool:
    compact = _compact(message)
    return _asks_event_detail(message) and any(word in compact for word in ("他", "她", "對方"))


def _strategy(ctx: PrivateAgentTurnContext, advisory: dict[str, Any]) -> dict[str, str]:
    prompt = f"""
你是私人媒人阿月的策略規劃器。你可閱讀對方私人資料，但絕不能輸出任何事實、引用、名字、偏好或原句。
只輸出 JSON，欄位和可用值如下：
pace: slow|normal|direct
 directness: gentle|balanced|clear
 tone: warm|playful|calm
 topic_style: curious|practical|empathetic|light
 next_action: ask_open_question|share_self|suggest_topic|wait|clarify
使用者訊息：{ctx.message}
對方私人資料：{json.dumps(advisory, ensure_ascii=False)}
"""
    fallback = {"pace": "normal", "directness": "balanced", "tone": "warm", "topic_style": "curious", "next_action": "suggest_topic"}
    allowed = {
        "pace": {"slow", "normal", "direct"},
        "directness": {"gentle", "balanced", "clear"},
        "tone": {"warm", "playful", "calm"},
        "topic_style": {"curious", "practical", "empathetic", "light"},
        "next_action": {"ask_open_question", "share_self", "suggest_topic", "wait", "clarify"},
    }
    try:
        raw = json.loads(generate_chat_completion(prompt, temperature=0, json_output=True).content)
        return {key: raw.get(key) if raw.get(key) in values else fallback[key] for key, values in allowed.items()}
    except Exception:
        return fallback


def _render_advice(ctx: PrivateAgentTurnContext, strategy: dict[str, str], facts: list[dict[str, str]]) -> str:
    prompt = f"""
你是阿月，使用者的共同朋友。請用自然、簡短、繁體中文給出一到三句具體建議。
你只能根據「可直接引用資料」說對方的特定事實；策略只代表語氣與做法，不能反推或透露私人資料。
不要說你看過對方的 profile、記憶、行事曆或私人資料。
使用者訊息：{ctx.message}
策略：{json.dumps(strategy, ensure_ascii=False)}
可直接引用資料：{json.dumps(facts, ensure_ascii=False)}
"""
    try:
        return generate_chat_completion(prompt, temperature=0.35, json_output=False).content.strip()
    except Exception:
        return "我會建議先從輕一點的話題接，讓對方比較好回；你也可以先分享一小段自己的經驗。"


def _fun_fact_reply(ctx: PrivateAgentTurnContext, *, dry_run: bool) -> tuple[bool, str]:
    """Delegate the actual queue mutation to the existing idempotent workflow."""
    if dry_run:
        return False, ""
    from services.relationship_engagement_service import participant_probe_state, queue_manual_fun_fact_probe

    state = participant_probe_state(ctx.match_doc, ctx.other_id)
    if state.get("kind") == "fun_fact" and state.get("status") in {"queued", "awaiting_answer", "awaiting_sentiment", "awaiting_consent"}:
        return True, "我已經在問這題了，等對方回覆就好。"
    now = time.time()
    if state.get("kind") == "fun_fact" and state.get("status") in {"completed", "declined"} and float(state.get("cooldown_until", 0) or 0) > now:
        return True, "這題最近已經問過了，我先不重複打擾對方；想換個方向我可以幫你想。"
    queued = queue_manual_fun_fact_probe(ctx.match_doc, ctx.user_id, ctx.other_id)
    return True, "我去問問他，拿到適合公開的小趣事再帶回來給你。" if queued else "這題目前不適合重複問，我先不打擾對方。"


def run_private_agent_turn(ctx: PrivateAgentTurnContext, *, mode: str) -> AgentResult:
    run_id = uuid.uuid4().hex
    started = time.perf_counter()
    trace: dict[str, Any] = {"route": "advice", "tools": [], "fallback_reason": None}
    if ctx.match_doc.get("status") != "accepted":
        result = AgentResult(handled=False, agent_run_id=run_id, agent_mode=mode, fallback_reason="match_not_accepted")
        _trace(run_id, ctx, mode, trace | {"fallback_reason": result.fallback_reason})
        return result

    try:
        if is_private_calendar_query(ctx.message) or _is_partner_event_detail_query(ctx.message):
            trace["route"] = "partner_availability"
            start, end, truncated = _calendar_range_for_message(ctx.message)
            access_enabled, busy = _partner_busy(ctx, start, end)
            trace["tools"].append("private_calendar.get_partner_availability")
            reply = "我只能看出那段時間已有安排，不能看到或透露內容。" if _asks_event_detail(ctx.message) else _calendar_reply(access_enabled, busy, truncated=truncated)
            result = AgentResult(handled=True, reply=reply, conversation_intent="private_partner_availability", agent_run_id=run_id, agent_mode=mode)
        elif is_private_fun_fact_request(ctx.message):
            trace["route"] = "fun_fact_disabled"
            result = AgentResult(
                handled=True,
                reply="這個功能目前先收起來了，不過我可以幫你想一個自然的開場方式。",
                conversation_intent="private_advice",
                agent_run_id=run_id,
                agent_mode=mode,
            )
        else:
            advisory = _partner_advisory_profile(ctx.other_id)
            facts = _shared_and_consented_facts(ctx)
            trace["tools"].append("private_relationship.get_safe_context")
            strategy = _strategy(ctx, advisory)
            reply = _render_advice(ctx, strategy, facts)
            result = AgentResult(handled=True, reply=reply, conversation_intent="private_relationship_advice", agent_run_id=run_id, agent_mode=mode)
    except Exception as exc:
        trace["fallback_reason"] = type(exc).__name__
        result = AgentResult(handled=False, agent_run_id=run_id, agent_mode=mode, fallback_reason=type(exc).__name__)

    trace["latency_ms"] = round((time.perf_counter() - started) * 1000)
    _trace(run_id, ctx, mode, trace)
    return result
