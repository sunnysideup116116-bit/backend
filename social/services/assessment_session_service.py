"""Canonical owner-scoped assessment sessions for Public Ayue and onboarding.

The service owns the short-lived draft and the commit boundary.  Callers can
ask it to start, advance, cancel or commit, but never write profile results or
temporary assessment fields directly.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Literal

from database import profiles_coll
from services.ai_service import analyze_big_five, analyze_deep_profile
from services.language_service import normalize_zh_tw


AssessmentKind = Literal["big_five", "deep_profile"]
ASSESSMENT_KINDS = frozenset({"big_five", "deep_profile"})
ACTIVE_SESSION_STATUSES = frozenset({"active", "awaiting_commit"})
ASSESSMENT_SESSION_TTL_SECONDS = 24 * 60 * 60

_CANCEL_CHOICES = frozenset({"取消", "退出", "結束測驗", "先停止", "不做了"})
_CONFIRM_CHOICES = frozenset({"好", "好的", "可以", "確認", "確定", "要", "yes", "ok"})


def _compact(message: str) -> str:
    return re.sub(r"\s+", "", str(message or "")).strip().lower()


def assessment_label(kind: str) -> str:
    return "基本性格探索" if kind == "big_five" else "深層探索"


def assessment_cancel_choice(message: str) -> bool:
    """Closed exit protocol; semantic intent remains the Planner's job."""
    return _compact(message) in _CANCEL_CHOICES


def assessment_commit_choice(message: str) -> str:
    """Closed confirmation protocol for applying an already typed draft."""
    compact = _compact(message)
    if compact in _CONFIRM_CHOICES:
        return "confirm"
    if compact in _CANCEL_CHOICES | {"不要", "不用", "先不要", "no"}:
        return "cancel"
    return "none"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return default


def _safe_revision(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _session(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    value = (profile or {}).get("agentic_assessment_session") or {}
    if (
        not isinstance(value, dict)
        or str(value.get("kind") or "") not in ASSESSMENT_KINDS
        or not str(value.get("session_id") or "")
        or str(value.get("status") or "") not in {"active", "awaiting_commit", "completed", "cancelled", "expired"}
    ):
        return None
    return value


def active_assessment_session(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    session = _session(profile)
    return session if session and session.get("status") == "active" else None


def awaiting_assessment_commit(profile: dict[str, Any] | None) -> dict[str, Any] | None:
    session = _session(profile)
    return session if session and session.get("status") == "awaiting_commit" else None


def assessment_public_state(profile: dict[str, Any] | None) -> dict[str, Any]:
    """Return only additive response metadata; never expose the session ID/draft."""
    session = _session(profile)
    if not session:
        return {"assessment_state": None, "assessment_kind": None, "assessment_revision": None}
    return {
        "assessment_state": str(session.get("status") or ""),
        "assessment_kind": str(session.get("kind") or ""),
        "assessment_revision": _safe_revision(session.get("revision")),
    }


def assessment_ui_projection(profile: dict[str, Any] | None, kind: AssessmentKind) -> dict[str, Any]:
    """Return the owner-facing typed value used by the legacy assessment UI."""
    source = profile or {}
    session = _session(source)
    draft: dict[str, Any] = {}
    if (
        session
        and session.get("kind") == kind
        and session.get("status") in ACTIVE_SESSION_STATUSES
        and isinstance(session.get("draft"), dict)
    ):
        draft = dict(session.get("draft") or {})
    completed = source.get(kind) if isinstance(source.get(kind), dict) else None
    return {**assessment_public_state(source), "value": draft or completed}


def _outcome_with_session(status: str, reply: str, session: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": status, "reply": reply}
    if session:
        result.update({
            "session_state": str(session.get("status") or "") or None,
            "kind": str(session.get("kind") or "") or None,
            "revision": _safe_revision(session.get("revision")),
        })
    return result


def _latest_session_outcome(user_id: str, status: str, reply: str) -> dict[str, Any]:
    latest = profiles_coll.find_one(
        {"user_id": user_id}, {"_id": 0, "agentic_assessment_session": 1},
    ) or {}
    return _outcome_with_session(status, reply, _session(latest))


def _first_question(kind: AssessmentKind) -> str:
    if kind == "big_five":
        return "好，我們用幾個輕鬆的問題慢慢認識你。臨時多出一段空閒時，你通常會先排好計畫，還是隨興看看想做什麼？"
    return "好，我們從你在意的生活開始聊。最近做過哪一件事，會讓你覺得這段時間過得很值得？"


def _safe_reply(value: Any) -> str:
    reply = normalize_zh_tw(str(value or ""), max_length=360).strip()
    if not reply or "系統錯誤" in reply or "error:" in reply.lower():
        return ""
    return reply


def _safe_text(value: Any, limit: int) -> str:
    return normalize_zh_tw(str(value or ""), max_length=limit).strip(" \t\n，,。")


def _safe_text_list(value: Any, limit: int = 4, item_limit: int = 36) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value:
        text = _safe_text(item, item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _clean_big_five(value: Any, previous: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    clean: dict[str, Any] = {}
    for key in ("O", "C", "E", "A", "N"):
        candidate = source.get(key, previous.get(key))
        if isinstance(candidate, (int, float)) and not isinstance(candidate, bool):
            clean[key] = max(1, min(10, round(float(candidate), 2)))
    summary = _safe_text(source.get("summary") or previous.get("summary"), 140)
    if summary:
        clean["summary"] = summary
    return clean


def _clean_deep_profile(value: Any, previous: dict[str, Any]) -> dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    clean = {
        "values": _safe_text_list(source.get("values", previous.get("values"))),
        "life_goals": _safe_text_list(source.get("life_goals", previous.get("life_goals"))),
        "relationship_needs": _safe_text_list(source.get("relationship_needs", previous.get("relationship_needs"))),
        "stress_coping": _safe_text(source.get("stress_coping") or previous.get("stress_coping"), 100),
        "ideal_future": _safe_text(source.get("ideal_future") or previous.get("ideal_future"), 120),
        "summary": _safe_text(source.get("summary") or previous.get("summary"), 140),
    }
    return {key: item for key, item in clean.items() if item}


def _complete_projection_is_valid(kind: str, projected: dict[str, Any]) -> bool:
    if kind == "big_five":
        return all(key in projected for key in ("O", "C", "E", "A", "N")) and bool(projected.get("summary"))
    if kind == "deep_profile":
        return all(bool(projected.get(key)) for key in (
            "values", "life_goals", "relationship_needs", "stress_coping", "ideal_future", "summary",
        ))
    return False


def _session_expired(session: dict[str, Any], now: float) -> bool:
    """Only a known elapsed deadline is automatically expired.

    A malformed legacy session must not be silently replaced by a new start.
    It remains fail-closed until an explicit reset/cancellation path handles it.
    """
    expires_at = _safe_float(session.get("expires_at"))
    return bool(expires_at and expires_at <= now)


def _open_session(profile: dict[str, Any] | None, now: float) -> dict[str, Any] | None:
    session = _session(profile)
    if session and session.get("status") in ACTIVE_SESSION_STATUSES and not _session_expired(session, now):
        return session
    return None


def _terminal_update(
    user_id: str, session_id: str, kind: str, state: str, revision: int,
) -> bool:
    result = profiles_coll.update_one(
        {
            "user_id": user_id,
            "agentic_assessment_session.session_id": session_id,
            "agentic_assessment_session.kind": kind,
            "agentic_assessment_session.status": {"$in": list(ACTIVE_SESSION_STATUSES)},
            "agentic_assessment_session.revision": revision,
        },
        {
            "$set": {
                "agentic_assessment_session.status": state,
                "agentic_assessment_session.revision": revision + 1,
                "agentic_assessment_session.updated_at": time.time(),
            },
            "$unset": {
                "agentic_assessment_session.draft": "",
                "temp_big_five": "", "temp_deep_profile": "",
                "interaction_count": "", "interaction_count_deep": "",
            },
        },
    )
    return bool(getattr(result, "modified_count", 0))


def start_assessment_session(user_id: str, kind: AssessmentKind, *, idempotency_key: str) -> dict[str, Any]:
    """Create exactly one active draft session; completed profile stays untouched."""
    if kind not in ASSESSMENT_KINDS:
        return {"status": "invalid_kind", "reply": "我沒有找到這種探索方式。"}
    now = time.time()
    profile = profiles_coll.find_one({"user_id": user_id})
    existing = _open_session(profile, now)
    if existing:
        if existing.get("start_idempotency_key") == idempotency_key and existing.get("status") == "active":
            return {"status": "already_started", "reply": _first_question(kind), "session": existing}
        return {
            "status": "already_active",
            "reply": f"你目前正在進行{assessment_label(str(existing.get('kind')))}；想先離開的話，回覆「結束測驗」。",
            "session": existing,
        }
    session = {
        "version": "v1", "session_id": uuid.uuid4().hex, "user_id": user_id,
        "kind": kind, "status": "active", "revision": 0, "turn_count": 0,
        "draft": {}, "created_at": now, "updated_at": now,
        "expires_at": now + ASSESSMENT_SESSION_TTL_SECONDS,
        "start_idempotency_key": idempotency_key,
    }
    if profile is None:
        profiles_coll.update_one({"user_id": user_id}, {"$setOnInsert": {"user_id": user_id}}, upsert=True)
    result = profiles_coll.update_one(
        {
            "user_id": user_id,
            "$or": [
                {"agentic_assessment_session": {"$exists": False}},
                {"agentic_assessment_session": None},
                {"agentic_assessment_session.status": {"$nin": list(ACTIVE_SESSION_STATUSES)}},
                {"agentic_assessment_session.expires_at": {"$lte": now}},
            ],
        },
        {
            "$set": {"agentic_assessment_session": session},
            "$unset": {
                "temp_big_five": "", "temp_deep_profile": "",
                "interaction_count": "", "interaction_count_deep": "",
            },
        },
    )
    if getattr(result, "modified_count", 0):
        return {"status": "started", "session": session, "reply": _first_question(kind)}
    latest = profiles_coll.find_one({"user_id": user_id}) or {}
    current = _open_session(latest, now)
    if current:
        if current.get("start_idempotency_key") == idempotency_key and current.get("status") == "active":
            return {"status": "already_started", "session": current, "reply": _first_question(kind)}
        return {
            "status": "already_active", "session": current,
            "reply": f"你目前正在進行{assessment_label(str(current.get('kind')))}；想先離開的話，回覆「結束測驗」。",
        }
    return {"status": "start_failed", "reply": "我現在還不能開始這段探索，晚一點再試一次好嗎？"}


def cancel_assessment_session(user_id: str, session_id: str, kind: str | None = None) -> dict[str, Any]:
    """Cancel active or pending-commit work without changing completed profile."""
    profile = profiles_coll.find_one({"user_id": user_id}, {"_id": 0, "agentic_assessment_session": 1}) or {}
    session = _session(profile)
    if not session or session.get("session_id") != session_id or (kind and session.get("kind") != kind):
        return _outcome_with_session("stale", "這段探索剛剛已有新的變動，我沒有取消其他狀態。", session)
    if session.get("status") not in ACTIVE_SESSION_STATUSES:
        status = "already_cancelled" if session.get("status") == "cancelled" else "stale"
        return _outcome_with_session(status, "這段探索已經取消。" if status == "already_cancelled" else "這段探索剛剛已有新的變動，我沒有覆寫它。", session)
    changed = _terminal_update(user_id, session_id, str(session.get("kind")), "cancelled", _safe_revision(session.get("revision")))
    if changed:
        cancelled = {**session, "status": "cancelled", "revision": _safe_revision(session.get("revision")) + 1}
        return _outcome_with_session("cancelled", "這段探索已取消。", cancelled)
    return _latest_session_outcome(user_id, "stale", "這段探索剛剛已有新的變動，我沒有覆寫它。")


def expire_assessment_session(user_id: str, session_id: str, kind: str | None = None) -> dict[str, Any]:
    profile = profiles_coll.find_one({"user_id": user_id}, {"_id": 0, "agentic_assessment_session": 1}) or {}
    session = _session(profile)
    if session and session.get("session_id") == session_id and (not kind or session.get("kind") == kind):
        revision = _safe_revision(session.get("revision"))
        if _terminal_update(user_id, session_id, str(session.get("kind")), "expired", revision):
            expired = {**session, "status": "expired", "revision": revision + 1}
            return _outcome_with_session("expired", "這段探索已經過期。", expired)
    return _latest_session_outcome(user_id, "stale", "這段探索剛剛已有新的變動，我沒有覆寫它。")


def _completion_reply(kind: str, draft: dict[str, Any]) -> str:
    summary = _safe_text(draft.get("summary"), 140)
    label = "基本性格" if kind == "big_five" else "深層資料"
    body = f"我整理好一版{label}摘要：{summary}。" if summary else f"我整理好一版{label}資料。"
    return body + "這是新的草稿，回覆「確認」才會套用；想保留原本資料可回覆「取消」。"


def advance_assessment_session(
    user_id: str, session_id: str, message: str, *, message_id: str | None = None,
) -> dict[str, Any]:
    """Apply one answer by revision CAS; completion only prepares a commit draft."""
    profile = profiles_coll.find_one(
        {
            "user_id": user_id,
            "agentic_assessment_session.session_id": session_id,
            "agentic_assessment_session.status": "active",
        }
    ) or {}
    session = active_assessment_session(profile)
    if not session or session.get("session_id") != session_id:
        return _latest_session_outcome(user_id, "stale", "這段探索剛剛已結束；你想聊別的也可以。")
    now = time.time()
    if _session_expired(session, now):
        outcome = expire_assessment_session(user_id, session_id, str(session.get("kind") or ""))
        return {**outcome, "reply": "剛剛那段探索已經過期；想重新開始時再跟我說就好。"}
    if message_id and session.get("last_message_id") == message_id:
        return _outcome_with_session("duplicate", "我有收到這個回答，會接著從目前進度繼續。", session)

    kind = str(session.get("kind") or "")
    previous = session.get("draft") if isinstance(session.get("draft"), dict) else {}
    turn_count = _safe_revision(session.get("turn_count"))
    if kind == "big_five":
        try:
            # Assessment turns intentionally do not inherit the completed
            # profile, conversation context, or durable memory.  The only
            # model inputs are this saved owner message and this session's
            # typed draft.
            raw = analyze_big_five(message, previous, turn_count, None)
        except Exception:
            raw = None
        projected = _clean_big_five((raw or {}).get("big_five"), previous)
    elif kind == "deep_profile":
        try:
            raw = analyze_deep_profile(message, previous, turn_count, None)
        except Exception:
            raw = None
        projected = _clean_deep_profile((raw or {}).get("deep_profile"), previous)
    else:
        return {"status": "stale", "reply": "這段探索剛剛已結束；你想聊別的也可以。"}
    if not isinstance(raw, dict):
        return _outcome_with_session("provider_error", "我剛剛沒有聽清楚，想請你換個方式說說看？", session)
    reply = _safe_reply(raw.get("reply"))
    if not reply:
        return _outcome_with_session("provider_error", "我剛剛沒有聽清楚，想請你換個方式說說看？", session)
    complete = bool(raw.get("is_complete"))
    if complete and not _complete_projection_is_valid(kind, projected):
        return _outcome_with_session("provider_error", "我想再多聽一點你的想法，才不會草率替你整理結果。", session)

    revision = _safe_revision(session.get("revision"))
    base_query: dict[str, Any] = {
        "user_id": user_id,
        "agentic_assessment_session.session_id": session_id,
        "agentic_assessment_session.kind": kind,
        "agentic_assessment_session.status": "active",
        "agentic_assessment_session.revision": revision,
    }
    if message_id:
        base_query["agentic_assessment_session.last_message_id"] = {"$ne": message_id}
    set_fields = {
        "agentic_assessment_session.draft": projected,
        "agentic_assessment_session.revision": revision + 1,
        "agentic_assessment_session.turn_count": turn_count + 1,
        "agentic_assessment_session.updated_at": now,
        "agentic_assessment_session.expires_at": now + ASSESSMENT_SESSION_TTL_SECONDS,
        "agentic_assessment_session.last_message_id": message_id,
    }
    if complete:
        set_fields["agentic_assessment_session.status"] = "awaiting_commit"
    result = profiles_coll.update_one(base_query, {"$set": set_fields})
    if getattr(result, "modified_count", 0):
        return {
            "status": "awaiting_commit" if complete else "continued",
            "reply": _completion_reply(kind, projected) if complete else reply,
            "session_state": "awaiting_commit" if complete else "active",
            "kind": kind, "draft": projected, "revision": revision + 1,
        }
    latest = profiles_coll.find_one({"user_id": user_id}, {"_id": 0, "agentic_assessment_session": 1}) or {}
    latest_session = _session(latest)
    if message_id and latest_session and latest_session.get("last_message_id") == message_id:
        return _outcome_with_session("duplicate", "我有收到這個回答，會接著從目前進度繼續。", latest_session)
    return _outcome_with_session("stale", "這段探索剛剛有新的變動，我先不重複記錄這次回答。", latest_session)


def commit_assessment_session(
    user_id: str, session_id: str, *, expected_revision: int, idempotency_key: str,
) -> dict[str, Any]:
    """Atomically replace just the completed profile section after confirmation."""
    profile = profiles_coll.find_one(
        {
            "user_id": user_id,
            "agentic_assessment_session.session_id": session_id,
            "agentic_assessment_session.status": "awaiting_commit",
        }
    ) or {}
    session = awaiting_assessment_commit(profile)
    if not session or session.get("session_id") != session_id:
        latest = profiles_coll.find_one(
            {"user_id": user_id, "agentic_assessment_session.session_id": session_id},
            {"_id": 0, "agentic_assessment_session": 1},
        ) or {}
        completed = _session(latest)
        if completed and completed.get("session_id") == session_id and completed.get("status") == "completed":
            return _outcome_with_session("already_committed", "這份新結果已經套用完成。", completed)
        return _outcome_with_session("stale", "這份探索草稿已經不在等待確認了。", completed)
    now = time.time()
    if _session_expired(session, now):
        expire_assessment_session(user_id, session_id, str(session.get("kind") or ""))
        return {"status": "expired", "reply": "這份探索草稿已經過期，原本的資料沒有變動。"}
    revision = _safe_revision(session.get("revision"))
    if revision != _safe_revision(expected_revision):
        return _outcome_with_session("stale", "這份探索草稿剛剛有變動，我沒有覆寫資料。", session)
    kind = str(session.get("kind") or "")
    draft = session.get("draft") if isinstance(session.get("draft"), dict) else {}
    if not _complete_projection_is_valid(kind, draft):
        return _outcome_with_session("invalid_draft", "這份草稿資料不完整，我沒有覆寫原本結果。", session)
    completed_field = "big_five" if kind == "big_five" else "deep_profile"
    result = profiles_coll.update_one(
        {
            "user_id": user_id,
            "agentic_assessment_session.session_id": session_id,
            "agentic_assessment_session.kind": kind,
            "agentic_assessment_session.status": "awaiting_commit",
            "agentic_assessment_session.revision": revision,
        },
        {
            "$set": {
                completed_field: draft,
                "agentic_assessment_session.status": "completed",
                "agentic_assessment_session.revision": revision + 1,
                "agentic_assessment_session.updated_at": now,
                "agentic_assessment_session.commit_idempotency_key": idempotency_key,
            },
            "$unset": {
                "agentic_assessment_session.draft": "",
                "temp_big_five": "", "temp_deep_profile": "",
                "interaction_count": "", "interaction_count_deep": "",
            },
        },
    )
    if getattr(result, "modified_count", 0):
        completed = {**session, "status": "completed", "revision": revision + 1}
        return _outcome_with_session("committed", "好，新的探索結果已套用；原本資料已由這份完成結果取代。", completed)
    latest = profiles_coll.find_one({"user_id": user_id}, {"_id": 0, "agentic_assessment_session": 1}) or {}
    latest_session = _session(latest)
    if latest_session and latest_session.get("session_id") == session_id and latest_session.get("status") == "completed":
        return _outcome_with_session("already_committed", "這份新結果已經套用完成。", latest_session)
    return _outcome_with_session("stale", "這份探索草稿剛剛有變動，我沒有覆寫資料。", latest_session)


def handle_assessment_ui_message(
    user_id: str, kind: AssessmentKind, message: str, *, initial_interest: str | None = None,
    initialize: bool = False,
) -> dict[str, Any]:
    """Compatibility facade for the existing /api/chat onboarding UI.

    The UI's explicit choice of an assessment stage is its start confirmation;
    all draft, completion and commit writes still use the same domain state as
    the public-agent workflow.
    """
    if kind == "big_five":
        interest = _safe_text(initial_interest, 120)
        if interest and interest not in {"無特別興趣", "沒有特別興趣"}:
            # This preserves the established onboarding field without coupling
            # it to the assessment draft or overwriting a later user choice.
            profiles_coll.update_one(
                {"user_id": user_id, "$or": [
                    {"initial_interest": {"$exists": False}},
                    {"initial_interest": ""},
                ]},
                {"$set": {"initial_interest": interest}}, upsert=True,
            )
    now = time.time()
    profile = profiles_coll.find_one({"user_id": user_id}) or {}
    session = _session(profile)
    if session and session.get("status") in ACTIVE_SESSION_STATUSES and _session_expired(session, now):
        expire_assessment_session(user_id, str(session.get("session_id")), str(session.get("kind")))
        session = None
    if session and session.get("status") == "awaiting_commit":
        choice = assessment_commit_choice(message)
        if choice == "confirm":
            return commit_assessment_session(
                user_id, str(session.get("session_id")),
                expected_revision=_safe_revision(session.get("revision")),
                idempotency_key=f"onboarding-ui-commit:{session.get('session_id')}",
            )
        if choice == "cancel":
            cancelled = cancel_assessment_session(user_id, str(session.get("session_id")), str(session.get("kind")))
            if cancelled.get("session_state") == "cancelled":
                return {**cancelled, "reply": "好，這份草稿先不套用，原本的資料會保留。", "kind": session.get("kind")}
            return cancelled
        return {"status": "awaiting_commit", "reply": _completion_reply(str(session.get("kind")), dict(session.get("draft") or {})), "kind": session.get("kind"), "draft": session.get("draft") or {}, "revision": _safe_revision(session.get("revision"))}
    if session and session.get("status") == "active" and session.get("kind") != kind:
        return {"status": "already_active", "reply": f"你目前正在進行{assessment_label(str(session.get('kind')))}；想先離開的話，回覆「結束測驗」。", "kind": session.get("kind")}
    if not session or session.get("status") != "active":
        started = start_assessment_session(user_id, kind, idempotency_key=f"onboarding-ui:{kind}")
        if started.get("status") not in {"started", "already_started"}:
            return started
        session = started.get("session") or {}
        if initialize:
            return {
                **started, "kind": kind, "session_state": "active",
                "revision": _safe_revision(session.get("revision")), "draft": session.get("draft") or {},
            }
    elif initialize:
        return _outcome_with_session("already_started", _first_question(kind), session) | {
            "draft": session.get("draft") or {},
        }
    if assessment_cancel_choice(message):
        cancelled = cancel_assessment_session(user_id, str(session.get("session_id")), kind)
        if cancelled.get("session_state") == "cancelled":
            return {**cancelled, "reply": "好，這段探索先停在這裡；原本已完成的資料我會保留。", "kind": kind}
        return cancelled
    return advance_assessment_session(user_id, str(session.get("session_id")), message)


def reset_assessment_session(user_id: str, kind: AssessmentKind) -> dict[str, Any]:
    """Compatibility reset for onboarding UI; never clears completed results."""
    profile = profiles_coll.find_one({"user_id": user_id}, {"_id": 0, "agentic_assessment_session": 1}) or {}
    session = _session(profile)
    if session and session.get("kind") == kind and session.get("status") in ACTIVE_SESSION_STATUSES:
        return cancel_assessment_session(user_id, str(session.get("session_id")), kind)
    # Old temporary fields may exist from a pre-session deployment. Clear them
    # only through the canonical service and leave completed profile untouched.
    fields = {"temp_big_five": "", "interaction_count": ""} if kind == "big_five" else {
        "temp_deep_profile": "", "interaction_count_deep": "",
    }
    profiles_coll.update_one({"user_id": user_id}, {"$unset": fields})
    return {"status": "reset"}
