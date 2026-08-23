# Flutter 從 V1 Demo 遷移到 Public V3：Agent 指南

> 對象：曾依照 [`chenjia0510/socialdemo_`](https://github.com/chenjia0510/socialdemo_) 搬過 V1 邏輯，現在要把 Flutter App 接到本 repository 現行 V3 的 coding agent。舊參考倉庫目前 `main` 指向 commit `5aaa20881b9c659d4ba4bfb5566f5628a92ac5ca`（2026-07-02）；它是舊 FastAPI／Web Demo 快照，不是現行 backend contract。

## 1. 先做決策：替換 V1 backend 子系統，不要清空整個專案

建議採用「backend replacement + Flutter adapter migration」，不要逐檔把 V3 合進 V1，也不要把 Flutter repo、資料庫或所有後端資產一刀清空。

如果 Flutter 專案內那份 Python backend 只是從 V1 複製、沒有同學獨有的 server-side domain logic，最安全的做法是：

1. 在新 branch 保存現況與 V1 baseline。
2. 列出同學後來新增的 server-side 差異。
3. 將 copied V1 的 `social/`、`matchmaker_agent/` 與啟動設定視為一個可替換子系統。
4. 以本 repo 的現行 V3 backend 作新 baseline。
5. 只把確認仍需要的自訂差異，依 V3 owner／typed contract 逐項重做。
6. Flutter UI 透過 API adapter 遷移，不把 Python agent orchestration 改寫成 Dart。

這不是「在原 branch 先刪光再慢慢補」。替換必須發生在可回復的 migration branch，且舊系統在 V3 contract tests 與 Flutter smoke tests 通過前仍可啟動比對。

### 應保留、替換與重做的範圍

| 類別 | 處理方式 | 例子 |
| --- | --- | --- |
| Flutter client | 保留 | 畫面、導航、主題、平台設定、既有 state management、登入流程 |
| Flutter API/data layer | 重做 adapter | NDJSON stream、typed response models、409 refresh、confirmation UI |
| 複製自 V1 的 Public Ayue backend | 整體替換 | `routers/chat.py` 內單一 prompt／keyword flow、V1 memory observer、V1 match fallback |
| 同學新增的 backend domain logic | 逐項移植 | 先確認 owner，再接到 V3 domain service／ToolSpec；不可直接貼回 router |
| 正式／共享資料 | 保留並另做 migration | Mongo collections、Neo4j、使用者帳號、已建立關係、行事曆 |
| secrets 與環境設定 | 重新建置 | 從 `.env.example` 建立，不複製或提交舊 `.env`、log、cache |

## 2. 為什麼不建議 line-by-line merge

V1 與 V3 不是同一 loop 的小改版。V1 把公開阿月對話、profile 更新、配對、主動訊息與部分關係功能集中在大型 router／prompt；V3 的 Public flow 是：

```text
Context Builder
  → Planner（typed DAG）
  → RuntimeRegistration / domain runtimes
  → Central Guard + Tool Registry
  → canonical domain services
  → Synthesizer
  → JSON / NDJSON final
```

逐檔 merge 最容易把下列 V1 行為帶回來：

- 在 router 以關鍵字判斷 intent。
- LLM 直接提供 user／match／event ID 或 revision。
- 第一次模型決定就執行寫入，跳過 confirmation。
- 配對 provider 失敗時硬選第一位候選人。
- `/api/memory/observe` 從任意聊天自由萃取記憶，或主服務直連 Neo4j fallback。
- Public V3 失敗後切回舊單一 loop。
- Client 從阿月文字猜配對／行事曆狀態。

這些都不是 V3 的相容路徑。V3 failure 必須 fail closed；Private V2 只是仍在使用的悄悄話 sibling runtime，不是 Public fallback。

## 3. 開始修改前的 source of truth

Agent 必須依序閱讀：

1. [`AGENTS.md`](../AGENTS.md)
2. [`AYUE_V3_ARCHITECTURE.md`](../AYUE_V3_ARCHITECTURE.md)
3. [`docs/architecture/09-runtime-interfaces.md`](./architecture/09-runtime-interfaces.md)
4. 要整合的 domain 文件，例如 `subagent-match.md`、`subagent-calendar.md`、`subagent-relationship.md`
5. 現行 Pydantic models、FastAPI OpenAPI 與 deterministic tests

不要把舊 repo 的 Markdown、route 實作或 prompt 當成 V3 規格。若文件與程式不一致，以現行 typed contract 為準並同步修文件。

## 4. Flutter 必須改的 HTTP contract

### 4.1 公開阿月聊天

| 項目 | V1 使用方式 | V3 整合方式 |
| --- | --- | --- |
| JSON chat | `POST /api/direct_chat` | 保留相容；`contact_id="ai_assistant"` 永遠進 Public V3 |
| Streaming | 無正式 Public stream | Public UI 優先 `POST /api/direct_chat/stream`，回 `application/x-ndjson` |
| Request | `user_id`、`contact_id`、`message` 等 | 保留原欄位，另有 optional mentions／assessment action；只傳 client 真正持有的值 |
| Final | 單一 JSON reply | stream 的 `final.response` 使用同一 JSON response contract；`agent_version="v3"` |
| Progress | 無或靠文字猜 | 只處理 `tool_started.text`，顯示一個暫時 bubble |

Public stream 只接受以下 event：

```text
run_started | tool_started | tool_finished | final | error
```

Flutter 的 parser 應以 UTF-8 + line splitter 逐行 decode JSON，不可當 SSE，也不可等整個 body 結束後才 parse。處理規則：

- `run_started`：保存本次 opaque `agent_run_id`，不顯示 internal debug。
- `tool_started`：建立或更新唯一 progress bubble。
- `tool_finished`：可更新進度狀態，但不要新增聊天訊息。
- `final`：清掉 progress，渲染 `response.messages`；舊畫面可 fallback 到 `response.reply`。只保存 server 已保存的結果，不要再 POST 一次相同 owner message。
- `error`／disconnect：清掉 progress，顯示 bounded error；不可改呼叫另一條 V1 runtime。

同一送出動作只能選 JSON 或 stream 其中一個 endpoint，不能兩個都打，否則 owner message 會重複。

### 4.2 非 Public 對話與悄悄話

- 一般聯絡人仍可使用 `/api/direct_chat` JSON；即使呼叫 stream endpoint，也只會得到一筆 `final` wrapper，沒有 Public sub-agent progress。
- 阿月悄悄話使用 `/api/mediator/private` 或 `/api/mediator/private/stream`，維持獨立 Private V2 contract。
- 不得把 Public history／context 傳進 Private，也不得讓 Flutter 根據 UI 狀態自行宣告 accepted relation；server 會重新驗證。

### 4.3 `@` mentions

`DirectChatRequest` 可帶 `mentioned_other_id`、`mentioned_other_ids` 與 `mentions_inline`。Client ID 只是一個 request hint；server 會依 canonical accepted relations 重驗，最多三位。Flutter 不得把未接受使用者包成 mention，也不能把 ID 顯示到聊天內容。

### 4.4 配對卡片與 CAS

新 UI 優先使用：

```text
POST /api/match/decision
```

Body 必須帶目前卡片取得的 `user_id`、opaque `match_id`、`action`、`expected_status` 與 `expected_revision`。`/api/match/accept`、`/api/match/decline` 仍是相容 facade，不應成為新 Flutter code 的主要入口。

收到 HTTP 409 時：

1. 不重送舊 decision。
2. 重新讀 `/api/match/status` 或該卡片的 `/api/match/state`。
3. 以 server 最新 status／revision 更新 UI。

Flutter 不從阿月回覆文字推測 `draft/pending/accepted/declined/expired`，也不自行開聊天室。`accepted` 是已建立聯絡關係，不是仍在進行的 active proposal。

### 4.5 寫入與 confirmation

Public Ayue 的 match search、match decision、profile assessment、calendar mutation、Relationship date-card 都必須先 confirmation。Flutter 只呈現 server reply／pending preview，使用者明確確認後再把「確認」送回同一 Public chat；client 不直接執行 agent tool，也不生成 match/event/revision authority。

Calendar 畫面自己的 typed CRUD endpoint 可以保留；自然語言 Calendar 寫入則由 Public V3 的 `calendar.submit_commands` 流程處理。共同約會卡走 `/api/relationship/date/*` 的 typed endpoints；Public date-card capability只建立空白卡，後續細節由雙方填寫。

### 4.6 Memory 與 port 9001

- Flutter 只呼叫 port 8000 的產品 API，不直接呼叫 9001 matchmaker／Neo4j endpoints。
- V1 的 `/api/memory/observe` 已刪除，不能移植。
- Owner profile extraction 由 server 在保存 owner 原句後排入 `profile_skills.py`，再經 `/api/memory/apply` 寫入 9001。
- Assistant reply、conversation history、tool result、match state 與對方資料都不能成為 owner memory write source。

## 5. 資料與部署：不要清資料庫

替換程式碼和清除資料是兩件事。除非使用者另行批准，不得呼叫 demo clear endpoints、`/api/clear_graph`，也不得刪 Mongo／Neo4j。

Migration branch 應先做：

1. 備份並列出 V1 collections／indexes／環境變數。
2. 用現行 V3 建立 indexes；正式修復腳本先 dry-run。
3. 對 `draft → pending → accepted` lifecycle、revision、已接受聯絡人、calendar events 與 memory projection 做抽樣驗證。
4. 只有 review 過的 migration 才 apply；V3 不可為讀取舊資料而恢復 V1 runtime。
5. Port 8000 `/api/health` 與 port 9001 `/health` 都通過後才切 client base URL。

## 6. 建議的 migration 順序

### Phase A：凍結與盤點

- 建立 migration branch／tag；保存可啟動的 Flutter + V1 baseline。
- 列出 Flutter 使用的每個 endpoint、request model、response field、polling timer 與卡片狀態。
- 將差異分類為 client-only、API adapter、server domain customization、可刪 V1 code。

### Phase B：建立乾淨 V3 backend baseline

- 直接使用本 repo 的 `social/` 與 `matchmaker_agent/`，不要從 V1 router 逐段貼回。
- 依 `.env.example` 重建設定，不搬 secrets／logs／cache。
- 先跑 compile、deterministic suite 與 health checks。

### Phase C：先接最穩定的讀取介面

- `/api/health`、`/api/init`、contacts、messages、settings。
- 為每個 response 建 typed Dart model；未知 additive field 要可忽略。
- 保留 server 傳回的 opaque card/reference value，不在 client 解讀 internal identity。

### Phase D：遷移 Public stream

- 實作 NDJSON line parser、單一 progress bubble、terminal cleanup、disconnect handling。
- 用 `messages` 渲染多 bubble，`reply` 只作相容 fallback。
- 確認一次送出只保存一筆 owner message、一筆 final assistant reply。

### Phase E：遷移有狀態的 UI

- Match card 改 `/api/match/decision` + expected revision + 409 refresh。
- Calendar／共同約會使用 typed state，不讀聊天文字猜狀態。
- Private Ayue 維持獨立 endpoint／view model。

### Phase F：才移植同學的自訂功能

每項功能先找 V3 owner：新 read capability 進 Tool Registry + typed projection；新 write 先進 canonical domain service，再補 confirmation、ownership、CAS、idempotency、stale 與 concurrency tests。不得把 domain logic 放回 Flutter、router 或另一個 keyword agent。

## 7. Agent 禁止事項

- 不得在沒有 verified inventory 的情況下刪除整個 Flutter repo、Git history、資料庫、migration／cleanup scripts 或 seed/demo assets。
- 不得把 V1 `routers/chat.py`、單一 prompt、`/api/memory/observe` 或「provider 失敗選第一位」fallback 搬回 V3。
- 不得把 Public V3 和 Private V2 合併。
- 不得讓 client 或模型決定 user/match/event ID、revision、accepted relation 或 write authority。
- 不得同時呼叫 JSON 與 stream chat endpoint。
- 不得把 progress、debug、tool name 或 raw provider payload存成聊天訊息。
- 不得用 reply 文案推測 canonical state。
- 不得因 migration 方便新增中文 keyword／regex router。

## 8. 最低驗收清單

Backend：

- Public `ai_assistant` JSON 與 stream 都回 `agent_version="v3"`，沒有 Public fallback。
- NDJSON 對外只有五種 allowlisted events，且不含 arguments/result/ID/revision。
- 所有五個 WRITE tools 都先 confirmation。
- Match decision stale 回 409，終態不被覆寫。
- Profile extraction 只使用已保存 owner 原句。
- Public／Private context 隔離測試通過。
- `GET /`、`GET /api/health`、port 9001 `/health` 回 200。

Flutter：

- Stream 可處理 chunk 被任意切斷、一個 chunk 多行、空行、error 與 disconnect。
- Progress bubble 永遠最多一個，final/error/disconnect 都會清除。
- 同一 owner send 只呼叫一個 endpoint。
- Match 409 會 refresh，不會重送舊 action。
- `messages`、`sources`、`place_cards`、`presentation_blocks` 都以 optional/additive 欄位解析；public cards 關閉時畫面仍正常。
- 非 Public chat、Private Ayue、Calendar、共同約會與 notifications 有各自 typed flow。

## 9. 可直接交給整合 agent 的任務描述

```text
請把目前 Flutter App 從 chenjia0510/socialdemo_ 的 V1 backend contract
遷移到 Dating-App 現行 Public V3。不要在 V1 backend 上逐檔合併；先保留
Flutter UI／平台設定／登入與 client state，將 copied V1 backend 視為可替換
子系統，以現行 V3 social + matchmaker_agent 建立新 baseline。

修改前閱讀 AGENTS.md、AYUE_V3_ARCHITECTURE.md、
docs/architecture/09-runtime-interfaces.md 與相關 subagent 文件。先盤點所有
Flutter endpoint 與自訂 backend 差異，再依本指南分 phase 遷移。Public chat
優先使用 /api/direct_chat/stream 的 NDJSON 五事件 contract；同一訊息不可再打
JSON endpoint。Match card 使用 /api/match/decision + expected_status +
expected_revision，409 時 refresh。所有寫入先 confirmation；不得從 reply 猜
state、不得恢復 V1 keyword router/memory observer/fallback，也不得清資料庫。

每個 phase 都要附：修改檔案、contract test、實際測試結果、尚未移植項目。
在 V3 backend suite、health checks 與 Flutter stream/CAS smoke tests 全通過前，
不要刪除可回復的 V1 baseline branch/tag。
```
