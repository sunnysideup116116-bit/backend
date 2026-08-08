# 公開阿月 V3 架構與 App 遷移指南

本文件描述目前 `Dating-App` 的實際架構。程式碼是最終真相；任何改動若影響本文所述 contract，必須在同一個變更中更新本文。

## 1. 系統定位

公開阿月 V3 是目前的正式 sub-agent 架構（取代舊的 V2 單一 agent loop）。它由五種角色協作：**Planner**（LLM）只做任務拆解、**Sub-agents**（LLM）提出工具呼叫、**Central Guard**（純程式碼）審核每個提案、**Scheduler**（純程式碼）編排執行與確認、**Synthesizer**（LLM）彙整成最終回覆。用「一句話」來說明這條流程：

```text
你問阿月一句話
  │
  ▼
① 排程器（程式）先檢查：你是不是正在做性格測驗？或是在等阿月「確認」？
   ├─ 是 → 直接處理（測驗/確認），不往下走
   └─ 否 → 繼續
  │
  ▼
② 規劃師（AI）把這句話拆成「工作清單」，例如「查行事曆」+「找餐廳」+「綜合回答」
  │
  ▼
③ 執行員（AI，每項工作各一位）決定要呼叫哪個工具、填入什麼條件
  │
  ▼
④ 守門員（程式）檢查每個工具呼叫：工具存在嗎？格式對嗎？重複嗎？會改資料嗎？
   ├─ 會改資料 → 不執行，先問你「要確認嗎？」
   └─ 只是查資料 → 通過
  │
  ▼
⑤ 工具執行：查資料 → 拿到結果（observation）
  │
  ▼
⑥ 彙整師（AI）把全部結果整理成一段人話回覆你
  │
  ▼
你看到回覆
```

重點：**AI 負責「聽懂」和「說話」，程式負責「安全」**。AI 永遠不能直接改資料——所有修改都要先經過守門員 + 你本人確認。

單回合的完整執行流程（含特殊入口、平行執行與 fail closed 路徑）見 §3.1；各角色的詳細職責見 `docs/architecture/`：Planner（`08-planner.md`）、Guard（`07-guard.md`）、生命週期（`03-v3-runtime-lifecycle.md`）。

它不是關鍵字 chatbot，也不是讓模型直接操作資料庫。公開阿月是本交友 App 內協助使用者認識人、牽線的 AI 媒人，不是另一位使用者，也不把目前 App 當成外部服務。LLM 負責語意規劃與自然回答；Scheduler、Guard、typed tools 與 domain services 負責權限、狀態和副作用安全。

公開阿月與阿月悄悄話都是 sibling runtimes，不是 parent/subagent 關係。悄悄話使用獨立的 context、registry、trace 與隱私 namespace；公開阿月不能任意把 history 或 prompt 交給它。

## 2. Repository 結構與責任

| 路徑 | 責任 |
| --- | --- |
| `social_demotest/main.py` | FastAPI app、routers 與 index 初始化 |
| `social_demotest/frontend.html` | 現行 Web UI、NDJSON progress、match/calendar cards |
| `social_demotest/routers/chat.py` | Chat aggregate router；統一 `/api` prefix 與 `Chat` OpenAPI tag |
| `social_demotest/routers/chat_onboarding.py` | Big Five／deep profile onboarding HTTP adapters |
| `social_demotest/routers/chat_messages.py` | 聊天紀錄與聯絡人清單 HTTP adapters |
| `social_demotest/routers/relationship_dates.py` | 共同約會 domain service 的 thin HTTP adapters |
| `social_demotest/routers/demo.py` | Demo-only reset endpoint |
| `social_demotest/routers/proactive.py` | 主動關心／mediator polling 的 thin HTTP adapter |
| `social_demotest/routers/private_mediator.py` | 悄悄話 JSON／NDJSON HTTP adapters 與既有 private orchestration |
| `social_demotest/routers/relationship_quiz.py` | 已接受配對的默契小測驗 HTTP adapters |
| `social_demotest/routers/public_chat.py` | Public `/api/direct_chat*` adapters；V3 orchestration |
| `social_demotest/services/ayue_agent/v3/scheduler.py` | 公開 V3 唯一 orchestrator：confirmation 入口、DAG 執行、平行、trace |
| `services/ayue_agent/v3/planner.py` | 輕量 LLM：只拆靜態子任務 DAG（`decompose_tasks` function calling） |
| `services/ayue_agent/v3/guard.py` | 中央 Guard：純程式碼驗證 schema、重複、步數、寫入 confirmation |
| `services/ayue_agent/v3/context_slicer.py` | 依 sub-agent 角色切 privacy-safe context slice |
| `services/ayue_agent/v3/confirmation.py` | 每位使用者單一、preview-bound confirmation 的 CAS 管理（`v3_pending_confirmations`） |
| `services/ayue_agent/v3/write_executors.py` | 已確認寫入的唯一執行路徑（domain service + idempotency） |
| `services/ayue_agent/v3/synthesizer.py` | 綜合所有 observation 產出最終回覆；地點卡以 typed tool 決定 |
| `services/ayue_agent/v3/sub_agents/` | calendar / places / match / relationship / profile 五個 sub-agent |
| `services/ayue_agent/context.py` | 每回合 privacy-safe context（`build_agent_turn_context_v2`） |
| `services/ayue_agent/contracts.py` | Provider-neutral contracts（`AgentTurnContextV2`、`AgentResult` 等） |
| `services/ayue_agent/router.py` | 僅保留 V3 共用的封閉協議與回覆清理（`confirmation_choice`、`_concise_public_reply`） |
| `services/ayue_agent/tool_registry.py` | ToolSpec、risk、schemas、progress、argument ownership |
| `services/ayue_agent/tools.py` | 唯讀 tool facade 與安全 projection |
| `services/ayue_agent/web_tools.py` | Tavily external-web adapter、URL safety 與 bounded projection |
| `services/ayue_agent/maps_client.py` | OpenStreetMap／Overpass 附近地點與距離 adapter、TTL cache(fallback provider) |
| `services/ayue_agent/google_places_client.py` | Google Places v1 / Routes API adapter、TTL cache(主要 provider) |
| `services/ayue_agent/capabilities.py` | 對使用者一致的產品能力與用詞真相 |
| `services/ayue_agent/proactive_scheduler.py` | Server-side 主動關心排程、claim 與重試 |
| `services/ayue_agent/match_opportunity.py` | 配對機會評估（profile basis、active-match block、guidance fingerprint） |
| `services/relationship_engagement_service.py` | Probe、feedback、關係摘要與 post-chat engagement state |
| `services/proactive_delivery_service.py` | Polling 時 mediator event／care delivery 的 claim 與投遞流程 |
| `services/profile_task_service.py` | 已保存 owner 訊息的 profile extraction 排程 facade |
| `services/assessment_session_service.py` | 公開阿月聊天室內基本性格／深層探索的 owner-scoped、短期 session lifecycle |
| `services/mediator_context_service.py` | Legacy public／private mediator 共用的 bounded context projection |
| `services/relationship_quiz_service.py` | 默契小測驗 lifecycle、答案驗證與完成結果 projection |
| `services/ayue_agent/private_v2.py` | 悄悄話的獨立 context、registry 與 composer |
| `services/match_state_service.py` | Canonical match read model |
| `services/match_action_service.py` | Agent/API 共用的 match action facade 與 transition effects |
| `services/match_decision_service.py` | Atomic match CAS transition |
| `services/calendar_service.py` | 本人行事曆 CRUD 與存取控制 |
| `services/date_coordination_service.py` | 共同約會、改期、取消與雙方同步 |
| `services/profile_skills.py` | 非同步 owner-only profile extraction pipeline |
| `services/profile_contracts.py` | Typed recent-context／memory extraction contract |
| `services/memory_service.py` | Owner-scoped durable memory domain facade、outbox 與 Mongo read projection |
| `services/semantic_plan_service.py` | Accepted pair room 的 shared semantic plan；不是 Public owner memory |
| `matchmaker_agent/` | Port 9001 候選排序、Neo4j 記憶與 feedback service |
| `social_demotest/tests/` | Offline deterministic contract、trajectory、state、privacy tests |

## 3. Public chat request lifecycle

### 3.1 單回合完整執行流程

一次對話的完整旅程，分四段說明：

**第一段：開場檢查（程式在做，AI 還沒出場）**

```text
使用者送出訊息
  │
  ▼
排程器問：「現在有沒有特殊狀態？」
  ├─ 性格測驗已做完、等你說「確認/取消」→ 只接受這兩個答案
  ├─ 性格測驗進行中 → 你說的每個字都當測驗答案
  ├─ 你在回覆上一句的「確認」→ 執行之前說好要改的事（改行程/找對象…）
  ├─ 你在回覆「取消」→ 把之前說好要改的事全部取消
  └─ 都不是 → 進入第二段
```

**第二段：規劃（AI 把一句話拆成工作清單）**

```text
規劃師（AI）讀你整句話，拆成一張工作清單，例如：
「這週日想吃牛排，順便看那天有沒有空」
  → 工作1: 查行事曆（週日有沒有行程）
  → 工作2: 找附近牛排店
  → 工作3: 綜合回答（清單最後一定有一項「綜合回答」）

拆不出來／AI 出錯 → 直接回「我現在沒辦法判斷這個請求」，
                      什麼工具都不會執行（fail closed，寧可保守也不亂做）
```

**第三段：執行（各工作並行，先過守門員）**

```text
每項工作派一位執行員（AI）：
  └─ 執行員決定「要呼叫哪個工具、條件是什麼」
      └─ 守門員（程式）逐個檢查：
           ├─ 工具存在？格式對？沒重複呼叫？次數沒超限？
           ├─ 只是查資料 → 通過，執行工具，拿到結果
           └─ 要改資料 → 一律不執行，改成「先問你確認」
                          （守門員連「改資料」的念頭都不放行）
平行規則：彼此沒關係的工作同時跑（最多 5 個）；有依賴的（先查行程才能改行程）就排隊等
```

**第四段：彙整與收尾（AI 說人話，程式存紀錄）**

```text
彙整師（AI）拿到所有工作結果（例如：週日晚有電影、附近有 3 家牛排）
  → 整理成一段回覆，有地點時附上地點卡

最後程式做的收尾：
  - 回覆存進聊天紀錄（只存一次）
  - 背景悄悄分析你的訊息 → 更新你的近期情境/記憶（跟回覆無關，不影響回答）
  - 全程只記錄「做了什麼」的安全摘要，不記錄你講了什麼內容（隱私）
```

### JSON endpoint

`POST /api/direct_chat` 保持 V1 相容，request 使用 `DirectChatRequest`：

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

V3 response 保留既有欄位，並可包含：

```json
{
  "reply": "有，對方已經接受了，聊天室也開啟了。",
  "is_locked": false,
  "conversation_intent": "match_status",
  "context_changed": false,
  "context_confirmation_needed": false,
  "agent_version": "v3",
  "agent_mode": "v3",
  "agent_run_id": "..."
}
```

### Streaming endpoint

公開阿月 UI 使用 `POST /api/direct_chat/stream`，request body 與 JSON endpoint 相同，response 為 `application/x-ndjson`：

```text
{"type":"run_started","agent_run_id":"..."}
{"type":"tool_started","agent_run_id":"...","text":"我看一下你的行事曆…"}
{"type":"tool_finished","agent_run_id":"...","outcome":"ok","duration_ms":42}
{"type":"final","response":{"reply":"...","agent_version":"v3","agent_run_id":"..."}}
```

公開介面只允許 `run_started`、`tool_started`、`tool_finished`、`final`、`error` 五種事件，絕不傳 plan、sub-agent 身分、task brief、tool 名稱、arguments/result、prompt、內部 ID、revision 或 raw exception。Web UI 在 progress 持續 250 ms 後才顯示單一暫時泡泡，新進度覆蓋該泡泡；final、error 或斷線時移除。

本機 demo 的「執行流程」不重用公開 stream。只有 `AYUE_LOCAL_DEBUG_TRACE=on`，且 client address 與 request host 都是 loopback 時，才可透過 `/api/debug/ayue-runs/{run_id}` 讀取該使用者本輪的暫存診斷。診斷資料只保存在 process memory，最多 16 輪、每輪 96 個 bounded events、30 分鐘 TTL，secret/token/API-key 欄位自動遮蔽，不寫入 Mongo trace。它可呈現 Planner、DAG layers、各 sub-agent input slice、prompt、available function schemas、function calls、Guard、executor arguments、typed result 與 Synthesizer。

Streaming worker 擁有本次 run 的 background tasks。瀏覽器斷線只停止傳送事件，已開始的操作仍由 idempotency 保護並完成。Owner message 只保存一次，progress 不保存，final assistant reply 只保存一次。

Ollama client 必須有 bounded HTTP timeout（`AYUE_OLLAMA_TIMEOUT_SECONDS`，預設 30 秒）；provider 卡住時 Planner/Sub-agent/Synthesizer 必須回到既有 fail-closed/fallback 路徑。Web UI 另以 120 秒 AbortController 作最後一道串流上限，任何結束路徑都清除 progress bubble。

## 4. 每回合 Context

`build_agent_turn_context_v2()` 每回合重新建立 `AgentTurnContextV2`：

- 本次 owner 原始訊息，清理後最多 1,600 字元。
- 最近 12 則對話，總長最多 6,000 字元。
- 本人近期情境。
- 本人手動保存的粗略所在地（城市／行政區）；不含地址或座標。
- 最多 8 筆本人相關記憶。
- 唯一可操作 proposal 的安全狀態與 server-side revision。
- 有效的 pending confirmation、calendar action draft、place-search draft 或 recent-context draft。
- 每回合建立一次的 Asia/Taipei authoritative clock 與相對日期解析。
- Public capability manifest。
- 本回合經 server 驗證的 @ 已接受聯絡人公開名稱；其內部 ID 僅保留 executor-side。超過三位時只告知 Planner 需要縮小範圍。

刻意排除：Mongo `_id`／document、`seed_user_*`、對方私人記憶、對方行事曆內容、無關舊媒合及完整原始 profile。

只有唯一 live proposal 時才進 context；若舊資料出現多筆 live proposal，fail closed，不讓模型任選一筆。舊 decline outcome 不會預載到每次聊天，避免一般「為什麼」被誤解成舊婉拒追問。

## 5. Planner、Guard、Sub-agents 與 Scheduler

### Planner（輕量 LLM）

只拆任務產靜態子任務 DAG，不填 args、不審核、不回覆。透過 `decompose_tasks` function calling 輸出 `Plan`：

```text
tasks: [{id, agent, depends_on[], task_brief}]
opportunity: {signal: none|social_opening, evidence_span, confidence} | null
```

- `agent` 只能是 `calendar | places | match | relationship | profile | synthesizer`。
- 一個 plan 最多三個 domain tasks 加一個 synthesizer；簡單聊天只能產生 synthesizer。
- 一定且只能有一個 synthesizer task 當終端，且必須依賴所有 terminal domain tasks；plan 不得有未知依賴、自我依賴或 cycle。
- 使用者表達想有人陪、想認識人或獨自參加不舒服時，Planner 在 `opportunity` 標記 `social_opening`（需 confidence ≥ 0.8 且 evidence_span 為原句連續子字串）。

### Central Guard（純程式碼）

驗證每個 sub-agent 的 tool proposal，依序檢查（第一個失敗即回傳對應 `GuardResultCode`）：

1. **工具註冊**：名稱存在於 `TOOL_REGISTRY`，否則 `tool_not_registered`。
2. **Schema 驗證**：arguments 符合該工具的 planner arguments model（`planner_arguments_allowed`），否則 `schema_invalid`。
3. **Forbidden fields 防線**：`FORBIDDEN_ARG_FIELDS = {user_id, match_id, event_id, revision, expected_status}` 由 `ToolProposal` 的 Pydantic validator 在提案建立時先攔截，Guard 的 schema 檢查是第二道防線（`forbidden_arg_field`）。
4. **Duplicate call**：同一回合內 `tool + normalized args` 重複 → `duplicate_call`；平行 task 也共用同一份去重狀態。
5. **全回合唯讀步數上限**：`AYUE_SUBAGENT_MAX_READS`（預設且硬上限 3）超限 → `step_limit_exceeded`。
6. **WRITE 工具一律回 `WRITE_REQUIRES_CONFIRMATION`**，由 Scheduler 建立 confirmation；READ 工具通過。

Guard 不審核語意、不猜意圖、不檢查參數「內容」的正確性（那由 tool facade 與 domain service 負責）。詳細設計見 `docs/architecture/07-guard.md`。

### Sub-agents（LLM + function calling）

calendar / places(+web) / match / relationship / profile 五個 sub-agent，各自以 function calling 提出 tool proposals。一次 LLM 呼叫可產出多個 tool call，Scheduler 逐一 guard 並執行；單一 call 失敗不丟棄其他 call。

### Scheduler（純程式碼）

`run_public_agent_turn_v3` 是唯一 orchestrator：

1. **Assessment 接管**：`awaiting_commit`／active assessment session 存在時，直接處理（commit/cancel/advance），不進 Planner。
2. **Confirmation 入口**：一般副作用只接受封閉的「確認／取消」；`profile.start_assessment` 的 pending confirmation 另外接受「開始／開始吧／開始啊／開始阿」，且只會匹配 assessment confirmation，不會誤確認 calendar 或其他 action。確認只執行使用者最後看到、與 preview/request fingerprint 綁定的最新一筆 pending confirmation。建立新 confirmation 時會作廢同一使用者較舊的 pending action。每回合最多建立一筆待確認副作用；Calendar Agent 可把有順序的多個 typed commands 放入同一個 server-owned plan，仍只需一次確認。
3. **Planner**：拆 DAG。
4. **Opportunity 處理**：`social_opening` 且 profile ready → 建立 guidance confirmation；not_ready → 回 missing-basis 問題。
5. **DAG 執行**：`_topological_layers` 分層，同層平行（`ThreadPoolExecutor`，`AYUE_SUBAGENT_MAX_PARALLEL` 預設且硬上限 2）。prior observation 只給 `depends_on` 宣告的依賴。
6. **Synthesizer**：彙整所有 observation 產出最終回覆；地點卡只有在存在候選卡片時才以 `decide_place_cards` typed tool 決定顯示。Capability/general chat 沒有 tool call 是合法結果。

### 執行規則

- **同層並行**：`depends_on=[]` 的任務同層執行，無執行順序保證。
- **prior observation 只給依賴**：同層無依賴任務之間互不可見；synthesizer 例外（彙整所有）。
- **多 tool calls 全數執行**：單一 call 失敗不使整個 task 標記失敗；只要至少一個 call 成功，task 即為 ok。
- **duplicate 與額度以整回合為範圍**：平行任務共用去重狀態、最多三個唯讀步驟與一筆 pending confirmation；Calendar Agent 的一個 command batch 可包含最多 10 個有序 mutation plans。
- **MENTIONED 工具需有 @ 對象**：無 @ 時該 call 標記 `FAILED/mentioned_required`，不執行、不崩潰 run。
- **sub-task 例外不連坐**：任何未捕獲例外轉成 `FAILED`，run 永不因單一 sub-task 崩潰成整段 error。
- **reuse within task**：`places.measure_distance` 同 task 內等價地點的第二次呼叫重用第一個 observation。
- **web.extract URL binding**：只能抽取本回合 web.search 結果或 owner 原句提供的公開 URL。

## 6. Confirmation 與寫入執行

### Preflight

WRITE proposal 通過 Guard 後，Scheduler 呼叫 `prepare_write_confirmation`：

- `match.start_search`：先 `assess_match_opportunity`；not_ready 回 missing-basis 問題，active_match_blocked 回「不重複開新搜尋」，ready 才建立 confirmation。
- `calendar.submit_commands`：Calendar Agent 只提交不含 authority fields 的 `CalendarCommand` batch。Scheduler 使用同一回合的 authoritative clock 做 deterministic preflight；create/update/cancel 的目標若需要只解析一次，產生不暴露給 LLM 的 `CalendarMutationPlan`。missing_fields/invalid_date/ambiguous/not_found/too_many/invalid_interval/stale_revision/invalid_command 是正常 `needs_clarification` outcome，不建立 confirmation。ready 時同一 batch 共用一筆 confirmation，確認後依序執行，第一個真正 failure 後停止。
- Typed command 只接受 canonical `action/title/target_hint/target_hints`；Scheduler 在嚴格驗證前僅為相容 provider 將 `type/summary/event_hint/event_hints` 映射到 canonical 欄位。最近一次唯一選取的行程與未完成 command 只保存 15 分鐘的 server-owned reference/draft projection；projection 不含 event_id/revision，且 reference stale 時不重新做自然語言辨識。
- `match.decide_active_proposal`：preflight 綁定當下 canonical proposal revision；確認執行時再次比對，stale 即拒絕。
- `profile.start_assessment`：驗證 kind（basic→big_five、deep→deep_profile）。

Pending payload 只保存 executor-safe 資料（event_id、revision、targets、proposed_form 等），Planner 永遠看不到。

### 執行

確認後 `ConfirmationManager.execute_confirmed` 先驗證 preview/request fingerprint，再以 CAS（pending→executing）claim 唯一一筆綁定 action，接著呼叫 `write_executors.execute_write`：

- `match.start_search` → `match_action_service.start_match_search`（idempotency key `confirmation:<id>`）
- `match.decide_active_proposal` → `decide_active_proposal`（revision CAS + stale response）
- `profile.start_assessment` → `assessment_session_service.start_assessment_session`
- `calendar.submit_commands` → `_execute_calendar_mutation_plans` → calendar/date coordination service（每個 plan 使用 confirmation-indexed idempotency key；順序執行、stop-on-failure、無 automatic rollback）
- `calendar.find_my_event` 只用於使用者真的詢問行程內容；mutation 不先做 read task，也不把 read projection 重新組成 `event_hint`。preflight 的 resolver 回傳 canonical event 後，executor 只使用 server-owned event_id/revision。缺少欄位、歧義與找不到目標回正常 clarification，不是 generic agent failure。

Current opportunity contract: an explicit match request (including retry/search wording such as 「再配對一次」) creates a `match` task and follows the normal confirmation path. An indirect `social_opening` is only a soft, expiring observation; it never creates a pending confirmation or claims that a search started. If the user accepts the soft offer, Scheduler converts that explicit confirmation into the normal `match.start_search` confirmation.

寫入一律走 domain service；Scheduler 只協調，不直接寫 MongoDB。

## 7. Match opportunity 與主動牽線

- Planner 在拆解時標記 `social_opening`（想有人陪、想認識人、獨自參加不舒服）。
- Scheduler 驗證 confidence ≥ 0.8 且 evidence_span 為原句子字串後，評估 profile basis：
  - `ready` → 產生短期 `match_opportunity_offer` observation，溫和詢問是否想找人；不建立 confirmation、不宣稱已開始搜尋。
  - `not_ready` → 不啟動配對，也不把這個背景機會當 runtime failure；讓 Synthesizer 依目前訊息自然回覆。
  - `active_match_blocked` → 不重複開新搜尋
- 明確「開始找人」的請求走同一條 preflight 路徑（`explicit_search=True`）。

## 8. Assessment sessions

基本性格與深層探索由 `services/assessment_session_service.py` 唯一管理。V3 Scheduler 在 Planner 前接管：

- `awaiting_commit`：回覆「確認」→ commit（revision CAS）；「取消」→ 保留原資料。
- active session：直接以本次 owner 訊息 advance；「先不做了／退出測驗／結束測驗」→ cancel。
- 逾時 → expire，原資料不變。
- Assessment 訊息不進近期情境或 durable memory extractor；`AgentResult` 帶 `assessment_state/kind/revision` 安全 metadata。

## 9. Trace 與資料安全

V3 agent trace 保存於 `agent_runs`（`agent_version: "v3"`），只允許：

- plan 摘要（task id/agent/depends_on）
- guard result codes
- tool name/ok/error code
- event sequence、latency、final intent/fallback code

Trace 不保存完整 prompt、owner message、observations、tool arguments/results、對方資料或 raw exception。Synthesizer 的 `reply_source`、`fallback_reason` 與 allowlisted `error_code` 只能記錄在本機 debug 或 allowlisted result metadata；`tool_calls=[]` 代表本回合沒有需要的工具，不代表執行失敗。新增欄位時必須先加入 allowlist 並補 privacy test。

## 10. Runtime flags

| Flag | 用途 |
| --- | --- |
| `AYUE_AGENT_V3_MODE=on\|off` | Sub-agent 架構或人工緊急 rollback（off 回 legacy 純聊天，不再有 V2 agent） |
| `AYUE_AGENT_V3_USER_ALLOWLIST` | 漸進式指定使用者；空值代表全部適用 mode |
| `AYUE_SUBAGENT_MAX_READS` | 整個回合唯讀上限，預設且硬上限 3 |
| `AYUE_SUBAGENT_MAX_PARALLEL` | 同層 sub-agent 最大並發數，預設且硬上限 2 |
| `AYUE_OLLAMA_TIMEOUT_SECONDS` | 單次 Ollama HTTP 呼叫 timeout，預設 30 秒，限制 5–120 秒 |
| `AYUE_LOCAL_DEBUG_TRACE` | 本機 demo-only 詳細執行 trace；預設 off，且 endpoint 仍要求 client/host 都是 loopback |
| `AYUE_RUNTIME_MODEL_SETTINGS_TOKEN` | 啟用 runtime model settings 管理 API 的 server-side admin token；未設定時不可修改 |
| `AYUE_ALLOWED_RUNTIME_MODELS` | 管理 API 可切換的 model allowlist；未設定時只允許目前設定值 |
| `AYUE_SUBAGENT_TIMEOUT_MS` | 單一 sub-agent LLM 呼叫逾時（`.env.example` 已保留，目前 scheduler 尚未消費此旗標；LLM 逾時行為由 `ai_service` 提供） |
| `AYUE_DEFAULT_TIMEZONE` | 預設 `Asia/Taipei` |
| `AYUE_CALENDAR_STATE_MONGO` | Calendar recent reference/draft 是否持久化到 Mongo；預設 `on`（仍有 process-memory fallback，TTL 15 分鐘） |
| `AYUE_PROFILE_SKILLS_MODE=on\|shadow\|off` | Profile extractor 寫入、shadow 觀察或停用（`shadow` 只記錄不寫入） |
| `AYUE_PROFILE_SKILLS_USER_ALLOWLIST` | Profile rollout allowlist |
| `AYUE_PRIVATE_AGENTIC_MODE` | Private runtime，與 Public V3 分開 |
| `AYUE_PRIVATE_AGENTIC_USER_ALLOWLIST` | Private rollout allowlist |
| `AYUE_MAPS_ENABLED` | 是否提供 OpenStreetMap／Overpass 地點工具(fallback provider) |
| `AYUE_MAPS_MONGO_CACHE` | 是否將地點工具 cache 寫入 Mongo；預設 `off` |
| `AYUE_GOOGLE_PLACE_CARDS_ENABLED` | 啟用 Google Places 為主要地點 provider；需同時設定下列兩把 key |
| `GOOGLE_PLACES_SERVER_API_KEY` | 後端 Places API New Text Search + Routes API key；僅限這些 API |
| `GOOGLE_MAPS_BROWSER_API_KEY` | 前端 Maps JavaScript / Maps Embed API key；必須限制 HTTP referrer；Embed 無限免費 |
| `AYUE_GOOGLE_PLACE_PHOTOS_ENABLED` | Place Details Photos SKU；預設 `off`，**2026-08-04 決議不顯示照片** |
| `AYUE_GOOGLE_DISTANCE_MATRIX_ENABLED` | Routes API Compute Routes Essentials；預設 `on`；`off` 時只回 OSM haversine 直線距離 |

> 計費等級注意：rating / userRatingCount / currentOpeningHours 屬 **Enterprise** SKU，本專案基於成本控制**一律不要求這些欄位**，place card 不顯示評分、評論數或營業狀態。

基礎服務另需 `MONGO_URI`、LLM/Ollama、Google embedding；設定 `TAVILY_API_KEY` 後才開啟 Web Search／Extract。地點工具的 provider 優先序為 Google Places(主要)→ OSM Nominatim/Overpass(fallback)。port 9001 matchmaker 需要 `LLM_*` 與 Neo4j 設定。可提交的欄位範例見 `social_demotest/.env.example` 與 `matchmaker_agent/.env.example`；包含真實密鑰的 `.env` 不得提交或交付。

## 11. App 遷移指南

### Backend contract

1. 先部署本版本 backend 與 Mongo indexes，保留既有 `/api/direct_chat` JSON endpoint。
2. 在測試使用者 allowlist 設定 `AYUE_AGENT_V3_MODE=on`；確認後再擴大範圍。
3. 不可在 App 自己重建 intent classification。App 只送原始訊息與必要 mention，語意由 V3 Planner 處理。
4. 不可在 V3 timeout/error 時由 App 再呼叫 legacy endpoint；這會造成訊息及副作用重複。

### Public Ayue chat UI

1. 只有 `contact_id == "ai_assistant"` 改用 `/api/direct_chat/stream`。
2. 逐行解析 NDJSON，忽略未知 event；不要假設一個 network chunk 就是一個完整 JSON event。
3. `tool_started.text` 顯示為單一暫時狀態；不要顯示技術 tool name。
4. `tool_finished` 只更新狀態，不新增永久訊息。
5. `final.response` 按原本 JSON response 處理並顯示正式回答。
6. `error`、斷線或沒有 final 時清除暫時泡泡，提示安全重試；不要自動重送可能含副作用的 request。
7. 防止同一使用者在 public run 尚未結束前重複送出。

### Match cards

1. 顯示 API 回傳的 `stage` 與 `proposal_revision`，不要只看舊 `status` 文案推測按鈕。
2. 接受／婉拒／取消呼叫 `POST /api/match/decision` 並攜帶 `expected_status` 與 `expected_revision`。
3. HTTP 409 代表 stale state；App 重新讀 `/api/match/status` 或 `/api/match/state` 後更新卡片，不重送舊 decision。
4. Accepted match 轉為 contact/chat；歷史 proposal card 保留但不可操作。

### Profile 與 Calendar

- App 不直接產生或提交 `current_context` 摘要；只送 owner 原始聊天訊息，profile pipeline 非同步更新。
- UI 顯示 profile 更新時應讀 server projection，不把 assistant reply 當 evidence。
- 所在地透過 `PATCH /api/profile/location` 手動更新，只保存城市與行政區。
- Calendar CRUD 與共同約會沿用 server revision/state；不要在 client 端只修改畫面。

### Rollback

緊急 rollback 只由部署環境把 `AYUE_AGENT_V3_MODE` 設為 `off` 並重啟服務。Rollback 是人工操作，不是 request-level fallback；App 不需要也不應知道 Planner 是否失敗。

## 12. 驗收基線

每次改動至少驗證：

- 一般聊天自然回答，不因沒有工具而拒答。
- 配對結果、目前日期、本人行事曆與近期情境都經正確 read tool。
- 地點推薦在搜尋條件已足夠時先讀取 places tool；「隨意推薦」不得重複追問料理類型。
- 明確找人先 confirmation，確認後只搜尋一次。
- Planner duplicate、壞 JSON、timeout、低信心不造成重複工具或 legacy fallback。
- Proposal stale revision、重複 request、雙方並發不覆寫終態。
- Profile evidence 可追溯、只來自 owner message，且摘要為繁體中文。
- Stream progress 不進聊天紀錄，事件不洩漏 arguments/results/ID。
- JSON direct chat、其他聯絡人及 private chat 不因 Public V3 改動而破壞。
- Test harness 不連正式 Atlas/Neo4j；完整 deterministic tests、Python compile、兩個服務健康檢查通過。

新增真實失敗案例時，優先將它匿名化後加入 V3 trajectory 測試，再修對應 contract、projection 或 prompt；不要先增加一句特例 regex。

## 13. 設計文件

完整 V3 設計見 `docs/superpowers/specs/2026-08-04-sub-agent-architecture-design.md`。
### Calendar clarification and fuzzy target contract

Calendar target lookup keeps exact matching as the compatibility path and may
return a bounded fuzzy suggestion for an owner-visible typo. Explicit date and
time constraints remain hard filters; ambiguous or close candidates become a
typed clarification with opaque `candidate_1..candidate_3` references. Only
safe labels reach the model. The Synthesizer may phrase missing-field and
candidate clarifications, but must not claim a mutation happened or replace a
server outcome with a fixed field list. A read miss may produce the same
bounded candidate suggestion, never a generic agent failure.
# Demo maintenance and match diagnostics

The local Demo destructive tools are guarded by `DEMO_DESTRUCTIVE_TOOLS_ENABLED`.
Full reset clears Neo4j first, then all non-system collections in the configured
Mongo database and V3 process-local fallback state; it is not a distributed
transaction. A failed subsystem must be surfaced as a typed partial failure.

Match search progress separates candidate qualification, matchmaker request,
matchmaker response, and proposal write. Public status exposes only allowlisted
error codes and failure stages; raw provider output, prompts, Graph content,
and exception messages remain server-side only.

If Mongo initialization or DNS fails, the HTTP service starts in a fail-closed
degraded mode. Demo status reports `mongo_status=unavailable`, destructive
cleanup returns `mongo_unavailable`, and no local fallback database is touched.
