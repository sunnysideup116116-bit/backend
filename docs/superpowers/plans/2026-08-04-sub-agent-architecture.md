# 公開阿月 V3 Sub-agent 架構實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Planner + Scheduler + 6 個 sub-agent + 中央 Guard + synthesizer 取代 V2 單一 agent loop，降低複雜有順序任務的失誤率。

**Architecture:** Scheduler（純程式碼）管理入口、confirmation、執行順序；Planner（LLM）只拆任務產靜態 DAG；6 個 sub-agent（LLM + function calling）各自在自己 domain 內產出 tool call proposal；中央 Guard（純程式碼）驗 proposal；synthesizer（LLM）綜合所有 observation 產出回覆。sub-agent 是「分工的 Planner」不是「分工的 executor」。

**Tech Stack:** Python 3.11+、Pydantic v2、FastAPI、MongoDB、Ollama function calling、unittest（現有測試框架）

## Global Constraints

- 維持 `AGENTS.md` 全部規則：唯一 orchestrator（Scheduler 取代 V2 runtime，不造第二套）、LLM 做語意程式做安全、Tool Registry 單一入口、副作用走 Domain Service、所有寫入先確認、Context 只能由 context.py 建立、Profile Pipeline 分離、配對狀態真相、API/UI 相容性、Trace allowlist
- `/api/direct_chat` JSON 與 `/api/direct_chat/stream` NDJSON contract 不變；只可增加 optional fields
- NDJSON public event 只允許：`run_started`、`tool_started`、`tool_finished`、`final`、`error`
- Trace 只保存 allowlisted metadata；禁止保存完整 prompt、args、observation、ID、revision
- sub-agent 不可填 `user_id`、`match_id`、`event_id`、`revision`、`expected_status`；由 executor 注入
- Test harness 必須使用 stub config 或 local test database；自動測試不得連線或修改正式 MongoDB Atlas／Neo4j
- Python source 必須 compile；主服務與 matchmaker 必須能啟動
- `AYUE_AGENT_V2_MODE` 保留供緊急 rollback；V3 用 `AYUE_AGENT_V3_MODE=on|off`
- 每個 sub-agent 各自最多 3 次唯讀；整回合不再有全局唯讀上限
- 寫入無數量上限，但每個寫入 proposal 都必須各自建立獨立 confirmation
- 靜態規劃，不 re-plan；失敗 sub-agent skip，synthesizer 處理缺口

---

## 檔案結構

```
social_demotest/services/ayue_agent/
├── runtime.py              # 保留 V2 loop（緊急 rollback）
├── v3/
│   ├── __init__.py          # 匯出 run_public_agent_turn_v3
│   ├── contracts.py         # SubTask / Plan / SubTaskResult / AgentContextSlice 等 typed contract
│   ├── scheduler.py         # Scheduler / Orchestrator（純程式碼）
│   ├── planner.py           # 輕量 Planner（LLM，產 DAG）
│   ├── guard.py             # 中央 Guard（純程式碼）
│   ├── synthesizer.py       # 最終綜合（LLM）
│   ├── context_slicer.py    # 從完整 context 切各 agent slice（純程式碼）
│   ├── sub_agents/
│   │   ├── __init__.py
│   │   ├── base.py          # 共用 sub-agent 呼叫邏輯（LLM + function calling + proposal 產出）
│   │   ├── calendar_agent.py
│   │   ├── places_agent.py
│   │   ├── match_agent.py
│   │   ├── relationship_agent.py
│   │   └── profile_agent.py
│   └── confirmation.py      # 多筆獨立 confirmation 管理（純程式碼）
├── context.py               # 保留（唯一 Context Builder）
├── tool_registry.py         # 保留（單一 Tool Registry）
└── tools.py                  # 保留（tool executor）
```

**新測試檔案：**
```
social_demotest/tests/
├── test_v3_contracts.py
├── test_v3_planner.py
├── test_v3_guard.py
├── test_v3_scheduler.py
├── test_v3_context_slicer.py
├── test_v3_confirmation.py
├── test_v3_sub_agents.py
├── test_v3_synthesizer.py
└── test_v3_trajectories.py
```

**修改既有檔案：**
- `social_demotest/services/ayue_agent/__init__.py`：新增 `run_public_agent_turn_v3` 匯出
- `social_demotest/routers/public_chat.py`：`direct_chat` 與 `direct_chat_stream` 依 `AYUE_AGENT_V3_MODE` 選擇 V3 或 V2 runtime
- `social_demotest/.env.example`：新增 V3 flag 範例
- `AYUE_V2_ARCHITECTURE.md`：附錄標註 V3 sub-agent 架構與 flag 關係

---

## Task 1: V3 typed contracts

**Files:**
- Create: `social_demotest/services/ayue_agent/v3/__init__.py`
- Create: `social_demotest/services/ayue_agent/v3/contracts.py`
- Test: `social_demotest/tests/test_v3_contracts.py`

**Interfaces:**
- Produces: `SubTask`, `Plan`, `SubTaskResult`, `SubTaskStatus`, `AgentContextSlice`, `ToolProposal`, `GuardDecision`, `GuardResultCode`

- [ ] **Step 1: Write the failing test**

```python
# social_demotest/tests/test_v3_contracts.py
import unittest
from pydantic import ValidationError

from services.ayue_agent.v3.contracts import (
    SubTask, Plan, SubTaskResult, SubTaskStatus, AgentContextSlice,
    ToolProposal, GuardDecision, GuardResultCode,
)


class V3ContractsTests(unittest.TestCase):
    def test_subtask_requires_id_agent_depends_on_task_brief(self):
        with self.assertRaises(ValidationError):
            SubTask(id="", agent="calendar", depends_on=[], task_brief="x")
        with self.assertRaises(ValidationError):
            SubTask(id="t1", agent="", depends_on=[], task_brief="x")
        with self.assertRaises(ValidationError):
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="")
        t = SubTask(id="t1", agent="calendar", depends_on=[], task_brief="check calendar")
        self.assertEqual(t.id, "t1")
        self.assertEqual(t.agent, "calendar")

    def test_subtask_rejects_unknown_agent(self):
        with self.assertRaises(ValidationError):
            SubTask(id="t1", agent="unknown_agent", depends_on=[], task_brief="x")

    def test_plan_requires_at_least_one_task(self):
        with self.assertRaises(ValidationError):
            Plan(tasks=[])

    def test_plan_synthesizer_must_be_terminal(self):
        # synthesizer task must not appear in any other task's depends_on list
        # because it is the terminal; this is enforced by validation
        t1 = SubTask(id="t1", agent="calendar", depends_on=[], task_brief="x")
        syn = SubTask(id="syn", agent="synthesizer", depends_on=["t1"], task_brief="綜合")
        plan = Plan(tasks=[t1, syn])
        self.assertEqual(len(plan.tasks), 2)

    def test_plan_rejects_dangling_dependency(self):
        t1 = SubTask(id="t1", agent="calendar", depends_on=["t0"], task_brief="x")
        with self.assertRaises(ValidationError):
            Plan(tasks=[t1])

    def test_subtask_result_status_enum(self):
        self.assertIn(SubTaskStatus.OK, SubTaskStatus)
        self.assertIn(SubTaskStatus.FAILED, SubTaskStatus)
        self.assertIn(SubTaskStatus.SKIPPED, SubTaskStatus)

    def test_agent_context_slice_holds_agent_name_and_payload(self):
        s = AgentContextSlice(agent="calendar", payload={"events": []})
        self.assertEqual(s.agent, "calendar")
        self.assertEqual(s.payload, {"events": []})

    def test_tool_proposal_holds_tool_name_and_arguments(self):
        p = ToolProposal(tool_name="calendar.list_my_events", arguments={})
        self.assertEqual(p.tool_name, "calendar.list_my_events")
        self.assertEqual(p.arguments, {})

    def test_tool_proposal_rejects_forbidden_id_fields(self):
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.cancel_my_event", arguments={"user_id": "x"})
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="match.decide_active_proposal", arguments={"match_id": "x"})
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.cancel_my_event", arguments={"revision": 1})
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.cancel_my_event", arguments={"expected_status": "draft"})

    def test_guard_decision_pass_or_fail_with_code(self):
        ok = GuardDecision(ok=True, code=GuardResultCode.PASSED)
        self.assertTrue(ok.ok)
        self.assertEqual(ok.code, GuardResultCode.PASSED)
        bad = GuardDecision(ok=False, code=GuardResultCode.SCHEMA_INVALID)
        self.assertFalse(bad.ok)
        self.assertEqual(bad.code, GuardResultCode.SCHEMA_INVALID)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest social_demotest/tests/test_v3_contracts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.ayue_agent.v3'`

- [ ] **Step 3: Write minimal implementation**

```python
# social_demotest/services/ayue_agent/v3/__init__.py
"""V3 sub-agent runtime for public Ayue."""

__all__: list[str] = []
```

```python
# social_demotest/services/ayue_agent/v3/contracts.py
"""Typed contracts for the V3 sub-agent runtime."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


# Forbidden ID/revision fields a sub-agent must never fill.
FORBIDDEN_ARG_FIELDS = frozenset({"user_id", "match_id", "event_id", "revision", "expected_status"})

VALID_AGENTS = frozenset({
    "calendar", "places", "match", "relationship", "profile", "synthesizer",
})


class SubTaskStatus(str, Enum):
    OK = "ok"
    FAILED = "failed"
    SKIPPED = "skipped"


class GuardResultCode(str, Enum):
    PASSED = "passed"
    SCHEMA_INVALID = "schema_invalid"
    FORBIDDEN_ARG_FIELD = "forbidden_arg_field"
    DUPLICATE_CALL = "duplicate_call"
    STEP_LIMIT_EXCEEDED = "step_limit_exceeded"
    WRITE_REQUIRES_CONFIRMATION = "write_requires_confirmation"
    TOOL_NOT_REGISTERED = "tool_not_registered"


class SubTask(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    agent: Literal["calendar", "places", "match", "relationship", "profile", "synthesizer"]
    depends_on: list[str] = Field(default_factory=list)
    task_brief: str = Field(min_length=1)


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tasks: list[SubTask] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_dag(self) -> "Plan":
        ids = {t.id for t in self.tasks}
        if len(ids) != len(self.tasks):
            raise ValueError("duplicate SubTask id")
        for t in self.tasks:
            for dep in t.depends_on:
                if dep not in ids:
                    raise ValueError(f"SubTask {t.id} depends on unknown {dep}")
        # synthesizer must be terminal: it must not appear in any other task's depends_on
        for t in self.tasks:
            if t.agent != "synthesizer":
                continue
            for other in self.tasks:
                if other.id == t.id:
                    continue
                if t.id in other.depends_on:
                    raise ValueError(f"synthesizer {t.id} must be terminal; referenced by {other.id}")
        return self


class AgentContextSlice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    agent: Literal["calendar", "places", "match", "relationship", "profile", "synthesizer"]
    payload: dict[str, Any] = Field(default_factory=dict)


class ToolProposal(BaseModel):
    """A sub-agent's tool-call proposal. Execution stays with the Scheduler/Guard."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1)
    arguments: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _reject_forbidden_fields(self) -> "ToolProposal":
        present = FORBIDDEN_ARG_FIELDS & set(self.arguments.keys())
        if present:
            raise ValueError(f"forbidden arg fields: {sorted(present)}")
        return self


class GuardDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    code: GuardResultCode
    reason: str = ""


class SubTaskResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: SubTaskStatus
    tool_name: str | None = None
    observation: dict[str, Any] | None = None
    error_code: str | None = None
    skip_reason: str | None = None
    guard_code: GuardResultCode | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest social_demotest/tests/test_v3_contracts.py -v`
Expected: PASS (all 9 tests)

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/__init__.py social_demotest/services/ayue_agent/v3/contracts.py social_demotest/tests/test_v3_contracts.py
git commit -m "feat(v3): add typed contracts for sub-agent runtime"
```

---

## Task 2: 中央 Guard（純程式碼）

**Files:**
- Create: `social_demotest/services/ayue_agent/v3/guard.py`
- Test: `social_demotest/tests/test_v3_guard.py`

**Interfaces:**
- Consumes: `ToolProposal`, `GuardDecision`, `GuardResultCode` from Task 1; `TOOL_REGISTRY`, `ToolRisk`, `planner_arguments_allowed`, `tool_call_key` from existing `tool_registry.py`
- Produces: `guard_proposal(proposal, agent_name, seen_keys, step_count, max_reads) -> GuardDecision`

- [ ] **Step 1: Write the failing test**

```python
# social_demotest/tests/test_v3_guard.py
import unittest

from services.ayue_agent.v3.contracts import ToolProposal, GuardResultCode
from services.ayue_agent.v3.guard import guard_proposal


class V3GuardTests(unittest.TestCase):
    def test_passes_valid_read_tool(self):
        p = ToolProposal(tool_name="calendar.list_my_events", arguments={})
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=0, max_reads=3)
        self.assertTrue(d.ok)
        self.assertEqual(d.code, GuardResultCode.PASSED)

    def test_rejects_unknown_tool(self):
        p = ToolProposal(tool_name="nope.bad", arguments={})
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=0, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.TOOL_NOT_REGISTERED)

    def test_rejects_forbidden_arg_field(self):
        # ToolProposal's own validator should reject, but guard is a backstop.
        from pydantic import ValidationError
        with self.assertRaises(ValidationError):
            ToolProposal(tool_name="calendar.cancel_my_event", arguments={"revision": 1})

    def test_rejects_schema_invalid_args(self):
        p = ToolProposal(
            tool_name="calendar.create_my_event",
            arguments={"title": "晚餐"},  # 缺 date, start_time, end_time
        )
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=0, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.SCHEMA_INVALID)

    def test_rejects_duplicate_call(self):
        from services.ayue_agent.tool_registry import TOOL_REGISTRY, tool_call_key
        spec = TOOL_REGISTRY["calendar.list_my_events"]
        key = tool_call_key(spec, {})
        p = ToolProposal(tool_name="calendar.list_my_events", arguments={})
        d = guard_proposal(p, agent_name="calendar", seen_keys={key}, step_count=0, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.DUPLICATE_CALL)

    def test_rejects_step_limit_exceeded(self):
        p = ToolProposal(tool_name="calendar.list_my_events", arguments={})
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=3, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.STEP_LIMIT_EXCEEDED)

    def test_write_tool_requires_confirmation(self):
        p = ToolProposal(
            tool_name="calendar.create_my_event",
            arguments={
                "title": "看電影",
                "date": "2026-08-09",
                "start_time": "20:00",
                "end_time": "22:00",
            },
        )
        d = guard_proposal(p, agent_name="calendar", seen_keys=set(), step_count=0, max_reads=3)
        self.assertFalse(d.ok)
        self.assertEqual(d.code, GuardResultCode.WRITE_REQUIRES_CONFIRMATION)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest social_demotest/tests/test_v3_guard.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.ayue_agent.v3.guard'`

- [ ] **Step 3: Write minimal implementation**

```python
# social_demotest/services/ayue_agent/v3/guard.py
"""Central Guard for V3: pure deterministic checks, zero LLM."""

from __future__ import annotations

from services.ayue_agent.tool_registry import (
    TOOL_REGISTRY,
    ToolRisk,
    get_tool_spec,
    planner_arguments_allowed,
    tool_call_key,
)
from .contracts import GuardDecision, GuardResultCode, ToolProposal


def guard_proposal(
    proposal: ToolProposal,
    *,
    agent_name: str,
    seen_keys: set[tuple[str, str]],
    step_count: int,
    max_reads: int,
) -> GuardDecision:
    """Validate a sub-agent's tool-call proposal before execution.

    Pure deterministic checks; never calls LLM.
    """
    spec = get_tool_spec(proposal.tool_name)
    if spec is None:
        return GuardDecision(ok=False, code=GuardResultCode.TOOL_NOT_REGISTERED,
                             reason=f"tool {proposal.tool_name} not registered")

    # Schema validation: planner args must match the tool's planner_arguments_model.
    if not planner_arguments_allowed(spec, proposal.arguments):
        return GuardDecision(ok=False, code=GuardResultCode.SCHEMA_INVALID,
                             reason="arguments do not match planner schema")

    # Duplicate check: same tool+args already run by this agent.
    key = tool_call_key(spec, proposal.arguments)
    if key in seen_keys:
        return GuardDecision(ok=False, code=GuardResultCode.DUPLICATE_CALL,
                             reason="duplicate tool+args already run")

    # Step limit: per-agent read cap.
    if spec.risk is ToolRisk.READ and step_count >= max_reads:
        return GuardDecision(ok=False, code=GuardResultCode.STEP_LIMIT_EXCEEDED,
                             reason=f"agent {agent_name} exceeded {max_reads} reads")

    # Write tools never execute directly; Scheduler must build confirmation.
    if spec.risk is ToolRisk.WRITE:
        return GuardDecision(ok=False, code=GuardResultCode.WRITE_REQUIRES_CONFIRMATION,
                             reason=f"write tool {proposal.tool_name} requires confirmation")

    return GuardDecision(ok=True, code=GuardResultCode.PASSED, reason="")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest social_demotest/tests/test_v3_guard.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/guard.py social_demotest/tests/test_v3_guard.py
git commit -m "feat(v3): add central Guard with deterministic proposal validation"
```

---

## Task 3: Context Slicer（純程式碼）

**Files:**
- Create: `social_demotest/services/ayue_agent/v3/context_slicer.py`
- Test: `social_demotest/tests/test_v3_context_slicer.py`

**Interfaces:**
- Consumes: `AgentTurnContextV2` from existing `contracts.py`; `AgentContextSlice` from Task 1
- Produces: `slice_for_agent(agent_name, turn_ctx, prior_observations) -> AgentContextSlice`

- [ ] **Step 1: Write the failing test**

```python
# social_demotest/tests/test_v3_context_slicer.py
import unittest

from services.ayue_agent.contracts import AgentTurnContextV2, TurnClockV1
from services.ayue_agent.v3.context_slicer import slice_for_agent


def _clock():
    return TurnClockV1(
        timezone="Asia/Taipei", utc_iso="2026-08-04T12:00:00+00:00",
        local_iso="2026-08-04T20:00:00+08:00", local_date="2026-08-04",
        local_time="20:00", weekday_zh_tw="星期二",
    )


class V3ContextSlicerTests(unittest.TestCase):
    def setUp(self):
        self.turn = AgentTurnContextV2(
            user_id="owner", room_id="room", message="我這星期日想去和小李吃晚餐",
            recent_messages=[{"role": "user", "content": "之前聊過的事"}],
            recent_context="最近在規劃週末",
            user_location="三民區",
            relevant_memories=["喜歡牛排"],
            clock=_clock(),
        )

    def test_calendar_slice_excludes_match_and_places(self):
        s = slice_for_agent("calendar", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "calendar")
        # calendar slice should contain events/time/history but NOT match proposal or places
        self.assertIn("clock", s.payload)
        self.assertIn("recent_messages", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("user_location", s.payload)

    def test_places_slice_excludes_calendar_and_match(self):
        s = slice_for_agent("places", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "places")
        self.assertIn("user_location", s.payload)
        self.assertIn("clock", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("recent_context", s.payload)

    def test_match_slice_excludes_calendar_details(self):
        s = slice_for_agent("match", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "match")
        self.assertIn("clock", s.payload)
        self.assertIn("active_proposal", s.payload)
        self.assertNotIn("user_location", s.payload)
        self.assertNotIn("recent_context", s.payload)

    def test_relationship_slice_excludes_calendar_and_match_details(self):
        s = slice_for_agent("relationship", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "relationship")
        self.assertIn("mentioned_contacts", s.payload)
        self.assertIn("recent_messages", s.payload)
        self.assertNotIn("active_proposal", s.payload)

    def test_profile_slice_excludes_match_and_calendar(self):
        s = slice_for_agent("profile", self.turn, prior_observations=[])
        self.assertEqual(s.agent, "profile")
        self.assertIn("recent_context", s.payload)
        self.assertIn("relevant_memories", s.payload)
        self.assertNotIn("active_proposal", s.payload)
        self.assertNotIn("user_location", s.payload)

    def test_synthesizer_slice_contains_all_observations(self):
        prior = [{"task_id": "t1", "tool": "calendar.list_my_events", "result": {"events": []}}]
        s = slice_for_agent("synthesizer", self.turn, prior_observations=prior)
        self.assertEqual(s.agent, "synthesizer")
        self.assertIn("observations", s.payload)
        self.assertEqual(s.payload["observations"], prior)
        self.assertIn("recent_messages", s.payload)

    def test_prior_observations_injected_into_dependent_places_slice(self):
        prior = [{"task_id": "t2", "tool": "places.search_nearby", "result": {"places": [{"name": "巖炙炭燒牛排"}]}}]
        s = slice_for_agent("places", self.turn, prior_observations=prior)
        self.assertIn("prior_observations", s.payload)
        self.assertEqual(s.payload["prior_observations"], prior)

    def test_unknown_agent_raises(self):
        with self.assertRaises(ValueError):
            slice_for_agent("nope", self.turn, prior_observations=[])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest social_demotest/tests/test_v3_context_slicer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# social_demotest/services/ayue_agent/v3/context_slicer.py
"""Deterministic context slicer: cuts privacy-safe slices for each sub-agent."""

from __future__ import annotations

from typing import Any

from services.ayue_agent.contracts import AgentTurnContextV2
from .contracts import AgentContextSlice


def slice_for_agent(
    agent_name: str,
    turn_ctx: AgentTurnContextV2,
    *,
    prior_observations: list[dict[str, Any]],
) -> AgentContextSlice:
    """Return a privacy-safe context slice for the named sub-agent.

    The Scheduler calls this before invoking a sub-agent. Each slice contains
    only the fields that agent is allowed to see, per the V3 spec §8.
    """
    clock_dump = turn_ctx.clock.model_dump()

    if agent_name == "calendar":
        return AgentContextSlice(agent="calendar", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "clock": clock_dump,
            # calendar agent may need recent_context to understand scheduling context
            "recent_context": turn_ctx.recent_context,
            "prior_observations": prior_observations,
        })

    if agent_name == "places":
        return AgentContextSlice(agent="places", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "user_location": turn_ctx.user_location,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "match":
        return AgentContextSlice(agent="match", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "active_proposal": turn_ctx.active_proposal,
            "latest_match_outcome": turn_ctx.latest_match_outcome,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "relationship":
        return AgentContextSlice(agent="relationship", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "mentioned_contacts": turn_ctx.mentioned_contacts,
            "mentioned_contact_overflow": turn_ctx.mentioned_contact_overflow,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "profile":
        return AgentContextSlice(agent="profile", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "recent_context": turn_ctx.recent_context,
            "relevant_memories": turn_ctx.relevant_memories,
            "clock": clock_dump,
            "prior_observations": prior_observations,
        })

    if agent_name == "synthesizer":
        return AgentContextSlice(agent="synthesizer", payload={
            "message": turn_ctx.message,
            "recent_messages": turn_ctx.recent_messages,
            "recent_context": turn_ctx.recent_context,
            "user_location": turn_ctx.user_location,
            "clock": clock_dump,
            "observations": prior_observations,
        })

    raise ValueError(f"unknown agent: {agent_name}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest social_demotest/tests/test_v3_context_slicer.py -v`
Expected: PASS (all 8 tests)

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/context_slicer.py social_demotest/tests/test_v3_context_slicer.py
git commit -m "feat(v3): add deterministic context slicer for sub-agents"
```

---

## Task 4: 多筆獨立 Confirmation 管理（純程式碼）

**Files:**
- Create: `social_demotest/services/ayue_agent/v3/confirmation.py`
- Test: `social_demotest/tests/test_v3_confirmation.py`

**Interfaces:**
- Consumes: existing Mongo `db` for pending confirmations (same collection pattern as V2)
- Produces: `ConfirmationManager` class with `create_confirmation`, `list_active`, `execute_all_confirmed`, `cancel_all`

- [ ] **Step 1: Write the failing test**

```python
# social_demotest/tests/test_v3_confirmation.py
import unittest
from unittest.mock import MagicMock, patch

from services.ayue_agent.v3.confirmation import ConfirmationManager


class V3ConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.coll = MagicMock()
        self.mgr = ConfirmationManager(self.coll)

    def test_create_confirmation_stores_pending_with_ttl(self):
        self.mgr.create_confirmation(
            user_id="owner", agent_name="calendar",
            tool_name="calendar.cancel_my_event",
            arguments={"event_hint": "看電影"},
            ttl_seconds=900,
        )
        self.coll.insert_one.assert_called_once()
        doc = self.coll.insert_one.call_args[0][0]
        self.assertEqual(doc["user_id"], "owner")
        self.assertEqual(doc["tool_name"], "calendar.cancel_my_event")
        self.assertEqual(doc["status"], "pending")
        self.assertEqual(doc["agent_name"], "calendar")

    def test_list_active_returns_only_pending_unexpired(self):
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.cancel_my_event",
             "arguments": {"event_hint": "看電影"}, "status": "pending", "agent_name": "calendar",
             "expires_at": 9999999999.0},
            {"_id": "c2", "user_id": "owner", "tool_name": "match.start_search",
             "arguments": {}, "status": "pending", "agent_name": "match",
             "expires_at": 9999999999.0},
        ]
        actives = self.mgr.list_active(user_id="owner")
        self.assertEqual(len(actives), 2)

    def test_cancel_all_marks_confirmations_cancelled(self):
        self.coll.update_many.return_value = MagicMock(modified_count=2)
        self.mgr.cancel_all(user_id="owner")
        self.coll.update_many.assert_called_once()

    def test_execute_confirmed_returns_per_confirmation_result(self):
        # Each confirmation executes independently via the provided executor callback.
        # If one is stale (executor returns ok=False with stale code), others still run.
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.cancel_my_event",
             "arguments": {"event_hint": "看電影"}, "status": "pending", "agent_name": "calendar"},
        ]
        self.coll.update_one.return_value = MagicMock(modified_count=1)

        def fake_executor(tool_name, arguments, user_id):
            return MagicMock(ok=True, data={"cancelled": True}, error_code=None)

        results = self.mgr.execute_confirmed(user_id="owner", executor=fake_executor)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])

    def test_stale_confirmation_does_not_overwrite_terminal_state(self):
        self.coll.find.return_value = [
            {"_id": "c1", "user_id": "owner", "tool_name": "calendar.cancel_my_event",
             "arguments": {"event_hint": "看電影"}, "status": "pending", "agent_name": "calendar"},
        ]
        self.coll.update_one.return_value = MagicMock(modified_count=0)  # CAS miss

        def stale_executor(tool_name, arguments, user_id):
            return MagicMock(ok=False, data={}, error_code="stale_revision")

        results = self.mgr.execute_confirmed(user_id="owner", executor=stale_executor)
        self.assertFalse(results[0]["ok"])
        self.assertEqual(results[0]["error_code"], "stale_revision")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest social_demotest/tests/test_v3_confirmation.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# social_demotest/services/ayue_agent/v3/confirmation.py
"""Multi-confirmation manager: independent CAS per confirmation, no cross-invalidation."""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from pydantic import BaseModel


class ConfirmationRecord(BaseModel):
    confirmation_id: str
    user_id: str
    agent_name: str
    tool_name: str
    arguments: dict[str, Any]
    status: str = "pending"
    created_at: float
    expires_at: float


class ConfirmationManager:
    """Manages multiple independent pending confirmations per user.

    Each confirmation executes independently via CAS. Stale confirmations report
    `stale_revision` without overwriting terminal state; they do not invalidate
    sibling confirmations.
    """

    def __init__(self, collection: Any) -> None:
        self._coll = collection

    def create_confirmation(
        self, *, user_id: str, agent_name: str, tool_name: str,
        arguments: dict[str, Any], ttl_seconds: int = 900,
    ) -> str:
        confirmation_id = uuid.uuid4().hex
        now = time.time()
        self._coll.insert_one({
            "_id": confirmation_id,
            "user_id": user_id,
            "agent_name": agent_name,
            "tool_name": tool_name,
            "arguments": arguments,
            "status": "pending",
            "created_at": now,
            "expires_at": now + ttl_seconds,
        })
        return confirmation_id

    def list_active(self, *, user_id: str) -> list[dict[str, Any]]:
        now = time.time()
        cursor = self._coll.find({
            "user_id": user_id,
            "status": "pending",
            "expires_at": {"$gt": now},
        })
        return list(cursor)

    def cancel_all(self, *, user_id: str) -> int:
        result = self._coll.update_many(
            {"user_id": user_id, "status": "pending"},
            {"$set": {"status": "cancelled"}},
        )
        return getattr(result, "modified_count", 0)

    def execute_confirmed(
        self, *, user_id: str,
        executor: Callable[[str, dict[str, Any], str], Any],
    ) -> list[dict[str, Any]]:
        """Execute all active confirmations for the user via the executor callback.

        Each confirmation runs independently. The executor returns an object with
        `.ok` and `.error_code`. CAS failures (ok=False, error_code=stale_revision)
        are reported per-confirmation and do not affect siblings.
        """
        actives = self.list_active(user_id=user_id)
        results: list[dict[str, Any]] = []
        for rec in actives:
            cid = rec["_id"]
            tool_name = rec["tool_name"]
            arguments = rec["arguments"]
            # Mark executing atomically (CAS on status pending -> executing).
            claimed = self._coll.update_one(
                {"_id": cid, "status": "pending"},
                {"$set": {"status": "executing"}},
            )
            if getattr(claimed, "modified_count", 0) == 0:
                # Another worker already took it or it was cancelled concurrently.
                continue
            try:
                tool_result = executor(tool_name, arguments, user_id)
            except Exception as exc:
                self._coll.update_one({"_id": cid}, {"$set": {"status": "failed", "error": str(exc)}})
                results.append({"confirmation_id": cid, "ok": False, "error_code": "executor_exception"})
                continue
            ok = getattr(tool_result, "ok", False)
            data = getattr(tool_result, "data", {})
            error_code = getattr(tool_result, "error_code", None)
            new_status = "completed" if ok else "failed"
            self._coll.update_one(
                {"_id": cid},
                {"$set": {"status": new_status, "result": data, "error_code": error_code}},
            )
            results.append({
                "confirmation_id": cid,
                "ok": ok,
                "tool_name": tool_name,
                "data": data,
                "error_code": error_code,
            })
        return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest social_demotest/tests/test_v3_confirmation.py -v`
Expected: PASS (all 5 tests)

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/confirmation.py social_demotest/tests/test_v3_confirmation.py
git commit -m "feat(v3): add multi-confirmation manager with independent CAS"
```

---

## Task 5: Sub-agent base + 5 個 domain sub-agents

**Files:**
- Create: `social_demotest/services/ayue_agent/v3/sub_agents/__init__.py`
- Create: `social_demotest/services/ayue_agent/v3/sub_agents/base.py`
- Create: `social_demotest/services/ayue_agent/v3/sub_agents/calendar_agent.py`
- Create: `social_demotest/services/ayue_agent/v3/sub_agents/places_agent.py`
- Create: `social_demotest/services/ayue_agent/v3/sub_agents/match_agent.py`
- Create: `social_demotest/services/ayue_agent/v3/sub_agents/relationship_agent.py`
- Create: `social_demotest/services/ayue_agent/v3/sub_agents/profile_agent.py`
- Test: `social_demotest/tests/test_v3_sub_agents.py`

**Interfaces:**
- Consumes: `AgentContextSlice` from Task 1; `TOOL_REGISTRY`, `planner_tool_names`, `planner_arguments_schema` from existing `tool_registry.py`; `generate_chat_completion_with_tools` from existing `ai_service.py`
- Produces: `run_sub_agent(agent_name, context_slice, task_brief) -> ToolProposal | None` for each agent

- [ ] **Step 1: Write the failing test**

```python
# social_demotest/tests/test_v3_sub_agents.py
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContextV2, TurnClockV1
from services.ayue_agent.v3.contracts import AgentContextSlice, ToolProposal
from services.ayue_agent.v3.sub_agents.calendar_agent import run as run_calendar
from services.ayue_agent.v3.sub_agents.places_agent import run as run_places
from services.ayue_agent.v3.sub_agents.match_agent import run as run_match
from services.ayue_agent.v3.sub_agents.relationship_agent import run as run_relationship
from services.ayue_agent.v3.sub_agents.profile_agent import run as run_profile
from services.ai_service import ToolCallResult


def _clock():
    return TurnClockV1(
        timezone="Asia/Taipei", utc_iso="2026-08-04T12:00:00+00:00",
        local_iso="2026-08-04T20:00:00+08:00", local_date="2026-08-04",
        local_time="20:00", weekday_zh_tw="星期二",
    )


def _slice(agent, payload):
    return AgentContextSlice(agent=agent, payload=payload)


def _fc_result(content="", tool_calls=None):
    return ToolCallResult(content=content, tool_calls=tool_calls or [])


class V3SubAgentTests(unittest.TestCase):
    def test_calendar_agent_produces_list_events_proposal(self):
        slc = _slice("calendar", {
            "message": "我這週日有空嗎？",
            "recent_messages": [],
            "clock": _clock().model_dump(),
            "recent_context": "",
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "calendar.list_my_events", "arguments": {}}]),
        ):
            proposal = run_calendar(slc, task_brief="檢查使用者這週日是否有行程衝突")
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.tool_name, "calendar.list_my_events")

    def test_places_agent_produces_search_nearby_proposal(self):
        slc = _slice("places", {
            "message": "附近有什麼牛排？",
            "recent_messages": [],
            "user_location": "三民區",
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{
                "name": "places.search_nearby",
                "arguments": {"anchor": "三民區", "categories": ["restaurant"], "cuisine": "牛排"},
            }]),
        ):
            proposal = run_places(slc, task_brief="搜尋使用者附近的牛排餐廳")
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.tool_name, "places.search_nearby")

    def test_match_agent_produces_get_status_proposal(self):
        slc = _slice("match", {
            "message": "我的配對進度如何？",
            "recent_messages": [],
            "active_proposal": None,
            "latest_match_outcome": None,
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "match.get_status", "arguments": {}}]),
        ):
            proposal = run_match(slc, task_brief="讀取配對狀態")
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.tool_name, "match.get_status")

    def test_relationship_agent_produces_list_contacts_proposal(self):
        slc = _slice("relationship", {
            "message": "我認識的人有誰？",
            "recent_messages": [],
            "mentioned_contacts": [],
            "mentioned_contact_overflow": False,
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "relationship.list_accepted_contacts", "arguments": {}}]),
        ):
            proposal = run_relationship(slc, task_brief="列出已接受聯絡人")
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.tool_name, "relationship.list_accepted_contacts")

    def test_profile_agent_produces_get_self_summary_proposal(self):
        slc = _slice("profile", {
            "message": "你了解我多少？",
            "recent_messages": [],
            "recent_context": "",
            "relevant_memories": [],
            "clock": _clock().model_dump(),
            "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "profile.get_self_summary", "arguments": {}}]),
        ):
            proposal = run_profile(slc, task_brief="讀取本人 profile")
        self.assertIsNotNone(proposal)
        self.assertEqual(proposal.tool_name, "profile.get_self_summary")

    def test_calendar_agent_returns_none_on_llm_timeout(self):
        slc = _slice("calendar", {
            "message": "x", "recent_messages": [], "clock": _clock().model_dump(),
            "recent_context": "", "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            side_effect=TimeoutError("timeout"),
        ):
            proposal = run_calendar(slc, task_brief="x")
        self.assertIsNone(proposal)

    def test_calendar_agent_returns_none_on_unknown_tool(self):
        slc = _slice("calendar", {
            "message": "x", "recent_messages": [], "clock": _clock().model_dump(),
            "recent_context": "", "prior_observations": [],
        })
        with patch(
            "services.ayue_agent.v3.sub_agents.base.generate_chat_completion_with_tools",
            return_value=_fc_result(tool_calls=[{"name": "nope.bad", "arguments": {}}]),
        ):
            proposal = run_calendar(slc, task_brief="x")
        self.assertIsNone(proposal)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest social_demotest/tests/test_v3_sub_agents.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# social_demotest/services/ayue_agent/v3/sub_agents/__init__.py
"""Sub-agent modules: each is a thin LLM + function-calling wrapper over its tool set."""
```

```python
# social_demotest/services/ayue_agent/v3/sub_agents/base.py
"""Shared sub-agent execution: build tools, call LLM, parse proposal."""

from __future__ import annotations

import json
from typing import Any, Iterable

from services.ai_service import generate_chat_completion_with_tools
from services.ayue_agent.tool_registry import (
    TOOL_REGISTRY,
    get_tool_spec,
    planner_arguments_allowed,
    planner_arguments_schema,
)
from ..contracts import AgentContextSlice, ToolProposal


def _build_tools(tool_names: Iterable[str]) -> list[dict]:
    tools = []
    for name in sorted(tool_names):
        spec = get_tool_spec(name)
        if spec is None:
            continue
        schema = planner_arguments_schema(spec)
        schema.pop("title", None)
        for prop in schema.get("properties", {}).values():
            prop.pop("title", None)
        if "$defs" in schema:
            for defn in schema["$defs"].values():
                defn.pop("title", None)
        tools.append({
            "type": "function",
            "function": {"name": name, "description": spec.description, "parameters": schema},
        })
    return tools


def _agent_prompt(system_line: str, task_brief: str, slice_payload: dict[str, Any]) -> str:
    return f"""{system_line}
你的任務：{task_brief}
你只能使用提供的工具。不可輸出 ID、revision、資料庫欄位、對方行事曆內容。
呼叫工具後直接結束；不要閒聊或重述。
安全 context：{json.dumps(slice_payload, ensure_ascii=False)}"""


def run_sub_agent(
    *, tool_names: frozenset[str], system_line: str,
    context_slice: AgentContextSlice, task_brief: str,
) -> ToolProposal | None:
    """Call the LLM with function calling and parse the first tool call into a ToolProposal.

    Returns None on: timeout, bad JSON, unknown tool, schema-invalid args.
    The caller (Scheduler) will mark this sub-task as failed.
    """
    try:
        tools = _build_tools(tool_names)
        prompt = _agent_prompt(system_line, task_brief, context_slice.payload)
        result = generate_chat_completion_with_tools(prompt, tools, temperature=0)
        if not result.tool_calls:
            return None
        tc = result.tool_calls[0]
        tool_name = tc.get("name", "")
        arguments = tc.get("arguments", {}) or {}
        if tool_name not in tool_names:
            return None
        spec = get_tool_spec(tool_name)
        if spec is None:
            return None
        if not planner_arguments_allowed(spec, arguments):
            return None
        return ToolProposal(tool_name=tool_name, arguments=arguments)
    except Exception:
        return None
```

```python
# social_demotest/services/ayue_agent/v3/sub_agents/calendar_agent.py
from services.ayue_agent.tool_registry import planner_tool_names
from ..contracts import AgentContextSlice, ToolProposal
from .base import run_sub_agent

_SYSTEM = "你是阿月的行事曆助手，只能讀取與管理本人的行事曆。"
_TOOLS = planner_tool_names(
    can_start_search=False, can_decide_active_proposal=False, can_edit_calendar=True,
    can_read_mentioned_contacts=False, can_use_web=False, can_use_places=False,
    can_start_assessments=False,
) | frozenset({"calendar.list_my_events", "calendar.find_my_event"})
# Keep only calendar tools.
_TOOLS = frozenset(t for t in _TOOLS if t.startswith("calendar."))


def run(context_slice: AgentContextSlice, *, task_brief: str) -> ToolProposal | None:
    return run_sub_agent(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
```

```python
# social_demotest/services/ayue_agent/v3/sub_agents/places_agent.py
from services.ayue_agent.tool_registry import PLACES_TOOLS, WEB_TOOLS
from ..contracts import AgentContextSlice, ToolProposal
from .base import run_sub_agent

_SYSTEM = "你是阿月的地點與網路助手，負責查詢附近餐廳、景點、距離與公開網路資訊。"
_TOOLS = PLACES_TOOLS | WEB_TOOLS


def run(context_slice: AgentContextSlice, *, task_brief: str) -> ToolProposal | None:
    return run_sub_agent(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
```

```python
# social_demotest/services/ayue_agent/v3/sub_agents/match_agent.py
from ..contracts import AgentContextSlice, ToolProposal
from .base import run_sub_agent

_SYSTEM = "你是阿月的配對助手，只能讀取配對狀態與操作唯一可決定提案。"
_TOOLS = frozenset({
    "match.get_status", "match.get_counterparty_summary",
    "match.start_search", "match.decide_active_proposal",
})


def run(context_slice: AgentContextSlice, *, task_brief: str) -> ToolProposal | None:
    return run_sub_agent(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
```

```python
# social_demotest/services/ayue_agent/v3/sub_agents/relationship_agent.py
from ..contracts import AgentContextSlice, ToolProposal
from .base import run_sub_agent

_SYSTEM = "你是阿月的關係助手，負責讀取已接受聯絡人與已驗證互動摘要。"
_TOOLS = frozenset({
    "relationship.get_verified_evidence",
    "relationship.get_mentioned_contact_summary",
    "relationship.list_accepted_contacts",
})


def run(context_slice: AgentContextSlice, *, task_brief: str) -> ToolProposal | None:
    return run_sub_agent(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
```

```python
# social_demotest/services/ayue_agent/v3/sub_agents/profile_agent.py
from ..contracts import AgentContextSlice, ToolProposal
from .base import run_sub_agent

_SYSTEM = "你是阿月的個人檔案助手，負責讀取本人 profile、近期情境、記憶與測驗。"
_TOOLS = frozenset({
    "profile.get_recent_context", "profile.get_self_summary",
    "profile.start_assessment", "memory.search_my_profile",
})


def run(context_slice: AgentContextSlice, *, task_brief: str) -> ToolProposal | None:
    return run_sub_agent(
        tool_names=_TOOLS, system_line=_SYSTEM,
        context_slice=context_slice, task_brief=task_brief,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest social_demotest/tests/test_v3_sub_agents.py -v`
Expected: PASS (all 7 tests)

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/sub_agents/ social_demotest/tests/test_v3_sub_agents.py
git commit -m "feat(v3): add 5 domain sub-agents with function-calling proposal output"
```

---

## Task 6: Planner（輕量 LLM，產靜態 DAG）

**Files:**
- Create: `social_demotest/services/ayue_agent/v3/planner.py`
- Test: `social_demotest/tests/test_v3_planner.py`

**Interfaces:**
- Consumes: `AgentTurnContextV2` from existing `contracts.py`; `generate_chat_completion` from existing `ai_service.py`
- Produces: `plan_turn(turn_ctx, pending_confirmations) -> Plan | None`

- [ ] **Step 1: Write the failing test**

```python
# social_demotest/tests/test_v3_planner.py
import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.contracts import AgentTurnContextV2, TurnClockV1
from services.ayue_agent.v3.contracts import Plan
from services.ayue_agent.v3.planner import plan_turn


def _clock():
    return TurnClockV1(
        timezone="Asia/Taipei", utc_iso="2026-08-04T12:00:00+00:00",
        local_iso="2026-08-04T20:00:00+08:00", local_date="2026-08-04",
        local_time="20:00", weekday_zh_tw="星期二",
    )


class V3PlannerTests(unittest.TestCase):
    def _turn(self, message):
        return AgentTurnContextV2(
            user_id="owner", room_id="room", message=message,
            clock=_clock(),
        )

    def test_steak_example_produces_calendar_places_synthesizer_dag(self):
        turn = self._turn("我這星期日想去和小李吃晚餐，想吃附近的牛排和再去吃甜點，你覺得如何？")
        dag_json = json.dumps({
            "tasks": [
                {"id": "t1", "agent": "calendar", "depends_on": [], "task_brief": "檢查使用者這週日是否有行程衝突"},
                {"id": "t2", "agent": "places", "depends_on": [], "task_brief": "搜尋使用者附近的牛排餐廳"},
                {"id": "t3", "agent": "places", "depends_on": ["t2"], "task_brief": "搜尋 t2 牛排店附近的甜點店"},
                {"id": "t4", "agent": "synthesizer", "depends_on": ["t1", "t2", "t3"], "task_brief": "綜合行事曆衝突、牛排與甜點推薦，給使用者想法與提醒"}
            ]
        }, ensure_ascii=False)
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion",
            return_value=SimpleNamespace(content=dag_json, input_tokens=0, output_tokens=0, duration_ms=0, prompt=""),
        ):
            plan = plan_turn(turn, pending_confirmations=[])
        self.assertIsNotNone(plan)
        self.assertIsInstance(plan, Plan)
        agents = [t.agent for t in plan.tasks]
        self.assertIn("calendar", agents)
        self.assertIn("places", agents)
        self.assertIn("synthesizer", agents)
        # synthesizer must be terminal
        syn = [t for t in plan.tasks if t.agent == "synthesizer"]
        self.assertEqual(len(syn), 1)

    def test_simple_chat_produces_synthesizer_only(self):
        turn = self._turn("最近還好嗎")
        dag_json = json.dumps({
            "tasks": [
                {"id": "t1", "agent": "synthesizer", "depends_on": [], "task_brief": "直接回覆使用者"}
            ]
        }, ensure_ascii=False)
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion",
            return_value=SimpleNamespace(content=dag_json, input_tokens=0, output_tokens=0, duration_ms=0, prompt=""),
        ):
            plan = plan_turn(turn, pending_confirmations=[])
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.tasks), 1)
        self.assertEqual(plan.tasks[0].agent, "synthesizer")

    def test_bad_json_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion",
            return_value=SimpleNamespace(content="not json", input_tokens=0, output_tokens=0, duration_ms=0, prompt=""),
        ):
            plan = plan_turn(turn, pending_confirmations=[])
        self.assertIsNone(plan)

    def test_timeout_returns_none(self):
        turn = self._turn("x")
        with patch(
            "services.ayue_agent.v3.planner.generate_chat_completion",
            side_effect=TimeoutError("timeout"),
        ):
            plan = plan_turn(turn, pending_confirmations=[])
        self.assertIsNone(plan)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest social_demotest/tests/test_v3_planner.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# social_demotest/services/ayue_agent/v3/planner.py
"""Lightweight V3 Planner: only decomposes the request into a static sub-task DAG."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from services.ai_service import generate_chat_completion
from services.ayue_agent.contracts import AgentTurnContextV2
from .contracts import Plan


_AGENT_DESCRIPTIONS = {
    "calendar": "行事曆助手：查詢與管理本人的行程、衝突檢查、建立/修改/取消行程",
    "places": "地點與網路助手：查詢附近餐廳/景點、兩地距離、上網搜尋公開資訊",
    "match": "配對助手：讀取配對狀態、開始找人、對唯一提案表達有興趣或婉拒",
    "relationship": "關係助手：讀取已接受聯絡人清單、@ 對象公開摘要、已驗證互動",
    "profile": "個人檔案助手：讀取本人 profile、近期情境、記憶、開始測驗",
    "synthesizer": "綜合助手：拿所有 sub-agent 的結果產出最終回覆，不再 call function",
}


def _planner_prompt(turn_ctx: AgentTurnContextV2, pending_confirmations: list[dict[str, Any]]) -> str:
    agents_desc = "\n".join(f"- {k}: {v}" for k, v in _AGENT_DESCRIPTIONS.items())
    payload = {
        "message": turn_ctx.message,
        "recent_messages": turn_ctx.recent_messages,
        "recent_context": turn_ctx.recent_context,
        "user_location": turn_ctx.user_location,
        "relevant_memories": turn_ctx.relevant_memories,
        "clock": turn_ctx.clock.model_dump(),
        "active_proposal": turn_ctx.active_proposal,
        "mentioned_contacts": turn_ctx.mentioned_contacts,
        "pending_confirmations": pending_confirmations,
    }
    return f"""你是公開阿月的任務規劃器。你的唯一工作是：把使用者的請求拆成一張子任務 DAG。
不要填 function 參數、不要審核、不要回覆使用者。只輸出 JSON。

可用 sub-agent：
{agents_desc}

規則：
1. 每個 task 有 id、agent、depends_on（前置 task id 清單，空=可立刻跑）、task_brief（一句話描述這個子任務要查/做什麼）。
2. 平行可做的任務 depends_on=[]，有順序依賴的填前置 id。
3. 最後必須有一個 synthesizer task 綜合所有結果。synthesizer 必須依賴所有其他 task。
4. 涉及時間/日期/排行程的請求必須包含 calendar agent。
5. 涉及地點/餐廳/網路的請求包含 places agent。
6. 涉及配對的請求包含 match agent。
7. 涉及 @ 對象或已認識的人包含 relationship agent。
8. 涉及「我是誰/你了解我嗎」包含 profile agent。
9. 一般聊天只需 synthesizer。

只輸出 JSON，格式：{{"tasks":[{{"id":"t1","agent":"calendar","depends_on":[],"task_brief":"..."}}]}}

安全 context：{json.dumps(payload, ensure_ascii=False)}"""


def plan_turn(turn_ctx: AgentTurnContextV2, *, pending_confirmations: list[dict[str, Any]]) -> Plan | None:
    """Call LLM to decompose the request into a static Plan. Returns None on failure."""
    try:
        prompt = _planner_prompt(turn_ctx, pending_confirmations)
        result = generate_chat_completion(prompt, temperature=0)
        raw = str(result.content or "").strip()
        plan = Plan.model_validate_json(raw)
        return plan
    except (ValidationError, Exception):
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest social_demotest/tests/test_v3_planner.py -v`
Expected: PASS (all 4 tests)

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/planner.py social_demotest/tests/test_v3_planner.py
git commit -m "feat(v3): add lightweight Planner producing static sub-task DAG"
```

---

## Task 7: Synthesizer（最終綜合 LLM）

**Files:**
- Create: `social_demotest/services/ayue_agent/v3/synthesizer.py`
- Test: `social_demotest/tests/test_v3_synthesizer.py`

**Interfaces:**
- Consumes: `AgentContextSlice` from Task 1; `generate_chat_completion` from existing `ai_service.py`; existing `capabilities.py` helpers (`normalize_public_language`, `_concise_public_reply`, `capability_answer`, `is_capability_query`, `contains_unsupported_random_match_claim`, `matching_truth_reply`)
- Produces: `synthesize(context_slice) -> str`

- [ ] **Step 1: Write the failing test**

```python
# social_demotest/tests/test_v3_synthesizer.py
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.ayue_agent.v3.contracts import AgentContextSlice
from services.ayue_agent.v3.synthesizer import synthesize


class V3SynthesizerTests(unittest.TestCase):
    def _slice(self, observations):
        return AgentContextSlice(agent="synthesizer", payload={
            "message": "我這星期日想吃牛排",
            "recent_messages": [],
            "recent_context": "",
            "user_location": "三民區",
            "clock": {"timezone": "Asia/Taipei", "local_date": "2026-08-04", "local_time": "20:00"},
            "observations": observations,
        })

    def test_produces_reply_from_observations(self):
        slc = self._slice([
            {"task_id": "t1", "status": "ok", "tool": "calendar.list_my_events",
             "result": {"events": [{"title": "看電影", "date": "2026-08-09", "start_time": "20:00"}]}},
            {"task_id": "t2", "status": "ok", "tool": "places.search_nearby",
             "result": {"places": [{"name": "巖炙炭燒牛排", "distance_m": 726}]}},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion",
            return_value=SimpleNamespace(
                content="你週日晚有電影，不過附近有巖炙炭燒牛排不錯，要不要改午餐？",
                input_tokens=0, output_tokens=0, duration_ms=0, prompt="",
            ),
        ):
            reply = synthesize(slc)
        self.assertIn("電影", reply)
        self.assertIn("牛排", reply)

    def test_handles_partial_failure(self):
        slc = self._slice([
            {"task_id": "t1", "status": "ok", "tool": "calendar.list_my_events",
             "result": {"events": [{"title": "看電影", "date": "2026-08-09"}]}},
            {"task_id": "t2", "status": "failed", "tool": None,
             "error_code": "llm_timeout"},
            {"task_id": "t3", "status": "skipped", "tool": None,
             "skip_reason": "dependency_failed"},
        ])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion",
            return_value=SimpleNamespace(
                content="你週日晚有電影，不過餐廳我這次沒查到，要不要我等一下再查？",
                input_tokens=0, output_tokens=0, duration_ms=0, prompt="",
            ),
        ):
            reply = synthesize(slc)
        self.assertIn("電影", reply)
        self.assertIn("沒查到", reply)

    def test_fallback_on_provider_error(self):
        slc = self._slice([])
        with patch(
            "services.ayue_agent.v3.synthesizer.generate_chat_completion",
            side_effect=Exception("provider down"),
        ):
            reply = synthesize(slc)
        # Should still produce some safe fallback reply, not crash
        self.assertIsInstance(reply, str)
        self.assertTrue(len(reply) > 0)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest social_demotest/tests/test_v3_synthesizer.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# social_demotest/services/ayue_agent/v3/synthesizer.py
"""V3 Synthesizer: combines all sub-agent observations into the final user reply."""

from __future__ import annotations

import json
from typing import Any

from services.ai_service import generate_chat_completion
from services.ayue_agent.capabilities import (
    contains_unsupported_random_match_claim,
    is_capability_query,
    matching_truth_reply,
    normalize_public_language,
    capability_answer,
)
from services.ayue_agent.router import _concise_public_reply, _INTERNAL_META_REPLY_RE
from .contracts import AgentContextSlice


def _build_prompt(slice_payload: dict[str, Any]) -> str:
    return f"""你是公開阿月，這個交友 App 內幫使用者認識人、牽線的 AI 媒人。
直接回應使用者現在這句話。一般聊天最多 2 句、約 80 個中文字，只問一個真正有用的問題。
observations 是本回合各 sub-agent 已驗證的結果；只要它能回答問題，就必須以它為主要依據自然作答。
若某個 sub-task 的 status 不是 ok（failed 或 skipped），自然說明那部分沒查到，不可假裝有結果。
不要提及工具、函式、模型、prompt、權限、系統限制或內部流程。稱呼人時只用「對象、人選、對方、旅伴」，不可使用「物件」。
只輸出要給使用者的回覆，不要 JSON。

安全 context：{json.dumps(slice_payload, ensure_ascii=False)}"""


def synthesize(context_slice: AgentContextSlice) -> str:
    """Produce the final user reply from all sub-agent observations."""
    payload = context_slice.payload
    if is_capability_query(payload.get("message", "")):
        return capability_answer()
    try:
        prompt = _build_prompt(payload)
        result = generate_chat_completion(prompt, temperature=0.65)
        reply = _concise_public_reply(
            normalize_public_language(str(result.content or "").strip()),
            preserve_details=True,
        )
        if reply and not _INTERNAL_META_REPLY_RE.search(reply) and not contains_unsupported_random_match_claim(reply):
            return reply
    except Exception:
        pass
    # Deterministic fallback
    message = payload.get("message", "")
    if any(word in message for word in ("配對", "媒合", "找人", "人選", "隨機")):
        return matching_truth_reply()
    return "我這次沒能完整查到資料，要不要再說一次你想查什麼？"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest social_demotest/tests/test_v3_synthesizer.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/synthesizer.py social_demotest/tests/test_v3_synthesizer.py
git commit -m "feat(v3): add Synthesizer combining observations into final reply"
```

---

## Task 8: Scheduler / Orchestrator（純程式碼，整合所有元件）

**Files:**
- Create: `social_demotest/services/ayue_agent/v3/scheduler.py`
- Test: `social_demotest/tests/test_v3_scheduler.py`

**Interfaces:**
- Consumes: `Plan`, `SubTask`, `SubTaskResult`, `SubTaskStatus`, `AgentContextSlice`, `ToolProposal`, `GuardDecision` from Task 1; `guard_proposal` from Task 2; `slice_for_agent` from Task 3; `ConfirmationManager` from Task 4; sub-agent `run` functions from Task 5; `plan_turn` from Task 6; `synthesize` from Task 7; `execute_tool`, `executor_arguments_for_turn`, `tool_call_key` from existing modules; `build_agent_turn_context_v2`, `build_turn_clock` from existing `context.py`/`time_context.py`; `confirmation_choice` from existing `router.py`
- Produces: `run_public_agent_turn_v3(ctx, mode, on_progress) -> AgentResult`

- [ ] **Step 1: Write the failing test**

```python
# social_demotest/tests/test_v3_scheduler.py
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentTurnContext, AgentResult
from services.ayue_agent.v3.contracts import Plan, SubTask, SubTaskStatus, ToolProposal
from services.ayue_agent.v3.scheduler import run_public_agent_turn_v3


class V3SchedulerTests(unittest.TestCase):
    def _ctx(self, message="我這星期日想吃牛排"):
        return AgentTurnContext(user_id="owner", room_id="room", message=message)

    def test_steak_example_full_flow(self):
        """The canonical trajectory: calendar + places(牛排) → places(甜點) → synthesizer."""
        ctx = self._ctx("我這星期日想去和小李吃晚餐，想吃附近的牛排和再去吃甜點")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="檢查週日衝突"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="搜牛排"),
            SubTask(id="t3", agent="places", depends_on=["t2"], task_brief="搜甜點"),
            SubTask(id="t4", agent="synthesizer", depends_on=["t1", "t2", "t3"], task_brief="綜合"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=plan), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.sub_agents.calendar_agent.run", return_value=ToolProposal(tool_name="calendar.list_my_events", arguments={})), \
             patch("services.ayue_agent.v3.sub_agents.places_agent.run", side_effect=[
                 ToolProposal(tool_name="places.search_nearby", arguments={"anchor": "三民區", "categories": ["restaurant"]}),
                 ToolProposal(tool_name="places.search_nearby", arguments={"anchor": "巖炙炭燒牛排", "categories": ["cafe"]}),
             ]), \
             patch("services.ayue_agent.v3.scheduler.execute_tool") as mock_exec, \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value="你週日晚有電影，不過附近有巖炙牛排不錯，要不要改午餐？"):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_exec.return_value = MagicMock(ok=True, data={"events": []}, error_code=None)
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertIsInstance(result, AgentResult)
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v3")

    def test_failed_sub_agent_skipped_and_synthesizer_handles_gap(self):
        ctx = self._ctx("查一下")
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="查行事曆"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="查餐廳"),
            SubTask(id="t3", agent="synthesizer", depends_on=["t1", "t2"], task_brief="綜合"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=plan), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.sub_agents.calendar_agent.run", return_value=ToolProposal(tool_name="calendar.list_my_events", arguments={})), \
             patch("services.ayue_agent.v3.sub_agents.places_agent.run", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", return_value=MagicMock(ok=True, data={}, error_code=None)), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value="行事曆 ok，不過餐廳沒查到"):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)

    def test_planner_returns_none_yields_fail_closed(self):
        ctx = self._ctx("壞掉")
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=None), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build:
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v3")
        self.assertIsNotNone(result.fallback_reason)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest social_demotest/tests/test_v3_scheduler.py -v`
Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: Write minimal implementation**

```python
# social_demotest/services/ayue_agent/v3/scheduler.py
"""V3 Scheduler / Orchestrator: pure-code orchestration of the sub-agent runtime."""

from __future__ import annotations

import os
import time
import uuid
from typing import Any, Callable

from services.ayue_agent.contracts import AgentResult, AgentTurnContext
from services.ayue_agent.context import build_agent_turn_context_v2
from services.ayue_agent.public_relationship_projection import validated_mentioned_contact_ids
from services.ayue_agent.router import confirmation_choice
from services.ayue_agent.time_context import build_turn_clock
from services.ayue_agent.tool_registry import (
    TOOL_REGISTRY, ToolRisk, executor_arguments_for_turn, tool_call_key,
)
from services.ayue_agent.tools import execute_tool as _execute_tool
from database import db

from .contracts import (
    AgentContextSlice, GuardDecision, GuardResultCode, Plan, SubTask,
    SubTaskResult, SubTaskStatus, ToolProposal,
)
from .context_slicer import slice_for_agent
from .guard import guard_proposal
from .planner import plan_turn
from .synthesizer import synthesize
from .confirmation import ConfirmationManager
from .sub_agents.calendar_agent import run as run_calendar
from .sub_agents.places_agent import run as run_places
from .sub_agents.match_agent import run as run_match
from .sub_agents.relationship_agent import run as run_relationship
from .sub_agents.profile_agent import run as run_profile


ProgressCallback = Callable[[dict[str, Any]], Any]
MAX_READS = max(1, int(os.getenv("AYUE_SUBAGENT_MAX_READS", "3")))

_CONFIRMATIONS = db["v3_pending_confirmations"]

_SUB_AGENT_RUNNERS = {
    "calendar": run_calendar,
    "places": run_places,
    "match": run_match,
    "relationship": run_relationship,
    "profile": run_profile,
}


def agent_mode_for_user_v3(user_id: str) -> str:
    mode = os.getenv("AYUE_AGENT_V3_MODE", "off").strip().lower()
    if mode not in {"off", "on"}:
        mode = "off"
    allowlist = {v.strip() for v in os.getenv("AYUE_AGENT_V3_USER_ALLOWLIST", "").split(",") if v.strip()}
    return mode if not allowlist or user_id in allowlist else "off"


def _topological_layers(plan: Plan) -> list[list[SubTask]]:
    """Group tasks into execution layers by dependency depth."""
    done: set[str] = set()
    layers: list[list[SubTask]] = []
    remaining = list(plan.tasks)
    while remaining:
        ready = [t for t in remaining if all(dep in done for dep in t.depends_on)]
        if not ready:
            # Cycle or missing dep — mark remaining as skipped.
            break
        layers.append(ready)
        for t in ready:
            done.add(t.id)
            remaining.remove(t)
    return layers


def _run_sub_task(
    task: SubTask, turn_ctx: Any, prior_observations: list[dict[str, Any]],
    *, seen_keys: set[tuple[str, str]], step_counts: dict[str, int],
    on_progress: ProgressCallback | None, run_id: str,
) -> SubTaskResult:
    """Run a single sub-task: slice context, call sub-agent, guard, execute."""
    context_slice = slice_for_agent(task.agent, turn_ctx, prior_observations=prior_observations)
    runner = _SUB_AGENT_RUNNERS.get(task.agent)
    if runner is None:
        return SubTaskResult(task_id=task.id, status=SubTaskStatus.SKIPPED,
                              skip_reason=f"no runner for agent {task.agent}")
    try:
        proposal = runner(context_slice, task_brief=task.task_brief)
    except Exception:
        return SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED, error_code="sub_agent_exception")
    if proposal is None:
        return SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED, error_code="sub_agent_no_proposal")

    # Central Guard
    decision = guard_proposal(
        proposal, agent_name=task.agent,
        seen_keys=seen_keys, step_count=step_counts.get(task.agent, 0),
        max_reads=MAX_READS,
    )
    if not decision.ok:
        if decision.code == GuardResultCode.WRITE_REQUIRES_CONFIRMATION:
            # Build pending confirmation; synthesizer will tell user to confirm.
            _CONFIRMATIONS.insert_one({
                "_id": uuid.uuid4().hex,
                "user_id": turn_ctx.user_id,
                "agent_name": task.agent,
                "tool_name": proposal.tool_name,
                "arguments": proposal.arguments,
                "status": "pending",
                "created_at": time.time(),
                "expires_at": time.time() + 900,
            })
            return SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                                  tool_name=proposal.tool_name,
                                  observation={"pending_confirmation": True, "tool_name": proposal.tool_name})
        return SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                              tool_name=proposal.tool_name, guard_code=decision.code)

    # Execute the read tool
    spec = TOOL_REGISTRY[proposal.tool_name]
    safe_args = executor_arguments_for_turn(
        spec, getattr(turn_ctx, "_mentioned_ids", []) or [],
        proposal.arguments if spec.argument_source.value == "planner_grounded" else None,
    )
    key = tool_call_key(spec, safe_args)
    seen_keys.add(key)
    step_counts[task.agent] = step_counts.get(task.agent, 0) + 1
    try:
        tool_result = _execute_tool(
            type("TC", (), {"name": proposal.tool_name, "arguments": safe_args})(),
            turn_ctx._raw_ctx, clock=turn_ctx.clock,
        )
    except Exception:
        return SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                              tool_name=proposal.tool_name, error_code="tool_exception")
    if not tool_result.ok:
        return SubTaskResult(task_id=task.id, status=SubTaskStatus.FAILED,
                              tool_name=proposal.tool_name, error_code=tool_result.error_code)
    return SubTaskResult(task_id=task.id, status=SubTaskStatus.OK,
                          tool_name=proposal.tool_name, observation=tool_result.data)


def run_public_agent_turn_v3(
    ctx: AgentTurnContext, *, mode: str = "on", on_progress: ProgressCallback | None = None,
) -> AgentResult:
    """V3 sub-agent runtime entry point."""
    mentioned_ids, mention_overflow = validated_mentioned_contact_ids(ctx.user_id, ctx.mentioned_ids)
    ctx = ctx.model_copy(update={
        "mentioned_ids": mentioned_ids,
        "mention_overflow": bool(ctx.mention_overflow or mention_overflow),
    })
    run_id = uuid.uuid4().hex
    clock = build_turn_clock(ctx.message)
    turn = build_agent_turn_context_v2(ctx, clock=clock)
    turn._raw_ctx = ctx  # type: ignore[attr-defined]
    turn._mentioned_ids = mentioned_ids  # type: ignore[attr-defined]

    # Entry: pending confirmation check
    choice = confirmation_choice(ctx.message)
    mgr = ConfirmationManager(_CONFATIONS)
    if choice == "confirm":
        results = mgr.execute_confirmed(user_id=ctx.user_id, executor=lambda tn, args, uid: _execute_tool(type("TC",(),{"name":tn,"arguments":args})(), ctx, clock=clock))
        synth_slice = slice_for_agent("synthesizer", turn, prior_observations=[{"task_id":"confirm","status":"ok","result":results}])
        reply = synthesize(synth_slice)
        return AgentResult(handled=True, reply=reply, agent_run_id=run_id, agent_mode="v3")
    if choice == "cancel":
        mgr.cancel_all(user_id=ctx.user_id)
        return AgentResult(handled=True, reply="已取消。", agent_run_id=run_id, agent_mode="v3")

    # Normal flow: Planner → execute DAG → synthesizer
    plan = plan_turn(turn, pending_confirmations=mgr.list_active(user_id=ctx.user_id))
    if plan is None:
        return AgentResult(handled=True, reply="我現在手邊資料不夠，能不能再說一次？",
                           agent_run_id=run_id, agent_mode="v3", fallback_reason="planner_invalid")

    seen_keys: set[tuple[str, str]] = set()
    step_counts: dict[str, int] = {}
    task_results: dict[str, SubTaskResult] = {}

    for layer in _topological_layers(plan):
        # In this synchronous implementation, we run layer tasks sequentially.
        # Parallelism can be added later; correctness first.
        for task in layer:
            if task.agent == "synthesizer":
                continue
            prior = [{"task_id": r.task_id, "status": r.status.value, "tool": r.tool_name,
                      "result": r.observation, "error_code": r.error_code,
                      "skip_reason": r.skip_reason}
                     for r in task_results.values()]
            result = _run_sub_task(task, turn, prior, seen_keys=seen_keys,
                                    step_counts=step_counts, on_progress=on_progress, run_id=run_id)
            task_results[task.id] = result

    # Skipped tasks: dependencies that failed
    for task in plan.tasks:
        if task.id in task_results:
            continue
        if task.agent == "synthesizer":
            continue
        task_results[task.id] = SubTaskResult(task_id=task.id, status=SubTaskStatus.SKIPPED,
                                                skip_reason="dependency_failed")

    # Synthesizer
    prior = [{"task_id": r.task_id, "status": r.status.value, "tool": r.tool_name,
              "result": r.observation, "error_code": r.error_code,
              "skip_reason": r.skip_reason}
             for r in task_results.values()]
    synth_slice = slice_for_agent("synthesizer", turn, prior_observations=prior)
    reply = synthesize(synth_slice)
    return AgentResult(handled=True, reply=reply, agent_run_id=run_id, agent_mode="v3")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest social_demotest/tests/test_v3_scheduler.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 5: Commit**

```bash
git add social_demotest/services/ayue_agent/v3/scheduler.py social_demotest/tests/test_v3_scheduler.py
git commit -m "feat(v3): add Scheduler orchestrating Planner→DAG→sub-agents→Guard→synthesizer"
```

---

## Task 9: 整合至 public_chat router + V3 flag

**Files:**
- Modify: `social_demotest/services/ayue_agent/__init__.py`
- Modify: `social_demotest/routers/public_chat.py`
- Modify: `social_demotest/.env.example`
- Test: `social_demotest/tests/test_v3_trajectories.py`

**Interfaces:**
- Consumes: `run_public_agent_turn_v3` from Task 8; `agent_mode_for_user_v3` from Task 8; existing `run_public_agent_turn`, `agent_mode_for_user` from V2

- [ ] **Step 1: Write the failing test**

```python
# social_demotest/tests/test_v3_trajectories.py
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from services.ayue_agent.contracts import AgentResult, AgentTurnContext
from services.ayue_agent.v3.contracts import Plan, SubTask, ToolProposal


class V3TrajectoryTests(unittest.TestCase):
    """End-to-end trajectory tests for the canonical steak example."""

    def test_steak_example_produces_calendar_conflict_and_places_recommendation(self):
        ctx = AgentTurnContext(
            user_id="owner", room_id="room",
            message="我這星期日想去和小李吃晚餐，想吃附近的牛排和再去吃甜點，你覺得如何？",
        )
        plan = Plan(tasks=[
            SubTask(id="t1", agent="calendar", depends_on=[], task_brief="檢查週日衝突"),
            SubTask(id="t2", agent="places", depends_on=[], task_brief="搜牛排"),
            SubTask(id="t3", agent="places", depends_on=["t2"], task_brief="搜甜點"),
            SubTask(id="t4", agent="synthesizer", depends_on=["t1", "t2", "t3"], task_brief="綜合"),
        ])
        with patch("services.ayue_agent.v3.scheduler.plan_turn", return_value=plan), \
             patch("services.ayue_agent.v3.scheduler.build_agent_turn_context_v2") as mock_build, \
             patch("services.ayue_agent.v3.sub_agents.calendar_agent.run",
                   return_value=ToolProposal(tool_name="calendar.list_my_events", arguments={})), \
             patch("services.ayue_agent.v3.sub_agents.places_agent.run", side_effect=[
                 ToolProposal(tool_name="places.search_nearby", arguments={"anchor": "三民區", "categories": ["restaurant"]}),
                 ToolProposal(tool_name="places.search_nearby", arguments={"anchor": "巖炙炭燒牛排", "categories": ["cafe"]}),
             ]), \
             patch("services.ayue_agent.v3.scheduler.execute_tool", side_effect=[
                 MagicMock(ok=True, data={"events": [{"title": "看電影", "date": "2026-08-09", "start_time": "20:00"}]}, error_code=None),
                 MagicMock(ok=True, data={"places": [{"name": "巖炙炭燒牛排", "distance_m": 726}]}, error_code=None),
                 MagicMock(ok=True, data={"places": [{"name": "某甜點店", "distance_m": 200}]}, error_code=None),
             ]), \
             patch("services.ayue_agent.v3.synthesizer.synthesize", return_value="你週日晚有電影，不過附近有巖炙牛排不錯，甜點也有，要不要改午餐？"):
            mock_build.return_value = MagicMock()
            mock_build.return_value.clock = MagicMock(model_dump=lambda: {})
            mock_build.return_value.user_id = "owner"
            from services.ayue_agent.v3.scheduler import run_public_agent_turn_v3
            result = run_public_agent_turn_v3(ctx, mode="on")
        self.assertTrue(result.handled)
        self.assertEqual(result.agent_mode, "v3")
        self.assertIn("電影", result.reply)
        self.assertIn("牛排", result.reply)

    def test_v3_mode_off_falls_back_to_v2(self):
        from services.ayue_agent.v3.scheduler import agent_mode_for_user_v3
        with patch.dict("os.environ", {"AYUE_AGENT_V3_MODE": "off"}):
            self.assertEqual(agent_mode_for_user_v3("anyone"), "off")

    def test_v3_mode_on_with_allowlist_blocks_unlisted_user(self):
        from services.ayue_agent.v3.scheduler import agent_mode_for_user_v3
        with patch.dict("os.environ", {"AYUE_AGENT_V3_MODE": "on", "AYUE_AGENT_V3_USER_ALLOWLIST": "alice,bob"}):
            self.assertEqual(agent_mode_for_user_v3("alice"), "on")
            self.assertEqual(agent_mode_for_user_v3("charlie"), "off")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest social_demotest/tests/test_v3_trajectories.py -v`
Expected: FAIL

- [ ] **Step 3: Modify `__init__.py` to export V3**

```python
# social_demotest/services/ayue_agent/__init__.py
"""Bounded, local agent runtime for the public Ayue assistant."""

from __future__ import annotations


def run_public_agent_turn(*args, **kwargs):
    from .runtime import run_public_agent_turn as _run_public_agent_turn
    return _run_public_agent_turn(*args, **kwargs)


def run_public_agent_turn_v3(*args, **kwargs):
    from .v3.scheduler import run_public_agent_turn_v3 as _run_v3
    return _run_v3(*args, **kwargs)


__all__ = ["run_public_agent_turn", "run_public_agent_turn_v3"]
```

- [ ] **Step 4: Modify `public_chat.py` to route by V3 flag**

In `social_demotest/routers/public_chat.py`, update the import block (around line 24-31) to add the V3 imports, and update the two places that call `agent_mode_for_user` to also check V3. Find this existing pattern:

```python
from services.ayue_agent import run_public_agent_turn
from services.ayue_agent.runtime import agent_mode_for_user
```

Replace with:

```python
from services.ayue_agent import run_public_agent_turn, run_public_agent_turn_v3
from services.ayue_agent.runtime import agent_mode_for_user
from services.ayue_agent.v3.scheduler import agent_mode_for_user_v3
```

Then update `direct_chat` (around line 863) and `direct_chat_stream` (around line 799) — find the existing line:

```python
public_agent_mode = agent_mode_for_user(req.user_id) if req.contact_id == "ai_assistant" else "off"
```

Replace with:

```python
v3_mode = agent_mode_for_user_v3(req.user_id) if req.contact_id == "ai_assistant" else "off"
v2_mode = agent_mode_for_user(req.user_id) if req.contact_id == "ai_assistant" else "off"
public_agent_mode = "v3" if v3_mode == "on" else ("on" if v2_mode == "on" else "off")
```

And in the stream endpoint (around line 831), find:

```python
if req.contact_id == "ai_assistant" and agent_mode_for_user(req.user_id) == "on":
```

Replace with:

```python
if req.contact_id == "ai_assistant" and (agent_mode_for_user_v3(req.user_id) == "on" or agent_mode_for_user(req.user_id) == "on"):
```

Then in the call site (around line 639), find:

```python
agent_result = run_public_agent_turn(
```

Replace the call to check V3 first:

```python
if v3_mode == "on":
    agent_result = run_public_agent_turn_v3(
        agent_ctx, mode="on", on_progress=on_progress if streaming else None,
    )
else:
    agent_result = run_public_agent_turn(
        agent_ctx, mode="on", on_progress=on_progress if streaming else None,
    )
```

- [ ] **Step 5: Add V3 flags to `.env.example`**

Append to `social_demotest/.env.example`:

```
# V3 sub-agent runtime (replaces V2 loop when on)
AYUE_AGENT_V3_MODE=off
AYUE_AGENT_V3_USER_ALLOWLIST=
AYUE_SUBAGENT_MAX_READS=3
AYUE_SUBAGENT_TIMEOUT_MS=
```

- [ ] **Step 6: Run test to verify it passes**

Run: `python -m pytest social_demotest/tests/test_v3_trajectories.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 7: Run all V3 tests together**

Run: `python -m pytest social_demotest/tests/test_v3_*.py -v`
Expected: PASS (all V3 tests)

- [ ] **Step 8: Run existing V2 tests to confirm no regression**

Run: `python -m pytest social_demotest/tests/test_ayue_agent_planner_sequences.py social_demotest/tests/test_ayue_agent_registry.py social_demotest/tests/test_ayue_agent_v2_policy.py -v`
Expected: PASS (V2 tests unaffected)

- [ ] **Step 9: Commit**

```bash
git add social_demotest/services/ayue_agent/__init__.py social_demotest/routers/public_chat.py social_demotest/.env.example social_demotest/tests/test_v3_trajectories.py
git commit -m "feat(v3): integrate V3 sub-agent runtime into public_chat with flag-based routing"
```

---

## Task 10: 更新 AYUE_V2_ARCHITECTURE.md 附錄

**Files:**
- Modify: `AYUE_V2_ARCHITECTURE.md`

- [ ] **Step 1: Append V3 appendix to the end of `AYUE_V2_ARCHITECTURE.md`**

Add at the end of the file:

```markdown

## 14. V3 Sub-agent 架構（附錄）

V3 是取代 V2 單一 loop 的 sub-agent 架構，以 `AYUE_AGENT_V3_MODE=on` 啟用。V2 保留為緊急 rollback。

### 角色與流程

- **Scheduler**（純程式碼）：管理 confirmation 入口、執行順序、平行、context slice 分配、步數計數
- **Planner**（輕量 LLM）：只拆任務產靜態子任務 DAG，不填 args、不審核、不回覆
- **6 個 Sub-agent**（LLM + function calling）：calendar / places(+web) / match / relationship / profile / synthesizer。各自最多 3 次唯讀
- **中央 Guard**（純程式碼）：驗 proposal 的 schema、args 安全、重複、步數、寫入 confirmation
- **Synthesizer**（LLM）：綜合所有 observation 產出回覆，不再 call function

### 與 V2 的相容性

- `/api/direct_chat` JSON 與 `/api/direct_chat/stream` NDJSON contract 不變
- NDJSON public event 維持 `run_started`、`tool_started`、`tool_finished`、`final`、`error`
- `AYUE_AGENT_V2_MODE=off` + `AYUE_AGENT_V3_MODE=on` = V3 運行
- `AYUE_AGENT_V2_MODE=on` + `AYUE_AGENT_V3_MODE=off` = V2 運行（現狀）
- V3 失敗不自動回 V2；rollback 是人工切換 flag

### V3 專屬 flags

| Flag | 用途 |
| --- | --- |
| `AYUE_AGENT_V3_MODE=on\|off` | Sub-agent 架構或緊急 rollback |
| `AYUE_AGENT_V3_USER_ALLOWLIST` | 漸進式指定使用者 |
| `AYUE_SUBAGENT_MAX_READS` | 每個 sub-agent 唯讀上限，預設 3 |
| `AYUE_SUBAGENT_TIMEOUT_MS` | 單一 sub-agent LLM 呼叫逾時 |

### 設計文件

完整 V3 設計見 `docs/superpowers/specs/2026-08-04-sub-agent-architecture-design.md`。
```

- [ ] **Step 2: Commit**

```bash
git add AYUE_V2_ARCHITECTURE.md
git commit -m "docs: add V3 sub-agent architecture appendix to AYUE_V2_ARCHITECTURE.md"
```

---

## Self-Review

**Spec coverage check:**
- §1 設計動機 → Task 9 trajectory test 驗證牛排例子
- §2 整體架構與角色 → Task 8 Scheduler 整合所有角色
- §3 六個 sub-agent → Task 5 實作 5 個 domain agents + Task 7 synthesizer
- §4 Planner DAG → Task 6 Planner 產 Plan
- §5 中央 Guard → Task 2 Guard
- §6 步數額度 → Task 2 Guard 驗 step_count + Task 8 Scheduler 維護 step_counts
- §7 Confirmation / 失敗處理 → Task 4 ConfirmationManager + Task 8 Scheduler skip 邏輯
- §8 Context 隱私邊界 → Task 3 context_slicer
- §9 Trace / public event → Task 8 Scheduler 維持既有 NDJSON event 介面（on_progress callback）
- §10 Runtime flags → Task 9 `.env.example` + `agent_mode_for_user_v3`
- §11 測試 → 每個 Task 都有對應測試
- §12 檔案結構 → 所有檔案在 Task 中建立

**Placeholder scan:** 無 TBD/TODO；每個 step 都有實際程式碼。

**Type consistency:** `SubTask`、`Plan`、`ToolProposal`、`GuardDecision`、`AgentContextSlice`、`SubTaskResult` 在 Task 1 定義，後續 Task 使用相同名稱與欄位。`guard_proposal` 簽名在 Task 2 與 Task 8 呼叫一致。`slice_for_agent` 簽名在 Task 3 與 Task 8 呼叫一致。`run_public_agent_turn_v3` 簽名在 Task 8 與 Task 9 呼叫一致。

無遺漏，計畫完整。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-08-04-sub-agent-architecture.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?

> ARCHIVED / HISTORICAL PLAN: not current runtime instructions. See AGENTS.md and AYUE_V3_ARCHITECTURE.md.
