# 03. 公開阿月 V3 Runtime 生命週期

> 本篇說明「使用者送出一句話到阿月回覆」之間發生的一切：每個階段的 owner、產物與失敗行為。程式碼真相在 `social_demotest/services/ayue_agent/v3/`。

## 1. 入口與 HTTP 契約

公開阿月 UI 使用 `POST /api/direct_chat/stream`（NDJSON）；其他聯絡人維持 `POST /api/direct_chat`（JSON）。兩個端點由 `routers/public_chat.py` 提供，request body 都是 `DirectChatRequest`：

```json
{
  "user_id": "owner",
  "contact_id": "ai_assistant",
  "message": "他回覆了沒？",
  "chat_type": "direct",
  "mentioned_other_id": null,
  "mentioned_other_ids": []
}
```

**公開 stream 允許的 NDJSON event 只有**：`run_started`、`tool_started`、`tool_finished`、`final`、`error`。`_sanitize_public_stream_event` 會把其他 event 丟棄；arguments、tool result、prompt 與 debug content 不會跨 HTTP 邊界外送。

JSON 與 stream 的 final response 都保留 `reply`，並可附加 bounded `messages`（最多三則）。儲存仍是一回合一筆 assistant message；多氣泡只以 `presentation_messages` metadata 作為 UI projection。

實際呼叫鏈：

```text
direct_chat (routers/public_chat.py)
  → _complete_public_turn
      - 保存使用者訊息（唯一一次）
      - 組 AgentTurnContext（含 recent_history ≤12 則、user_profile、提及清單）
  → run_public_agent_turn_v3 (services/ayue_agent/__init__.py → v3/scheduler.py)
  → save_message(ai_assistant 回覆)（唯一一次）
  → 背景 queue_profile_skills（僅非 assessment 回合）
```

## 2. Scheduler：一回合的階段

`scheduler.py:run_public_agent_turn_v3` 是公開阿月**唯一 orchestrator**。回合開始先建 `run_id`、`TurnClockV1`（台北時間＋相對日期解析）與 allowlisted trace 骨架，然後依序走以下階段：

### 階段 0：特殊入口（不跑 Planner）

1. **Assessment commit**：若存在 `awaiting_assessment_commit` 的探索 session，只接受封閉的「確認／取消」協議，否則提示；逾期則 expire。
2. **Active assessment session**：存在進行中的基本／深層探索時，任何訊息都作為該 session 的答案（`advance_assessment_session`），不跑 Planner。
3. **Confirmation**：`confirmation_choice(message)` 解析封閉確認協議；一般寫入接受「確認／好」等既有確認字串，assessment start 另接受限定的「開始／開始吧／開始啊／開始阿」。確認時由 `ConfirmationManager.execute_confirmed` 執行最新一筆 pending confirmation。Calendar Agent 的多個 typed commands 在建立時已合併為一個 server-owned plan，確認後依序執行；取消時清除該使用者全部 pending。

### 階段 1：Planner 拆解（LLM）

`planner.py:plan_turn` 用單一 `decompose_tasks` function calling，模型輸出的 tool call arguments 就是 typed `Plan`。Planner **只輸出靜態子任務 DAG**：每個 `SubTask` 有 `id`、`agent`（calendar/places/web/match/relationship/profile/synthesizer）、`depends_on`、`task_brief`。

`Plan` 的 DAG 驗證（`contracts.py`，純程式碼）：

- id 不可重複；`depends_on` 必須指向存在的 id。
- synthesizer 必須是終端：不能被任何其他 task 依賴。
- Planner 另可輸出 `opportunity`（`signal=social_opening` + `evidence_span` + `confidence≥0.8`），Scheduler 會再驗證 evidence_span 是原句連續子字串。這是非動作性的短期溫和提議，不會建立 pending confirmation；明確的開始／重試配對請求必須產生 `match` task，走一般 confirmation path。

Planner 無效（無 tool call、名字錯誤、schema 不符、逾時）→ **fail closed**：回「我現在沒辦法判斷這個請求要不要執行」，不執行任何工具。

產品身份／入口問題使用 typed `mode="product_info"` 與最多三個
`product_info_topics`，由 Scheduler 直接投影 versioned capability manifest；
不使用自然語言 regex 重新路由，也不建立 domain task。Public 與 Private 是
同一位阿月的不同 bounded surface，Private 訊息不自動回流 Public profile/memory。

### 階段 2：拓撲分層執行 sub-agents

`_topological_layers` 依 `depends_on` 把 task 分成層；同層用 `ThreadPoolExecutor` 平行執行（`AYUE_SUBAGENT_MAX_PARALLEL` 預設且硬上限 2），有依賴的層依序執行。

每個 task 由 `_run_sub_task` 執行：

1. `slice_for_agent(task.agent, turn, prior_observations)` 切出該 agent 的 privacy-safe context slice（見下方 §4）。
2. 呼叫該 sub-agent（如 `sub_agents/calendar_agent.py`），內部是 `base.py:run_sub_agents`：組 tools（依 registry schema）、組 prompt、`generate_chat_completion_with_tools(temperature=0)`。
3. 解析**所有** tool calls 成 `ToolProposal`（模型一次回覆多個呼叫時全部保留，一個失敗不丟棄其他）；`places.search_nearby` 的 categories 會先做 deterministic repair。
4. 每個 proposal 依序：Guard → 注入 executor 參數 → 去重檢查 → 執行工具。

依賴失敗的 task 會被標記 `SKIPPED (dependency_failed)`，不會執行。

### 階段 3：Central Guard（純程式碼）

`guard.py:guard_proposal` 是唯一把關者，完全不呼叫 LLM，依序檢查：

| 檢查 | GuardResultCode |
| --- | --- |
| 工具存在於 registry | `tool_not_registered` |
| planner arguments 符合該工具的 planner schema（`planner_arguments_allowed`） | `schema_invalid` |
| 相同 tool+arguments 是否已執行過（`tool_call_key`，以 executor 參數為準） | `duplicate_call` |
| 每個 sub-agent 的唯讀步數上限（`AYUE_SUBAGENT_MAX_READS`，預設 3） | `step_limit_exceeded` |
| 寫入工具不得直接執行，必須建立 confirmation | `write_requires_confirmation` |

Guard 通過後各 runtime 再做三道 runtime 檢查：

- `@` 系工具（`MENTIONED_RELATIONSHIP`/`MENTIONED_CONTACTS`）若本回合沒有 server 驗證過的 mention id → `mentioned_required` 失敗。
- `reuse_success_within_turn`（如 `places.measure_distance`）：同 sub-task 內已有成功的相同結果時重用 observation，不再讀一次。
- `web.extract` 的 URL 必須是「本回合搜尋結果或使用者提供」的受信任 URL；Web Runtime 透過 `v3/guarded_execution.py:web_extract_urls_allowed` 綁定。

### 階段 4：Typed Tool 執行

唯讀工具經 `tools.py:execute_tool` 執行：依 `executor_key` 分派到對應 facade（calendar/match/profile/relationship/web/places），facade 內用 domain service 讀 canonical 資料，輸出必須通過該工具的 `output_model` 驗證（`extra="forbid"`），失敗回 `invalid_tool_output`。

一般寫入工具在此階段**不會執行**：Guard 回 `write_requires_confirmation`。Calendar Agent 的 `calendar.submit_commands` 由 Calendar Runtime 先做 deterministic preflight（使用 authoritative clock、必要時只 resolve 一次、查衝突、組 preview），產生不暴露給 LLM 的 `CalendarMutationPlan`；舊的 direct Calendar proposal 不再註冊，若由過期 confirmation 送入則 fail closed。在 `v3_pending_confirmations` 插入 pending 紀錄（TTL 900 秒），把 `pending_confirmation` observation 交給 Synthesizer 向使用者確認。missing_fields、invalid_date、ambiguous、not_found、too_many、invalid_interval 不建立 confirmation，而是正常 clarification。

### 階段 5：Synthesizer（LLM）

`synthesizer.py:synthesize` 收集所有非 SKIPPED 的 observation，經 `_strip_place_internals`（移除 map_url/place_id/photo_url 等內部欄位）後組成 prompt；server-owned confirmation reply、typed calendar clarification、capability answer 與 assessment domain reply 先 deterministic 直出，不再交給 LLM 改寫；其他結果才由模型產出回覆。若候選地點卡存在，模型需以 `decide_place_cards` tool call 決定 `show_all/select/none`；沒有候選卡片時 `tools=[]`、`tool_calls=[]` 是正常結果。Scheduler 把卡片決策套用回 server-side 投影，作為 `place_cards` 回傳。

Web-only 的 `web_research.v1` 若為 `answered`，或為含有 useful direct findings 的 `partial`，會進入同一個 Synthesizer composition path，允許自然 prose 或輕量 Markdown；不再固定輸出 deterministic headings。`insufficient_evidence`、`unavailable`、沒有 useful findings，以及 provider/model/safety failure 仍使用 deterministic Web fallback。Synthesizer 只收到該 typed Web result 作為 Web-only grounding；source URLs 與 `web_source_*` refs 由 server-owned metadata 提供，模型不得新增未觀察到的連結或 reference。

Synthesizer 也會套用 `capabilities.py` 的用詞真相（不得宣稱「隨機配對」）與 `_concise_public_reply` 清理。`SynthesizerMetrics.reply_source` 區分 `capability`、`verified_observation`、`llm` 與 fallback；provider error、空內容或被拒絕的模型內容會標成 `degraded`，不得顯示為成功。

### 階段 6：結果與 Trace

`AgentResult` 回傳（reply、sources、place_cards、llm_call_metrics、assessment 狀態等）；Final reply 必須與 Synthesizer 節點結果相同。Trace 只存 allowlisted metadata（plan 摘要、guard codes、tool result codes、event sequence、latency、Synthesizer fallback code），**不含 prompt、原句、arguments、tool result、ID、revision 或 raw exception**。

## 3. 寫入確認流程（confirmation）

```text
Calendar Agent 提出 `calendar.submit_commands` typed command batch
  → command guard（只驗 schema 與 authority-free contract）
  → Calendar Runtime: deterministic calendar preflight（resolve/衝突/preview；canonical target 只解析一次）
      → needs_clarification/denied: observation = calendar_command_result（Synthesizer 向使用者追問或說明權限）
      → 成功: 寫入 v3_pending_confirmations {status: pending, expires_at: +900s}
  → Synthesizer 回覆 preview + 「回覆『確認』才會真的變更」
使用者回覆「確認」
  → Scheduler: ConfirmationManager.execute_confirmed
      - CAS: pending → executing（修改數 0 代表已被其他 worker 取走，跳過）
      - calendar plan 共用一個 confirmation，確認後 sequential execution、stop-on-failure、無 automatic rollback
      - execute_write(tool, args, ctx, turn, run_id, index, payload)
      - 完成後寫 completed/failed，附 result
```

`execute_write`（`write_executors.py`）是**已確認寫入的唯一執行路徑**，內部呼叫 canonical domain service：

| 工具 | Executor | Domain service |
| --- | --- | --- |
| `match.start_search` | `_start_search` | `match_action_service.start_match_search` → `enqueue_match_search` |
| `match.decide_active_proposal` | `_decide_active_proposal` | `match_action_service.decide_active_proposal`（revision CAS） |
| `profile.start_assessment` | `_start_assessment` | `assessment_session_service.start_assessment_session` |
| `calendar.submit_commands` | `_execute_calendar_mutation_plans`／`_calendar_execute` | `calendar_service` / `date_coordination_service` |

冪等性：`_claim_once` 以 `idempotency_key`（`confirmation:{id}` 或 `{run_id}:{index}`）在 `agent_tool_calls` 做 `$setOnInsert` upsert，重放直接回「已處理過」；calendar 每筆另帶 `agent_action_key`。Stale（revision 不匹配）回報最新狀態且不覆寫終態（HTTPException 409 → `stale_revision`）。

## 4. Context Slice（每個 agent 看到什麼）

`context_slicer.py:slice_for_agent` 決定各 agent 的可見欄位：

| agent | payload 欄位 |
| --- | --- |
| calendar | message、recent_messages、clock、recent_context、calendar_draft、calendar_recent_reference、prior_observations |
| places | message、recent_messages、user_location、clock、prior_observations |
| web | message、最近 4 則 bounded messages、user_location、clock、prior_observations |
| match | message、recent_messages、active_proposal、latest_match_outcome、clock、prior_observations |
| relationship | message、recent_messages、mentioned_contacts、mentioned_contact_overflow、clock、prior_observations |
| profile | message、recent_messages、recent_context、relevant_memories、clock、prior_observations |
| synthesizer | message、recent_messages、recent_context、user_location、clock、observations |

`PublicAgentTurnContext`（`context.py:build_public_agent_turn_context` 組建）的總體限制：最近 12 則訊息、合計 6000 字元；近期記憶最多 8 筆；prompt 不含 `seed_user_*`、Mongo document、未公開 ID、對方私人記憶或行事曆內容。

## 5. 背景流程（非同步）

- **Profile extraction**：`public_chat.py` 在回合結束後把已保存的 owner 訊息排入 `profile_task_service` → `profile_skills.py`，message_id 去重，evidence 必須是原句連續子字串；assessment 答案不會進入此 pipeline。
- **配對搜尋 job**：`match.start_search` 確認後入 `match_search_jobs` 佇列，`match_search_worker` 消費（claim/lease/progress），完成後呼叫媒婆 9001 `/api/match`，產出唯一 draft proposal，並以 mediator event 通知。
- **主動關心**：`proactive_scheduler` 每 15 秒掃描到期使用者，`proactive_care.py` 依最近訊息、前一則回覆、近期情境與口吻產生 care；atomic claim 保證每個 owner activity 最多一則 care。

## 6. 失敗行為總表

| 情境 | 行為 |
| --- | --- |
| Planner 無效/逾時 | fail closed，直接回覆，不執行工具 |
| sub-agent 無 tool call | `sub_agent_no_proposal`（Calendar command 缺欄位、歧義、找不到目標則由 preflight 回 `needs_clarification`，不是此 generic failure） |
| sub-agent exception | `sub_agent_exception`，其他 task 照跑 |
| Guard 拒絕 | 該 proposal 標記失敗 code，其他 proposal 不受影響 |
| 工具失敗 | 回 `error_code`，observation 不進 Synthesizer 的成功集 |
| 寫入 preflight 失敗 | `preflight_rejected`，Synthesizer 追問 |
| confirmation 逾期 | 執行時視同失效（expires_at 過期不再列為 active） |
| stale revision | 回報最新狀態，不覆寫終態 |
| effect 失敗（通知等） | transition 已提交，effect 失敗不重送 |


> Current lifecycle: Public requests always enter this Scheduler；rollback 只能透過 deployment／commit rollback。
## Current Web Agent lifecycle

Every registered runtime uses the same internal `TaskRunnerResult` and
`run(context_slice, *, task, services)` signature. Proposal runners return
guarded proposals; Calendar, Web, and ProductInfo runtimes return completed
`SubTaskResult` values. `RuntimeRegistration` carries optional direct-chat and
confirmed-result projections, so Scheduler does not branch on domain result
shapes.

When the Planner emits `agent="web"`, Scheduler dispatches the registered
`v3/web_runtime.py` runner and collects one typed result. Web Runtime returns
each bounded `web.search`/`web.extract` observation to the Web Agent before the
next decision, then emits one typed `web_research.v1` result for Synthesizer.
The runtime owns a research/tool phase with at most three tool-producing calls
and a separate bounded finish-only phase. It keeps the existing two initial
searches, one refinement search, and one extract step over two URLs. Decision or
model failures do not consume tool budget; when observations exist, the
finish-only phase still gets its final decision opportunity and cannot execute a
new Web tool.
Evidence is graded against the original answer target; adjacent-only or
conflicting evidence cannot become an answered claim. Missing credentials,
model failure, and no direct evidence have separate typed outcomes.

### Places -> Web candidate collaboration

For current/public criteria that typed Places data cannot answer, the Planner
may emit `places -> web -> synthesizer`. Scheduler projects at most five
validated Places candidates into the Web slice with ephemeral
`place_candidate_*` refs. Web search, extraction, findings, and final card
selection retain the same ref; provider IDs and map internals never enter the
Web prompt or public response. Synthesizer receives all non-skipped domain
observations even when the terminal dependency is only the Web task.

Retrieval count and display count are separate. Up to eight Places candidates
may be retrieved, while a normal grounded recommendation selects two or three
cards (maximum four unless the user explicitly requests more). Missing Web
evidence preserves an explicit limitation and does not hide all Places cards.
