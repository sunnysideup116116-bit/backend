# 公開阿月 V3：現行架構

> 本文件只描述目前程式，不保存已完成的 Public V1/V2 migration 計畫或舊 prompt 快照。層與層之間的精確 typed interface 請見 [`docs/architecture/09-runtime-interfaces.md`](./docs/architecture/09-runtime-interfaces.md)；各 domain 行為請見 `docs/architecture/subagent-*.md`。

## 1. Current baseline

- Public Ayue 永遠走 V3 sub-agent runtime；唯一 orchestrator 是 `social_demotest/services/ayue_agent/v3/scheduler.py`。
- Public 失敗時 fail closed。Rollback 只能透過 deployment／commit rollback，不存在 request-level legacy fallback、rollout allowlist 或第二套 public router。
- Private Ayue 是仍在使用、與 Public 隔離的 current V2 runtime，由 `routers/private_mediator.py`、`services/ayue_agent/private_v2.py` 與 `private_calendar.py` 擁有。
- `public-v1`、`web_research.v1`、`product_info.v1`、`TurnClockV1` 等名稱是 typed payload/schema version，不是 Public V1 runtime。
- `runtime.py`、`legacy_match_routing.py`、`private_runtime.py` 與舊 Public 單一-loop prompt 文件均已移除；不得恢復。

## 2. 系統定位

公開阿月是交友 App 內協助使用者認識人、牽線與整理行動的 AI 媒人，不是另一位使用者，也不是外部服務。產品身份與語氣的程式真相位於 `services/ayue_agent/product_identity.py` 與 `capabilities.py`。

```text
HTTP adapter
  -> Context Builder
  -> Planner（靜態 DAG）
  -> Runtime registrations / Sub-agents
       -> Central Guard
       -> Typed tools / domain services
  -> Verified observations
  -> Synthesizer
  -> AgentResult / Final
```

核心分工：

- LLM：理解語意、拆任務、提出 authority-free tool proposal、根據 observation 組回覆。
- 程式：ownership、schema、budget、URL/mention binding、confirmation、CAS、idempotency、privacy projection 與 trace allowlist。
- Domain service：canonical read/write truth；Router、Scheduler、Agent 與 UI 不重複實作同一狀態轉移。

## 3. Repository owners

| Owner | 路徑 | 責任 |
| --- | --- | --- |
| Public HTTP | `social_demotest/routers/public_chat.py` | JSON/NDJSON adapter、訊息唯一保存、mention 驗證、呼叫 V3 |
| Context | `services/ayue_agent/context.py` | 建立唯一 prompt-safe `PublicAgentTurnContext` |
| Planner | `services/ayue_agent/v3/planner.py` | `decompose_tasks` function calling，輸出 direct chat 或靜態 DAG |
| Contracts | `services/ayue_agent/v3/contracts.py` | `Plan`、`SubTask`、`ToolProposal`、`SubTaskResult` |
| Orchestrator | `services/ayue_agent/v3/scheduler.py` | 特殊入口、DAG、registration dispatch、共用 budgets、confirmation、trace、final |
| Runtime interface | `services/ayue_agent/v3/runtime_registry.py` | `RuntimeRegistration`、`TaskRunnerResult` |
| Guard adapter | `services/ayue_agent/v3/guarded_execution.py` | Guard→executor args→typed tool；Web URL binding |
| Calendar runtime | `services/ayue_agent/v3/calendar_runtime.py` | clarification、draft/reference、reads、commands、preflight、confirmation preparation |
| Web runtime | `services/ayue_agent/v3/web_runtime.py` | bounded research/finish phases 與 `web_research.v1` assembly |
| ProductInfo | `services/ayue_agent/v3/sub_agents/product_info_agent.py` | bounded allowlisted product knowledge 與 `product_info.v1` observation |
| Guard | `services/ayue_agent/v3/guard.py` | 純程式碼 registry/schema/duplicate/budget/write-confirmation validation |
| Tool registry | `services/ayue_agent/tool_registry.py` | ToolSpec、三層 schema、risk、argument source、progress |
| Tool reads | `services/ayue_agent/tools.py` | 唯讀 capability facade 與 typed output projection |
| Confirmed writes | `services/ayue_agent/v3/write_executors.py` | 已確認寫入的唯一執行入口 |
| Synthesizer | `services/ayue_agent/v3/synthesizer.py` | 根據 verified observations 產出 user-facing response |
| Public debug | `services/ayue_agent/v3/debug_trace.py` | localhost-only ephemeral debug events |

## 4. Public request lifecycle

### 4.1 HTTP input

Public UI 使用 `POST /api/direct_chat/stream`；`POST /api/direct_chat` 保留 JSON contract。`routers/public_chat.py`：

1. 驗證 public contact 與最多三個 accepted mentions。
2. 保存 owner message 一次。
3. 組 `AgentTurnContext`，呼叫 `run_public_agent_turn_v3()`。
4. 保存 final assistant message 一次。
5. 非 assessment owner message 才背景排入 profile extraction。

### 4.2 Planner 前的特殊入口

Scheduler 先處理：

- assessment commit／active session；
- typed `assessment_action="cancel"`；
- pending confirmation 的封閉「確認／取消」協議；
- 逾期、stale 或已被其他 worker claim 的 confirmation。

這些狀態不進 Planner，避免一般對話重新解讀 authority-bearing action。

### 4.3 Planner

Planner 只呼叫 `decompose_tasks`：

- `mode="direct_chat"`：不需要 App/domain/private/external truth 的 bounded conversational reply。
- `mode="tasks"`：1–4 個 `SubTask`，最多三個 domain tasks，恰好一個 terminal synthesizer。

Agent allowlist：

```text
calendar | places | web | match | relationship | profile | product_info | synthesizer
```

`task_brief` 保存目標與限制，不是 tool arguments。只有 Web 可帶 `evidence_policy=casual_discovery|strict_verification`。

舊 provider 可能送出 task-free `mode="product_info"`／`product_info_topics`；compatibility boundary 只把它正規化成 `product_info -> synthesizer` DAG。新 Planner、fixture 與文件不得產生舊 envelope。

### 4.4 DAG execution

Scheduler 依 dependency 做 topological layers；同層最多平行 2 個 task。每個 domain task：

1. `context_slicer.py` 產生最小 `AgentContextSlice`。
2. 透過 `RuntimeRegistration` 呼叫統一 runner。
3. Runner 回 `TaskRunnerResult.proposals` 或 `completed_results`，兩者恰好一種。
4. Proposal 逐筆走 Central Guard 與 typed tool；completed result 由 owning specialist runtime 完成 bounded interpretation/assembly，Scheduler 不重做 domain 判斷。
5. 依賴沒有成功 observation 的下游 task 標記 `SKIPPED/dependency_failed`。

正式 runner interface：

```python
run(context_slice, *, task, services)
    -> tuple[TaskRunnerResult, SubAgentMetrics | None]
```

目前 completed specialist runtimes 是 Calendar、Web、ProductInfo；Places、Match、Relationship、Profile 使用 proposal runner。

### 4.5 Final composition

一般 DAG 的 user-facing wording 只由 Synthesizer 產生。例外只限 server-owned deterministic outputs：confirmation、confirmed result、typed clarification、assessment lifecycle、安全/錯誤 fallback。

Synthesizer 只能使用本回合 verified observations。Map URL、source URL、place candidate ref、Web source ref 由 server-owned catalog 綁定；模型不能新增未觀察到的 reference。

`compose_public_reply.presentation_class` 的正式 enum 不變。Synthesizer boundary 只對 provider drift 做窄幅相容：舊的 `itinerary` 正規化為 `grounded_recommendation`，其他未知值改用既有安全預設 `conversation`，避免丟棄其餘已通過驗證的自然文字。這項相容不放寬 candidate refs、Web URL/source refs、internal IDs 或 mutation authority 的驗證。

## 5. Context 與 privacy

`services/ayue_agent/context.py` 是 Public Context 唯一 owner：

- recent messages ≤12；總字元 ≤6,000；relevant memories ≤8。
- 不輸出 raw Mongo/Neo4j document、`seed_user_*`、未公開 ID、對方私人記憶或對方行事曆。
- 只有已接受關係且 server 驗證的 public display projection 能進 context。
- Calendar、Web、ProductInfo 等 agent 只收到 `context_slicer.py` 明列欄位。
- ProductInfo 不收到 owner profile/calendar/relationship private state。

Public 與 Private 不交換完整 prompt/history。Private 只讀其 current accepted room 允許的 bounded relationship context；Private owner messages不自動進 Public profile/memory pipeline。

## 6. Tool Registry、Guard 與 budgets

所有公開 capability 必須先註冊 `ToolSpec`：

```text
name / risk / executor_key / description / progress_text
planner_arguments_model / executor_arguments_model / output_model
argument_source / confirmation / reuse policy
```

模型永遠不能提供 `user_id`、match/proposal/event ID、revision 或 expected status。

Guard 檢查：

- registered tool；
- planner schema；
- shared Public-run duplicate key；
- per-task read budget（預設 3）；
- WRITE 一律回 `write_requires_confirmation`。

同一 `tool + normalized executor arguments` 在同一 Public run 不重跑。每回合最多建立一筆 pending side effect。

目前 22 個 capabilities：18 READ、4 WRITE。完整表見 [`docs/architecture/04-tool-registry.md`](./docs/architecture/04-tool-registry.md)。

## 7. Confirmation 與 writes

```text
WRITE proposal / Calendar command batch
  -> schema + domain preflight
  -> pending confirmation（TTL 900s，preview-bound）
  -> owner 確認
  -> ConfirmationManager CAS pending -> executing
  -> write_executors
  -> canonical domain service（CAS + idempotency）
```

目前 WRITE capabilities：

- `match.start_search`
- `match.decide_active_proposal`
- `profile.start_assessment`
- `calendar.submit_commands`

Calendar mutation 只使用 `calendar.submit_commands`。一至十筆 authority-free commands 可形成一筆 confirmation；Calendar Runtime resolve canonical target 一次，建立不進 LLM 的 `CalendarMutationPlan`。舊 create/update/cancel tool names 不再註冊，stale confirmation fail closed。

## 8. Domain specialists

### Calendar

擁有本人的行程 reads、typed mutation commands、15 分鐘 draft/recent-reference、deterministic preflight 與 post-mutation verification。不可讀對方行事曆。共同約會的 mutation 仍由 `date_coordination_service.py` 處理雙方同步與通知。

### Places

只擁有 structured nearby search、distance 與 place cards。Search radius 對 OSM 與 Google 都是 hard bound；保存位置只允許粗粒度城市／行政區。

### Web

Web Runtime 擁有 research/finish phases，research 最多三個 tool calls、extract 最多一次；finish-only phase 不消耗 Web tool budget。Web Agent 只依 `phase`／`available_actions` 行動，`round_index` 僅供診斷。

Search rows 有獨立 projection cap；達到 search cap 不會停止掃描後續 observations，因此 late extract 仍可進 finalization。URL 必須綁定本回合搜尋結果或 owner 原句提供的安全公開 URL。

### ProductInfo

ProductInfo 是 first-class read-only DAG specialist。它最多兩輪、最多六個 allowlisted knowledge sections，輸出 `product_info.v1` observation；section hints 只屬其內部 retrieval，不是 Planner taxonomy 或全域 keyword router。Progress 使用 `product_info.process` 虛擬步驟；它不是 Tool Registry capability，也不消耗 Web/tool budget。

### Match / Relationship / Profile

- Match 讀 canonical proposal/status，搜尋與決策走 confirmation/CAS。
- Relationship 只讀 accepted relations 與 server 驗證 mentions 的 public projection。
- Profile 讀本人資料與 assessment start；owner memory extraction 由獨立 `profile_skills.py` pipeline 擁有，不由聊天 agent 寫入。

## 9. Match state

Canonical lifecycle：

```text
draft -> pending -> accepted
             \-> declined
draft/pending -> expired
```

- 只有 live `draft/pending` 阻擋新的 active proposal。
- `accepted` 是已建立聯絡關係，不是 active proposal。
- `match_decision_service.py` 擁有 status+revision CAS；stale 回最新狀態，不覆寫終態。
- `match_action_service.py` 只在 transition 成功後執行通知、聊天室、feedback 等 effects；effect 失敗不讓已提交 transition 被重送。

## 10. Profile、Memory 與 Context Engine

Owner message pipeline：

```text
saved owner message
  -> profile_skills.py typed extraction + contiguous evidence span
  -> memory_service.apply_profile_memory_proposals
  -> matchmaker /api/memory/apply
  -> Neo4j owner-scoped durable memory
  -> Mongo profile_memory_preview（read projection only）
```

- assistant reply、conversation history、tool result、match state 或對方資料不能成為 profile write source。
- 已移除 `/api/memory/observe` 與主服務 direct Neo4j fallback；9001 failure 進 bounded outbox，不能改抓 raw data。
- `semantic_plans`／room chat triples 是雙人關係 context，不自動轉成任一方 durable preference。
- 未來 Context Engine 只能輸出 bounded versioned typed bundle，先做 owner/room/accepted-relation hard isolation，再做 ranking/budget/dedup。

完整規則見 [`MEMORY_CONTEXT_ENGINE_GUIDE.md`](./MEMORY_CONTEXT_ENGINE_GUIDE.md)。

## 11. HTTP、UI、progress 與 debug

Public NDJSON 對外 event allowlist：

```text
run_started | tool_started | tool_finished | final | error
```

- Public UI 只顯示一個暫時 progress bubble；final/error/disconnect 時清除。
- Progress、debug event、tool name 不存成聊天訊息。
- `reply` 保留相容性；`messages` 最多三個；`place_cards`、`sources`、`presentation_blocks` 是 additive typed projection。
- Debug 只對 loopback/localhost 開放，使用 `agent_run_id` 關聯 ephemeral run。Public trace 只存 allowlisted metadata，不存 prompt、owner 原句、arguments/result、raw exception、ID 或 revision。

## 12. Testing baseline

所有行為修改必須有 deterministic test；自動測試不得連線或修改正式 MongoDB Atlas、Neo4j、Tavily 或 Google API。

最低覆蓋：

- Planner function schema、DAG、routing 與 fail closed。
- `RuntimeRegistration`／`TaskRunnerResult` contract。
- Context slices 與 Public/Private privacy isolation。
- Guard codes、tool schema/output、duplicate、budgets。
- Confirmation、CAS、idempotency、stale、concurrency。
- Web research/finish、late extract projection、URL/source/subject binding。
- Calendar command/preflight/draft/reference/verification。
- ProductInfo retrieval/observation/progress/debug。
- JSON/NDJSON compatibility、trace/event allowlists。

標準指令見 [`docs/architecture/06-testing.md`](./docs/architecture/06-testing.md)。

## 13. Current documentation index

- [`docs/architecture/01-project-overview.md`](./docs/architecture/01-project-overview.md)：onboarding 與服務邊界。
- [`docs/architecture/02-python-modules.md`](./docs/architecture/02-python-modules.md)：模組 owner map。
- [`docs/architecture/03-v3-runtime-lifecycle.md`](./docs/architecture/03-v3-runtime-lifecycle.md)：單回合 lifecycle。
- [`docs/architecture/04-tool-registry.md`](./docs/architecture/04-tool-registry.md)：22 個 ToolSpec。
- [`docs/architecture/05-matchmaker-and-memory.md`](./docs/architecture/05-matchmaker-and-memory.md)：9001、Neo4j 與 profile memory pipeline。
- [`docs/architecture/06-testing.md`](./docs/architecture/06-testing.md)：測試策略。
- [`docs/architecture/07-guard.md`](./docs/architecture/07-guard.md)：Central Guard。
- [`docs/architecture/08-planner.md`](./docs/architecture/08-planner.md)：Planner DAG contract。
- [`docs/architecture/09-runtime-interfaces.md`](./docs/architecture/09-runtime-interfaces.md)：HTTP、Context、Planner、runner、Tool/Guard、write 與 observation interfaces。
- `docs/architecture/subagent-*.md`：各 domain specialist 的現行行為。

修改 runtime contract、tool list、state machine、stream/debug envelope 或 environment flag 時，必須同步更新本文件、interfaces 文件與對應 domain 文件。

### Places optional typed enrichment

Places ordinary searches keep the base Google field mask. The existing Places
tool contracts may opt into bounded `rating`, `hours`, or `walking` enrichments;
`resolve_place` supports only `rating` and `hours`, and the empty enrichment
list is the default. Rating/count and current-opening-hours fields are fetched
only when the Places task requires them. Walking candidate data is produced by
one bounded Routes `computeRouteMatrix` call and is attached independently per
destination; a failed matrix element does not fail the Places result.

The existing `places.measure_distance` contract remains backward compatible
with `DRIVE` as the default and accepts `WALK` for explicit requests. No
Planner DAG, Scheduler, Web Runtime, Synthesizer presentation, or frontend card
UI behavior changes as part of this enrichment surface.
