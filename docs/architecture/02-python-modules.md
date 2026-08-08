# 02. Python 模組地圖

> 本篇逐一說明 `social_demotest/` 的 Python 檔案在做什麼，方便新工程師在改動前先定位 owner。所有路徑以 `social_demotest/` 為根（有特別註明者除外）。程式碼是最終真相；若本文與程式碼不符，以程式碼為準並更新本文。

## 1. 啟動層

### `main.py`
FastAPI app 入口。註冊五個 top-level routers（`frontend`、`chat`、`match`、`system`、`calendar`；`chat` 本身是 aggregate，底下再掛 leaf routers）。

`startup` 時依序建立索引並啟動兩個 background worker：

- `start_match_search_worker()` — 配對搜尋 job 的消費 worker（`match_search_job_service.py`）。
- `start_proactive_care_scheduler()` — 主動關心排程 loop（`ayue_agent/proactive_scheduler.py`）。

`shutdown` 時停止這兩個 worker。另外在啟動時建立：calendar、agent run、map cache、profile skill、match 的索引。

### `config.py`
集中讀取環境變數（Mongo、Ollama/LLM 模型、功能旗標等）。測試用 `AYUE_SKIP_DOTENV=1` 跳過 `.env` 載入。

### `database.py`
MongoDB 連線與 collection 句柄（`db`、`profiles_coll`、`matches_coll`、`messages_coll` 等），是各 service 存取 Mongo 的唯一入口。

### `models.py`
Pydantic request/response models（例如 `DirectChatRequest`）。

### `tags.json`
（輔助資料）Tag/標籤定義，供配對資格或 UI 使用。

## 2. HTTP 層（`routers/`）

| 檔案 | 責任 |
| --- | --- |
| `chat.py` | Aggregate router：統一 `/api` prefix 與 `Chat` OpenAPI tag，掛載下列 leaf routers |
| `chat_messages.py` | 聊天紀錄、聯絡人清單（`/api/messages`、`/api/contacts` 等） |
| `chat_onboarding.py` | Big Five／deep profile onboarding 的 HTTP adapters |
| `public_chat.py` | **公開阿月入口**：`POST /api/direct_chat`（JSON）與 `/api/direct_chat/stream`（NDJSON）。保存訊息、驗證 @ 提及、呼叫 V3 scheduler、背景觸發 profile extraction、AI 協助共同約會表單更新 |
| `private_mediator.py` | 悄悄話入口：`/api/mediator/private*`，JSON 與 NDJSON 兩種，內部走 private_v2 runtime |
| `match.py` | 配對端點與 candidate pipeline：搜尋 job 觸發、狀態查詢、`/api/match/decision`（卡片 CAS 決策）、方向性媒合理由（directional reasons）建構 |
| `calendar.py` | 行事曆 CRUD、改期/取消/設定 |
| `relationship_dates.py` | 共同約會 domain service 的 thin HTTP adapters |
| `relationship_quiz.py` | 已接受配對的默契小測驗 |
| `proactive.py` | 主動關心／mediator 事件 polling 的 thin adapter |
| `system.py` | Demo 系統端點：初始化、seed、清除、設定、profile 記憶 UI 端點、debug 端點 |
| `demo.py` | Demo-only reset endpoint |
| `frontend.py` | 服務 `frontend.html` |

注意：`public_chat.py` 是唯一能呼叫 `run_public_agent_turn_v3` 的 HTTP 層；`chat.py` 不再包含自由文字 router，V3 失敗不會掉回 legacy public routing。

## 3. Domain 層（`services/`）

### 配對與關係

| 檔案 | 責任 |
| --- | --- |
| `match_state_service.py` | Canonical match 唯讀模型：`get_match_status_snapshot`、`verified_accepted_match_query`、`reconcile_live_match` |
| `match_decision_service.py` | Atomic match CAS transition（status + revision compare-and-set） |
| `match_action_service.py` | Agent/API 共用的 match action facade：`start_match_search`、`decide_match`、`decide_active_proposal`、`apply_transition_effects`（通知、開聊天室、feedback、GIF 慶祝） |
| `match_search_job_service.py` | 持久化搜尋 job 佇列與 worker：claim、lease、progress、idempotency |
| `match_reason_service.py` | 媒合理由文字的安全投影與 fallback |
| `relationship_engagement_service.py` | Probe、feedback、關係摘要、post-chat engagement state |
| `relationship_quiz_service.py` | 默契小測驗 lifecycle |
| `semantic_plan_service.py` | Accepted pair room 的 shared semantic plan 與 chat triples（**不是** Public owner memory） |

### 行事曆與約會

| 檔案 | 責任 |
| --- | --- |
| `calendar_service.py` | 本人行事曆 CRUD、衝突檢查、存取控制、`get_calendar_context` |
| `date_coordination_service.py` | 共同約會：邀請、表單、改期（reschedule）、取消、雙方同步與通知 |

### 記憶與 Profile

| 檔案 | 責任 |
| --- | --- |
| `profile_skills.py` | 非同步 owner-only profile extraction pipeline（近期情境＋長期記憶），message_id idempotency、evidence span 驗證 |
| `profile_contracts.py` | Typed extraction contract（`ProfileExtractionDecision` 等） |
| `profile_projection.py` | 內部 ID／受保護內容清理與安全投影 |
| `profile_location.py` | 城市／行政區的粗粒度位置正規化 |
| `profile_task_service.py` | 已保存 owner 訊息的 profile extraction 排程 facade |
| `memory_service.py` | Owner-scoped durable memory domain facade、Neo4j outbox、Mongo read projection |
| `assessment_session_service.py` | 聊天室內基本性格（Big Five）／深層探索的 session lifecycle、commit 與 CAS |

### 媒人與對話（`services/ayue_agent/`）

V3 runtime 見 `03-v3-runtime-lifecycle.md`；此處列非 V3 檔案：

| 檔案 | 責任 |
| --- | --- |
| `context.py` | 每回合 privacy-safe context：`build_public_context`、`build_public_agent_turn_context` |
| `contracts.py` | Provider-neutral contracts（`AgentTurnContext`、`PublicAgentTurnContext`、`AgentResult`、`ToolResult`） |
| `router.py` | 只剩 V3 共用的封閉協議：`confirmation_choice`（確認/取消解析）、`_concise_public_reply` |
| `time_context.py` | `build_turn_clock`：台北時間與訊息中相對日期解析 |
| `capabilities.py` | 對使用者一致的產品能力與用詞真相 |
| `tool_registry.py` | **ToolSpec、risk、schemas、progress、argument ownership 的唯一真相**（見 `04-tool-registry.md`） |
| `tools.py` | 唯讀 tool facade 與安全投影（`execute_tool`） |
| `web_tools.py` | Tavily 網路搜尋／extract adapter、URL 安全檢查 |
| `maps_client.py` | OpenStreetMap／Overpass 附近地點、距離、resolve adapter，TTL cache |
| `google_places_client.py` | Google Places v1 / Routes API adapter（選用，失敗自動回退 OSM） |
| `match_opportunity.py` | 配對機會評估：profile basis、active-match block、guidance fingerprint |
| `public_relationship_projection.py` | 已接受聯絡人的公開投影：`validated_mentioned_contact_ids`、`mentioned_contact_summary`、`accepted_contact_summaries` |
| `proactive_care.py` | 主動關心的 typed care surface：claim、generation、grounding 驗證 |
| `proactive_scheduler.py` | Server-side 主動關心排程 loop |
| `private_v2.py` | 悄悄話的獨立 context、registry、composer |
| `private_calendar.py` | Private V2 的 bounded 日期範圍與 busy/free projection helper |

### 其他

| 檔案 | 責任 |
| --- | --- |
| `ai_service.py` | LLM 呼叫包裝：`generate_chat_completion`、`generate_chat_completion_with_tools`（function calling）、embedding、Big Five / deep profile 分析、媒合、共同約會協調 prompt |
| `chat_service.py` | room id 產生、`save_message`（使用者訊息只存一次）、system message 去重 |
| `giphy_service.py` | 配對成功慶祝 GIF（選用） |
| `language_service.py` | 繁中文字正規化 |
| `mediator_context_service.py` | Legacy public/private mediator 共用的 bounded context projection |
| `mediator_event_service.py` | Mediator 事件佇列：`queue_mediator_event`、`claim_next_mediator_event`（polling 用） |
| `proactive_delivery_service.py` | Polling 時 mediator event／care delivery 的 claim 與投遞 |
| `skill_loader.py` | 讀取 `skills/` 下的 versioned prompt policy |

## 4. 媒婆服務（`matchmaker_agent/`）

| 檔案 | 責任 |
| --- | --- |
| `agent_api.py` | FastAPI adapters（port 9001）：`/api/match`（排序）、`/api/feedback`（婉拒反思→寫 Neo4j）、`/api/global_reflection`（全域法則）、`/api/memory/*`（偏好記憶 CRUD）、`/api/chat_triples`（雙人聊天三元組）、`/api/clear_graph`（demo）、`/health` |
| `matchmaker.py` | `MatchmakerAgent`：以 LLM 依 context 30% / graph memory 25% / deep profile 20% / Big Five 15% / 話題 10% 加權評估候選人，輸出嚴格 JSON（0 或 1 位人選） |

## 5. 修改指南（owner 對照）

- 改公開阿月行為 → 先看 `ayue_agent/v3/` 對應層，不要直接改 `public_chat.py` 的 domain logic。
- 改工具 → 改 `tool_registry.py` + `tools.py`（唯讀）或 `write_executors.py` + domain service（寫入），並更新 Planner prompt。
- 改配對狀態 → `match_decision_service.py` / `match_action_service.py`。
- 改行事曆 → `calendar_service.py` / `date_coordination_service.py`。
- 改記憶 → `profile_skills.py`（extraction）與 `memory_service.py`（durable write facade）。
- 改 UI → 後端 typed contract 完成後才動 `frontend.html`。
- 不得把 Private calendar projection 搬回 router；正式資料 cleanup CLI、模型供應商設定也不在一般 runtime 任務範圍。


> Current ownership override (2026 cleanup): Public is unconditionally V3. Private is the separate current V2 runtime. Bounded availability helpers live in `services/ayue_agent/private_calendar.py`.
