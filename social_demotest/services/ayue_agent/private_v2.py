"""OpenClaw-style sibling runtime for the accepted-pair private mediator."""

from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal

from pydantic import BaseModel, ConfigDict, Field

from database import db, messages_coll, profiles_coll
from services.ai_service import generate_chat_completion
from services.chat_service import generate_room_id
from services.profile_projection import safe_recent_context

from .contracts import AgentResult
from .private_runtime import (
    _calendar_range_for_message,
    _partner_advisory_profile,
    _partner_busy,
    _shared_and_consented_facts,
    private_agent_mode_for_user,
)
from .public_relationship_projection import display_name, safe_public_profile


PRIVATE_RUNS = db["private_agent_runs"]
PRIVATE_CONFIRM_TTL = 15 * 60
MAX_STEPS = 3
YES = {"好", "好的", "可以", "確認", "確定", "要", "yes", "ok"}
NO = {"不要", "不用", "取消", "先不要", "no"}


class PrivateToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    risk: Literal["read", "write"]
    description: str
    progress_text: str
    requires_confirmation: bool = False


PRIVATE_TOOL_REGISTRY = {
    "private.relationship.get_pair_summary": PrivateToolSpec(name="private.relationship.get_pair_summary", risk="read", description="讀取這段已接受關係中可公開、共同或已同意分享的摘要。", progress_text="我整理一下你們已確認的共同資訊…"),
    "private.relationship.get_shared_history": PrivateToolSpec(name="private.relationship.get_shared_history", risk="read", description="讀取這一對在共同聊天室的近期對話脈絡。", progress_text="我回看一下你們最近聊到哪裡…"),
    "private.calendar.get_counterparty_availability": PrivateToolSpec(name="private.calendar.get_counterparty_availability", risk="read", description="確認對方在指定期間的 busy/free 時段；不可讀取行程內容。", progress_text="我確認一下對方那段時間是否已有安排…"),
    "private.date.start_coordination": PrivateToolSpec(name="private.date.start_coordination", risk="write", description="發起雙方同意的約會協調；會通知對方，必須先確認。", progress_text="我準備先問對方是否願意一起協調約會…", requires_confirmation=True),
}


class PrivateAgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["final", "tool_call", "confirmation"]
    intent: Literal["advice", "pair_summary", "shared_history", "availability", "date_coordination", "unclear"] = "advice"
    tool_name: str | None = None
    arguments: dict[str, str] = Field(default_factory=dict)
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_span: str = ""
    strategy: Literal["warm", "playful", "calm", "direct"] = "warm"
    reply: str = ""


@dataclass(frozen=True)
class PrivateAgentTurnContextV2:
    user_id: str
    other_id: str
    room_id: str
    message: str
    pair_revision: int
    viewer_profile: dict[str, Any]
    counterparty_shareable: dict[str, Any]
    counterparty_advisory: dict[str, Any]
    shared_history: list[dict[str, str]]
    private_history: list[dict[str, str]]
    shared_facts: list[dict[str, str]]
    local_time: str


def private_v2_mode_for_user(user_id: str) -> str:
    return private_agent_mode_for_user(user_id)


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", value or "").lower()


def _confirmation_choice(message: str) -> str:
    compact = _compact(message)
    return "confirm" if compact in YES else "cancel" if compact in NO else "none"


def _bounded_history(room_id: str, *, owner_id: str, other_id: str | None = None) -> list[dict[str, str]]:
    raw = list(messages_coll.find({"room_id": room_id}, {"_id": 0, "sender_id": 1, "content": 1}).sort("timestamp", -1).limit(12))[::-1]
    total, result = 0, []
    for item in raw:
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()[:500]
        if not content or total + len(content) > 6000:
            continue
        sender = item.get("sender_id")
        role = "本人" if sender == owner_id else "對方" if other_id and sender == other_id else "阿月"
        result.append({"role": role, "content": content})
        total += len(content)
    return result


def build_private_turn_context_v2(user_id: str, other_id: str, message: str, match_doc: dict[str, Any]) -> PrivateAgentTurnContextV2:
    viewer = profiles_coll.find_one({"user_id": user_id}, {"_id": 0, "current_context": 1, "initial_interest": 1, "big_five.summary": 1, "profile_memory_preview": 1}) or {}
    own_memories = [
        str(item.get("label") or "")[:50] for item in (viewer.get("profile_memory_preview") or [])
        if isinstance(item, dict) and item.get("label")
    ][:8]
    shareable = safe_public_profile(other_id)
    shareable["display_name"] = display_name(other_id)
    pair_room = generate_room_id(match_doc["from_user"], match_doc["to_user"])
    private_room = f"mediator_private::{user_id}::{other_id}"
    return PrivateAgentTurnContextV2(
        user_id=user_id, other_id=other_id, room_id=private_room, message=message,
        pair_revision=int(match_doc.get("proposal_revision", 0) or 0),
        viewer_profile={
            "recent_context": safe_recent_context(viewer.get("current_context"), ""),
            "initial_interest": str(viewer.get("initial_interest") or "")[:120],
            "personality_summary": str((viewer.get("big_five") or {}).get("summary") or "")[:180],
            "memories": own_memories,
        },
        counterparty_shareable=shareable,
        counterparty_advisory=_partner_advisory_profile(other_id),
        shared_history=_bounded_history(pair_room, owner_id=user_id, other_id=other_id),
        private_history=_bounded_history(private_room, owner_id=user_id),
        shared_facts=_shared_and_consented_facts(type("PrivateContext", (), {"match_doc": match_doc, "user_id": user_id, "other_id": other_id})()),
        local_time=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
    )


def _planner_prompt(ctx: PrivateAgentTurnContextV2, observations: list[dict[str, Any]]) -> str:
    safe = {
        "message": ctx.message, "viewer_profile": ctx.viewer_profile,
        "counterparty_shareable": ctx.counterparty_shareable,
        "shared_history": ctx.shared_history, "private_history": ctx.private_history,
        "shared_facts": ctx.shared_facts, "local_time": ctx.local_time,
        "visible_tools": sorted(PRIVATE_TOOL_REGISTRY), "observations": observations,
        # This section is planner-only. It can influence only the four strategy
        # labels below, never text or factual output.
        "counterparty_advisory_strategy_only": ctx.counterparty_advisory,
    }
    return f"""你是阿月悄悄話的單一回合規劃與回覆器。先判斷是否需要工具；若輸出 final，reply 必須是可直接給使用者的完整自然回答。
你正在協助一位已接受配對中的使用者。本人與對方資料已分開標示；對方 advisory 僅能影響 strategy（warm/playful/calm/direct），不可出現在事實、工具參數或回答裡。
只能使用 visible_tools。對方行事曆只能查 busy/free。開始約會協調會打擾對方，必須輸出 confirmation，不能直接 tool_call。問趣事功能目前暫停，不可提議或執行；被問到時自然說功能先收起來，並改為協助想開場。一般建議、看共同聊天、問對方是誰或近況可用 read tools；沒有需要讀取時輸出 final。不可輸出 ID、revision、資料庫欄位、私人資料或工具內部資訊。
reply 只能依 safe context 與 observations 作答，以繁體中文、1 到 3 句；不可提及工具、模型、系統、權限、私人資料、ID、revision 或資料庫。對方行事曆只能說 busy/free。
只輸出 JSON：{{"kind":"final|tool_call|confirmation","intent":"advice|pair_summary|shared_history|availability|date_coordination|unclear","tool_name":null,"arguments":{{"scope":""}},"confidence":0.0,"evidence_span":"使用者原句子字串","strategy":"warm|playful|calm|direct","reply":""}}。
安全 context：{json.dumps(safe, ensure_ascii=False)}"""


def _plan(ctx: PrivateAgentTurnContextV2, observations: list[dict[str, Any]]) -> PrivateAgentDecision | None:
    try:
        decision = PrivateAgentDecision.model_validate(json.loads(generate_chat_completion(_planner_prompt(ctx, observations), temperature=0, json_output=True)))
        if decision.kind != "final" and (decision.confidence < .65 or decision.tool_name not in PRIVATE_TOOL_REGISTRY):
            return None
        if decision.kind == "confirmation" and not PRIVATE_TOOL_REGISTRY[decision.tool_name].requires_confirmation:
            return None
        if decision.evidence_span and decision.evidence_span not in ctx.message:
            return None
        return decision
    except Exception:
        return None


def _safe_pair_summary(ctx: PrivateAgentTurnContextV2) -> dict[str, Any]:
    return {"counterparty": ctx.counterparty_shareable, "shared_facts": ctx.shared_facts[:8]}


def _availability(ctx: PrivateAgentTurnContextV2, scope: str) -> dict[str, Any]:
    start, end, truncated = _calendar_range_for_message(scope or ctx.message)
    access, busy = _partner_busy(type("PrivateContext", (), {"user_id": ctx.user_id, "other_id": ctx.other_id})(), start, end)
    return {"access": access, "busy": busy, "truncated": truncated}


def _execute_read(name: str, ctx: PrivateAgentTurnContextV2, arguments: dict[str, str]) -> tuple[bool, dict[str, Any], str | None]:
    if name == "private.relationship.get_pair_summary":
        return True, _safe_pair_summary(ctx), None
    if name == "private.relationship.get_shared_history":
        return True, {"messages": ctx.shared_history}, None
    if name == "private.calendar.get_counterparty_availability":
        return True, _availability(ctx, str(arguments.get("scope") or "")), None
    return False, {}, "tool_not_allowed"


def _execute_write(name: str, user_id: str, other_id: str, match_doc: dict[str, Any]) -> tuple[bool, str]:
    if name == "private.date.start_coordination":
        from services.date_coordination_service import create_invite
        return bool(create_invite(match_doc, user_id, other_id)), "date_invite_created"
    return False, "tool_not_allowed"


def _save_confirmation(ctx: PrivateAgentTurnContextV2, decision: PrivateAgentDecision) -> str:
    profiles_coll.update_one({"user_id": ctx.user_id}, {"$set": {"private_agent_confirmation": {
        "other_id": ctx.other_id, "action": decision.tool_name, "pair_revision": ctx.pair_revision,
        "created_at": time.time(), "strategy": decision.strategy,
    }}}, upsert=True)
    return "要我現在送出這個邀請嗎？你回覆「確認」我才會進行。"


def _consume_confirmation(ctx: PrivateAgentTurnContextV2, match_doc: dict[str, Any]) -> tuple[bool, str | None]:
    choice = _confirmation_choice(ctx.message)
    if choice == "none":
        return False, None
    profile = profiles_coll.find_one_and_update(
        {"user_id": ctx.user_id, "private_agent_confirmation.other_id": ctx.other_id},
        {"$unset": {"private_agent_confirmation": ""}},
    ) or {}
    pending = profile.get("private_agent_confirmation") or {}
    if not pending or float(pending.get("created_at", 0) or 0) + PRIVATE_CONFIRM_TTL < time.time():
        return True, "剛才那個邀請已經過期了；如果你還想進行，我可以再幫你整理一次。"
    if choice == "cancel":
        return True, "好，我先不會通知對方。"
    if str(pending.get("action") or "") == "private.relationship.request_fun_fact":
        return True, "這個功能目前先收起來了，我先不會聯絡對方。"
    if match_doc.get("status") != "accepted" or int(match_doc.get("proposal_revision", 0) or 0) != int(pending.get("pair_revision", -1)):
        return True, "你們的關係狀態剛有變動，我先不送出舊的邀請。"
    ok, code = _execute_write(str(pending.get("action") or ""), ctx.user_id, ctx.other_id, match_doc)
    if code == "date_invite_created":
        return True, "好，我已經先問對方是否願意一起協調約會；對方同意後會到共同聊天室確認。" if ok else "目前已經有一個約會協調在進行中，我先不重複通知對方。"
    return True, "這個邀請目前無法送出。"


def _compose(ctx: PrivateAgentTurnContextV2, observations: list[dict[str, Any]], strategy: str) -> str:
    safe = {
        "message": ctx.message, "viewer_profile": ctx.viewer_profile,
        "counterparty_shareable": ctx.counterparty_shareable, "shared_history": ctx.shared_history,
        "shared_facts": ctx.shared_facts, "observations": observations, "strategy": strategy,
    }
    prompt = f"""你是阿月，正在私下協助一段已接受配對。以繁體中文、1 到 3 句自然回答。
只能依 safe context 回答；不能提及、猜測或暗示對方未公開的私人資料、私人悄悄話或行事曆內容。calendar observation 只能說是否有既有安排與時段，不能說活動內容。不要提及工具、模型、系統或權限。
安全 context：{json.dumps(safe, ensure_ascii=False)}"""
    try:
        reply = str(generate_chat_completion(prompt, temperature=.45) or "").strip()
        if reply and not re.search(r"(?:私人資料|工具|prompt|seed_user|資料庫)", reply, re.I):
            return reply[:360]
    except Exception:
        pass
    if observations and observations[-1].get("tool") == "private.relationship.get_shared_history":
        return "我看過你們最近的對話了；可以先接住對方最後提到的事，再分享一小段自己的感受，會比較自然。"
    return "我會建議先從你們已經聊過的內容延伸，不急著一次問太多；你想的話可以把你準備傳的話貼給我一起調整。"


def _planner_reply(decision: PrivateAgentDecision) -> str | None:
    reply = re.sub(r"\s+", " ", str(decision.reply or "")).strip()
    if not reply or len(reply) > 360:
        return None
    if re.search(r"(?:私人資料|工具|prompt|seed_user|資料庫|revision|系統|權限)", reply, re.I):
        return None
    return reply


def _trace(run_id: str, payload: dict[str, Any]) -> None:
    try:
        PRIVATE_RUNS.insert_one({"run_id": run_id, "surface": "private_mediator_v2", "created_at": time.time(), **payload})
    except Exception:
        pass


def run_private_agent_turn_v2(
    *, user_id: str, other_id: str, message: str, match_doc: dict[str, Any], on_progress: Callable[[dict[str, str]], None] | None = None,
    agent_run_id: str | None = None,
) -> AgentResult:
    started = time.perf_counter()
    run_id, observations, trace = agent_run_id or uuid.uuid4().hex, [], {"visible_tools": sorted(PRIVATE_TOOL_REGISTRY), "decisions": [], "guard": [], "tools": [], "context_ms": 0, "model_ms": [], "tool_ms": []}
    def emit(event_type: str, **payload: str) -> None:
        if on_progress:
            on_progress({"type": event_type, "agent_run_id": run_id, **payload})
    try:
        context_started = time.perf_counter()
        ctx = build_private_turn_context_v2(user_id, other_id, message, match_doc)
        trace["context_ms"] = round((time.perf_counter() - context_started) * 1000)
        consumed, reply = _consume_confirmation(ctx, match_doc)
        if consumed:
            result = AgentResult(handled=True, reply=reply, conversation_intent="private_confirmation", agent_run_id=run_id, agent_mode="v2")
        else:
            result = None
            seen: set[str] = set()
            for index in range(MAX_STEPS):
                model_started = time.perf_counter()
                decision = _plan(ctx, observations)
                trace["model_ms"].append(round((time.perf_counter() - model_started) * 1000))
                if not decision:
                    compose_started = time.perf_counter()
                    result = AgentResult(handled=True, reply=_compose(ctx, observations, "warm"), conversation_intent="private_advice", agent_run_id=run_id, agent_mode="v2", fallback_reason="planner_invalid")
                    trace["model_ms"].append(round((time.perf_counter() - compose_started) * 1000))
                    break
                trace["decisions"].append({"kind": decision.kind, "tool": decision.tool_name, "confidence": decision.confidence})
                if decision.kind == "confirmation":
                    trace["guard"].append("confirmation_saved")
                    result = AgentResult(handled=True, reply=_save_confirmation(ctx, decision), conversation_intent="private_confirmation", agent_run_id=run_id, agent_mode="v2")
                    break
                if decision.kind == "final":
                    reply = _planner_reply(decision)
                    if reply:
                        result = AgentResult(handled=True, reply=reply, conversation_intent="private_advice", agent_run_id=run_id, agent_mode="v2")
                    else:
                        compose_started = time.perf_counter()
                        result = AgentResult(handled=True, reply=_compose(ctx, observations, decision.strategy), conversation_intent="private_advice", agent_run_id=run_id, agent_mode="v2")
                        trace["model_ms"].append(round((time.perf_counter() - compose_started) * 1000))
                    break
                spec = PRIVATE_TOOL_REGISTRY.get(str(decision.tool_name or ""))
                if not spec or spec.risk != "read":
                    trace["guard"].append("tool_not_allowed")
                    result = AgentResult(handled=True, reply=_compose(ctx, observations, decision.strategy), conversation_intent="private_advice", agent_run_id=run_id, agent_mode="v2", fallback_reason="tool_not_allowed")
                    break
                arguments = {"scope": str(decision.arguments.get("scope") or "")[:80]}
                key = spec.name + json.dumps(arguments, ensure_ascii=False, sort_keys=True)
                if key in seen:
                    trace["guard"].append("duplicate_observation_reused")
                    result = AgentResult(handled=True, reply=_compose(ctx, observations, decision.strategy), conversation_intent="private_advice", agent_run_id=run_id, agent_mode="v2")
                    break
                seen.add(key)
                step = f"{index}:read"
                emit("tool_started", step_id=step, text=spec.progress_text)
                tool_started = time.perf_counter()
                ok, data, code = _execute_read(spec.name, ctx, arguments)
                trace["tool_ms"].append(round((time.perf_counter() - tool_started) * 1000))
                emit("tool_finished", step_id=step, outcome="ok" if ok else "error")
                trace["tools"].append({"name": spec.name, "ok": ok, "code": code})
                if not ok:
                    result = AgentResult(handled=True, reply="我剛才沒有取得足夠的資訊；你想先換個問法，還是告訴我你最想確認哪一點？", conversation_intent="private_clarification", agent_run_id=run_id, agent_mode="v2")
                    break
                observations.append({"tool": spec.name, "result": data})
            if result is None:
                compose_started = time.perf_counter()
                result = AgentResult(handled=True, reply=_compose(ctx, observations, "warm"), conversation_intent="private_advice", agent_run_id=run_id, agent_mode="v2")
                trace["model_ms"].append(round((time.perf_counter() - compose_started) * 1000))
    except Exception as exc:
        trace["exception"] = type(exc).__name__
        result = AgentResult(handled=True, reply="我剛才沒能整理好這件事。你可以換個方式告訴我，你最想確認哪一點？", conversation_intent="private_clarification", agent_run_id=run_id, agent_mode="v2", fallback_reason=type(exc).__name__)
    trace["latency_ms"] = round((time.perf_counter() - started) * 1000)
    trace["result"] = {"intent": result.conversation_intent, "fallback": result.fallback_reason}
    _trace(run_id, trace)
    return result
