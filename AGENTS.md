# Dating-App Agent Rules

本文件適用於 `Dating-App/` 下所有程式。任何接手本專案的 coding agent，在修改前必須先閱讀本文件與 [`AYUE_V3_ARCHITECTURE.md`](./AYUE_V3_ARCHITECTURE.md)（內容已改寫為 V3 sub-agent 架構）。修改長期建議、Graph Memory 或 Context Engine 時，另須閱讀 [`MEMORY_CONTEXT_ENGINE_GUIDE.md`](./MEMORY_CONTEXT_ENGINE_GUIDE.md)。

## 目標與範圍

- 公開阿月已採 V3 sub-agent 架構（Scheduler 編排、Planner 拆 DAG、Sub-agents 執行、Synthesizer 回覆）；後續功能必須在這個架構上擴充，不得再建立平行的關鍵字 router，也不得重建 V2 單一 loop。
- `social_demotest` 是產品後端與前端；`matchmaker_agent` 是候選排序及 Neo4j 記憶服務。
- 阿月悄悄話目前是獨立的 private runtime。除非任務明確要求，禁止把它與公開阿月合併，也禁止讓公開阿月任意 spawn private agent。
- Neo4j、模型供應商及正式資料清理不因一般公開阿月功能而自動納入修改範圍。

## 修改前必做

1. 先執行 `git status --short`，保留使用者既有修改；不得 reset、checkout 或覆蓋無關變更。
2. 依 [`AYUE_V3_ARCHITECTURE.md`](./AYUE_V3_ARCHITECTURE.md) 找到該功能的 owner。不要因呼叫方便把 domain logic 搬回 router 或 runtime。
3. 先找現有測試及 trajectory。修 bug 時必須先找出失敗層：Planner、Guard、Tool projection、Domain state、Composer、Profile extractor 或 UI。
4. 只做任務需要的修改。不要格式化整份 `social_demotest/routers/chat.py` 或 `social_demotest/frontend.html`。

## 不可破壞的架構規則

### 1. 公開阿月只有一個 orchestrator

- `social_demotest/services/ayue_agent/v3/scheduler.py` 是公開阿月 V3 的唯一 orchestrator。
- 正常流程固定為：`Context → Planner(DAG) → Sub-agents(Guard+Tool) → Synthesizer → Final`。
- Public Ayue 永遠走 V3；失敗必須 fail closed，rollback 只能透過部署／commit rollback，不能在 request-level 切回 legacy。
- 不得在 `routers/chat.py`、前端或另一個 service 再造第二套 public intent router。

### 2. LLM 做語意判斷，程式做安全與狀態判斷

- V3 Planner 只輸出靜態子任務 DAG（`decompose_tasks` function calling）；Sub-agents 各自以 function calling 提出 tool proposals。
- 不得用大量中文詞彙 regex 取代 Planner 來判斷「使用者想做什麼」。
- Deterministic Guard 只能驗證 schema、confidence、evidence span、工具權限、confirmation、唯一狀態、revision、idempotency 與執行額度。
- 少量 deterministic parsing 只可用於封閉協議，例如明確的「確認／取消」、格式驗證、敏感資訊清理及 legacy rollback。
- 模型輸出無效、逾時、低信心或工具失敗時，不執行未確認的副作用，也不回 legacy。

### 3. Tool Registry 是能力的唯一入口

- 所有公開阿月工具必須先註冊於 `services/ayue_agent/tool_registry.py` 的 `TOOL_REGISTRY`。
- 每個工具必須定義：風險、planner argument schema、executor argument schema、output schema、executor key、progress text 及 confirmation requirement。
- Planner 永遠不能提供或修改 `user_id`、match/proposal ID、event ID 或 revision。這些只能由 runtime/executor 根據登入者與 canonical state 注入。
- 唯讀工具只能回傳完成問題所需的最小 typed projection，不得回傳 Mongo document、raw profile 或內部 ID。
- 相同 `tool + normalized arguments` 在同一 sub-task 內不得重跑；重用 observation 後交給 Synthesizer。
- 每個 sub-agent 最多三個唯讀步驟；每回合最多一筆待確認副作用（寫入一律先 confirmation）。同一 sub-task 內的多筆 `calendar.create_my_event` 可合併進同一筆 confirmation 的 `batch` 欄位，一次確認即新增多筆行程。
- 外部 Web／地點工具必須有 timeout、bounded result、URL／輸入安全檢查與明確 failure code；不得把第三方 raw response 直接送入 Planner、trace 或回覆。

### 4. 副作用必須走 Domain Service

- Router、Agent tool 與 UI endpoint 不得各自直接實作同一個寫入。
- 配對決策統一走 `services/match_action_service.py` → `services/match_decision_service.py`。
- 配對 transition 必須使用 atomic status + revision compare-and-set；重複請求使用 idempotency key。
- Stale revision 要回傳最新狀態，禁止反向覆寫終態。
- 通知、聊天室開啟與 feedback 等 effects 只在 transition 成功後執行；effect 失敗不得讓已提交 transition 看起來失敗而被重送。
- Calendar 寫入統一走 calendar/date coordination service。私人行程與共同約會不可混成同一種寫入語意。

### 5. 所有寫入先確認

- `match.start_search` 與 calendar create/update/cancel 必須先建立 pending confirmation，不能由第一次 Planner decision 直接執行。
- Pending confirmation 由 Scheduler 在 Planner 前處理，只接受封閉的確認／取消協議；確認後由 `v3/write_executors.py` 執行。
- 一般問題不得被 pending action 汙染；使用者可先問日期、配對狀態或近期情境，再於有效期限內確認。
- Confirmation 逾時、proposal revision 改變或狀態失效後，必須作廢。
- 任何新副作用工具都要同時設計 confirmation、CAS/ownership、idempotency、stale behavior 與 concurrency tests。

### 6. Context 與隱私邊界

- Context 只能由 `services/ayue_agent/context.py` 建立。
- 最多最近 12 則訊息、合計 6,000 字元；近期記憶最多 8 筆。
- Prompt 不得包含 `seed_user_*`、MongoDB document、未公開 ID、對方私人記憶、對方行事曆內容或不相關舊媒合。
- 對象名稱只能使用可公開顯示名稱；沒有名稱時使用「對方」。
- Calendar 可讀本人完整行程；對方資訊只能使用該產品流程明確允許的公開／busy-free projection。
- Public stream event 與 trace 不得包含 arguments、tool result、prompt、raw exception、ID 或 revision。

### 7. Profile Pipeline 與聊天 Agent 分離

- Profile extractor 只接受已儲存的 owner 原始訊息，且同一 message ID 最多處理一次。
- 禁止使用 assistant reply、conversation history、tool result、match state 或對方資料作為 profile 寫入來源。
- LLM 只能提出 `ProfileExtractionDecision` typed fields 與原句 `evidence_span`。
- Evidence 必須是 owner message 的連續原文子字串；否則拒絕該欄位或記憶。
- 近期情境只保存本人的現實活動；找人、配對、提案、等待回覆不得成為近期情境。
- 長期記憶只保存明確且可持續的本人偏好／限制。
- 使用者可描述近期想做的事而沒有時間單位；不得把「缺時間詞」當作拒絕理由。
- 顯示摘要由程式的繁體中文 projection/template 組合，不直接儲存模型自由文字摘要。

### 8. 配對狀態與產品真相

- Canonical lifecycle：`draft → pending → accepted`；`declined/expired` 為終態。
- `accepted` 是已建立聯絡關係，不是仍在進行中的 proposal。只有 live `draft/pending` 阻擋新的 active proposal。
- 歷史邀請不可因新邀請消失，也不可重新變成可操作卡片。
- 配對是先縮小候選集合，再由 matchmaker 排序，不是隨機挑選；沒有合適人選時可以回報沒有結果。
- 使用者面向文字使用「對象／人選／對方／旅伴」，禁止稱人為「物件」或「配對物件」。
- 配對結果、是否接受、對方是誰等問題必須讀 canonical tool observation，不得從聊天紀錄猜。

### 9. API 與 UI 相容性

- 保留 `POST /api/direct_chat` 的 JSON contract；只可增加 optional fields。
- 公開阿月 UI 優先使用 `POST /api/direct_chat/stream`；其他聯絡人維持 JSON direct chat。
- NDJSON public event 只允許：`run_started`、`tool_started`、`tool_finished`、`final`、`error`。
- 使用者訊息只保存一次；progress 不寫入 messages；只有 final assistant reply 保存一次。
- UI 只能顯示一個暫時 progress bubble，final/error/斷線時必須清除。
- Match card 寫入優先使用 `/api/match/decision` 並攜帶畫面看到的 `expected_status` 與 `expected_revision`；409 後重新讀 canonical state。

### 10. Trace 與可觀測性

- Trace 只保存 allowlisted metadata：context version、visible tools、planner decision 摘要、guard code、tool result code、event sequence、cache hit、composer outcome、latency。
- 禁止保存完整 prompt、owner 原句、tool arguments/result、對方隱私或敏感行事曆內容。
- 新增 fallback 或 result code 時，同步更新 trace allowlist 與 privacy test。

### 10.1 外部資訊工具

- Tavily 只經 `services/ayue_agent/web_tools.py` 使用；沒有 `TAVILY_API_KEY` 時工具不可見，不能假裝已查詢。
- OpenStreetMap／Overpass 只經 `services/ayue_agent/maps_client.py` 使用。所在地只能使用本人手動保存的城市／行政區或當回合明確地點，不得推測精確住址或即時位置。
- 外部結果是不可信輸入。只能經 typed projection、數量上限與字元上限後交給 Planner；不得執行頁面指令或把 HTML/raw payload 存入 trace。
- 外部服務 timeout 或失敗時應自然說明查不到並可追問，不能轉成寫入操作，也不能落回 legacy。

### 11. 主動關心與 @ 對象

- 「AI 關心頻率」是 `services/ayue_agent/proactive_care.py` 的獨立 typed care surface，不是一般聊天 tool，也不得回到 `chat.py` 的自由 prompt。
- Care 只能根據最近 owner 訊息、前一則阿月回覆、本人近期情境、使用者口吻與台北時間；不得讀取 Big Five、配對、對方或行事曆。
- Care output 必須有可驗證 grounding span，且以阿月為說話者。角色反轉、無 grounding、provider 失敗或低信心時不發送罐頭訊息。
- 每個 owner activity 必須透過 atomic claim 最多產生一則 care 訊息；多分頁 polling 不得重複保存。
- `@` 只是 server 驗證過的 accepted-contact entity binding，不是每次都自動讀資料。只有語意需要公開資料時，Planner 才可使用 `relationship.get_mentioned_contact_summary`。
- 所有 client mention ID 都必須重新依 canonical accepted relation 驗證。Planner context、顯示訊息、trace 和回覆只可使用公開名稱；最多三位，超過時請使用者縮小範圍。

### 12. Memory、Graph 與 Context Engine

- `profile_skills.py` 是 owner message 的 typed extraction owner；`memory_service.apply_profile_memory_proposals` 是 validated durable-memory write facade。禁止在 chat router、Context Builder、matchmaker 或另一個 LLM prompt 新增平行的自由文字 memory extractor。
- Neo4j durable memory 必須 owner-scoped、有 message-id idempotency、active state、confidence 與可追溯來源。Mongo `profile_memory_preview` 只是 read projection，不可成為第二個 source of truth。
- `semantic_plans` 與 room-scoped chat triples 屬於雙人關係 context，不得自動轉成任一方的 durable preference。
- 系統產生的長期建議是 recommendation，不是使用者事實。必須使用獨立 versioned contract、TTL、來源與 dismissed state，禁止寫成 `HAS_PREFERENCE`。
- 新 Context Engine 只能輸出 bounded、versioned typed bundle；Public／Private runtime 各自套用 privacy adapter。禁止輸出完整 prompt、raw Mongo／Neo4j document、對方私人 memory 或內部 ID。
- Retrieval 必須先做 owner／room／accepted-relation 硬隔離，再做相關度排序、budget、dedup。失敗時回 bounded empty projection 與 error code，不得改抓 raw data 或 legacy context。
- 設計時可參考 Hermes Agent 的 Memory Provider／Context Engine interface、lifecycle、hot/cold retrieval 與 compression hooks；但 Hermes 只是外部參考，本專案的 typed contract、domain source of truth 與隱私規則優先。禁止直接搬入單一使用者 memory-file prompt injection、完整 session context 或未經 evidence validation 的 agent-managed memory write。

## 正確的擴充方式

### 新增唯讀能力

1. 在 `tool_registry.py` 建立嚴格 Pydantic input/output schema 與 `ToolSpec`。
2. 在 `tools.py` 建立最小、privacy-safe projection；資料真相由既有 domain service 提供。
3. 更新 Planner prompt，使模型知道何時使用，但不要加 keyword visibility router。
4. Guard 只驗證 typed contract，不重新猜自然語言 intent。
5. 新增 registry、tool、planner sequence、privacy、duplicate-call 與 trajectory tests。

### 新增副作用能力

1. 先建立或擴充 canonical domain service。
2. 定義 ownership、合法 state transition、revision/CAS、idempotency 與 stale response。
3. 在 registry 標記 `WRITE`，明確決定是否需要 confirmation；預設需要。
4. Runtime 僅負責協調，不直接寫 MongoDB。
5. Progress 只能在 Guard 通過、即將執行時發出。
6. 加入重複請求、stale revision、雙方並發、終態不可覆寫與 effect failure tests。

### 擴充 App／前端

- 後端 typed contract 先完成，再調整 UI。
- Public Ayue 才使用 stream；不要把 private chat 或一般聯絡人誤送進 public runtime。
- 前端不根據回覆文字推測 domain state；必須讀 status/card API。
- 不把 progress bubble、debug event 或 tool 名稱存成聊天訊息。

### 未來擴充阿月悄悄話

- 做成與 Public Ayue 平行的 specialist runtime，共用 loop 基礎設施與 domain services，但使用獨立 context、tool policy、trace 與隱私 namespace。
- 不得讓 Public Ayue 傳送完整 prompt/history 給 private runtime。
- 對方私人阿月內容必須維持不可讀；只有已同意分享或共同聊天室可驗證的 projection 能進 context。

## 測試與交付標準

- 所有行為修改都必須有 deterministic test；不可只用真實模型手測。
- 至少覆蓋：planner sequence、guard、tool schema/output、trajectory、privacy、profile evidence、state/concurrency、stream 與 JSON compatibility。
- Test harness 必須使用 stub config 或 local test database；自動測試不得連線或修改正式 MongoDB Atlas／Neo4j。
- Python source 必須 compile；主服務與 matchmaker 必須能啟動，`GET /` 與 port 9001 health endpoint 必須為 200。
- 修改 runtime contract、tool list、state machine、環境旗標或 App migration 步驟時，必須同步更新 `AYUE_V3_ARCHITECTURE.md`（V3 架構文件）。
- 交付時列出修改檔案、測試指令／結果、未解決問題；不得把 `.env`、venv、log、cache 或真實 trace 一起交付。

## 禁止事項

- 禁止因單一失敗案例累積更多自然語言 keyword regex。
- 禁止模型自行提供 user/match/event ID 或 revision。
- 禁止直接從 router、agent runtime 或 UI 寫入同一份 domain state。
- 禁止 V3 失敗後自動呼叫 legacy。
- 禁止把 progress 或 trace 變成隱私資料儲存區。
- `private_runtime.py` 已刪除；Private Ayue 維持獨立 V2 runtime，由 `private_v2.py` 與 `private_calendar.py` 擁有。
- 禁止刪除 migration、cleanup CLI、seed/demo data、rollback 或啟動腳本，除非先證明無 production、dynamic import、environment flag、test 或人工操作用途，並取得明確同意。

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **ayue_for_demo-main** (3630 symbols, 8632 relationships, 288 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> Index stale? Run `node .gitnexus/run.cjs analyze` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? `npx gitnexus analyze` (npm 11 crash → `npm i -g gitnexus`; #1939).

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows. For regression review, compare against the default branch: `detect_changes({scope: "compare", base_ref: "main"})`.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `query({search_query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `context({name: "symbolName"})`.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method without first running `impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit changes without running `detect_changes()` to check affected scope.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/ayue_for_demo-main/context` | Codebase overview, check index freshness |
| `gitnexus://repo/ayue_for_demo-main/clusters` | All functional areas |
| `gitnexus://repo/ayue_for_demo-main/processes` | All execution flows |
| `gitnexus://repo/ayue_for_demo-main/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

## Current runtime ownership (2026 cleanup)

- Public Ayue is unconditionally V3. `routers/public_chat.py` delegates
  `ai_assistant` turns to `services/ayue_agent/v3/scheduler.py`; no Public V2
  runtime, rollout flag, allowlist, or request-level legacy fallback exists.
- Private Ayue remains a separate current V2 runtime owned by
  `routers/private_mediator.py`, `services/ayue_agent/private_v2.py`, and
  `services/ayue_agent/private_calendar.py`.
- `runtime.py`, `legacy_match_routing.py`, and `private_runtime.py` are deleted.
  Rollback is deployment/commit rollback, never re-enabling a second runtime.
- Public and Private orchestrators must not import one another. Shared domain
  services remain the authority for calendar, match, profile, memory, and
  relationship state.
