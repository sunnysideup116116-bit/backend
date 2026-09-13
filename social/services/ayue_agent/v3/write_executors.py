"""V3 write executors: the only path that performs confirmed side effects.

Every write goes through a canonical domain service.  Planner/sub-agent
arguments never contain IDs or revisions; executors inject them from the
turn context and canonical state.
"""

from __future__ import annotations

import time
import uuid
import re
from typing import Any

from database import db
from services.ai_service import generate_chat_completion
from services.match_action_service import (
    decide_active_event_invitation, decide_active_proposal, start_match_search,
)
from services.match_search_job_service import (
    INVITE_ON_MATCH, active_match_search_job, cancel_match_search,
)
from services.proposal_namespace import RELATIONSHIP_MATCH_NAMESPACE
from services.assessment_session_service import start_assessment_session
from services.ayue_agent.v3.public_reply import validate_public_reply
from services.ayue_agent.match_opportunity import (
    assess_match_opportunity, missing_basis_question,
)
from services.ayue_agent.public_relationship_projection import (
    display_name as relationship_display_name,
    resolve_accepted_contact_name,
)
from .date_coordination_references import (
    CANCELLABLE_STATUSES as DATE_COORDINATION_CANCELLABLE_STATUSES,
)
from services.match_search_context import safe_search_context, search_context_for_turn
TOOL_CALLS = db["agent_tool_calls"]

_DATE_CARD_CAPABILITY_QUESTION_RE = re.compile(
    r"(?:"
    r"(?:約會卡|約會邀請卡|約會邀請).{0,10}(?:可以|能不能|可不可以|能否).{0,10}(?:取消|撤回)"
    r"|(?:可以|能不能|可不可以|能否).{0,10}(?:取消|撤回).{0,10}(?:約會卡|約會邀請卡|約會邀請)"
    r")(?:嗎|呢|？|\?)?$"
)


def _is_date_card_capability_question(message: Any) -> bool:
    """Recognise the closed capability question that must stay read-only."""
    compact = re.sub(r"\s+", "", str(message or "")).strip()
    return bool(compact and _DATE_CARD_CAPABILITY_QUESTION_RE.search(compact))


def _idempotency_key(confirmation_id: str | None, run_id: str, index: int, suffix: str = "") -> str:
    base = f"confirmation:{confirmation_id}" if confirmation_id else f"{run_id}:{index}"
    return f"{base}:{suffix}" if suffix else base


def _claim_once(key: str) -> bool:
    """Return True if this key has not been executed before (idempotency)."""
    prior = TOOL_CALLS.find_one_and_update(
        {"idempotency_key": key},
        {"$setOnInsert": {"idempotency_key": key, "created_at": time.time(), "state": "running"}},
        upsert=True,
    )
    return prior is None


def _finish(key: str, status: str, result: dict[str, Any]) -> None:
    try:
        TOOL_CALLS.update_one({"idempotency_key": key}, {"$set": {"state": status, "result": result}})
    except Exception:
        pass


def _start_search(
    ctx: Any, run_id: str, index: int, *, confirmation_id: str | None,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, str, str | None]:
    key = _idempotency_key(confirmation_id, run_id, index)
    if not _claim_once(key):
        return True, "我已經處理過這次搜尋。", None
    try:
        search_context = safe_search_context((payload or {}).get("search_context"))
        search_kwargs: dict[str, Any] = {}
        if search_context:
            search_kwargs["search_context"] = search_context
        if (
            str((payload or {}).get("delivery_mode") or "").strip() == INVITE_ON_MATCH
            and search_context.get("invitation_topic")
        ):
            search_kwargs["delivery_mode"] = INVITE_ON_MATCH
        result = start_match_search(
            ctx.user_id, source="agent_v3", force_new=True, idempotency_key=key,
            origin_room_id=str(ctx.room_id or ""),
            **search_kwargs,
        )
        status = result.get("status", "failed")
        invite_on_match = search_kwargs.get("delivery_mode") == INVITE_ON_MATCH
        queued_reply = (
            "好，我開始幫你找。找到合適的人後，我會替你送出邀請；有結果時會回來告訴你。"
            if invite_on_match
            else "好，我開始幫你找，通常約需要 1–3 分鐘。你可以先繼續跟我聊，找到後我會回來。"
        )
        already_queued_reply = (
            "這次已經開始找了。找到合適的人後，我會替你送出邀請；有結果時會回來告訴你。"
            if invite_on_match
            else "這次搜尋已經排進去了，通常約需要 1–3 分鐘；你可以先繼續跟我聊。"
        )
        reply = {
            "queued": queued_reply,
            "already_queued": already_queued_reply,
            "already_active": "你目前還有一張進行中的提案，我先不重複開新搜尋。",
            "already_searching": "我正在幫你找，先不用重複送出。",
            "quota_exceeded": "今天已經幫你介紹 3 位新朋友了，明天可以再找；已送出的邀請還是能繼續回覆。",
            "no_candidates": "這輪暫時沒有合適的新對象。",
            "insufficient_common_ground": "這輪還找不到足夠的共同依據；你可以補充這次想一起做的事或主題，再決定是否重新搜尋。",
        }.get(status, "這次搜尋沒有成功啟動，我沒有把它當作已完成。")
        _finish(key, "done", {"status": status, "reply": reply})
        ok = status in {"queued", "already_queued", "already_searching"}
        return ok, reply, None if ok else "search_not_started"
    except Exception as exc:
        return False, "我現在不能安全地開始搜尋，請稍後再試。", type(exc).__name__


def _cancel_search(
    ctx: Any, run_id: str, index: int, payload: dict[str, Any] | None,
    *, confirmation_id: str | None,
) -> tuple[bool, str, str | None]:
    key = _idempotency_key(confirmation_id, run_id, index, suffix="cancel_search")
    if not _claim_once(key):
        return True, "我已經處理過這次取消搜尋。", None
    expected_job_id = str((payload or {}).get("match_search_job_id") or "")
    if not expected_job_id:
        return False, "這次取消搜尋的確認資料已失效，請重新查看目前狀態。", "match_search_confirmation_invalid"
    try:
        result = cancel_match_search(
            ctx.user_id, source="agent_v3", expected_job_id=expected_job_id,
        )
        status = str(result.get("status") or "")
        if status == "cancelled":
            reply = "好，這次配對搜尋已取消。"
            _finish(key, "done", {"status": status, "reply": reply})
            return True, reply, None
        if status == "already_active":
            return False, "搜尋已結束，而且目前已有一張配對提案；我沒有取消新的狀態。", "stale_match_search"
        return False, "這次搜尋已經結束或更新，我沒有取消其他搜尋。", "stale_match_search"
    except Exception as exc:
        return False, "我現在不能安全地取消搜尋，請稍後再試。", type(exc).__name__


def _decide_active_proposal(
    ctx: Any, turn: Any, run_id: str, index: int,
    arguments: dict[str, Any], payload: dict[str, Any] | None,
) -> tuple[bool, str, str | None]:
    decision = str(arguments.get("decision") or "")
    authority = payload or {}
    expected_revision = authority.get("proposal_revision")
    if (
        decision not in {"interested", "declined", "cancelled"}
        or not str(authority.get("match_id") or "")
        or str(authority.get("proposal_namespace") or "") != RELATIONSHIP_MATCH_NAMESPACE
        or authority.get("expected_status") not in {"draft", "pending"}
        or type(expected_revision) is not int or expected_revision < 0
    ):
        return False, "這次確認缺少有效的提案綁定資料，沒有執行變更。", "decision_not_actionable"
    try:
        outcome = decide_active_proposal(
            user_id=ctx.user_id,
            decision=decision,
            expected_revision=expected_revision,
            idempotency_key=_idempotency_key(authority.get("_confirmation_id"), run_id, index),
            expected_match_id=str(authority.get("match_id") or ""),
            expected_status=str(authority.get("expected_status") or ""),
        )
        if outcome.get("stale"):
            latest = str(outcome.get("current_status") or "")
            reply = {
                "accepted": "這張提案剛剛已更新：你們已經互相接受，聊天室也已開啟。",
                "declined": "這張提案剛剛已更新為婉拒，我沒有覆寫最新結果。",
                "pending": "這張提案剛剛已更新，現在正在等待對方回覆。",
                "draft": "這張提案剛剛已更新，請以最新提案狀態為準。",
            }.get(latest, "這張提案剛剛已更新，我沒有覆寫最新結果。")
            return True, reply, "stale_revision"
        if outcome.get("status") != "success":
            return False, "我現在不能安全地更新這張提案。", str(outcome.get("status") or "decision_failed")
        reply = {
            "interested": "好，我已更新這張牽線提案。",
            "declined": "好，這張提案已替你婉拒。",
            "cancelled": "好，這次等待中的配對已替你撤回。",
        }[decision]
        return True, reply, None
    except Exception as exc:
        return False, "我現在不能安全地更新這張提案。", type(exc).__name__


def _decide_active_event_invitation(
    ctx: Any, turn: Any, run_id: str, index: int,
    arguments: dict[str, Any], payload: dict[str, Any] | None,
) -> tuple[bool, str, str | None]:
    invitation = turn.active_event_invitation or {}
    decision = str(arguments.get("decision") or "")
    expected_revision = int((payload or {}).get("proposal_revision", 0) or 0)
    if (
        not invitation.get("user_can_decide")
        or decision not in {"interested", "declined"}
        or expected_revision <= 0
        or int(invitation.get("proposal_revision", 0) or 0) != expected_revision
    ):
        return False, "目前沒有一張可由你決定的活動牽線邀請。", "event_decision_not_actionable"
    try:
        outcome = decide_active_event_invitation(
            user_id=ctx.user_id,
            decision=decision,
            expected_revision=expected_revision,
            idempotency_key=_idempotency_key(None, run_id, index, suffix="event_invitation"),
        )
        if outcome.get("stale"):
            return True, "這張活動邀請剛剛已更新，我沒有覆寫最新結果。", "stale_revision"
        if outcome.get("status") != "success":
            return False, "我現在不能安全地更新這張活動邀請。", str(outcome.get("status") or "event_decision_failed")
        if decision == "interested":
            return True, "好，我已替你對這張活動邀請表示有興趣。", None
        return True, "好，這張活動邀請已替你婉拒。", None
    except Exception as exc:
        return False, "我現在不能安全地更新這張活動邀請。", type(exc).__name__


def _assessment_opening_question(ctx: Any, kind: str) -> str:
    """Generate one validated public opening; never mutate assessment state."""
    profile = ctx.user_profile if isinstance(ctx.user_profile, dict) else {}
    interests: list[str] = []
    for key in ("interests", "hobbies"):
        value = profile.get(key)
        if isinstance(value, list):
            for item in value:
                text = re.sub(r"\s+", " ", str(item or "")).strip()[:40]
                if text and text not in interests:
                    interests.append(text)
                if len(interests) == 3:
                    break
        if len(interests) == 3:
            break
    dimension = (
        "日常做決定、安排事情、與人互動時的自然習慣"
        if kind == "big_five"
        else "價值觀、人生選擇、關係需要或壓力下的真實感受"
    )
    interest_context = "、".join(interests) if interests else "沒有額外興趣資料"
    result = generate_chat_completion(
        (
            f"探索類型：{kind}\n可用興趣：{interest_context}\n"
            "只輸出一個自然、具體、容易回答的繁體中文問題。"
        ),
        temperature=0.75,
        max_tokens=180,
        model_owner="profile",
        system_prompt=(
            "你是公開 Candy 的個人探索開場提問者。"
            f"問題要自然觸及{dimension}，不要解釋、不要自我介紹、不要列選項。"
        ),
    )
    validation = validate_public_reply(
        getattr(result, "content", ""),
        preserve_details=True,
        reject_internal_identifiers=True,
        reject_structured_output=True,
        max_chars=360,
        max_sentences=3,
    )
    question = str(validation.reply or "").strip()
    if not question or not question.endswith(("？", "?")):
        return ""
    return question


def _start_assessment(ctx: Any, arguments: dict[str, Any], *, confirmation_id: str | None) -> tuple[bool, str, str | None]:
    kind = {"basic": "big_five", "deep": "deep_profile"}.get(str(arguments.get("kind") or ""))
    if kind is None:
        return False, "我沒有找到要開始的探索類型，你可以告訴我想做基本性格還是深層探索。", "assessment_unknown_kind"
    key = _idempotency_key(confirmation_id, "assessment", 0, suffix=kind)
    if not _claim_once(key):
        prior = TOOL_CALLS.find_one({"idempotency_key": key}) or {}
        saved = prior.get("result") if isinstance(prior.get("result"), dict) else {}
        reply = str(saved.get("reply") or "").strip()
        if saved.get("status") in {"started", "already_started"} and reply:
            return True, reply, None
        return False, "這次開始尚未完成，也沒有重複開新的探索。", "assessment_start_incomplete"
    try:
        opening_question = _assessment_opening_question(ctx, kind)
        if not opening_question:
            _finish(key, "failed", {"status": "opening_generation_failed"})
            return False, "這次還沒有成功開始探索。", "assessment_opening_generation_failed"
        outcome = start_assessment_session(
            ctx.user_id,
            kind,
            idempotency_key=key,
            room_id=str(ctx.room_id or "").strip() or None,
            opening_question=opening_question,
        )
    except Exception as exc:
        _finish(key, "failed", {"status": "assessment_start_failed"})
        return False, "剛剛沒有成功開始，我沒有改動原本的資料。你想再試一次時跟我說。", type(exc).__name__
    ok = outcome.get("status") in {"started", "already_started"}
    reply = str(outcome.get("reply") or "").strip()
    _finish(key, "done" if ok else "failed", {
        "status": outcome.get("status"),
        "reply": reply if ok else "",
    })
    return ok, reply, None if ok else str(outcome.get("status") or "assessment_start_failed")


def _contact_resolution_failure(status: str, *, name_hint: str = "") -> str:
    if status == "ambiguous":
        return "我找到不只一位可能的對象，請指定一位聯絡人後再試一次。"
    if status == "too_many":
        return "你的聯絡人較多，請用 @ 或從聯絡人清單指定一位。"
    if status == "unavailable":
        return "我現在無法安全確認這位聯絡人，請稍後再試。"
    if status == "missing_recent":
        return "我還不確定你說的對方是誰，可以說名字或指定一位聯絡人嗎？"
    if name_hint:
        return "我在你已建立聯絡的對象裡找不到這個名字，可以再說一次或指定一位聯絡人嗎？"
    return "我還不確定你要邀請哪一位，可以說名字或指定一位聯絡人嗎？"


def _name_is_grounded_in_public_chat(name: str, ctx: Any, turn: Any) -> bool:
    """Allow a resolved public name from the current bounded conversation."""
    target = re.sub(r"\s+", "", str(name or "")).casefold()
    if len(target) < 2:
        return False
    texts = [str(getattr(ctx, "message", "") or "")]
    texts.extend(
        str(item.get("content") or "")
        for item in (getattr(turn, "recent_messages", None) or [])
        if isinstance(item, dict)
    )
    return any(target in re.sub(r"\s+", "", text).casefold() for text in texts)


def _prepare_date_coordination(
    arguments: dict[str, Any], ctx: Any, turn: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    from services.date_coordination_service import LIVE_STATUSES, find_accepted_match

    mention_ids = list(getattr(turn, "_mentioned_ids", []) or [])
    if bool(getattr(turn, "mentioned_contact_overflow", False)) or len(mention_ids) > 1:
        return None, _contact_resolution_failure("ambiguous")

    target_id = ""
    safe_label = ""
    resolution_kind = ""
    target_source = str(arguments.get("target_source") or "")
    evidence_span = str(arguments.get("target_evidence_span") or "")
    if len(mention_ids) == 1:
        target_id = mention_ids[0]
        safe_label = relationship_display_name(target_id)
        resolution_kind = "mention"
    elif target_source == "name":
        if not evidence_span or not _name_is_grounded_in_public_chat(
            evidence_span, ctx, turn,
        ):
            return None, _contact_resolution_failure("not_found", name_hint=evidence_span)
        resolved = resolve_accepted_contact_name(ctx.user_id, evidence_span)
        if resolved.status not in {"resolved_exact", "resolved_phonetic", "resolved_fuzzy"} or not resolved.other_id:
            if resolved.status == "ambiguous":
                names = "、".join(resolved.candidates[:3])
                return None, f"我找到不只一位可能的對象：{names or '請指定一位'}。你想邀請哪一位？"
            return None, _contact_resolution_failure(resolved.status, name_hint=evidence_span)
        target_id = resolved.other_id
        safe_label = resolved.display_name
        resolution_kind = resolved.kind or "exact"
    else:
        return None, _contact_resolution_failure("not_found")

    if not target_id:
        return None, _contact_resolution_failure("not_found")
    try:
        match = find_accepted_match(ctx.user_id, target_id)
    except Exception:
        return None, _contact_resolution_failure("unavailable")
    if not match:
        return None, "這位目前不是已建立聯絡的對象，我沒有建立邀請卡。"
    coordination = match.get("date_coordination") or {}
    if coordination.get("status") in LIVE_STATUSES:
        if coordination.get("status") == "pending_partner":
            return None, f"你和「{safe_label}」已經有一張等待回覆的約會邀請卡，我不會重複建立。"
        return None, f"你和「{safe_label}」已有進行中的約會安排，我不會再建立新的卡片。"
    revision = int(match.get("proposal_revision", 0) or 0)
    return {
        "action": "relationship.start_date_coordination",
        "arguments": {},
        "data": {
            "other_id": target_id,
            "match_id": str(match.get("_id") or ""),
            "expected_match_revision": revision,
            "safe_label": safe_label[:30],
            "resolution_kind": resolution_kind,
        },
    }, (
        (f"我把「{evidence_span}」理解成「{safe_label}」。" if resolution_kind in {"fuzzy", "phonetic"} else "")
        + f"要幫你和「{safe_label}」建立約會邀請卡嗎？確認後才會送出。"
    )


def _start_date_coordination(
    ctx: Any, turn: Any, run_id: str, index: int,
    arguments: dict[str, Any], confirmation_id: str | None,
    payload: dict[str, Any] | None,
) -> tuple[bool, str, str | None]:
    from services.date_coordination_service import LIVE_STATUSES, create_invite, find_accepted_match

    data = payload or {}
    other_id = str(data.get("other_id") or "")
    safe_label = str(data.get("safe_label") or "對方")
    expected_match_id = str(data.get("match_id") or "")
    expected_revision = int(data.get("expected_match_revision", 0) or 0)
    if not other_id or not expected_match_id:
        return False, "這筆邀請確認資訊已失效，請重新提出邀請。", "date_coordination_payload_invalid"
    try:
        match = find_accepted_match(ctx.user_id, other_id)
    except Exception:
        match = None
    if not match or str(match.get("_id") or "") != expected_match_id:
        return False, "你和這位對象的聯絡狀態已變更，因此我沒有建立邀請卡。", "stale_relationship"
    current_revision = int(match.get("proposal_revision", 0) or 0)
    if expected_revision and current_revision != expected_revision:
        return False, "你和這位對象的聯絡狀態已變更，因此我沒有建立邀請卡。", "stale_relationship"
    existing = match.get("date_coordination") or {}
    if existing.get("status") in LIVE_STATUSES:
        return True, f"你和「{safe_label}」已經有進行中的約會邀請卡，我沒有重複建立。", "date_coordination_already_live"

    key = _idempotency_key(confirmation_id, run_id, index, suffix="date_coordination")
    if not _claim_once(key):
        return True, f"你和「{safe_label}」的空白約會邀請卡已經建立。", None
    try:
        coordination = create_invite(
            match, ctx.user_id, other_id,
            expected_match_revision=expected_revision or None,
        )
    except Exception as exc:
        _finish(key, "failed", {"error_code": "date_coordination_write_failed"})
        return False, "我現在無法建立約會邀請卡，請稍後再試。", type(exc).__name__
    if coordination is None:
        try:
            latest = find_accepted_match(ctx.user_id, other_id)
        except Exception:
            latest = None
        latest_coordination = (latest or {}).get("date_coordination") or {}
        if latest_coordination.get("status") in LIVE_STATUSES:
            _finish(key, "done", {"status": "already_live"})
            return True, f"你和「{safe_label}」已經有進行中的約會邀請卡，我沒有重複建立。", "date_coordination_already_live"
        _finish(key, "failed", {"error_code": "stale_relationship"})
        return False, "你和這位對象的聯絡狀態已變更，因此我沒有建立邀請卡。", "stale_relationship"
    _finish(key, "done", {"status": "created"})
    return True, f"你和「{safe_label}」的空白約會邀請卡已建立，目前等待對方接受。", None


def _date_coordination_candidates(user_id: str, other_id: str | None = None) -> list[dict[str, Any]]:
    """Read current accepted date cards without exposing their authority."""
    from services.date_coordination_service import find_accepted_match
    from services.match_state_service import verified_accepted_match_query
    from database import matches_coll

    if other_id:
        try:
            match = find_accepted_match(user_id, other_id)
        except Exception:
            return []
        return [match] if isinstance(match, dict) else []
    try:
        rows = list(matches_coll.find(verified_accepted_match_query(user_id)))
    except Exception:
        return []
    return [row for row in rows if isinstance(row, dict)]


def _match_by_authority(user_id: str, match_id: str) -> dict[str, Any] | None:
    if not match_id:
        return None
    from bson.objectid import ObjectId
    from services.match_state_service import verified_accepted_match_query
    from database import matches_coll

    query: dict[str, Any]
    try:
        query = {"_id": ObjectId(match_id)}
    except Exception:
        query = {"_id": match_id}
    try:
        match = matches_coll.find_one(query)
    except Exception:
        return None
    if not isinstance(match, dict):
        return None
    if user_id not in {match.get("from_user"), match.get("to_user")}:
        return None
    try:
        accepted = matches_coll.find_one(
            {"$and": [verified_accepted_match_query(user_id), {"_id": match.get("_id")}]}
        )
    except Exception:
        accepted = None
    return accepted if isinstance(accepted, dict) else None


def _resolve_date_coordination_for_cancel(
    arguments: dict[str, Any], ctx: Any, turn: Any,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str, str | None]:
    """Resolve the requested date card, keeping every ID server-owned."""
    target_source = str(arguments.get("target_source") or "")
    evidence_span = str(arguments.get("target_evidence_span") or "").strip()
    mention_ids = list(getattr(turn, "_mentioned_ids", []) or [])
    if bool(getattr(turn, "mentioned_contact_overflow", False)) or len(mention_ids) > 1:
        return None, None, "", "我找到不只一位可能的對象，請指定一位約會對象後再試一次。"

    # A concrete @ mention is a stronger referent than a short-lived recent
    # reference, even if the provider selected recent_action in its proposal.
    if len(mention_ids) == 1 and target_source in {"recent_action", "summary_singleton", "singleton"}:
        target_source = "mention"
    match: dict[str, Any] | None = None
    label = "對方"
    if target_source == "focused_card":
        authority = getattr(turn, "_focused_match_authority", None) or {}
        match = _match_by_authority(ctx.user_id, str(authority.get("match_id") or ""))
    elif target_source == "mention":
        if len(mention_ids) != 1:
            return None, None, "", "請指定一位約會對象後再試一次。"
        other_id = str(mention_ids[0] or "")
        match_rows = _date_coordination_candidates(ctx.user_id, other_id)
        match = match_rows[0] if len(match_rows) == 1 else None
        label = relationship_display_name(other_id)[:30] or "對方"
    elif target_source == "name":
        if not evidence_span or not _name_is_grounded_in_public_chat(
            evidence_span, ctx, turn,
        ):
            return None, None, "", "我還不確定你指的是哪一位，可以說名字或指定一張約會卡嗎？"
        resolved = resolve_accepted_contact_name(ctx.user_id, evidence_span)
        if resolved.status == "ambiguous":
            names = "、".join(resolved.candidates[:3])
            return None, None, "", f"我找到不只一位可能的對象：{names or '請指定一位'}。你要取消哪一張約會卡？"
        if resolved.status not in {"resolved_exact", "resolved_phonetic", "resolved_fuzzy"} or not resolved.other_id:
            return None, None, "", "我在已建立聯絡的對象裡找不到這個名字，可以再說一次嗎？"
        match_rows = _date_coordination_candidates(ctx.user_id, resolved.other_id)
        match = match_rows[0] if len(match_rows) == 1 else None
        label = str(resolved.display_name or relationship_display_name(resolved.other_id) or "對方")[:30]
    elif target_source in {"summary_singleton", "singleton", ""}:
        # No model or client identity is trusted here. Resolve the only
        # currently cancellable card from canonical accepted relationships;
        # zero and many are explicit outcomes rather than guesses.
        candidates = [
            row for row in _date_coordination_candidates(ctx.user_id)
            if str((row.get("date_coordination") or {}).get("status") or "")
            in DATE_COORDINATION_CANCELLABLE_STATUSES
        ]
        if not candidates:
            return None, None, "", "目前沒有可以取消的約會卡。"
        if len(candidates) > 1:
            return None, None, "", "目前有不只一張可取消的約會卡，請指定對方或從卡片上操作。"
        match = candidates[0]
    else:
        return None, None, "", "我還不確定你要取消哪一張約會卡，可以說對方名字或指定卡片嗎？"

    if match is None:
        candidates = [
            row for row in _date_coordination_candidates(ctx.user_id)
            if str((row.get("date_coordination") or {}).get("status") or "")
            in DATE_COORDINATION_CANCELLABLE_STATUSES
        ]
        if len(candidates) > 1:
            return None, None, "", "目前有不只一張可取消的約會卡，請指定對方或從卡片上操作。"
        if not candidates:
            return None, None, "", "目前沒有可以取消的約會卡。"
        # Do not silently fall back from an explicit target to another card.
        if target_source in {"mention", "name", "focused_card"}:
            return None, None, "", "這張約會卡已不存在或狀態已更新，請重新查看後再試一次。"
        match = candidates[0]
    coordination = match.get("date_coordination") or {}
    status = str(coordination.get("status") or "")
    if status not in DATE_COORDINATION_CANCELLABLE_STATUSES:
        return None, None, "", "這張約會卡已結束或取消，我沒有再次執行。"
    if label == "對方":
        other_id = match.get("to_user") if match.get("from_user") == ctx.user_id else match.get("from_user")
        label = relationship_display_name(str(other_id or ""))[:30] or "對方"
    return match, coordination, label, None


def _cancel_date_coordination_preview(
    match: dict[str, Any], coordination: dict[str, Any], label: str, user_id: str,
) -> tuple[dict[str, Any], str]:
    from database import calendar_events_coll

    event = None
    event_id = str(coordination.get("calendar_event_id") or "")
    if event_id:
        try:
            event = calendar_events_coll.find_one({"event_id": event_id, "source_type": "date"})
        except Exception:
            event = None
    status = str(coordination.get("status") or "")
    preview = (
        f"要撤回剛傳給{label}的約會邀請嗎？"
        if status == "pending_partner"
        else f"要取消你和{label}正在協調的約會嗎？"
        if status == "active"
        else f"要取消你和{label}的共同約會嗎？"
    )
    if event or event_id:
        preview += "取消後會同步更新雙方行事曆，要繼續嗎？"
    other_id = match.get("to_user") if match.get("from_user") == user_id else match.get("from_user")
    return {
        "action": "relationship.cancel_date_coordination",
        "arguments": {},
        "data": {
            "match_id": str(match.get("_id") or ""),
            "coordination_id": str(coordination.get("coordination_id") or ""),
            "other_id": str(other_id or ""),
            "expected_status": status,
            "expected_revision": int(coordination.get("revision", 1) or 1),
            "expected_coordination_revision": int(coordination.get("revision", 1) or 1),
            "expected_event_revision": int(event.get("revision", 1) or 1) if event else None,
            "calendar_event_id": event_id,
            "safe_label": label[:30],
        },
    }, preview


def _prepare_cancel_date_coordination(
    arguments: dict[str, Any], ctx: Any, turn: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    if _is_date_card_capability_question(getattr(ctx, "message", "")):
        from services.ayue_agent.capabilities import PRODUCT_INFO_FAILURE_FALLBACKS
        return None, PRODUCT_INFO_FAILURE_FALLBACKS["date_coordination_cancel"]
    match, coordination, label, error = _resolve_date_coordination_for_cancel(arguments, ctx, turn)
    if error:
        return None, error
    if not match or not coordination:
        return None, "目前沒有可以取消的約會卡。"
    if coordination.get("calendar_event_id"):
        from database import calendar_events_coll
        try:
            event = calendar_events_coll.find_one({
                "event_id": str(coordination.get("calendar_event_id") or ""),
                "source_type": "date",
            })
        except Exception:
            event = None
        if not event:
            return None, "這張共同約會的行事曆資料不一致，我沒有建立取消確認。"
    return _cancel_date_coordination_preview(match, coordination, label, ctx.user_id)


def _cancel_date_coordination(
    ctx: Any, turn: Any, run_id: str, index: int,
    arguments: dict[str, Any], confirmation_id: str | None,
    payload: dict[str, Any] | None,
) -> tuple[bool, str, str | None]:
    from fastapi import HTTPException
    from services.date_coordination_service import cancel_coordination_or_event, find_accepted_match

    data = payload or {}
    match_id = str(data.get("match_id") or "")
    coordination_id = str(data.get("coordination_id") or "")
    other_id = str(data.get("other_id") or "")
    expected_status = str(data.get("expected_status") or "")
    expected_coord_revision = data.get("expected_coordination_revision", data.get("expected_revision"))
    event_revision = data.get("expected_event_revision")
    try:
        expected_coord_revision = int(expected_coord_revision)
    except (TypeError, ValueError):
        expected_coord_revision = 0
    if (
        not match_id or not coordination_id or not other_id
        or expected_status not in DATE_COORDINATION_CANCELLABLE_STATUSES
        or expected_coord_revision <= 0
    ):
        return False, "這筆約會取消確認資料已失效，請重新查看約會卡。", "date_coordination_payload_invalid"
    try:
        event_revision = int(event_revision) if event_revision is not None else None
    except (TypeError, ValueError):
        return False, "這筆約會取消確認資料已失效，請重新查看約會卡。", "date_coordination_payload_invalid"
    key = _idempotency_key(confirmation_id, run_id, index, suffix="date_coordination_cancel")
    if not _claim_once(key):
        return True, "我已經處理過這次約會取消。", None
    try:
        match = find_accepted_match(ctx.user_id, other_id)
        current_coordination = match.get("date_coordination") or {}
        if str(match.get("_id") or "") != match_id or str(current_coordination.get("coordination_id") or "") != coordination_id:
            raise HTTPException(status_code=409, detail="約會剛剛已變更，請重新確認")
        if str(current_coordination.get("status") or "") != expected_status or int(current_coordination.get("revision", 1) or 1) != expected_coord_revision:
            raise HTTPException(status_code=409, detail="約會剛剛已變更，請重新確認")
        coordination = cancel_coordination_or_event(
            ctx.user_id,
            other_id,
            coordination_id,
            expected_revision=event_revision,
            expected_status=expected_status,
            expected_coordination_revision=expected_coord_revision,
            idempotency_key=key,
        )
    except HTTPException as exc:
        _finish(key, "failed", {"error_code": "date_coordination_stale" if exc.status_code == 409 else "date_coordination_cancel_failed"})
        if exc.status_code == 409:
            return False, "這張約會卡剛剛已變更，請重新查看後再確認。", "date_coordination_stale"
        return False, "我現在無法取消這張約會卡，請稍後再試。", "date_coordination_cancel_failed"
    except Exception as exc:
        _finish(key, "failed", {"error_code": "date_coordination_cancel_failed"})
        return False, "我現在無法取消這張約會卡，請稍後再試。", type(exc).__name__
    _finish(key, "done", {"status": "cancelled"})
    if expected_status == "pending_partner":
        reply = "好，這張約會邀請已替你撤回。"
    elif event_revision is not None or data.get("calendar_event_id"):
        reply = "好，共同約會已取消，雙方行事曆也會同步更新。"
    else:
        reply = "好，這張約會協調已取消。"
    return True, reply, None


def _calendar_event_label(event: dict) -> str:
    from datetime import datetime, timedelta
    from services.calendar_service import as_utc, get_timezone
    zone = get_timezone(event.get("timezone") or "Asia/Taipei")
    start_value = event["start_at"]
    end_value = event["end_at"]
    if isinstance(start_value, str):
        start_value = datetime.fromisoformat(start_value.replace("Z", "+00:00"))
    if isinstance(end_value, str):
        end_value = datetime.fromisoformat(end_value.replace("Z", "+00:00"))
    start = as_utc(start_value).astimezone(zone)
    end = as_utc(end_value).astimezone(zone)
    if event.get("source_type") == "date":
        title = str(event.get("activity") or event.get("title") or "共同約會").strip()
    else:
        title = str(event.get("title") or event.get("activity") or "這筆行程").strip()
    if event.get("all_day"):
        inclusive_end = end.date() - timedelta(days=1)
        if inclusive_end == start.date():
            return f"{start.month}/{start.day} 全天 {title}"
        return f"{start.month}/{start.day}–{inclusive_end.month}/{inclusive_end.day} 全天 {title}"
    if end.date() != start.date():
        return f"{start.month}/{start.day} {start:%H:%M}–{end.month}/{end.day} {end:%H:%M} {title}"
    return f"{start.month}/{start.day} {start:%H:%M}–{end:%H:%M} {title}"


def _execute_calendar_mutation_plans(
    ctx: Any,
    plans_payload: list[dict[str, Any]],
    *,
    confirmation_id: str,
) -> tuple[bool, str, str | None]:
    """Execute server-owned Calendar plans sequentially.

    The plan contains canonical IDs/revisions produced by preflight.  This
    function deliberately never calls a natural-language resolver.  A batch
    stops at the first real failure; already committed operations are retained
    because Calendar writes do not provide a distributed transaction.
    """
    from fastapi import HTTPException
    from services.calendar_service import (
        cancel_event, cancel_targets_are_current, create_personal_event,
        update_personal_event,
    )
    from services.date_coordination_service import cancel_coordination_or_event, request_reschedule
    from .calendar_commands import CalendarMutationPlan

    try:
        plans = [CalendarMutationPlan.model_validate(item) for item in plans_payload]
    except Exception:
        return False, "這次行程確認資料已失效，請重新告訴我想怎麼安排。", "calendar_plan_invalid"
    if not plans:
        return False, "這次沒有可執行的行程變更。", "calendar_plan_empty"

    from .calendar_references import remember_recent_mutation

    operation_records: list[dict[str, Any]] = []
    batch_action = (
        getattr(plans[0].action, "value", str(plans[0].action))
        if len(plans) == 1 else "batch"
    )

    def remember_outcome(outcome: str) -> None:
        try:
            remember_recent_mutation(
                ctx.user_id, action=batch_action, outcome=outcome,
                operations=operation_records,
            )
        except Exception:
            # Verification state is advisory; a storage outage must not turn a
            # committed Calendar mutation into a failed response.
            pass

    def append_operation(
        plan: CalendarMutationPlan,
        event: dict[str, Any] | None = None,
        *,
        expected_status: str,
    ) -> None:
        action = getattr(plan.action, "value", str(plan.action))
        form = dict(plan.form or {})
        if event:
            label = _calendar_event_label(event)
            event_id = str(event.get("event_id") or plan.event_id or "")
            revision = int(event.get("revision", plan.expected_revision or 0) or 0)
        else:
            changes = dict(plan.changes or {})
            label = str(
                form.get("title") or form.get("activity")
                or changes.get("title") or changes.get("activity") or "行程"
            )[:180]
            event_id = str(plan.event_id or "")
            revision = int(plan.expected_revision or 0)
        operation_records.append({
            "action": action,
            "event_id": event_id,
            "revision": revision,
            "source_type": str(plan.source_type or "personal"),
            "other_id": str(plan.other_id or ""),
            "coordination_id": str(plan.coordination_id or ""),
            "expected_status": expected_status,
            "safe_label": label,
        })

    # Preserve the existing all-target stale protection for a pure cancellation
    # batch.  Mixed commands still rely on each domain service's CAS at the
    # exact write point.
    if all(plan.action == "cancel" for plan in plans):
        targets = [
            {
                "event_id": plan.event_id,
                "event_revision": plan.expected_revision,
                "event_source_type": plan.source_type,
            }
            for plan in plans
        ]
        if not cancel_targets_are_current(ctx.user_id, targets):
            return False, "其中一筆行程剛剛有變動，我沒有刪除任何行程。請重新確認。", "stale_revision"

    key = f"calendar-confirmation:{confirmation_id}"
    completed: list[str] = []
    for index, plan in enumerate(plans):
        item_key = f"{key}:{index}"
        try:
            if plan.action == "create":
                event = create_personal_event(ctx.user_id, plan.form, agent_action_key=item_key)
                completed.append(_calendar_event_label(event))
                append_operation(
                    plan, event,
                    expected_status=str((event or {}).get("status") or "confirmed"),
                )
                continue

            event_id = str(plan.event_id or "")
            revision = int(plan.expected_revision or 0)
            if plan.action == "update":
                if plan.source_type == "date":
                    coordination, event = request_reschedule(
                        ctx.user_id, str(plan.other_id or ""), event_id,
                        dict(plan.form), expected_revision=revision, idempotency_key=item_key,
                    )
                    form = coordination.get("form") or {}
                    completed.append(
                        f"改期：{str(form.get('date') or '')[5:].replace('-', '/')} "
                        f"{form.get('start_time', '')}–{form.get('end_time', '')} "
                        f"{form.get('activity') or '共同約會'}"
                    )
                else:
                    event = update_personal_event(
                        ctx.user_id, event_id, dict(plan.changes),
                        expected_revision=revision, agent_action_key=item_key,
                    )
                    completed.append(_calendar_event_label(event))
                append_operation(
                    plan, event,
                    expected_status=str((event or {}).get("status") or "confirmed"),
                )
                continue

            event = None
            if plan.source_type == "date":
                coordination = cancel_coordination_or_event(
                    ctx.user_id, str(plan.other_id or ""), str(plan.coordination_id or ""),
                    expected_revision=revision, idempotency_key=item_key,
                )
                title = str((coordination.get("form") or {}).get("activity") or "共同約會")
                completed.append(f"取消共同約會「{title}」")
            else:
                event = cancel_event(
                    ctx.user_id, event_id, personal_only=True,
                    expected_revision=revision, agent_action_key=item_key,
                )
                completed.append(f"取消「{_calendar_event_label(event)}」")
            append_operation(plan, event, expected_status="cancelled")
        except HTTPException as exc:
            code = "stale_revision" if exc.status_code == 409 else "calendar_write_failed"
            if not completed:
                message = (
                    "這筆行程剛剛有變動，我沒有覆寫它。請重新確認。"
                    if code == "stale_revision" else "這筆行程現在無法變更，請重新確認。"
                )
                remember_outcome("failed")
                return False, message, code
            remember_outcome("partial")
            return True, (
                "已處理：" + "、".join(completed) +
                f"。第 {index + 1} 筆沒有完成，後續變更已停止，請重新查看。"
            ), "partial"
        except Exception as exc:
            if not completed:
                remember_outcome("failed")
                return False, "我這筆行程目前沒有成功變更，先沒有把它當作已完成。請直接告訴我想改成哪一天、幾點。", type(exc).__name__
            remember_outcome("partial")
            return True, (
                "已處理：" + "、".join(completed) +
                f"。第 {index + 1} 筆沒有完成，後續變更已停止，請重新查看。"
            ), "partial"

    remember_outcome("success")
    return True, "已處理：" + "、".join(completed) + "。", None


def _calendar_execute(
    ctx: Any,
    tool_name: str,
    arguments: dict[str, Any],
    payload: dict[str, Any] | None,
    *,
    confirmation_id: str,
) -> tuple[bool, str, str | None]:
    """Execute only the typed ``calendar.submit_commands`` confirmation plan."""
    if tool_name != "calendar.submit_commands":
        return False, "這個行程確認已失效，請重新告訴我你想怎麼安排。", "calendar_legacy_tool_disabled"
    payload = payload or {}
    if payload.get("calendar_plan_version") != 1:
        return False, "這次行程確認資料已失效，請重新告訴我想怎麼安排。", "calendar_plan_invalid"
    plans = payload.get("plans")
    if not isinstance(plans, list) or not plans:
        return False, "這次沒有可執行的行程變更。", "calendar_plan_empty"
    return _execute_calendar_mutation_plans(
        ctx, plans, confirmation_id=confirmation_id,
    )


_WRITE_EXECUTORS = {
    "match.start_search": lambda ctx, turn, run_id, index, args, cid, payload: _start_search(
        ctx, run_id, index, confirmation_id=cid, payload=payload,
    ),
    "match.cancel_search": lambda ctx, turn, run_id, index, args, cid, payload: _cancel_search(ctx, run_id, index, payload, confirmation_id=cid),
    "match.decide_active_proposal": lambda ctx, turn, run_id, index, args, cid, payload: _decide_active_proposal(ctx, turn, run_id, index, args, payload),
    "match.decide_active_event_invitation": lambda ctx, turn, run_id, index, args, cid, payload: _decide_active_event_invitation(ctx, turn, run_id, index, args, payload),
    "profile.start_assessment": lambda ctx, turn, run_id, index, args, cid, payload: _start_assessment(ctx, args, confirmation_id=cid),
    "relationship.start_date_coordination": _start_date_coordination,
    "relationship.cancel_date_coordination": _cancel_date_coordination,
}


def prepare_write_confirmation(
    tool_name: str, arguments: dict[str, Any], ctx: Any, turn: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a proposed write and return (pending_payload, preview_reply).

    Returns (None, error_reply) when the write cannot be confirmed (not ready,
    blocked, ambiguous, missing fields).  The payload carries executor-only
    data (event IDs, revisions, targets) that never reach the planner.
    """
    if tool_name == "match.start_search":
        assessment = assess_match_opportunity(ctx.user_profile or {}, ctx.user_id, explicit_search=True)
        if assessment.state == "not_ready":
            return None, "我想先多了解你的方向，才能幫你找得更準。" + missing_basis_question(assessment)
        if assessment.state == "active_match_blocked":
            return None, "你目前還有一段配對正在進行，我先不重複開新搜尋。"
        search_context = search_context_for_turn(
            (arguments or {}).get("search_context"),
            message=getattr(ctx, "message", ""),
            message_id=getattr(ctx, "message_id", None),
            history=getattr(ctx, "recent_history", None),
        )
        semantic_request = (arguments or {}).get("search_request")
        invitation_evidence = ""
        if isinstance(semantic_request, dict):
            # Classifications are not write authority. Ground the requested
            # activity in this turn, then persist the user's visible preview.
            from services.match_search_context import bounded_search_text
            message = bounded_search_text(getattr(ctx, "message", ""), 600)
            topic_span = bounded_search_text(semantic_request.get("topic"), 80)
            if semantic_request.get("kind") == "activity":
                if not topic_span or topic_span not in message:
                    return None, "你這次想找人一起做什麼活動？確認活動後，我會先找人選給你看。"
                search_context = safe_search_context({
                    "invitation_topic": topic_span, "query_text": message,
                    "source_message_id": getattr(ctx, "message_id", ""),
                })
                span = bounded_search_text(semantic_request.get("invitation_evidence"), 120)
                if span and span in message:
                    invitation_evidence = span
            else:
                search_context = {}
        data = {"search_context": search_context} if search_context else {}
        topic = str(search_context.get("invitation_topic") or "").strip()
        if topic:
            # This flag is created only inside the preview-bound confirmation
            # record.  The worker accepts it only alongside this same topic,
            # so an old proposal or a client supplied boolean cannot authorize
            # an automatic message to the other participant.
            if invitation_evidence:
                data["delivery_mode"] = INVITE_ON_MATCH
                data["invitation_evidence"] = invitation_evidence
            query_text = str(search_context.get("query_text") or "")
            skill_requested = bool(
                re.search(
                    r"(?:也?會|擅長|熟悉|懂得)\s*" + re.escape(topic),
                    query_text,
                )
            )
            skill_note = f"不一定已經會{topic}。" if skill_requested else ""
            preview = (
                f"我會依「{topic}」這個邀請需求，找一位可以替你詢問的人選；"
                "找到後就替你問問願不願意認識，不會先假設對方也喜歡這項活動。"
                f"{skill_note}要我開始找並送出邀請嗎？"
            ) if invitation_evidence else (
                f"我會依你想找人一起{topic}的需求搜尋，先給你看人選與推薦說明。"
                f"不會先假設對方也喜歡或擅長這項活動。{skill_note}"
                "你看完再決定是否送出邀請；現在只開始搜尋，可以嗎？"
            )
        elif isinstance(getattr(turn, "active_proposal", None), dict) and (
            getattr(turn, "active_proposal", {}).get("stage") == "waiting_other"
        ):
            preview = (
                "可以，原本那張邀請會繼續等對方回覆。"
                "這次有特別想一起做的事嗎？沒有的話，我就依你最近的近況找。"
                "要我現在開始找嗎？"
            )
        else:
            preview = (
                "這次沒有指定活動，我會依你最近分享的近況找人。"
                "要我現在開始嗎？"
            )
        return {"action": tool_name, "arguments": {}, "data": data}, preview
    if tool_name == "match.cancel_search":
        job = active_match_search_job(ctx.user_id)
        if not job or str(job.get("status") or "") not in {"queued", "running"}:
            return None, "目前沒有正在進行、可以取消的配對搜尋。"
        job_id = str(job.get("job_id") or "")
        if not job_id:
            return None, "這次搜尋的狀態已更新，請重新查看後再取消。"
        return {
            "action": tool_name,
            "arguments": {},
            "data": {"match_search_job_id": job_id},
        }, "要取消目前正在進行的配對搜尋嗎？確認後我才會停止。"
    if tool_name == "match.decide_active_proposal":
        proposal = turn.active_proposal or {}
        decision = str(arguments.get("decision") or "")
        allowed_actions = set(proposal.get("allowed_actions") or [])
        authority = getattr(turn, "_active_proposal_authority", None) or {}
        if decision not in allowed_actions or decision not in {"interested", "declined", "cancelled"}:
            return None, "目前沒有一張可由你決定的配對提案。"
        revision = proposal.get("proposal_revision")
        if type(revision) is not int or revision < 0 or not str(authority.get("match_id") or ""):
            return None, "這張提案的狀態已經更新，請重新查看後再決定。"
        counterparty = str(proposal.get("counterparty") or "對方")
        action_label = {
            "interested": "表示有興趣",
            "declined": "婉拒",
            "cancelled": "撤回等待",
        }[decision]
        return {
            "action": tool_name,
            "arguments": {"decision": decision},
            "data": {
                "match_id": str(authority.get("match_id") or ""),
                "expected_status": str(authority.get("expected_status") or ""),
                "proposal_revision": revision,
                "proposal_namespace": str(authority.get("proposal_namespace") or ""),
            },
        }, f"要對 {counterparty} 的提案{action_label}嗎？確認後我才會送出。"
    if tool_name == "match.decide_active_event_invitation":
        invitation = turn.active_event_invitation or {}
        decision = str(arguments.get("decision") or "")
        if not invitation.get("user_can_decide") or decision not in {"interested", "declined"}:
            return None, "目前沒有一張可由你決定的活動牽線邀請。"
        revision = int(invitation.get("proposal_revision", 0) or 0)
        if revision <= 0:
            return None, "這張活動邀請的狀態已更新，請重新查看後再決定。"
        title = str(invitation.get("event_title") or "這個活動")[:80]
        action_label = "表示有興趣" if decision == "interested" else "婉拒"
        return {
            "action": tool_name,
            "arguments": {"decision": decision},
            "data": {"proposal_revision": revision},
        }, f"要對「{title}」的活動牽線邀請{action_label}嗎？確認後我才會送出。"
    if tool_name == "relationship.start_date_coordination":
        return _prepare_date_coordination(arguments, ctx, turn)
    if tool_name == "relationship.cancel_date_coordination":
        return _prepare_cancel_date_coordination(arguments, ctx, turn)
    if tool_name == "profile.start_assessment":
        kind = {"basic": "big_five", "deep": "deep_profile"}.get(str(arguments.get("kind") or ""))
        if kind is None:
            return None, "我還不確定你想重新做哪一種探索類型。"
        from services.assessment_session_service import assessment_label
        return {"action": tool_name, "arguments": dict(arguments), "data": {}}, (
            f"要重新開始{assessment_label(kind)}嗎？新的結果完成前，原本的資料會保留。回覆「確認」就開始，也可以回覆「取消」。"
        )
    return None, "我現在不能安全地處理這個操作。"


def execute_write(
    tool_name: str,
    arguments: dict[str, Any],
    ctx: Any,
    turn: Any,
    run_id: str,
    index: int,
    *,
    confirmation_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[bool, str, str | None]:
    """Execute one confirmed write through its canonical domain service."""
    if tool_name.startswith("calendar."):
        cid = (payload or {}).get("_confirmation_id") or confirmation_id or uuid.uuid4().hex
        return _calendar_execute(ctx, tool_name, arguments, payload, confirmation_id=cid)
    executor = _WRITE_EXECUTORS.get(tool_name)
    if executor is None:
        return False, "我現在不能安全地處理這個操作。", "write_executor_not_registered"
    cid = (payload or {}).get("_confirmation_id") or confirmation_id
    return executor(ctx, turn, run_id, index, arguments, cid, payload)
