# V3 Feature Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 V3 sub-agent runtime 達到 V2 的功能對等（寫入執行、match opportunity、assessment session、trace、sources、metrics、reuse、idempotency），之後移除 V2 並改寫文件。

**Architecture:** 擴充 `services/ayue_agent/v3/`：新增 write executors 模組（含 confirmation preflight）、Scheduler 接入 opportunity/assessment/trace、Planner 增加 opportunity signal、Synthesizer 增加 confirmation/guidance 回覆規則。V3 成為唯一 public runtime。

**Tech Stack:** Python 3.13、FastAPI、MongoDB、Ollama function calling、unittest/pytest。

## Global Constraints

- 所有測試離線執行，不得連線正式 MongoDB Atlas／Neo4j。
- Python source 必須 compile；`GET /` 與 port 9001 health 必須 200。
- 遵守 AGENTS.md：不建平行 keyword router；寫入走 domain service；寫入前先 confirmation；trace 只存 allowlisted metadata。
- 禁止修改 `private_runtime.py`、`private_v2.py`、`private_mediator.py` 的 private 邏輯。
- 禁止格式化整份 `routers/chat.py` 或 `frontend.html`。
- 測試指令：在 `social_demotest/` 下執行 `python -m pytest tests/<file> -q`。

---

### Task 1: V3 write executors 模組（含 idempotency）

**Files:**
- Create: `social_demotest/services/ayue_agent/v3/write_executors.py`
- Test: `social_demotest/tests/test_v3_write_executors.py`

**Interfaces:**
- Produces: `execute_write(tool_name, arguments, ctx, turn, run_id, index, confirmation_id=None) -> tuple[bool, str, str | None]` — 回傳 (ok, reply, error_code)。`confirmation_id` 非 None 時用於 idempotency key。

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_v3_write_executors.py
import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.v3.write_executors import execute_write


class V3WriteExecutorsTests(unittest.TestCase):
    def _ctx(self):
        return AgentTurnContext(user_id="owner", room_id="room", message="確認")

    def test_start_search_queues_job(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.start_match_search",
                   return_value={"status": "queued"}) as start, \
             patch("services.ayue_agent.v3.write_executors.TOOL_CALLS.find_one_and_update",
                   return_value=None), \
             patch("services.ayue_agent.v3.write_executors.TOOL_CALLS.update_one"):
            ok, reply, code = execute_write(
                "match.start_search", {}, ctx, turn, "run1", 0,
                confirmation_id="conf1",
            )
        self.assertTrue(ok)
        self.assertIn("1–3 分鐘", reply)
        start.assert_called_once()
        self.assertEqual(start.call_args.kwargs["idempotency_key"], "confirmation:conf1")

    def test_decide_active_proposal_uses_revision_cas(self):
        ctx = self._ctx()
        turn = MagicMock()
        turn.active_proposal = {"user_can_decide": True, "proposal_revision": 2}
        with patch("services.ayue_agent.v3.write_executors.decide_active_proposal",
                   return_value={"status": "success"}) as decide:
            ok, reply, code = execute_write(
                "match.decide_active_proposal", {"decision": "interested"},
                ctx, turn, "run1", 0,
            )
        self.assertTrue(ok)
        self.assertEqual(decide.call_args.kwargs["expected_revision"], 2)

    def test_decide_active_proposal_not_actionable(self):
        ctx = self._ctx()
        turn = MagicMock()
        turn.active_proposal = {"user_can_decide": False}
        ok, reply, code = execute_write(
            "match.decide_active_proposal", {"decision": "interested"},
            ctx, turn, "run1", 0,
        )
        self.assertFalse(ok)
        self.assertEqual(code, "decision_not_actionable")

    def test_unknown_write_tool_fails(self):
        ctx = self._ctx()
        ok, reply, code = execute_write("nope.tool", {}, ctx, MagicMock(), "run1", 0)
        self.assertFalse(ok)
        self.assertEqual(code, "write_executor_not_registered")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_v3_write_executors.py -q`（在 `social_demotest/` 下）
Expected: FAIL — ModuleNotFoundError

- [ ] **Step 3: 實作 write_executors.py**

```python
"""V3 write executors: the only path that performs confirmed side effects.

Every write goes through a canonical domain service.  Planner/sub-agent
arguments never contain IDs or revisions; executors inject them from the
turn context and canonical state.
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from database import db
from services.match_action_service import decide_active_proposal, start_match_search
from services.assessment_session_service import start_assessment_session

TOOL_CALLS = db["agent_tool_calls"]


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


def _start_search(ctx: Any, run_id: str, index: int, *, confirmation_id: str | None) -> tuple[bool, str, str | None]:
    key = _idempotency_key(confirmation_id, run_id, index)
    if not _claim_once(key):
        return True, "我已經處理過這次搜尋。", None
    try:
        result = start_match_search(
            ctx.user_id, source="agent_v3", force_new=True, idempotency_key=key,
        )
        status = result.get("status", "failed")
        reply = {
            "queued": "好，我開始幫你找，通常約需要 1–3 分鐘。你可以先繼續跟我聊，找到後我會回來。",
            "already_queued": "這次搜尋已經排進去了，通常約需要 1–3 分鐘；你可以先繼續跟我聊。",
            "already_active": "你目前還有一張進行中的提案，我先不重複開新搜尋。",
            "already_searching": "我正在幫你找，先不用重複送出。",
            "no_candidates": "這輪暫時沒有合適的新對象。",
        }.get(status, "這次搜尋沒有成功啟動，我沒有把它當作已完成。")
        _finish(key, "done", {"status": status, "reply": reply})
        return status in {"queued", "already_queued", "already_active", "already_searching", "no_candidates"}, reply, None
    except Exception as exc:
        return False, "我現在不能安全地開始搜尋，請稍後再試。", type(exc).__name__


def _decide_active_proposal(ctx: Any, turn: Any, run_id: str, index: int, arguments: dict[str, Any]) -> tuple[bool, str, str | None]:
    proposal = turn.active_proposal or {}
    decision = str(arguments.get("decision") or "")
    if not proposal.get("user_can_decide") or not decision:
        return False, "我需要你對目前這張提案明確表示有興趣或婉拒。", "decision_not_actionable"
    try:
        outcome = decide_active_proposal(
            user_id=ctx.user_id,
            decision=decision,
            expected_revision=int(proposal.get("proposal_revision", 0)),
            idempotency_key=_idempotency_key(None, run_id, index),
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
        return True, "好，我已更新這張牽線提案。" if decision == "interested" else "好，這張提案已替你婉拒。", None
    except Exception as exc:
        return False, "我現在不能安全地更新這張提案。", type(exc).__name__


def _start_assessment(ctx: Any, arguments: dict[str, Any], *, confirmation_id: str | None) -> tuple[bool, str, str | None]:
    kind = {"basic": "big_five", "deep": "deep_profile"}.get(str(arguments.get("kind") or ""))
    if kind is None:
        return False, "我沒有找到要開始的探索類型，你可以告訴我想做基本性格還是深層探索。", "assessment_unknown_kind"
    key = _idempotency_key(confirmation_id, "assessment", 0, suffix=kind)
    if not _claim_once(key):
        return True, "這個探索確認正在處理，我不會重複開始。", None
    try:
        outcome = start_assessment_session(ctx.user_id, kind, idempotency_key=key)
    except Exception as exc:
        return False, "剛剛沒有成功開始，我沒有改動原本的資料。你想再試一次時跟我說。", type(exc).__name__
    ok = outcome.get("status") in {"started", "already_started"}
    _finish(key, "done", {"status": outcome.get("status")})
    return ok, str(outcome.get("reply") or "我們可以從一個輕鬆的問題開始。"), None if ok else str(outcome.get("status") or "assessment_start_failed")


def _calendar_execute(ctx: Any, tool_name: str, arguments: dict[str, Any], payload: dict[str, Any] | None, *, confirmation_id: str) -> tuple[bool, str, str | None]:
    from fastapi import HTTPException
    from services.calendar_service import (
        cancel_event, cancel_targets_are_current, create_personal_event,
        update_personal_event,
    )
    from services.date_coordination_service import cancel_coordination_or_event, request_reschedule

    payload = payload or {}
    key = f"calendar-confirmation:{confirmation_id}"
    try:
        if tool_name == "calendar.create_my_event":
            event = create_personal_event(ctx.user_id, arguments, agent_action_key=key)
            return True, f"已加入行程：{_calendar_event_label(event)}。", None
        event_id = str(payload.get("event_id") or "")
        revision = int(payload.get("event_revision", 0) or 0)
        is_shared = payload.get("event_source_type") == "date"
        other_id = str(payload.get("event_other_id") or "")
        if tool_name == "calendar.update_my_event":
            if is_shared:
                coordination, event = request_reschedule(
                    ctx.user_id, other_id, event_id,
                    dict(payload.get("proposed_form") or {}),
                    expected_revision=revision, idempotency_key=key,
                )
                form = coordination.get("form") or {}
                proposed_label = (
                    f"{str(form.get('date') or '')[5:].replace('-', '/')} "
                    f"{form.get('start_time', '')}–{form.get('end_time', '')} "
                    f"{form.get('activity') or '共同約會'}"
                ).strip()
                return True, f"已提出改期：{proposed_label}。對方已收到通知，確認後才會正式變更。", None
            changes = {k: v for k, v in arguments.items() if k != "event_hint" and v is not None}
            event = update_personal_event(
                ctx.user_id, event_id, changes, expected_revision=revision, agent_action_key=key,
            )
            return True, f"已更新行程：{_calendar_event_label(event)}。", None
        if tool_name == "calendar.cancel_my_event":
            if is_shared:
                coordination = cancel_coordination_or_event(
                    ctx.user_id, other_id, str(payload.get("coordination_id") or ""),
                    expected_revision=revision, idempotency_key=key,
                )
                title = str((coordination.get("form") or {}).get("activity") or "共同約會")
                return True, f"已取消共同約會「{title}」，對方已收到通知，雙方行事曆也已同步。", None
            event = cancel_event(
                ctx.user_id, event_id, personal_only=True, expected_revision=revision, agent_action_key=key,
            )
            return True, f"已取消行程：{_calendar_event_label(event)}。", None
        if tool_name == "calendar.cancel_my_events":
            targets = list(payload.get("targets") or [])
            if not cancel_targets_are_current(ctx.user_id, targets):
                return False, "其中一筆行程剛剛有變動，我沒有刪除任何行程。請重新確認。", "stale_revision"
            completed: list[str] = []
            failed = 0
            for index, target in enumerate(targets):
                target_key = f"{key}:{index}"
                try:
                    if target.get("event_source_type") == "date":
                        cancel_coordination_or_event(
                            ctx.user_id, str(target.get("event_other_id") or ""),
                            str(target.get("coordination_id") or ""),
                            expected_revision=int(target.get("event_revision", 0) or 0),
                            idempotency_key=target_key,
                        )
                    else:
                        cancel_event(
                            ctx.user_id, str(target.get("event_id") or ""),
                            personal_only=True,
                            expected_revision=int(target.get("event_revision", 0) or 0),
                            agent_action_key=target_key,
                        )
                    completed.append(str(target.get("safe_label") or "這筆行程"))
                except Exception:
                    failed += 1
            if not completed:
                return False, "這些行程現在無法變更；我沒有確認到任何刪除結果。", "calendar_write_failed"
            reply = f"已取消 {len(completed)} 筆行程：" + "、".join(f"「{label}」" for label in completed) + "。"
            if failed:
                reply += f"另有 {failed} 筆沒有刪除，請再查看後重試。"
            return True, reply, "partial" if failed else None
        return False, "這個行程確認已失效，請重新告訴我你想怎麼安排。", "unknown_calendar_action"
    except HTTPException as exc:
        if exc.status_code == 409:
            return False, "這筆行程剛剛有變動，我沒有覆寫它。請告訴我最新想怎麼改。", "stale_revision"
        return False, "這筆行程現在無法變更；你可以再告訴我想處理哪一筆嗎？", "calendar_write_failed"
    except Exception as exc:
        return False, "我這次沒有改動你的行程，請稍後再試。", type(exc).__name__


def _calendar_event_label(event: dict) -> str:
    from datetime import datetime
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
    return f"{start.month}/{start.day} {start:%H:%M}–{end:%H:%M} {title}"


_WRITE_EXECUTORS = {
    "match.start_search": lambda ctx, turn, run_id, index, args, cid: _start_search(ctx, run_id, index, confirmation_id=cid),
    "match.decide_active_proposal": _decide_active_proposal,
    "profile.start_assessment": lambda ctx, turn, run_id, index, args, cid: _start_assessment(ctx, args, confirmation_id=cid),
    "calendar.create_my_event": lambda ctx, turn, run_id, index, args, cid: _calendar_execute(ctx, "calendar.create_my_event", args, None, confirmation_id=cid or uuid.uuid4().hex),
    "calendar.update_my_event": lambda ctx, turn, run_id, index, args, cid: _calendar_execute(ctx, "calendar.update_my_event", args, None, confirmation_id=cid or uuid.uuid4().hex),
    "calendar.cancel_my_event": lambda ctx, turn, run_id, index, args, cid: _calendar_execute(ctx, "calendar.cancel_my_event", args, None, confirmation_id=cid or uuid.uuid4().hex),
    "calendar.cancel_my_events": lambda ctx, turn, run_id, index, args, cid: _calendar_execute(ctx, "calendar.cancel_my_events", args, None, confirmation_id=cid or uuid.uuid4().hex),
}


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
    executor = _WRITE_EXECUTORS.get(tool_name)
    if executor is None:
        return False, "我現在不能安全地處理這個操作。", "write_executor_not_registered"
    if tool_name.startswith("calendar."):
        return _calendar_execute(ctx, tool_name, arguments, payload, confirmation_id=confirmation_id or uuid.uuid4().hex)
    return executor(ctx, turn, run_id, index, arguments, confirmation_id)
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_v3_write_executors.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/write_executors.py social_demotest/tests/test_v3_write_executors.py
git commit -m "feat(v3): add write executors with idempotency for confirmed side effects"
```

---

### Task 2: Confirmation preflight（calendar preview、match opportunity gate）

**Files:**
- Modify: `social_demotest/services/ayue_agent/v3/write_executors.py`
- Modify: `social_demotest/services/ayue_agent/v3/confirmation.py`
- Test: `social_demotest/tests/test_v3_write_executors.py`（追加）

**Interfaces:**
- Produces: `prepare_write_confirmation(tool_name, arguments, ctx, turn) -> tuple[dict | None, str | None]` — 回傳 (pending_payload, preview_reply)；payload 含 `arguments`（executor-safe）與 `data`（event_id/revision/targets/proposed_form 等 executor-only 欄位）。回傳 (None, error_reply) 表示不可建立 confirmation。
- Produces: `ConfirmationManager.create_confirmation(..., payload: dict | None = None)`；`execute_confirmed` 的 executor 簽名改為 `(tool_name, arguments, user_id, payload)`。

- [ ] **Step 1: 寫失敗測試**

```python
# 追加到 tests/test_v3_write_executors.py
from services.ayue_agent.v3.write_executors import prepare_write_confirmation


class V3WritePreflightTests(unittest.TestCase):
    def _ctx(self):
        return AgentTurnContext(user_id="owner", room_id="room", message="確認")

    def test_start_search_not_ready_returns_error_reply(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.assess_match_opportunity") as assess:
            assess.return_value = MagicMock(state="not_ready", reason_codes=("profile_basis_insufficient",))
            payload, reply = prepare_write_confirmation("match.start_search", {}, ctx, turn)
        self.assertIsNone(payload)
        self.assertIn("多了解你的方向", reply)

    def test_start_search_ready_returns_payload(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.assess_match_opportunity") as assess:
            assess.return_value = MagicMock(state="ready", reason_codes=())
            payload, reply = prepare_write_confirmation("match.start_search", {}, ctx, turn)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["action"], "match.start_search")
        self.assertIn("確認", reply)

    def test_calendar_create_preview(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.write_executors.normalize_form", return_value={
                 "date": "2026-08-10", "start_time": "19:00", "end_time": "20:00", "title": "晚餐",
             }), \
             patch("services.ayue_agent.v3.write_executors._parse_local_interval",
                   return_value=(MagicMock(), MagicMock(), None)), \
             patch("services.ayue_agent.v3.write_executors.conflicts_for_viewer", return_value=[]):
            payload, reply = prepare_write_confirmation(
                "calendar.create_my_event",
                {"title": "晚餐", "date": "2026-08-10", "start_time": "19:00", "end_time": "20:00"},
                ctx, turn,
            )
        self.assertIsNotNone(payload)
        self.assertIn("晚餐", reply)
        self.assertIn("確認", reply)

    def test_calendar_cancel_not_found_returns_error(self):
        ctx = self._ctx()
        turn = MagicMock()
        with patch("services.ayue_agent.v3.write_executors.calendar_access_enabled", return_value=True), \
             patch("services.ayue_agent.v3.write_executors.resolve_owned_event",
                   return_value=(None, "not_found")):
            payload, reply = prepare_write_confirmation(
                "calendar.cancel_my_event", {"event_hint": "不存在的行程"}, ctx, turn,
            )
        self.assertIsNone(payload)
        self.assertIn("找不到", reply)

    def test_assessment_unknown_kind_returns_error(self):
        ctx = self._ctx()
        payload, reply = prepare_write_confirmation(
            "profile.start_assessment", {"kind": "weird"}, ctx, MagicMock(),
        )
        self.assertIsNone(payload)
        self.assertIn("探索類型", reply)
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_v3_write_executors.py -q`
Expected: FAIL — prepare_write_confirmation not defined

- [ ] **Step 3: 實作 preflight**

在 `write_executors.py` 追加：

```python
from services.ayue_agent.match_opportunity import (
    assess_match_opportunity, missing_basis_question,
)

_CALENDAR_WRITE_ACTIONS = {
    "calendar.create_my_event", "calendar.update_my_event",
    "calendar.cancel_my_event", "calendar.cancel_my_events",
}


def _calendar_preview_and_payload(ctx: Any, tool_name: str, arguments: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    from fastapi import HTTPException
    from services.calendar_service import (
        _parse_local_interval, calendar_access_enabled, conflicts_for_viewer,
        get_timezone, normalize_form, resolve_owned_event, resolve_owned_events_for_cancel,
    )
    from database import calendar_events_coll

    if not calendar_access_enabled(ctx.user_id):
        return None, "我目前不能存取你的行事曆；你可以先到日曆設定確認是否已授權。"
    event: dict | None = None
    targets: list[dict[str, Any]] = []
    conflicts: list[dict] = []
    try:
        if tool_name == "calendar.create_my_event":
            form = normalize_form(arguments)
            start_at, end_at, _ = _parse_local_interval(form)
            arguments = {**arguments, **form}
            conflicts = conflicts_for_viewer(ctx.user_id, [ctx.user_id], start_at, end_at)
            preview = f"要新增 {form['date'][5:].replace('-', '/')} {form['start_time']}–{form['end_time']}「{form['title']}」嗎？"
        elif tool_name == "calendar.cancel_my_events":
            events, resolution = resolve_owned_events_for_cancel(
                ctx.user_id,
                mode=str(arguments.get("mode") or ""),
                event_hints=list(arguments.get("event_hints") or []),
            )
            if resolution == "ambiguous":
                return None, "有一筆行程對應到不只一個結果。你可以補上日期或完整名稱嗎？"
            if resolution == "not_found":
                return None, "我找不到其中一筆自己的行程。你可以補上日期或名稱嗎？"
            if resolution == "too_many":
                return None, "接下來的行程超過 10 筆；請先指定想取消哪些日期。"
            if resolution or not events:
                return None, "我還需要更明確的行程名稱或日期，才能一次取消多筆。"
            targets = [_calendar_pending_target(item, ctx.user_id) for item in events]
            labels = "、".join(f"「{target['safe_label']}」" for target in targets)
            preview = f"要取消這 {len(targets)} 筆行程嗎：{labels}？"
            if any(target["event_source_type"] == "date" for target in targets):
                preview += " 其中共同約會會同步雙方行事曆並通知對方。"
        else:
            event, resolution = resolve_owned_event(ctx.user_id, arguments.get("event_hint", ""))
            if resolution == "ambiguous":
                return None, "我找到不只一筆符合的行程。你可以補上日期或完整名稱嗎？"
            if not event:
                return None, "我找不到這筆自己的行程。你可以補上日期或名稱嗎？"
            is_shared = event.get("source_type") == "date"
            if tool_name == "calendar.cancel_my_event":
                targets = [_calendar_pending_target(event, ctx.user_id)]
                preview = f"要取消「{_calendar_event_label(event)}」嗎？"
                if is_shared:
                    preview += " 這是共同約會，取消後會同步雙方行事曆並通知對方。"
            else:
                if is_shared and event.get("status") != "confirmed":
                    return None, "這筆共同約會正在等待重新確認；你可以先取消目前改期，或直接取消整筆約會。"
                zone = get_timezone(event.get("timezone") or "Asia/Taipei")
                from services.calendar_service import as_utc
                start = as_utc(event["start_at"]).astimezone(zone)
                end = as_utc(event["end_at"]).astimezone(zone)
                current = {
                    "title": (event.get("activity") if is_shared else event.get("title") or event.get("activity")) or "行程",
                    "date": start.date().isoformat(), "start_time": start.strftime("%H:%M"),
                    "end_time": end.strftime("%H:%M"), "timezone": event.get("timezone") or "Asia/Taipei",
                    "location": event.get("location") or "", "notes": event.get("notes") or "",
                }
                changes = {k: v for k, v in arguments.items() if k != "event_hint" and v is not None}
                if not changes:
                    return None, f"你想把「{_calendar_event_label(event)}」改成什麼呢？"
                if is_shared:
                    shared_changes = dict(changes)
                    if "title" in shared_changes:
                        shared_changes["activity"] = shared_changes.pop("title")
                    proposed = normalize_form({
                        **current, "activity": current["title"],
                        "budget": event.get("budget") or "", **shared_changes,
                    })
                else:
                    proposed = normalize_form({**current, **changes})
                start_at, end_at, _ = _parse_local_interval(proposed)
                conflicts = conflicts_for_viewer(
                    ctx.user_id,
                    list(event.get("participants") or [ctx.user_id]) if is_shared else [ctx.user_id],
                    start_at, end_at, event.get("event_id"),
                )
                arguments = {"event_hint": arguments["event_hint"], **changes}
                proposed_title = proposed["activity"] if is_shared else proposed["title"]
                preview = f"要把「{_calendar_event_label(event)}」改成 {proposed['date'][5:].replace('-', '/')} {proposed['start_time']}–{proposed['end_time']}「{proposed_title}」嗎？"
                if is_shared:
                    preview += " 對方會收到改期通知，重新確認後才會正式變更。"
    except HTTPException as exc:
        return None, f"我還需要補齊行程資訊：{exc.detail}。"
    if conflicts:
        preview += f" 這會和你現有的 {len(conflicts)} 筆行程重疊；仍要這樣安排嗎？"
    payload = {
        "action": tool_name,
        "arguments": arguments,
        "data": {
            "event_id": event.get("event_id") if event else None,
            "event_revision": int(event.get("revision", 1)) if event else None,
            "event_source_type": event.get("source_type") if event else None,
            "event_other_id": (
                next((p for p in (event.get("participants") or []) if p != ctx.user_id), None)
                if event and event.get("source_type") == "date" else None
            ),
            "coordination_id": event.get("coordination_id") if event else None,
            "targets": targets,
            "proposed_form": (
                proposed if event and tool_name == "calendar.update_my_event" and event.get("source_type") == "date" else None
            ),
        },
    }
    return payload, preview + " 回覆「確認」才會真的變更。"


def _calendar_pending_target(event: dict, user_id: str) -> dict[str, Any]:
    return {
        "event_id": str(event.get("event_id") or ""),
        "event_revision": int(event.get("revision", 1)),
        "event_source_type": str(event.get("source_type") or "personal"),
        "event_other_id": (
            next((p for p in (event.get("participants") or []) if p != user_id), None)
            if event.get("source_type") == "date" else None
        ),
        "coordination_id": event.get("coordination_id"),
        "safe_label": _calendar_event_label(event),
    }


def prepare_write_confirmation(
    tool_name: str, arguments: dict[str, Any], ctx: Any, turn: Any,
) -> tuple[dict[str, Any] | None, str | None]:
    """Validate a proposed write and return (pending_payload, preview_reply).

    Returns (None, error_reply) when the write cannot be confirmed (not ready,
    blocked, ambiguous, missing fields).  The payload carries executor-only
    data (event IDs, revisions, targets) that never reach the planner.
    """
    if tool_name in _CALENDAR_WRITE_ACTIONS:
        return _calendar_preview_and_payload(ctx, tool_name, arguments)
    if tool_name == "match.start_search":
        assessment = assess_match_opportunity(ctx.user_profile or {}, ctx.user_id, explicit_search=True)
        if assessment.state == "not_ready":
            return None, "我想先多了解你的方向，才能幫你找得更準。" + missing_basis_question(assessment)
        if assessment.state == "active_match_blocked":
            return None, "你目前還有一段配對正在進行，我先不重複開新搜尋。"
        return {"action": tool_name, "arguments": {}, "data": {}}, (
            "我會依你的近況、偏好和個性挑選，不會隨機配對。要我現在開始找就回覆「確認」；也可以先補充條件。"
        )
    if tool_name == "profile.start_assessment":
        kind = {"basic": "big_five", "deep": "deep_profile"}.get(str(arguments.get("kind") or ""))
        if kind is None:
            return None, "我還不確定你想重新做哪一種探索。"
        from services.assessment_session_service import assessment_label
        return {"action": tool_name, "arguments": dict(arguments), "data": {}}, (
            f"要重新開始{assessment_label(kind)}嗎？新的結果完成前，原本的資料會保留。回覆「確認」就開始，也可以回覆「取消」。"
        )
    return None, "我現在不能安全地處理這個操作。"
```

修改 `confirmation.py`：

```python
    def create_confirmation(
        self, *, user_id: str, agent_name: str, tool_name: str,
        arguments: dict[str, Any], ttl_seconds: int = 900,
        payload: dict[str, Any] | None = None,
    ) -> str:
        confirmation_id = uuid.uuid4().hex
        now = time.time()
        self._coll.insert_one({
            "_id": confirmation_id,
            "user_id": user_id,
            "agent_name": agent_name,
            "tool_name": tool_name,
            "arguments": arguments,
            "payload": payload or {},
            "status": "pending",
            "created_at": now,
            "expires_at": now + ttl_seconds,
        })
        return confirmation_id
```

`execute_confirmed` 的 executor 呼叫改為 `executor(tool_name, arguments, user_id, rec.get("payload") or {})`。

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_v3_write_executors.py tests/test_v3_confirmation.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/write_executors.py social_demotest/services/ayue_agent/v3/confirmation.py social_demotest/tests/test_v3_write_executors.py
git commit -m "feat(v3): confirmation preflight with calendar preview and match opportunity gate"
```

---

### Task 3: Scheduler 接入 write executors 與 confirmation payload

**Files:**
- Modify: `social_demotest/services/ayue_agent/v3/scheduler.py`
- Modify: `social_demotest/services/ayue_agent/v3/synthesizer.py`
- Test: `social_demotest/tests/test_v3_scheduler.py`（追加）

**Interfaces:**
- Consumes: `prepare_write_confirmation`, `execute_write`（Task 1/2）
- Produces: confirmation observation 含 `preview`；confirm 路徑的 observation 含執行結果。

- [ ] **Step 1: 寫失敗測試**

```python
# 追加到 tests/test_v3_scheduler.py
class V3SchedulerWriteTests(unittest.TestCase):
    def _ctx(self, message="確認"):
        return AgentTurnContext(user_id="owner", room_id="room", message=message)

    def test_write_proposal_creates_confirmation_with_preview(self):
        ctx = self._ctx("幫我找人")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="match", depends_on=[], task_brief="開始找人"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("好", None, _synth_metrics())
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "match": MagicMock(return_value=(
                     [ToolProposal(tool_name="match.start_search", arguments={})], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.prepare_write_confirmation",
                   return_value=({"action": "match.start_search", "arguments": {}, "data": {}}, "要開始找人嗎？回覆「確認」")) as prepare, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value._mentioned_ids = []
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        prepare.assert_called_once()
        insert.assert_called_once()
        obs = seen_obs.get("observations", [])
        self.assertEqual(obs[0]["status"], "ok")
        self.assertTrue(obs[0]["observation"]["pending_confirmation"])
        self.assertIn("確認", obs[0]["observation"]["preview"])

    def test_confirm_path_executes_write_and_relays_reply(self):
        ctx = self._ctx("確認")
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("好，我開始幫你找", None, _synth_metrics())
        with patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS") as coll, \
             patch("services.ayue_agent.v3.scheduler.execute_write",
                   return_value=(True, "好，我開始幫你找，通常約需要 1–3 分鐘。", None)) as exec_write, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            coll.find.return_value = [{
                "_id": "c1", "user_id": "owner", "agent_name": "match",
                "tool_name": "match.start_search", "arguments": {},
                "payload": {}, "status": "pending",
                "created_at": 0, "expires_at": 1e18,
            }]
            coll.update_one.return_value = MagicMock(modified_count=1)
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        exec_write.assert_called_once()
        self.assertEqual(exec_write.call_args.kwargs["confirmation_id"], "c1")
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_v3_scheduler.py -q`
Expected: FAIL

- [ ] **Step 3: 實作 scheduler 修改**

在 `scheduler.py`：

1. import 改為：
```python
from .write_executors import execute_write, prepare_write_confirmation
```

2. `_run_sub_task` 中 WRITE_REQUIRES_CONFIRMATION 分支改為：
```python
            if decision.code == GuardResultCode.WRITE_REQUIRES_CONFIRMATION:
                payload, preview = prepare_write_confirmation(
                    proposal.tool_name, proposal.arguments, turn_ctx._raw_ctx, turn_ctx,
                )
                if payload is None:
                    print(f"  [{task.id}#{index}] result=FAILED  preflight={preview}")
                    results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                                  tool_name=proposal.tool_name,
                                                  error_code="preflight_rejected",
                                                  observation={"preview": preview}))
                    continue
                _CONFIRMATIONS.insert_one({
                    "_id": uuid.uuid4().hex,
                    "user_id": turn_ctx.user_id,
                    "agent_name": task.agent,
                    "tool_name": proposal.tool_name,
                    "arguments": payload.get("arguments") or {},
                    "payload": payload.get("data") or {},
                    "status": "pending",
                    "created_at": time.time(),
                    "expires_at": time.time() + 900,
                })
                print(f"  [{task.id}#{index}] result=OK (pending_confirmation for {proposal.tool_name})")
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                              tool_name=proposal.tool_name,
                                              observation={
                                                  "pending_confirmation": True,
                                                  "tool_name": proposal.tool_name,
                                                  "preview": preview or "",
                                              }))
                continue
```

3. confirm 路徑改為：
```python
    if choice == "confirm":
        print("\n  [entry] confirmation=confirm → executing pending confirmations")
        results = mgr.execute_confirmed(
            user_id=ctx.user_id,
            executor=lambda tn, args, uid, payload: execute_write(
                tn, args, ctx, turn, run_id, 0,
                confirmation_id=None, payload=payload,
            ),
        )
        synth_slice = slice_for_agent("synthesizer", turn, prior_observations=[{
            "task_id": "confirm", "status": "ok", "tool": None,
            "result": results, "error_code": None, "skip_reason": None,
        }])
        reply, _card_decision, synth_metrics = synthesizer.synthesize(synth_slice)
        ...
```

注意：`execute_confirmed` 的 executor 現在收 4 個參數（含 payload）。`execute_write` 的 `confirmation_id` 由 `execute_confirmed` 內部無法取得（它只傳 tool/args/user/payload）——改為在 payload 中帶 `confirmation_id`，或讓 `execute_confirmed` 傳入。修改 `confirmation.py` 的 `execute_confirmed`：executor 呼叫改為 `executor(tool_name, arguments, user_id, rec.get("payload") or {})`，並在 payload 中注入 `{"_confirmation_id": cid}`：

```python
            try:
                tool_result = executor(
                    tool_name, arguments, user_id,
                    {**(rec.get("payload") or {}), "_confirmation_id": cid},
                )
```

4. `execute_write` 的 calendar 分支需要從 payload 取 `_confirmation_id`：
```python
    if tool_name.startswith("calendar."):
        cid = (payload or {}).get("_confirmation_id") or confirmation_id or uuid.uuid4().hex
        return _calendar_execute(ctx, tool_name, arguments, payload, confirmation_id=cid)
```

5. `_run_sub_task` 中 `execute_tool` 呼叫不變（唯讀）。

- [ ] **Step 4: synthesizer 回覆規則**

在 `synthesizer.py` `_build_prompt` 的規則區塊追加：

```
11. 【確認流程】若 observations 中 tool 為 null 且 result 是確認執行結果陣列，直接以其中的 reply 欄位回覆（多筆用「、」連接），不要改寫或加罐頭句。
12. 【待確認】若 observation 有 pending_confirmation 與 preview，直接以 preview 內容回覆使用者，請其回覆「確認」或「取消」。
```

`_observation_fallback` 追加：

```python
    for obs in observations:
        if obs.get("tool") is None and isinstance(obs.get("result"), list):
            replies = [str(r.get("reply") or "") for r in obs["result"] if isinstance(r, dict) and r.get("reply")]
            if replies:
                return "、".join(replies)
        if obs.get("observation") and isinstance(obs.get("observation"), dict) and obs["observation"].get("pending_confirmation"):
            preview = str(obs["observation"].get("preview") or "")
            if preview:
                return preview
```

- [ ] **Step 5: 執行測試確認通過**

Run: `python -m pytest tests/test_v3_scheduler.py tests/test_v3_synthesizer.py tests/test_v3_confirmation.py tests/test_v3_write_executors.py -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/scheduler.py social_demotest/services/ayue_agent/v3/synthesizer.py social_demotest/services/ayue_agent/v3/confirmation.py social_demotest/tests/test_v3_scheduler.py
git commit -m "feat(v3): scheduler executes confirmed writes via write executors"
```

---

### Task 4: V3 trace 持久化

**Files:**
- Modify: `social_demotest/services/ayue_agent/v3/scheduler.py`
- Test: `social_demotest/tests/test_v3_scheduler.py`（追加）

**Interfaces:**
- Produces: `_persist_trace(run_id, ctx, payload)` — 寫入 `agent_runs`，只存 allowlisted metadata。

- [ ] **Step 1: 寫失敗測試**

```python
# 追加到 tests/test_v3_scheduler.py
class V3SchedulerTraceTests(unittest.TestCase):
    def test_trace_persisted_with_allowlisted_fields(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="嗨")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查行程"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "calendar": MagicMock(return_value=(
                     [ToolProposal(tool_name="calendar.list_my_events", arguments={})], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool",
                   return_value=MagicMock(ok=True, data={"events": []}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("行程ok", None, _synth_metrics())), \
             patch("services.ayue_agent.v3.scheduler._persist_trace") as persist:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_id = "owner"
            run_public_agent_turn_v3(ctx, mode="on")
        self.assertEqual(persist.call_count, 1)
        payload = persist.call_args.args[2]
        self.assertEqual(payload["agent_version"], "v3")
        self.assertIn("plan", payload)
        self.assertIn("tool_results", payload)
        self.assertIn("event_sequence", payload)
        self.assertNotIn("message", payload)
        self.assertNotIn("prompt", payload)
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_v3_scheduler.py -q`
Expected: FAIL — _persist_trace not called

- [ ] **Step 3: 實作 trace**

在 `scheduler.py` 追加：

```python
RUNS = db["agent_runs"]


def _persist_trace(run_id: str, ctx: Any, payload: dict[str, Any]) -> None:
    """Persist an allowlisted, privacy-safe V3 trace only."""
    try:
        RUNS.insert_one({
            "run_id": run_id,
            "user_id": ctx.user_id,
            "room_id": ctx.room_id,
            "agent_version": "v3",
            "created_at": time.time(),
            **payload,
        })
    except Exception as exc:
        print(f"Agent trace skipped: {type(exc).__name__}")
```

在 `run_public_agent_turn_v3` 中收集並在 return 前呼叫：

```python
    trace = {
        "plan": [
            {"id": t.id, "agent": t.agent, "depends_on": t.depends_on}
            for t in plan.tasks
        ] if plan else [],
        "guard_results": [],
        "tool_results": [],
        "event_sequence": [],
        "latency_ms": run_total_ms,
        "result": {
            "handled": result.handled,
            "conversation_intent": result.conversation_intent,
            "fallback_reason": result.fallback_reason,
        },
    }
    _persist_trace(run_id, ctx, trace)
```

guard/tool 記錄：在 `_run_sub_task` 的 guard 與 tool 執行處，透過一個共享 list 收集（用 `guard_lock` 保護）。為簡化，`_run_sub_task` 接收 `trace` 參數並 append：

```python
        with guard_lock:
            decision = guard_proposal(...)
            trace["guard_results"].append(decision.code.value)
        ...
            trace["tool_results"].append({"tool": proposal.tool_name, "ok": tool_result.ok, "code": tool_result.error_code})
```

`_run_sub_task` 簽名加 `trace: dict[str, Any]`；`_run_one` 傳入。event_sequence：在 `_emit_progress` 中收集（V3 的 `_emit_progress` 目前無 trace 參數——改為在 scheduler 層維護 `event_sequence` list，`_emit_progress` 呼叫處 append）。

簡化做法：`_emit_progress` 加 `trace` 參數（同 V2）：

```python
def _emit_progress(callback, event_type, *, trace=None, **payload):
    if trace is not None:
        trace["event_sequence"].append(event_type)
    if callback is None:
        return
    try:
        callback({"type": event_type, **payload})
    except Exception:
        pass
```

所有 `_emit_progress` 呼叫處傳 `trace=trace`（scheduler 內約 10 處）。

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_v3_scheduler.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/scheduler.py social_demotest/tests/test_v3_scheduler.py
git commit -m "feat(v3): persist allowlisted agent_runs trace"
```

---

### Task 5: V3 Planner opportunity signal + Scheduler 處理

**Files:**
- Modify: `social_demotest/services/ayue_agent/v3/planner.py`
- Modify: `social_demotest/services/ayue_agent/v3/contracts.py`
- Modify: `social_demotest/services/ayue_agent/v3/scheduler.py`
- Test: `social_demotest/tests/test_v3_planner.py`、`social_demotest/tests/test_v3_scheduler.py`

**Interfaces:**
- Produces: `Plan.opportunity: OpportunitySignal | None`；`OpportunitySignal(signal: Literal["none","social_opening"], evidence_span: str, confidence: float)`。
- Produces: Scheduler 在 Planner 後、執行 DAG 前處理 opportunity：ready → 建立 confirmation + guidance reply；not_ready → 直接回覆 missing_basis。

- [ ] **Step 1: 寫失敗測試**

```python
# 追加到 tests/test_v3_planner.py
class V3PlannerOpportunityTests(unittest.TestCase):
    def test_plan_parses_opportunity_signal(self):
        from services.ayue_agent.v3.contracts import OpportunitySignal
        from services.ayue_agent.v3.planner import _DecomposeTasksArguments
        args = _DecomposeTasksArguments.model_validate({
            "tasks": [{"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "回覆"}],
            "opportunity": {"signal": "social_opening", "evidence_span": "一個人去有點孤單", "confidence": 0.9},
        })
        self.assertEqual(args.opportunity.signal, "social_opening")
        self.assertEqual(args.opportunity.evidence_span, "一個人去有點孤單")
```

```python
# 追加到 tests/test_v3_scheduler.py
class V3SchedulerOpportunityTests(unittest.TestCase):
    def test_social_opening_creates_guidance_confirmation(self):
        from services.ayue_agent.v3.contracts import OpportunitySignal
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="一個人去有點孤單")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="synthesizer", depends_on=[], task_brief="回覆"),
        ], opportunity=OpportunitySignal(signal="social_opening", evidence_span="一個人去有點孤單", confidence=0.9))
        seen_obs = {}
        def fake_synth(slice_payload, candidate_cards=None):
            seen_obs["observations"] = slice_payload.payload.get("observations", [])
            return ("好", None, _synth_metrics())
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.assess_match_opportunity") as assess, \
             patch("services.ayue_agent.v3.scheduler._CONFIRMATIONS.insert_one") as insert, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", side_effect=fake_synth):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_profile = {}
            assess.return_value = MagicMock(state="ready", reason_codes=(), fingerprint="fp1")
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        insert.assert_called_once()
        obs = seen_obs.get("observations", [])
        self.assertTrue(any(
            o.get("observation", {}).get("pending_confirmation") for o in obs
        ))
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_v3_planner.py tests/test_v3_scheduler.py -q`
Expected: FAIL

- [ ] **Step 3: 實作**

`contracts.py` 追加：

```python
class OpportunitySignal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal: Literal["none", "social_opening"] = "none"
    evidence_span: str = ""
    confidence: float = 0.0
```

`Plan` 追加欄位：

```python
    opportunity: OpportunitySignal | None = None
```

`planner.py`：

```python
class _OpportunityArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    signal: Literal["none", "social_opening"] = "none"
    evidence_span: str = ""
    confidence: float = 0.0


class _DecomposeTasksArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tasks: list[SubTask] = []
    opportunity: _OpportunityArguments | None = None
```

`plan_turn` 回傳處：

```python
        validated = _DecomposeTasksArguments.model_validate(arguments)
        opportunity = None
        if validated.opportunity is not None and validated.opportunity.signal == "social_opening":
            opportunity = OpportunitySignal(
                signal="social_opening",
                evidence_span=validated.opportunity.evidence_span,
                confidence=max(0.0, min(1.0, validated.opportunity.confidence)),
            )
        plan = Plan(tasks=validated.tasks, opportunity=opportunity)
        return plan, metrics
```

`_planner_prompt` 追加規則：

```
10. 若使用者表達想有人陪、想認識人或獨自參加不舒服時，在 opportunity 欄位填 signal="social_opening"、evidence_span（原句連續子字串）、confidence（0.0-1.0，需 ≥0.8）。只有明確表達期待或投入時才標；單純提到旅行、普通寒暄或負面情緒一律填 none。
```

`scheduler.py` 在 `plan is None` 檢查後、`_print_separator("PLAN")` 前處理：

```python
    opportunity = getattr(plan, "opportunity", None)
    if opportunity is not None and opportunity.signal == "social_opening":
        if opportunity.confidence < 0.8 or not opportunity.evidence_span or opportunity.evidence_span not in turn.message:
            opportunity = None
    if opportunity is not None:
        assessment = assess_match_opportunity(ctx.user_profile or {}, ctx.user_id, explicit_search=False)
        if assessment.state == "ready":
            _CONFIRMATIONS.insert_one({
                "_id": uuid.uuid4().hex,
                "user_id": ctx.user_id,
                "agent_name": "match",
                "tool_name": "match.start_search",
                "arguments": {},
                "payload": {"source": "opportunity_guidance", "guidance_fingerprint": assessment.fingerprint},
                "status": "pending",
                "created_at": time.time(),
                "expires_at": time.time() + 900,
            })
            lead = f"你提到「{opportunity.evidence_span}」。" if opportunity.evidence_span in turn.message else ""
            preview = (
                f"{lead}感覺這件事有人一起也不錯。"
                "我可以依你的近況和個性找一位合適人選，不會隨機配；要試試看嗎？"
            )
            synth_slice = slice_for_agent("synthesizer", turn, prior_observations=[{
                "task_id": "guidance", "status": "ok", "tool": None,
                "result": [{"pending_confirmation": True, "tool_name": "match.start_search", "preview": preview}],
                "error_code": None, "skip_reason": None,
            }])
            reply, _card_decision, synth_metrics = synthesizer.synthesize(synth_slice)
            _print_separator("V3 RUN END")
            return AgentResult(handled=True, reply=reply, agent_run_id=run_id, agent_mode="v3",
                               match_readiness_state="ready", match_guidance_shown=True)
        if assessment.state == "not_ready":
            from services.ayue_agent.match_opportunity import missing_basis_question
            reply = "我想先多了解你的方向，才能幫你找得更準。" + missing_basis_question(assessment)
            _print_separator("V3 RUN END")
            return AgentResult(handled=True, reply=reply, agent_run_id=run_id, agent_mode="v3",
                               match_readiness_state="not_ready")
```

import 追加：`from services.ayue_agent.match_opportunity import assess_match_opportunity`。

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_v3_planner.py tests/test_v3_scheduler.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/contracts.py social_demotest/services/ayue_agent/v3/planner.py social_demotest/services/ayue_agent/v3/scheduler.py social_demotest/tests/test_v3_planner.py social_demotest/tests/test_v3_scheduler.py
git commit -m "feat(v3): planner opportunity signal with guidance confirmation"
```

---

### Task 6: V3 assessment session 生命週期

**Files:**
- Modify: `social_demotest/services/ayue_agent/v3/scheduler.py`
- Test: `social_demotest/tests/test_v3_scheduler.py`（追加）

**Interfaces:**
- Consumes: `assessment_session_service` 的 `active_assessment_session`、`advance_assessment_session`、`cancel_assessment_session`、`expire_assessment_session`、`awaiting_assessment_commit`、`commit_assessment_session`、`assessment_commit_choice`、`assessment_cancel_choice`、`assessment_public_state`。
- Produces: `AgentResult.assessment_state/kind/revision`；assessment 訊息不進 planner。

- [ ] **Step 1: 寫失敗測試**

```python
# 追加到 tests/test_v3_scheduler.py
class V3SchedulerAssessmentTests(unittest.TestCase):
    def test_active_assessment_advances_without_planner(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="我喜歡戶外活動")
        with patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.active_assessment_session",
                   return_value={"session_id": "s1", "kind": "big_five", "expires_at": 1e18, "revision": 1}), \
             patch("services.ayue_agent.v3.scheduler.advance_assessment_session",
                   return_value={"status": "active", "session_state": "active", "kind": "big_five",
                                 "revision": 2, "reply": "好的，那假日你通常怎麼安排？"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertEqual(result.assessment_state, "active")
        self.assertEqual(result.assessment_kind, "big_five")
        plan.assert_not_called()

    def test_awaiting_commit_confirm_commits(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="確認")
        with patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler.awaiting_assessment_commit",
                   return_value={"session_id": "s1", "kind": "big_five", "revision": 3, "expires_at": 1e18}), \
             patch("services.ayue_agent.v3.scheduler.assessment_commit_choice", return_value="confirm"), \
             patch("services.ayue_agent.v3.scheduler.commit_assessment_session",
                   return_value={"status": "committed", "session_state": "completed",
                                 "kind": "big_five", "revision": 4, "reply": "已套用新的基本性格資料。"}), \
             patch("services.ayue_agent.v3.scheduler.plan_turn") as plan:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertEqual(result.assessment_state, "completed")
        plan.assert_not_called()
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_v3_scheduler.py -q`
Expected: FAIL

- [ ] **Step 3: 實作**

在 `scheduler.py` 的 `run_public_agent_turn_v3` 中，`confirmation_choice` 處理之後、Planner 之前插入：

```python
    from services.assessment_session_service import (
        active_assessment_session, advance_assessment_session,
        assessment_cancel_choice, assessment_commit_choice,
        assessment_public_state, awaiting_assessment_commit,
        cancel_assessment_session, commit_assessment_session,
        expire_assessment_session,
    )

    def _assessment_result(outcome: dict, session: dict, run_id: str) -> AgentResult:
        state = str(outcome.get("session_state") or outcome.get("status") or "active")
        return AgentResult(
            handled=True, reply=str(outcome.get("reply") or "你可以換個方式說說看？"),
            conversation_intent="assessment", agent_run_id=run_id, agent_mode="v3",
            profile_write_allowed=False, profile_write_reason="assessment",
            assessment_state=state,
            assessment_kind=str(outcome.get("kind") or session.get("kind") or "") or None,
            assessment_revision=outcome.get("revision", int(session.get("revision", 0) or 0)),
        )

    commit_session = awaiting_assessment_commit(ctx.user_profile)
    if commit_session:
        session_id = str(commit_session.get("session_id") or "")
        kind = str(commit_session.get("kind") or "")
        revision = int(commit_session.get("revision", 0) or 0)
        expires_at = float(commit_session.get("expires_at", 0) or 0)
        if expires_at and expires_at <= time.time():
            outcome = expire_assessment_session(ctx.user_id, session_id, kind)
            _print_separator("V3 RUN END")
            return _assessment_result(outcome, commit_session, run_id)
        choice = assessment_commit_choice(ctx.message)
        if choice == "none":
            _print_separator("V3 RUN END")
            return AgentResult(
                handled=True,
                reply="這份探索結果已整理好。回覆「確認」才會套用；想保留原本資料可以回覆「取消」。",
                conversation_intent="assessment", agent_run_id=run_id, agent_mode="v3",
                profile_write_allowed=False, profile_write_reason="assessment",
                assessment_state="awaiting_commit", assessment_kind=kind, assessment_revision=revision,
            )
        if choice == "cancel":
            outcome = cancel_assessment_session(ctx.user_id, session_id, kind)
            _print_separator("V3 RUN END")
            return _assessment_result(outcome, commit_session, run_id)
        outcome = commit_assessment_session(
            ctx.user_id, session_id, expected_revision=revision,
            idempotency_key=f"assessment-commit:{session_id}:{revision}",
        )
        _print_separator("V3 RUN END")
        return _assessment_result(outcome, commit_session, run_id)

    active = active_assessment_session(ctx.user_profile)
    if active:
        session_id = str(active.get("session_id") or "")
        kind = str(active.get("kind") or "")
        expires_at = float(active.get("expires_at", 0) or 0)
        if expires_at and expires_at <= time.time():
            outcome = expire_assessment_session(ctx.user_id, session_id, kind)
            _print_separator("V3 RUN END")
            return _assessment_result(outcome, active, run_id)
        if assessment_cancel_choice(ctx.message):
            outcome = cancel_assessment_session(ctx.user_id, session_id, kind)
            _print_separator("V3 RUN END")
            return _assessment_result(outcome, active, run_id)
        outcome = advance_assessment_session(
            ctx.user_id, session_id, ctx.message, message_id=ctx.message_id,
        )
        _print_separator("V3 RUN END")
        return _assessment_result(outcome, active, run_id)
```

注意：`confirmation_choice` 的「確認」處理必須在 assessment commit 之前？V2 順序是 pending confirmation → assessment commit → active assessment。但 assessment 的「確認」是 commit 協議，與一般 confirmation 不同。V3 目前先處理 `confirmation_choice`。為避免衝突：assessment commit 檢查放在 `confirmation_choice` 之前（因為 awaiting_commit 的「確認」必須先被 assessment 吃掉）。但一般 pending confirmation 的「確認」也要處理。順序：awaiting_commit → active assessment → confirmation_choice → planner。這樣 assessment 的確認不會被一般 confirmation 吃掉（因為 assessment 檢查在前）。

調整：把 assessment 區塊放在 `choice = confirmation_choice(ctx.message)` 之前。

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_v3_scheduler.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/scheduler.py social_demotest/tests/test_v3_scheduler.py
git commit -m "feat(v3): assessment session lifecycle in scheduler"
```

---

### Task 7: sources、llm_call_metrics、result metadata

**Files:**
- Modify: `social_demotest/services/ayue_agent/v3/scheduler.py`
- Test: `social_demotest/tests/test_v3_scheduler.py`（追加）

**Interfaces:**
- Produces: `AgentResult.sources`（web/places 安全引用）、`AgentResult.llm_call_metrics`（所有 agent 的 token/duration）、`AgentResult.place_cards`（已存在）。

- [ ] **Step 1: 寫失敗測試**

```python
# 追加到 tests/test_v3_scheduler.py
class V3SchedulerMetadataTests(unittest.TestCase):
    def test_sources_and_llm_metrics_populated(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="查一下附近餐廳")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="找餐廳"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(
                     [ToolProposal(tool_name="places.search_nearby",
                                   arguments={"anchor": "中壢", "categories": ["restaurant"]})], _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool",
                   return_value=MagicMock(ok=True, data={
                       "places": [{"name": "店A", "map_url": "https://www.openstreetmap.org/?mlat=25&mlon=121#map=18/25/121"}],
                   }, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("找到店A", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.sources)
        self.assertEqual(result.sources[0]["title"], "店A")
        self.assertTrue(result.llm_call_metrics)
        self.assertIn("input_tokens", result.llm_call_metrics[0])
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_v3_scheduler.py -q`
Expected: FAIL

- [ ] **Step 3: 實作**

`scheduler.py` 追加 `_public_sources`（移植 V2 邏輯，從 `SubTaskResult` 收集）：

```python
def _public_sources(task_results: dict[str, list[SubTaskResult]]) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for results in task_results.values():
        for r in results:
            if r.status is not SubTaskStatus.OK or not r.observation:
                continue
            tool_name = r.tool_name or ""
            data = r.observation or {}
            if tool_name == "web.search":
                candidates = data.get("results") or []
            elif tool_name == "web.extract":
                candidates = data.get("pages") or []
            elif tool_name == "places.search_nearby":
                candidates = [
                    {"title": str(item.get("name") or "地圖"), "url": str(item.get("map_url") or "")}
                    for item in (data.get("places") or [])
                ]
            elif tool_name == "places.resolve_place":
                place = data.get("place") or {}
                candidates = [{"title": str(place.get("name") or "地圖"), "url": str(place.get("map_url") or "")}]
            elif tool_name == "places.measure_distance":
                candidates = [{
                    "title": str(data.get("attribution") or "OpenStreetMap"),
                    "url": str(data.get("attribution_url") or ""),
                }]
            else:
                continue
            for item in candidates:
                url = str((item or {}).get("url") or "")
                if not is_safe_public_url(url) or url in seen:
                    continue
                seen.add(url)
                title = re.sub(r"\s+", " ", str((item or {}).get("title") or "")).strip()[:140]
                sources.append({"title": title or url, "url": url})
                if len(sources) == 5:
                    return sources
    return sources
```

`run_public_agent_turn_v3` return 前：

```python
    result.sources = _public_sources(task_results)
    result.llm_call_metrics = [
        {
            "agent": agent_id,
            "input_tokens": m.input_tokens,
            "output_tokens": m.output_tokens,
            "duration_ms": m.duration_ms,
        }
        for agent_id, m in all_agent_metrics
    ]
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_v3_scheduler.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/scheduler.py social_demotest/tests/test_v3_scheduler.py
git commit -m "feat(v3): populate sources and llm_call_metrics on AgentResult"
```

---

### Task 8: measure_distance reuse + web.extract URL binding

**Files:**
- Modify: `social_demotest/services/ayue_agent/v3/scheduler.py`
- Test: `social_demotest/tests/test_v3_scheduler.py`（追加）

- [ ] **Step 1: 寫失敗測試**

```python
# 追加到 tests/test_v3_scheduler.py
class V3SchedulerReuseTests(unittest.TestCase):
    def test_duplicate_distance_within_task_reuses_observation(self):
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="中壢到台北多遠")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="places", depends_on=[], task_brief="量距離"),
            SubTask(id="t2", agent="synthesizer", depends_on=["t1"], task_brief="彙整"),
        ])
        executed: list[str] = []
        def fake_exec(call, ctx, clock=None):
            executed.append(call.name)
            return MagicMock(ok=True, data={
                "origin_label": "中壢", "destination_label": "台北",
                "origin_kind": "explicit", "distance_m": 40000,
                "distance_basis": "straight_line", "attribution": "OSM", "attribution_url": "https://x",
            }, error_code=None)
        multi = [
            ToolProposal(tool_name="places.measure_distance",
                         arguments={"origin": "中壢", "destination": "台北"}),
            ToolProposal(tool_name="places.measure_distance",
                         arguments={"origin": "中壢市", "destination": "台北市"}),
        ]
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=(plan, _planner_metrics())), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.scheduler._SUB_AGENT_RUNNERS", {
                 "places": MagicMock(return_value=(multi, _sub_metrics())),
             }), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=fake_exec), \
             patch("services.ayue_agent.v3.synthesizer.synthesize",
                   return_value=("約 40 公里", None, _synth_metrics())):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            run_public_agent_turn_v3(ctx, mode="on")
        self.assertEqual(len(executed), 1, "paraphrased distance call must reuse the first observation")
```

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_v3_scheduler.py -q`
Expected: FAIL（執行 2 次）

- [ ] **Step 3: 實作**

在 `_run_sub_task` 的 guard 通過後、執行前，加入 reuse 檢查（task 範圍）：

```python
        spec = TOOL_REGISTRY[proposal.tool_name]
        if getattr(spec, "reuse_success_within_turn", False):
            reused = _has_reusable_success(spec, proposal.arguments, results)
            if reused:
                print(f"  [{task.id}#{index}] result=OK (reused prior observation)")
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                              tool_name=proposal.tool_name,
                                              observation={"reused": True}))
                continue
```

`_has_reusable_success`（移植 V2，task 範圍）：

```python
def _same_public_place_reference(requested: Any, resolved: Any) -> bool:
    def compact(value: Any) -> str:
        return re.sub(r"[\s\W_]+", "", str(value or "").lower(), flags=re.UNICODE)
    requested_key = compact(requested)
    resolved_key = compact(resolved)
    return bool(
        len(requested_key) >= 2 and len(resolved_key) >= 2
        and (requested_key in resolved_key or resolved_key in requested_key)
    )


def _has_reusable_success(spec: Any, arguments: dict[str, Any], results: list[SubTaskResult]) -> bool:
    if not getattr(spec, "reuse_success_within_turn", False):
        return False
    for r in results:
        if r.status is not SubTaskStatus.OK or r.tool_name != spec.name or not r.observation:
            continue
        result = r.observation
        if spec.name == "places.measure_distance":
            if str(result.get("origin_kind") or "") == "saved_profile":
                origin_matches = bool(arguments.get("use_saved_origin"))
            else:
                origin_matches = not bool(arguments.get("use_saved_origin")) and _same_public_place_reference(
                    arguments.get("origin"), result.get("origin_label"),
                )
            if origin_matches and _same_public_place_reference(
                arguments.get("destination"), result.get("destination_label"),
            ):
                return True
    return False
```

web.extract URL binding：在 `_run_sub_task` 執行前檢查：

```python
        if proposal.tool_name == "web.extract":
            urls = [str(u) for u in (proposal.arguments.get("urls") or [])]
            if not _web_extract_urls_allowed(turn_ctx, results, urls):
                print(f"  [{task.id}#{index}] result=FAILED  error_code=web_extract_url_not_bound")
                results.append(SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                                              tool_name=proposal.tool_name,
                                              error_code="web_extract_url_not_bound"))
                continue
```

```python
def _web_extract_urls_allowed(turn_ctx: Any, results: list[SubTaskResult], urls: list[str]) -> bool:
    allowed: set[str] = set()
    for r in results:
        if r.status is not SubTaskStatus.OK or r.tool_name != "web.search" or not r.observation:
            continue
        for item in ((r.observation or {}).get("results") or []):
            url = str((item or {}).get("url") or "")
            if is_safe_public_url(url):
                allowed.add(url)
    for raw in re.findall(r"https?://[^\s<>\]\[\"']+", str(getattr(turn_ctx, "message", "") or "")):
        url = raw.rstrip(".,，。!?！？:：;；)")
        if is_safe_public_url(url):
            allowed.add(url)
    return bool(urls) and all(str(url) in allowed for url in urls)
```

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_v3_scheduler.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/scheduler.py social_demotest/tests/test_v3_scheduler.py
git commit -m "feat(v3): reuse distance observation and bind web.extract URLs within task"
```

---

### Task 9: Router 與前端改為 V3-only

**Files:**
- Modify: `social_demotest/routers/public_chat.py`
- Modify: `social_demotest/routers/system.py`
- Modify: `social_demotest/frontend.html`
- Modify: `social_demotest/main.py`
- Test: `social_demotest/tests/test_ayue_agent_stream.py`（更新）

**Interfaces:**
- Produces: `agent_version: "v3"`；`AYUE_AGENT_V2_MODE` 不再讀取；`agent_mode_for_user` 移除。

- [ ] **Step 1: 更新測試**

`test_ayue_agent_stream.py` 中 `patch("routers.public_chat.agent_mode_for_user", ...)` 改為 `patch("routers.public_chat.agent_mode_for_user_v3", ...)`；`test_json_direct_chat_off_mode_executes_legacy_rollback_outcome_path` 改為驗證 V3 直接執行（移除 legacy 分支測試）。

- [ ] **Step 2: 執行確認失敗**

Run: `python -m pytest tests/test_ayue_agent_stream.py -q`
Expected: FAIL

- [ ] **Step 3: 實作**

`public_chat.py`：
- 移除 `from services.ayue_agent.runtime import agent_mode_for_user`，改為只 import `agent_mode_for_user_v3`。
- `_complete_public_v2_turn` 改名為 `_complete_public_turn`，移除 V2 分支，直接呼叫 `run_public_agent_turn_v3`。
- `_run_public_v2_stream_turn` 改名為 `_run_public_stream_turn`。
- `direct_chat` 中移除 `v2_mode`、`public_agent_mode` 的 V2 判斷與 legacy_match_routing 分支；`contact_id == "ai_assistant"` 一律走 V3。
- `agent_version` 回傳 `"v3"`。

`system.py`：
- `agent_version` 改為 `"v3" if agent_mode_for_user_v3(user_id) == "on" else "legacy"`（或直接 `"v3"`，視 allowlist 語意）。

`frontend.html:4705`：
```javascript
setText("demo-agent-version", data.agent_version === "v3" ? "V3 已啟用" : "Legacy rollback");
```

`main.py`：
- `from services.ayue_agent.runtime import ensure_indexes` 改為 `from services.ayue_agent.v3.scheduler import ensure_indexes`（Task 4 需在 scheduler 提供 `ensure_indexes`：建立 `agent_runs` 的 TTL index）。

- [ ] **Step 4: 執行測試確認通過**

Run: `python -m pytest tests/test_ayue_agent_stream.py tests/test_demo_tools.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add social_demotest/routers/public_chat.py social_demotest/routers/system.py social_demotest/frontend.html social_demotest/main.py social_demotest/tests/test_ayue_agent_stream.py
git commit -m "feat: public Ayue runs V3 only; agent_version v3"
```

---

### Task 10: 移除 V2 程式碼與測試

**Files:**
- Delete: `social_demotest/services/ayue_agent/runtime.py`
- Delete: `social_demotest/services/ayue_agent/legacy_match_routing.py`
- Delete: `social_demotest/services/ayue_agent/match_opportunity.py`（若 V3 已改用其函式則保留——見下方注意）
- Modify: `social_demotest/services/ayue_agent/router.py`（移除 V2 專屬：`plan_turn_v2_function_calling`、`guard_v2_decision`、`generate_final_reply_v2`、`generate_clarification_reply_v2`、`planner_final_reply_v2`、`observation_fallback_v2`、`_conversational_fallback`、`_clarification_fallback_v2`、`_fc_planner_prompt`、`_build_ollama_tools`、`_parse_meta_line`、`_infer_intent_from_tool`、`tool_policy_for_turn`；保留 V3 共用：`confirmation_choice`、`_concise_public_reply`、`_INTERNAL_META_REPLY_RE`）
- Modify: `social_demotest/services/ayue_agent/__init__.py`（移除 `run_public_agent_turn`）
- Delete V2 專屬測試：`test_ayue_agent_router.py`、`test_ayue_agent_trajectories.py`、`test_ayue_agent_v2_policy.py`、`test_ayue_agent_v2_tools.py`、`test_ayue_agent_planner_sequences.py`、`test_ayue_agent_calendar_actions.py`、`test_ayue_agent_place_cards.py`、`test_ayue_agent_web_tools.py`、`test_ayue_agent_maps_tools.py`、`test_ayue_agent_mentions.py`、`test_ayue_agent_registry.py`、`test_ayue_match_opportunity.py`、`test_ayue_phase4_cleanup.py`、`test_ayue_refactor_catalog.py`、`test_match_action_service.py`（若只測 `_decide_active_proposal` 則改測 write_executors）
- Test: 全量跑 `python -m pytest tests/ -q`

**注意：** `match_opportunity.py` 是 domain helper（V3 已使用 `assess_match_opportunity`），**保留**。`legacy_match_routing.py` 是 rollback-only，刪除。`runtime.py` 刪除前確認 `test_match_action_service.py` 的 `_decide_active_proposal` import 改為 `v3.write_executors`。

- [ ] **Step 1: 更新 test_match_action_service.py 的 import**

`from services.ayue_agent.runtime import _decide_active_proposal` → `from services.ayue_agent.v3.write_executors import _decide_active_proposal`（需在 write_executors 中保留同名函式）。

- [ ] **Step 2: 刪除檔案並更新 router.py / __init__.py**

- [ ] **Step 3: 全量測試**

Run: `python -m pytest tests/ -q`
Expected: 全數 PASS（V2 專屬測試已刪除）

- [ ] **Step 4: compile 檢查**

Run: `python -m compileall services routers main.py`
Expected: 無錯誤

- [ ] **Step 5: Commit**

```bash
git add -A social_demotest
git commit -m "refactor: remove V2 public runtime and its tests"
```

---

### Task 11: 文件改寫

**Files:**
- Rewrite: `AYUE_V2_ARCHITECTURE.md` → 改為 V3 架構文件（保留檔名或改名 `AYUE_V3_ARCHITECTURE.md` 並更新引用）
- Modify: `AGENTS.md`、`CLAUDE.md`、`README.md`、`MEMORY_CONTEXT_ENGINE_GUIDE.md`、`social_demotest/matchmaking_logic.md`、`social_demotest/PHASE0_AYUE_AUDIT.md` 中 V2 引用
- Modify: `social_demotest/AGENTS.md`、`social_demotest/CLAUDE.md`（gitnexus 區塊不動）

- [ ] **Step 1: 改寫架構文件**

以 V3 為主角：Scheduler/Planner/Guard/Sub-agents/Synthesizer 流程、confirmation 生命週期、write executors、opportunity、assessment、trace、flags（`AYUE_AGENT_V3_MODE`、`AYUE_SUBAGENT_*`）、API contract（`agent_version: "v3"`）、測試基線。

- [ ] **Step 2: 更新引用**

`AGENTS.md` 的「公開阿月已採 V2 單一 agent loop」改為 V3 sub-agent 架構；`AYUE_V2_ARCHITECTURE.md` 引用改為新檔名。

- [ ] **Step 3: Commit**

```bash
git add AYUE_V2_ARCHITECTURE.md AGENTS.md CLAUDE.md README.md MEMORY_CONTEXT_ENGINE_GUIDE.md social_demotest/matchmaking_logic.md social_demotest/PHASE0_AYUE_AUDIT.md
git commit -m "docs: rewrite architecture docs for V3 sub-agent runtime"
```

---

## Self-Review

**Spec coverage:**
- 寫入執行（Task 1-3）✓
- Match opportunity（Task 2 preflight + Task 5 planner signal）✓
- Assessment session（Task 6）✓
- Trace（Task 4）✓
- sources/metrics/metadata（Task 7）✓
- reuse + web.extract binding（Task 8）✓
- V3-only router/frontend（Task 9）✓
- 移除 V2（Task 10）✓
- 文件（Task 11）✓

**Placeholder scan:** 無 TBD/TODO；每步含完整程式碼。

**Type consistency:** `execute_write` 簽名在 Task 1 定義、Task 3 使用一致；`prepare_write_confirmation` 回傳 `(payload, reply)` 在 Task 2/3 一致；`ConfirmationManager.create_confirmation` 的 `payload` 參數在 Task 2 定義、Task 3 使用一致；`_persist_trace(run_id, ctx, payload)` 在 Task 4 定義與測試一致；`OpportunitySignal` 在 Task 5 定義與使用一致。
