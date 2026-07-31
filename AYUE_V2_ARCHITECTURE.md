# 公開阿月 V2 架構與 App 遷移指南

本文件描述目前 `Dating-App` 的實際架構。程式碼是最終真相；任何改動若影響本文所述 contract，必須在同一個變更中更新本文。

## 1. 系統定位

公開阿月 V2 是一個 bounded、OpenClaw-style 的單一 agent loop：

```mermaid
flowchart LR
    UI["Web / App UI"] --> API["Direct Chat Endpoint"]
    API --> C["Context Builder"]
    C --> P["LLM Planner"]
    P --> G["Deterministic Guard"]
    G -->|read| T["Typed Tool"]
    G -->|write| D["Canonical Domain Service"]
    T --> O["Verified Observation"]
    D --> O
    O --> P
    P --> F["Validated terminal reply / Composer fallback"]
    F --> API
    G -.-> E["Safe Progress Event"]
    API -.-> UI
```

它不是關鍵字 chatbot，也不是讓模型直接操作資料庫。LLM 負責語意規劃與自然回答；Runtime、Guard、typed tools 與 domain services 負責權限、狀態和副作用安全。

公開阿月與阿月悄悄話都是 sibling runtimes，不是 parent/subagent 關係。悄悄話 V2 使用獨立的 context、registry、trace 與隱私 namespace；公開阿月不能任意把 history 或 prompt 交給它。

## 2. Repository 結構與責任

| 路徑 | 責任 |
| --- | --- |
| `social_demotest/main.py` | FastAPI app、routers 與 index 初始化 |
| `social_demotest/frontend.html` | 現行 Web UI、NDJSON progress、match/calendar cards |
| `social_demotest/routers/chat.py` | API adapter、訊息保存、stream worker、V2/rollback 入口 |
| `social_demotest/services/ayue_agent/runtime.py` | 公開 V2 唯一 orchestrator |
| `services/ayue_agent/context.py` | 每回合 privacy-safe context |
| `services/ayue_agent/contracts.py` | Provider-neutral Planner/Tool/Result contracts |
| `services/ayue_agent/router.py` | Planner adapter、tool policy、Guard、Composer |
| `services/ayue_agent/tool_registry.py` | ToolSpec、risk、schemas、progress、argument ownership |
| `services/ayue_agent/tools.py` | 唯讀 tool facade 與安全 projection |
| `services/ayue_agent/web_tools.py` | Tavily external-web adapter、URL safety 與 bounded projection |
| `services/ayue_agent/maps_client.py` | OpenStreetMap／Overpass 附近地點與距離 adapter、TTL cache |
| `services/ayue_agent/capabilities.py` | 對使用者一致的產品能力與用詞真相 |
| `services/ayue_agent/proactive_scheduler.py` | Server-side 主動關心排程、claim 與重試 |
| `services/ayue_agent/private_v2.py` | 悄悄話 V2 的獨立 context、registry 與 composer |
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

V2 response 保留既有欄位，並可包含：

```json
{
  "reply": "有，對方已經接受了，聊天室也開啟了。",
  "is_locked": false,
  "conversation_intent": "match_status",
  "context_changed": false,
  "context_confirmation_needed": false,
  "agent_version": "v2",
  "agent_mode": "v2",
  "agent_run_id": "..."
}
```

### Streaming endpoint

公開阿月 UI 使用 `POST /api/direct_chat/stream`，request body 與 JSON endpoint 相同，response 為 `application/x-ndjson`：

```text
{"type":"run_started","agent_run_id":"..."}
{"type":"tool_started","agent_run_id":"...","step_id":"0:read","text":"我看一下目前的配對進度…"}
{"type":"tool_finished","agent_run_id":"...","step_id":"0:read","outcome":"ok"}
{"type":"final","response":{"reply":"...","agent_version":"v2","agent_run_id":"..."}}
```

公開事件絕不傳 tool arguments/result、prompt、ID、revision 或 raw exception。Web UI 在 progress 持續 250 ms 後才顯示單一暫時泡泡，新進度覆蓋該泡泡；final、error 或斷線時移除。

Streaming worker 擁有本次 run 的 background tasks。瀏覽器斷線只停止傳送事件，已開始的操作仍由 idempotency 保護並完成。Owner message 只保存一次，progress 不保存，final assistant reply 只保存一次。

## 4. 每回合 Context

`build_agent_turn_context_v2()` 每回合重新建立 `AgentTurnContextV2`：

- 本次 owner 原始訊息，清理後最多 1,600 字元。
- 最近 12 則對話，總長最多 6,000 字元。
- 本人近期情境。
- 本人手動保存的粗略所在地（城市／行政區）；不含地址或座標。
- 最多 8 筆本人相關記憶。
- 唯一可操作 proposal 的安全狀態與 server-side revision。
- 有效的 pending confirmation、calendar action draft 或 recent-context draft。
- 每回合建立一次的 Asia/Taipei authoritative clock 與相對日期解析。
- Public capability manifest。
- 本回合經 server 驗證的 @ 已接受聯絡人公開名稱；其內部 ID 僅保留 executor-side。超過三位時只告知 Planner 需要縮小範圍。

刻意排除：Mongo `_id`／document、`seed_user_*`、對方私人記憶、對方行事曆內容、無關舊媒合及完整原始 profile。

只有唯一 live proposal 時才進 context；若舊資料出現多筆 live proposal，fail closed，不讓模型任選一筆。舊 decline outcome 不會預載到每次聊天，避免一般「為什麼」被誤解成舊婉拒追問。

## 5. Planner、Guard 與 Loop

目前模型透過 JSON schema adapter 產生 `AgentDecision`；未來可換 native function calling，但 runtime contract 不變：

```text
kind: final | tool_call | confirmation
intent: chat | match_status | match_action | calendar | calendar_action |
        relationship | memory | time | web | unclear
tool_name: registered tool or null
arguments: schema-valid, never IDs/revisions
confidence: 0..1
evidence_span: exact owner-message substring when required
```

Runtime 規則：

- 最多三個 planner/tool steps。
- 同回合相同工具與 normalized arguments 只執行一次。
- 每回合最多一次 write side effect。
- Match status、time 與 calendar target 若缺 verified read，Guard 會先補 canonical read，而不是讓模型猜。
- Planner 無效、逾時、低信心、schema 不符或 tool output 不符 schema 時 fail closed。
- Planner 的 `final.reply` 在通過語言與安全驗證後可直接回覆，避免重複呼叫模型；空白或不安全輸出才由 Final Composer 使用「原始問題 + verified observations」生成。
- V2 error 不自動切回 legacy。

Pending confirmation 在 Planner 前處理。Match/calendar confirmation 預設 15 分鐘到期；calendar action draft 15 分鐘、recent-context draft 30 分鐘。狀態或 proposal revision 改變時，相關 confirmation 立即失效。

## 6. Tool Registry

### 唯讀工具

| Tool | 回傳的產品真相 |
| --- | --- |
| `system.get_current_time` | 本回合固定時間、日期、星期與相對日期 |
| `calendar.list_my_events` | 本人已授權行程；預設未來 90 天或問題指定日期 |
| `match.get_status` | Canonical search/proposal/terminal state 與聊天室狀態 |
| `match.get_counterparty_summary` | 唯一有效／已接受對象的公開名稱、近況、個性摘要、共同點與 chat state |
| `profile.get_recent_context` | 本人已保存的 current context 與 revision |
| `memory.search_my_profile` | 本人記憶摘要、近期情境及最多 8 筆偏好 |
| `relationship.get_verified_evidence` | 已接受關係中可驗證的共同互動摘要 |
| `relationship.get_mentioned_contact_summary` | 本回合 @ 的已接受聯絡人公開近況、初始想認識的事、個性摘要與可驗證共同點；只有有有效 @ 時可見 |
| `web.search` | Tavily 最新公開搜尋結果；只有設定 `TAVILY_API_KEY` 時可見 |
| `web.extract` | 本回合搜尋結果或 owner 明確提供公開網址的有限內容；拒絕內網與非 HTTP(S) URL |
| `places.search_nearby` | 以本人手動保存的粗略所在地或使用者指定地點，查詢附近餐廳／景點等公開地點 |
| `places.measure_distance` | 計算兩個公開地點的直線距離；不保存精確住址或即時定位 |

### 寫入工具

| Tool | Confirmation | 寫入 owner |
| --- | --- | --- |
| `match.start_search` | 必須 | `match_action_service.start_match_search` |
| `match.decide_active_proposal` | 只在唯一可決定 proposal 可見；由 revision Guard 保護 | `match_action_service.decide_active_proposal` |
| `calendar.create_my_event` | 必須 | Calendar service |
| `calendar.update_my_event` | 必須 | Calendar/date coordination service |
| `calendar.cancel_my_event` | 必須 | Calendar/date coordination service |

Planner 只提供自然任務參數，例如行程標題、日期、時間或 interested/declined。`user_id`、proposal/event ID、expected status 與 revision 全由 executor 根據登入者及 canonical state 取得。

`@` 是 entity binding 而不是自動查資料的開關：使用者真正詢問對方近況、特質或比較時，Planner 才會讀取公開摘要；普通提及與打招呼不讀。伺服器重新驗證所有 client IDs 是否為已接受聯絡人，且最多允許三位。

## 6.1 Proactive Care

主動關心由 `services/ayue_agent/proactive_scheduler.py` 的 server-side scheduler 產生與保存；不依賴瀏覽器輪詢。`GET /api/proactive_check` 只在使用者在線且不忙碌時，取回一次已保存的 delivery marker。它使用 `services/ayue_agent/proactive_care.py` 的 typed contract，而非 legacy persona prompt：

- context 僅含最近 owner 訊息、前一則阿月回覆、本人近期情境、使用者口吻與 Asia/Taipei 時段；不含 Big Five、配對、對方或行事曆；
- output 必須有 `message`、`focus`、`grounding_span`、`confidence`；角色反轉、無法驗證原文、低信心、壞 JSON 或 provider failure 一律不發送；
- atomic activity claim 確保同一次使用者活動即使多分頁 polling 也最多保存一則；event metadata 只保留 `proactive_care`、run ID 與 grounding source code。
- 頻率設定與每次本人公開阿月訊息都保存 `next_proactive_care_at`；provider 或格式失敗採 1、5、15 分鐘最多三次退避，無 grounding 則安全略過、不發罐頭訊息。

## 6.2 Private mediator V2

`POST /api/mediator/private` 保留 JSON 相容；`POST /api/mediator/private/stream` 在 `AYUE_PRIVATE_AGENTIC_MODE=on` 時提供 `run_started`、`tool_started`、`tool_finished`、`final`、`error` NDJSON 事件。

- Private context 分開保存本人 profile、對方 shareable projection、planner-only advisory、共同聊天室最近 12 則，以及本人自己的悄悄話最近 12 則。
- 對方與阿月的私人悄悄話永不進 context；對方 advisory 只能產生 `warm/playful/calm/direct` 的抽象策略，Final Composer 看不到原始 advisory。
- 目前 typed tools 為 pair summary、shared history、busy/free availability、request fun fact、start date coordination。後兩者都需要 pair-scoped 15 分鐘 confirmation。
- Private trace 不保存雙方 ID、profile、history、arguments 或 observations；V2 失敗不得落回 legacy private router。

## 7. Match lifecycle 與一致性

```mermaid
stateDiagram-v2
    [*] --> draft: 建立 proposal
    draft --> pending: 發起者有興趣
    draft --> declined: 發起者婉拒
    draft --> expired: 逾時
    pending --> accepted: 接收者接受
    pending --> declined: 接收者婉拒
    pending --> declined: 發起者取消
    pending --> expired: 逾時
    accepted --> [*]
    declined --> [*]
    expired --> [*]
```

`draft/pending` 是 live proposal；`accepted/declined/expired` 是歷史／終態。Accepted contact 不應被當成仍在等待的 proposal，也不應讓歷史卡片重新可操作。

每筆新 proposal 另保存 `directional_reason_v3`：每個理由綁定 `viewer_id` 與 `counterparty_id`，並以建立當下的 counterpart context snapshot 組成。所有卡片與通知依目前 viewer 取唯一綁定理由；不得用 `target/candidate` 欄位名稱重新猜測角色。舊 live proposal 可由 `scripts/repair_directional_match_reasons.py` 預設 dry-run 檢查，只有人工 review 後才可使用 `--apply`。

所有卡片與自然語言決策共用同一條寫入路徑：

```text
UI /api/match/decision ─┐
                        ├→ match_action_service → match_decision_service → Mongo CAS
Agent write tool ───────┘                                      │
                                                               └→ committed effects
```

CAS query 同時綁定 participant、expected status 與 expected revision。成功後 revision 加一並追加 `state_history`。Idempotency key 重送回同一結果；stale request 回最新 status/revision，不覆寫已提交終態。

Matchmaker flow 與公開阿月 loop 分離：Mongo vector search 先縮小候選集合，port 9001 matchmaker 再利用 profile、Neo4j 記憶及規則排序。這不是隨機配對；沒有符合 qualification 的人選時可回報沒有結果。

## 8. Calendar 與共同約會

- Public Ayue 可以讀取、建立、修改、取消本人的行程。
- Calendar read 只在使用者已授權時成功。
- 所有 Calendar writes 都需要確認。
- 私人行程直接修改本人資料。
- 共同約會取消時同步雙方並通知對方；改期會建立需對方重新確認的協調狀態。
- 「直接替我向對方約會／代替雙方答應」目前不是 Public Ayue 的能力，不能誤轉成私人日曆新增。
- 對方行事曆細節不能進 prompt 或回覆；跨使用者 availability 僅可使用產品允許的安全 projection。

## 9. Profile Pipeline

Profile extraction 是聊天完成後的 owner-scoped 非同步流程，不是 Agent observations 的副產品：

```mermaid
flowchart LR
    M["Saved owner message"] --> C["message_id idempotent claim"]
    C --> X["Typed Profile Extractor"]
    X --> V["Evidence / subject / confidence validation"]
    V --> R["Programmatic Traditional Chinese projection"]
    R --> P["Profile CAS update"]
    V --> N["Durable memory service"]
```

Extractor 只收到該筆已保存 owner message。`ProfileExtractionDecision` 可提出：

- `recent_context.action: update | clear | none`
- Typed fields：`activity`、`destination`、`timing`、`companion_intent`
- Durable memory：typed key、繁中 label、stance、category、confidence、evidence span

每個 evidence span 必須是 owner message 的連續子字串且 subject 必須是 owner。Match operation、等待回覆、assistant text、tool result、match state 與第三方特徵不得寫入近期情境。時間不是近期情境的必要條件。摘要由程式組合，不把模型自由文字直接存入 `current_context`。

同一 message ID 由 unique claim 保證只處理一次；current-context write 使用 revision CAS，避免較舊的非同步工作覆蓋較新訊息。

### 9.1 Durable memory、relationship graph 與 Context Engine

- 本人 durable preference 的 source of truth 是 Neo4j owner-scoped `HAS_PREFERENCE`；Mongo `profile_memory_preview` 只是 bounded read projection。
- 雙人聊天室的 `semantic_plans` 與 room-scoped KG triples 是 relationship state，不等於任一方的本人長期偏好。
- 系統生成的長期建議是 recommendation，不是 owner fact；不得寫入 Graph preference 或增加 evidence count。
- `context.py` 目前仍是 Public V2 唯一 Context Builder。未來若抽出 Context Engine，必須輸出 versioned typed bundle，再由 Public／Private adapter 套用各自 privacy policy；不可直接回傳 prompt 或 raw Graph。

詳細資料模型、建議 contract、整合點與測試要求見 [`MEMORY_CONTEXT_ENGINE_GUIDE.md`](./MEMORY_CONTEXT_ENGINE_GUIDE.md)。

## 10. Trace 與資料安全

V2 agent trace 保存於 `agent_runs`，tool idempotency 保存於 `agent_tool_calls`。Trace 只允許：

- context/capability version
- visible tool names
- planner kind/tool/confidence
- Guard result codes
- tool name/ok/error code
- event sequence、cache hits、composer outcome
- public progress delivery code、latency、final intent/fallback code

Trace 不保存完整 prompt、owner message、observations、tool arguments/results、對方資料或 raw exception。新增欄位時必須先加入 allowlist 並補 privacy test。

## 11. Runtime flags

Public V2 在程式中的預設值仍是 `off`，部署／App 測試必須明確設定：

| Flag | 用途 |
| --- | --- |
| `AYUE_AGENT_V2_MODE=on|off` | Public V2 或人工 legacy rollback |
| `AYUE_AGENT_V2_USER_ALLOWLIST` | 漸進式指定使用者；空值代表全部適用 mode |
| `AYUE_AGENT_MAX_STEPS` | Read loop 上限，程式硬限制最大 3 |
| `AYUE_DEFAULT_TIMEZONE` | 預設 `Asia/Taipei` |
| `AYUE_PROFILE_SKILLS_MODE=on|dry_run|off` | Profile extractor 寫入、觀察或停用 |
| `AYUE_PROFILE_SKILLS_USER_ALLOWLIST` | Profile rollout allowlist |
| `AYUE_PRIVATE_AGENTIC_MODE` | Private runtime，與 Public V2 分開 |
| `AYUE_PRIVATE_AGENTIC_USER_ALLOWLIST` | Private V2 rollout allowlist |
| `AYUE_MAPS_ENABLED` | 是否提供 OpenStreetMap／Overpass 地點工具 |
| `AYUE_MAPS_MONGO_CACHE` | 是否將地點工具 cache 寫入 Mongo；預設 `off` |

基礎服務另需 `MONGO_URI`、LLM/Ollama、Google embedding；設定 `TAVILY_API_KEY` 後才開啟 Web Search／Extract，選填 `TAVILY_PROJECT`。地點工具使用 Nominatim 與 Overpass 的公開 HTTP API，可用 `OSM_*` 變數替換 endpoint 與 user agent。port 9001 matchmaker 需要 `LLM_*` 與 Neo4j 設定。可提交的欄位範例見 `social_demotest/.env.example` 與 `matchmaker_agent/.env.example`；包含真實密鑰的 `.env` 不得提交或交付。

## 12. App 從 V1 遷移到 V2

### Backend contract

1. 先部署本版本 backend 與 Mongo indexes，保留既有 `/api/direct_chat` JSON endpoint。
2. 在測試使用者 allowlist 設定 `AYUE_AGENT_V2_MODE=on`；確認後再擴大範圍。
3. 不可在 App 自己重建 intent classification。App 只送原始訊息與必要 mention，語意由 V2 Planner 處理。
4. 不可在 V2 timeout/error 時由 App 再呼叫 V1 endpoint；這會造成訊息及副作用重複。

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
2. 接受／婉拒／取消呼叫 `POST /api/match/decision`：

```json
{
  "user_id": "owner",
  "match_id": "server-provided-id",
  "action": "accept",
  "expected_status": "draft",
  "expected_revision": 0,
  "explicit_reasons": []
}
```

3. HTTP 409 代表 stale state；App 重新讀 `/api/match/status` 或 `/api/match/state` 後更新卡片，不重送舊 decision。
4. Accepted match 轉為 contact/chat；歷史 proposal card 保留但不可操作。

### Profile 與 Calendar

- App 不直接產生或提交 `current_context` 摘要；只送 owner 原始聊天訊息，profile pipeline 非同步更新。
- UI 顯示 profile 更新時應讀 server projection，不把 assistant reply 當 evidence。
- 所在地透過 `PATCH /api/profile/location` 手動更新，只保存城市與行政區；第一版只用於顯示與附近公開資訊查詢，不進 matchmaker 排序。
- Calendar CRUD 與共同約會沿用 server revision/state；不要在 client 端只修改畫面。

### Rollback

緊急 rollback 只由部署環境把 `AYUE_AGENT_V2_MODE` 設為 `off` 並重啟服務。Rollback 是人工操作，不是 request-level fallback；App 不需要也不應知道 Planner 是否失敗。

## 13. 驗收基線

每次改動至少驗證：

- 一般聊天自然回答，不因沒有工具而拒答。
- 配對結果、目前日期、本人行事曆與近期情境都經正確 read tool。
- 明確找人先 confirmation，確認後只搜尋一次。
- Planner duplicate、壞 JSON、timeout、低信心不造成重複工具或 legacy fallback。
- Proposal stale revision、重複 request、雙方並發不覆寫終態。
- Profile evidence 可追溯、只來自 owner message，且摘要為繁體中文。
- Stream progress 不進聊天紀錄，事件不洩漏 arguments/results/ID。
- JSON direct chat、其他聯絡人及 private chat 不因 Public V2 改動而破壞。
- Test harness 不連正式 Atlas/Neo4j；完整 deterministic tests、Python compile、兩個服務健康檢查通過。

目前的 trajectory fixtures 位於：

- `social_demotest/tests/fixtures/ayue_public_trajectories.json`
- `social_demotest/tests/fixtures/ayue_refactor_trajectory_catalog.json`

新增真實失敗案例時，優先將它匿名化後加入 trajectory，再修對應 contract、projection 或 prompt；不要先增加一句特例 regex。
