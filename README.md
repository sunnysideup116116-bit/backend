# Graduate Project — 統一 AI 社交 / 媒合平台

> 本 README 由 **GitNexus 知識圖譜**（12419 symbols · 30883 relationships · 214 execution flows）加上對實際原始碼的逐步閱讀彙整而成。
> 知識圖譜索引：`Graduate_Project`（12419 symbols · 30883 relationships · 214 execution flows）。
> 重新索引指令：`node .gitnexus/run.cjs analyze`（或 `npx gitnexus analyze`）。

---

## 目錄

1. [專案概覽](#1-專案概覽)
2. [系統架構](#2-系統架構)
3. [Port 總覽 — 每個服務對應的 port](#3-port-總覽--每個服務對應的-port)
4. [服務責任表](#4-服務責任表)
5. [資料層](#5-資料層)
6. [App 點擊按鈕 → 觸發服務與流程](#6-app-點擊按鈕--觸發服務與流程)
   - 6.1 [登入 / 註冊](#61-登入--註冊)
   - 6.2 [個性對話（Big Five / Deep Profile）](#62-個性對話big-five--deep-profile)
   - 6.3 [與阿月（媒人助理）聊天](#63-與阿月媒人助理聊天)
   - 6.4 [開始配對（翻名單）](#64-開始配對翻名單)
   - 6.5 [接受 / 婉拒配對](#65-接受--婉拒配對)
   - 6.6 [配對成功後聊天（雙人聊天室）](#66-配對成功後聊天雙人聊天室)
   - 6.7 [私下找媒人打聽 / 約會協調](#67-私下找媒人打聽--約會協調)
   - 6.8 [默契小測驗 / 抽話題](#68-默契小測驗--抽話題)
   - 6.9 [AI 聊天教練（Guidance Copilot）](#69-ai-聊天教練guidance-copilot)
   - 6.10 [風險偵測與介入（每一次訊息）](#610-風險偵測與介入每一次訊息)
   - 6.11 [通知 / 主動配對輪詢](#611-通知--主動配對輪詢)
   - 6.12 [設定頁](#612-設定頁)
   - 6.13 [系統初始化 / 種子資料 / 清空](#613-系統初始化--種子資料--清空)
7. [後端關鍵執行流程（GitNexus 追蹤）](#7-後端關鍵執行流程gitnexus-追蹤)
8. [啟動方式](#8-啟動方式)
9. [功能區域（GitNexus 自動偵測）](#9-功能區域gitnexus-自動偵測)
10. [備註](#10-備註)

---

## 1. 專案概覽

這是一個**統一 AI 社交 / 媒合平台**，由四個協作層組成：

| 層 | 角色 | 技術 |
|---|---|---|
| **DatingApp/** | 使用者端 App（手機 / 桌面） | Flutter (Dart) |
| **Server/** | Python FastAPI 後端（統一入口 + 三個子服務） | FastAPI |
| **MongoDB Atlas (cloud)** | 雲端 MongoDB（含 `$vectorSearch`） | pymongo |
| **Appwrite (cloud)** | 使用者身份 / 個人資料 / 風險知識庫 (KB) | Appwrite SDK |

整個系統**不再使用 MySQL / SQLite**；風險後端已從 MySQL 遷移至 Appwrite KB database，其餘後端原本就用 Appwrite + MongoDB Atlas。

> 本地 MongoDB（`Server/docker/local-mongo/`）已停用且設為開機不自啟；後端透過 `Server/.env` 的 `MONGO_URI` / `AI_CHAT_MONGO_URI` 連到 **MongoDB Atlas 雲端**。若要本地開發可參考 §8.3 重新啟用。

---

## 2. 系統架構

```
┌──────────────┐   HTTP    ┌────────────────────────────────────────────────┐         ┌────────────────┐
│  DatingApp   │ ─────────▶│            Server (FastAPI :8000)              │◀────────▶│  MongoDB Atlas │
│  (Flutter)   │           │  main.py → mounts main_app + ai_gen routers   │  pymongo│  (雲端 Cluster) │
└──────────────┘           │                                                │         │  profiling_db  │
                           │  /            ← frontend HTML                  │         │  ai_chat_db    │
                           │  /api/*       ← main_app  (core)               │         └────────────────┘
                           │  /ai-gen/*    ← ai_gen                          │
                           │  /health      ← main.py                        │
                           └───────────────┬────────────────────────────────┘
                                           │ HTTP (localhost:9001)
                                           ▼
                           ┌──────────────────────────────┐   ┌────────────────────────┐
                           │  matchmaker_agent :9001      │   │  risk_backend :8001    │
                           │  媒婆 Agent (LLM + Neo4j)     │   │  風險偵測 / 介入引擎    │
                           └──────────────┬───────────────┘   └────────────┬───────────┘
                                          │ Neo4j (GraphMemory)             │ Appwrite SDK
                                          ▼                                 ▼
                           ┌──────────────────────────────┐   ┌────────────────────────┐
                           │  Neo4j (Aura / 本地)          │   │  Appwrite (cloud)      │
                           │  User–Trait 偏好圖譜          │   │  user auth/profiles +  │
                           │  GlobalRule 全域法則          │   │  risk KB database      │
                           └──────────────────────────────┘   └────────────────────────┘
```

### 統一入口 `Server/main.py`
- 載入頂層 `.env`（`override=True`）。
- 把 `main_app/`、`ai_gen/` 加入 `sys.path` 並 import 它們的 routers。
- 內掛載：
  - `main_app` routers → `/`（前端 HTML）、`/api/*`（chat、match、system、guidance、frontend）
  - `ai_gen.app:router` → `/ai-gen/*`
- `matchmaker_agent` 與 `risk_backend` 是**獨立行程**，分別在 `:9001` 與 `:8001`。
- `start_all.sh` 依序啟動：risk_backend（背景）→ matchmaker_agent（背景）→ main.py（前景）。

---

## 3. Port 總覽 — 每個服務對應的 port

| Port | 服務 | 入口檔 | 說明 |
|------|------|--------|------|
| **8000** | 統一 FastAPI 主伺服器 | `Server/main.py` | 前端頁面 `/`、核心 API `/api/*`、AI Gen `/ai-gen/*`、健康檢查 `/health`。內掛 `main_app` + `ai_gen`。 |
| **8001** | 風險偵測後端 (risk_backend) | `Server/risk_backend/main.py` | 獨立 FastAPI。`/api/v1/risk/detect`、`/api/v1/risk/feedback`、`/api/v1/risk/state`。被 `main_app/services/risk_client.py` 透過 HTTP 呼叫。 |
| **9001** | 媒婆 Agent (matchmaker_agent) | `Server/matchmaker_agent/agent_api.py` | 獨立 FastAPI。`/api/match`（LLM 配對決策）、`/api/feedback`（回饋→Neo4j）、`/api/global_reflection`（全域反思）、`/api/memory/*`（偏好記憶）、`/api/clear_graph`。 |
| **27017** | ~~本地 MongoDB~~（已停用） | `Server/docker/local-mongo/` | 已停止容器並設為開機不自啟。後端實際連到 **MongoDB Atlas 雲端**（`Server/.env` 的 `MONGO_URI` / `AI_CHAT_MONGO_URI`）。 |
| **MongoDB Atlas (cloud)** | 雲端 MongoDB | `Server/.env` | `profiling_db`（配對/聊天）、`ai_chat_db`（AI Gen）。含 `$vectorSearch`。 |
| **Appwrite cloud** | 身份 + KB | `DatingApp/lib/services/appwrite_config.dart`、`main_app/services/appwrite_service.py` | 使用者 auth、profile collection、Storage bucket、風險 KB database。 |
| **Neo4j (Aura/本地)** | 圖譜記憶 | `matchmaker_agent/agent_api.py` | `User–[HAS_PREFERENCE]→Trait`、`Agent–[LEARNED_RULE]→GlobalRule`。 |

> Flutter App 預設透過 `MATCHMAKER_API_URL` 或 `http://127.0.0.1:8000` 連到主伺服器（`matchmaking_api_service.dart:529`）。

---

## 4. 服務責任表

| 服務 | 資料夾 | Prefix | DB | 責任 |
|---|---|---|---|---|
| Core / main_app | `Server/main_app/` | `/`, `/api/*` | MongoDB Atlas `profiling_db` + Appwrite | 前端 HTML、聊天（SSE 風格）、配對（vector search + Agent）、guidance copilot、系統 init/seed、通知、設定、關係小遊戲、媒人私聊、約會協調 |
| Matchmaker agent | `Server/matchmaker_agent/` | `:9001` (standalone) | Neo4j | LLM 媒婆：配對決策、回饋→圖譜反思、全域抽象化法則、偏好記憶萃取 |
| AI gen | `Server/ai_gen/` | `/ai-gen/*` (掛在 8000) | MongoDB Atlas `ai_chat_db` | track-message 訊號追蹤、semantic-plan 語意計畫 + 知識圖譜三元組、generate-suggestion（Gemini）UI nudge |
| Risk backend | `Server/risk_backend/` | `:8001` (standalone) | **Appwrite KB database** | 風險偵測：rule engine + NLP engine + fusion + scenario + state machine + intervention engine + guardrail；被 `main_app/services/risk_client.py` 呼叫 |
| ~~local-mongo~~ | `Server/docker/local-mongo/` | — | — | **已停用**。Docker Compose（MongoDB + mongot）原本供本地開發；目前後端連 Atlas。要重新啟用見 §8.3 |

---

## 5. 資料層

| 資料 | 儲存 | 誰讀寫 |
|---|---|---|
| 使用者身份 / 個人資料 / 大頭照 | Appwrite cloud（`users`、profile collection、Storage bucket） | Flutter `auth_service.dart`、後端 `main_app/services/appwrite_service.py` |
| 配對 profile / matches / messages / context_embedding | **MongoDB Atlas** `profiling_db` | `main_app/routers/*`、`main_app/services/*` |
| AI Gen 聊天 log / semantic plan | **MongoDB Atlas** `ai_chat_db` | `ai_gen/app.py` |
| 風險知識庫（kb_configs / kb_rules / kb_interventions / kb_hard_blocks …） | **Appwrite KB database** | `risk_backend/app/services/kb_service.py` |
| 風險狀態歷史 / 介入 log / 訊息 log | Appwrite KB database | `risk_backend/app/services/chat_log_service.py` |
| 使用者偏好圖譜 / 全域法則 | Neo4j | `matchmaker_agent/agent_api.py` |
| 配對候選人 vector index | **MongoDB Atlas** `profiling_db.profiles`（`vector_index`） | `main_app/routers/match.py` |

---

## 6. App 點擊按鈕 → 觸發服務與流程

> 以下逐一說明 Flutter App 中每個使用者動作會觸發哪個服務、走哪個 port、以及完整流程。
> 流程圖中的 `file:line` 可直接跳到原始碼。

---

### 6.1 登入 / 註冊

**進入點**：`DatingApp/lib/main.dart:60` `_checkAuthStatus` → 未登入時顯示 `LoginPage`。

#### 登入（Email/Password）
```
[LoginPage 登入按鈕]                                              port: Appwrite cloud
   └─ AuthService.login(email, password)                          auth_service.dart:55
        └─ Appwrite Account.createEmailPasswordSession            Appwrite SDK
             └─ 成功 → _navigateAfterLogin → MainPage
```
- **Port**：Appwrite cloud（HTTPS），不經過自家後端。
- **服務**：Appwrite Auth。

#### Google 登入
```
[LoginPage Google 按鈕]
   └─ AuthService.signInWithGoogle()                              auth_service.dart:79
        └─ Appwrite Account.createOAuth2Session(provider: google)
```

#### 註冊
```
[RegisterPage 註冊按鈕]
   └─ AuthService.register(email, password, name)                 auth_service.dart:64
        └─ Appwrite Account.create
   → ProfileSetupPage 填寫性別/年齡/地區/興趣
   └─ AuthService.createUserProfile(...)                          auth_service.dart:106
        └─ Appwrite Databases.createDocument
   └─ AuthService.uploadProfilePhoto                              auth_service.dart:174
        └─ Appwrite Storage.createFile
```

> 登入/註冊**完全不碰 8000/8001/9001**，只走 Appwrite cloud。

---

### 6.2 個性對話（Big Five / Deep Profile）

**頁面**：`PersonalityChatPage`（`personality_chat_page.dart`）。
**目標**：透過與 AI 對話萃取 Big Five 性格 + Deep Profile 價值觀，作為後續配對依據。

#### 開啟頁面 / 載入狀態
```
initState                                                        personality_chat_page.dart:42
 └─ _initialize
      ├─ AuthService.getCurrentUser                              → Appwrite
      ├─ MatchmakingApiService.getMatchingAnalysis               matchmaking_api_service.dart:643
      │    └─ GET :8000/api/init?user_id=...                      routers/system.py:21
      │         └─ 讀 MongoDB profiles_coll → 回傳 is_complete, my_bf_summary ...
      └─ getPersonalityMessages (若已有歷史)
           └─ GET :8000/api/chat/messages/big_five?user_id=...    routers/chat.py:892
```

#### 發送訊息（state = big_five 或 deep_profile）
```
[輸入框 送出按鈕]                                                  port: 8000 (main_app) + Ollama
 └─ MatchmakingApiService.sendPersonalityMessage                 matchmaking_api_service.dart:620
      └─ POST :8000/api/chat  {user_id, message, state}           routers/chat.py:807
           ├─ state == "big_five"
           │    └─ analyze_big_five(message, prev, count, interest)  services/ai_service.py
           │         └─ Ollama LLM 生成 Big Five + reply
           │    └─ profiles_coll.update_one (存 temp_big_five / big_five)
           │    └─ 回傳 {reply, big_five, is_complete}
           └─ state == "deep_profile"
                └─ analyze_deep_profile(...)                      services/ai_service.py
                └─ 存 temp_deep_profile / deep_profile
```
- **觸發服務**：`main_app`（:8000）→ `services/ai_service.py` → 本地 Ollama（`OLLAMA_FAST_CHAT_MODEL`）。
- **完成後**：`is_complete=True` 時寫入正式 `big_five` / `deep_profile` 並由阿月留一句開場訊息。

#### 重置
```
[重置按鈕] POST :8000/api/chat/reset  routers/chat.py:878
```

---

### 6.3 與阿月（媒人助理）聊天

**頁面**：`MatchChatPage`（`match_chat_page.dart`）。
**角色**：阿月是媒人，會邊聊邊萃取 `context_signals`（activity/timing/preference/companion_intent），當「配對就緒分數」達標時主動詢問要不要翻名單。

#### 載入歷史訊息
```
initState                                                        match_chat_page.dart:37
 └─ _initializeUser
      ├─ AuthService.getCurrentUser                              → Appwrite
      └─ getAssistantMessages(userId)                            matchmaking_api_service.dart:546
           └─ GET :8000/api/messages/ai_assistant?user_id=...     routers/chat.py:892
                └─ 若無訊息 → save_message("哈囉，我是阿月…")（MongoDB）
```

#### 發送訊息給阿月
```
[輸入框 送出]                                                      port: 8000 → 8001 → 9001(記憶) + Ollama
 └─ sendAssistantMessage                                         matchmaking_api_service.dart:534
      └─ POST :8000/api/direct_chat  {user_id, contact_id:'ai_assistant', message}  routers/chat.py:909
           ├─ risk_client.check_risk(...)                         services/risk_client.py
           │    └─ POST :8001/api/v1/risk/detect                   risk_backend
           ├─ save_message(user, message) → MongoDB
           ├─ (背景) observe_user_memory(user, message, "global")
           │    └─ POST :9001/api/memory/observe                   matchmaker_agent → Neo4j
           ├─ 判斷 match_search 狀態 / active proposal / intent
           │    ├─ awaiting_confirmation → classify_proposal_intent
           │    │    ├─ accept → trigger_proactive_match(背景) → /api/match/request → 9001
           │    │    └─ decline → cancel
           │    ├─ active proposal → classify_proposal_intent("proposal_response")
           │    │    ├─ accept → accept_match → /api/match/accept
           │    │    └─ decline → decline_match → /api/match/decline → 9001 /api/feedback
           │    └─ is_match_status_question → match_status_reply
           ├─ 組 MEDIATOR_PERSONA prompt + 使用者資料 + 已接受配對證據
           │    └─ generate_chat_completion (Ollama, JSON)
           ├─ 解析 AI JSON：
           │    ├─ conversation_intent / explicit_match_request / context_signals
           │    ├─ readiness_score = deterministic_readiness(intent, signals)
           │    ├─ 若 context_should_update → 更新 current_context / context_embedding / revision
           │    └─ 若 readiness >= 75 且無 active match → 進入 awaiting_confirmation
           │         └─ AI 回覆附加「要我開始翻名單嗎？」
           ├─ 若 explicit_match_request=true 且無 active → trigger_proactive_match(背景)
           │    └─ /api/match/request → create_proactive_match_proposal → /api/match → 9001 /api/match
           ├─ save_message("ai_assistant", ai_reply) → MongoDB
           └─ 回傳 {reply, is_locked, match_readiness_score, conversation_intent, risk_assessment, ui_priority}
```
- **觸發服務**：`main_app`(:8000) → `risk_backend`(:8001) → `matchmaker_agent`(:9001, 記憶) → Ollama；可能再回到 `:9001` 做配對。
- **背景任務**：`observe_user_memory`、`trigger_proactive_match`、`summarize_relationship`。

#### 主動輪詢
```
_startProactivePolling (Timer.periodic)                          match_chat_page.dart
 └─ proactiveCheck(userId)
      └─ GET :8000/api/proactive_check?user_id=...               routers/chat.py
           └─ 回傳 {has_new, message, surface, type, other_id, matches}
                → 顯示 mediator_card / 通知
```

---

### 6.4 開始配對（翻名單）

**觸發點**：
1. 使用者在阿月聊天中說「幫我找人」→ `explicit_match_request=true`。
2. `MatchResultsPage` 的「開始配對」按鈕（`match_results_page.dart:69` `_requestMatches`）。
3. 阿月主動提案時（`awaiting_confirmation` → 使用者同意）。

#### 顯式配對請求流程
```
[MatchResultsPage 開始配對按鈕]                                   port: 8000 → 9001 → MongoDB
 └─ requestMatches(userId)                                       matchmaking_api_service.dart:722
      └─ POST :8000/api/match  {user_id}  (timeout 2 min)        routers/match.py:801
           └─ generate_matches_for_user                          match.py:402
                1. 載入 user_doc (MongoDB profiles_coll)
                2. 取 / 建 context_embedding (get_embedding)
                3. 排除已配對/已婉拒 user
                4. MongoDB $vectorSearch (vector_index, numCandidates=50, limit=20)
                5. 取每位 candidate 的 deep_profile
                6. strip_agent_payload (壓縮 payload)
                7. POST :9001/api/match {target_user, candidates, target_deep_profile}
                   └─ matchmaker_agent agent_api.py:187
                        ├─ 平行讀 Neo4j：target memory / candidate memory / global rules
                        ├─ agent.match(...) → LLM 多維度思考
                        └─ 回傳 {matches:[{matched_user_id, contrast_label, recommendation_reason, ...}]}
                8. build_validated_match_explanation (證據式評分)
                   ├─ get_user_graph_memories (9001 memory) ← 實際由 main_app services/memory_service
                   ├─ 計算 context / graph / values / personality 分數
                   └─ 產出 reason_items + top_reasons
                9. matches_coll.insert_one (status="draft")
                10. 回傳 {matches:[...], debug_info}
```
- **觸發服務**：`main_app`(:8000) → `matchmaker_agent`(:9001) → Neo4j → Ollama（LLM）→ MongoDB。

#### 主動配對（背景）
```
trigger_proactive_match → create_proactive_match_proposal        match.py:649
 ├─ reconcile_match_state (過期 draft/pending)
 ├─ claim matchmaking_in_progress lock
 ├─ generate_matches_for_user (同上)
 ├─ profiles_coll.update (active_match_proposal_id, match_search=waiting_user)
 └─ queue_mediator_event(user, "match_proposal", matches=[...])
      → 之後使用者透過 proactive_check 輪詢拿到 mediator_card
```

#### 配對狀態查詢
```
GET :8000/api/match/status?user_id=...                           match.py:773
 └─ reconcile_match_state + build_active_proposal_card
```

#### 取消
```
POST :8000/api/match/cancel                                      match.py:765
```

---

### 6.5 接受 / 婉拒配對

**UI**：`MatchResultsPage` 的接受/婉拒對話框（`match_results_page.dart:90` `_respond`）；或在阿月聊天中用自然語言回覆（`classify_proposal_intent`）。

#### 接受（兩階段狀態機）
```
[接受按鈕]                                                        port: 8000 → 9001(反思, 成功時)
 └─ acceptMatch                                                   matchmaking_api_service.dart:737
      └─ POST :8000/api/match/accept  {user_id, match_id}        match.py:805
           ├─ 情境 A：發起者接受 draft → pending
           │    └─ matches_coll.update status=pending
           │    └─ queue_mediator_event(to_id, "incoming_match_interest", matches=[...])
           │         → 對方輪詢時收到邀請卡
           ├─ 情境 B：接收者接受 pending → accepted
           │    └─ matches_coll.update status=accepted
           │    └─ (背景) trigger_global_reflection
           │         └─ POST :9001/api/global_reflection {from/to big_five, context}
           │              └─ agent.generate_global_reflection → 寫 Neo4j GlobalRule
           │    └─ create_ai_opening_message(match_id, from, to, reason)
           │         └─ save_message("ai_assistant", opening) → 雙人聊天室
           │    └─ queue_mediator_event(雙方, "match_connected")
           └─ 回傳 {status, new_status, other_id, other_name}
```
- **觸發服務**：`main_app`(:8000) → (成功時) `matchmaker_agent`(:9001) `global_reflection` → Neo4j。

#### 婉拒
```
[婉拒按鈕]                                                        port: 8000 → 9001(feedback)
 └─ declineMatch                                                  matchmaking_api_service.dart:748
      └─ POST :8000/api/match/decline  {user_id, match_id, explicit_reasons}  match.py:941
           ├─ matches_coll.update status=declined
           ├─ 情境 A：發起者婉拒 draft
           │    └─ (背景) POST :9001/api/feedback {user_id, target_id, action:'decline', target_traits, explicit_reasons}
           │         └─ agent.generate_graph_reflection → 寫 Neo4j HAS_PREFERENCE (偏好/地雷)
           ├─ 情境 B：接收者婉拒 pending
           │    └─ 同上但 user_id=接收者
           └─ 情境 C：發起者撤回 pending（不回饋 Agent）
```
- **觸發服務**：`main_app`(:8000) → `matchmaker_agent`(:9001) `/api/feedback` → Neo4j。

---

### 6.6 配對成功後聊天（雙人聊天室）

**頁面**：`ChatRoomPage`（`chat_room_page.dart`）。
**來源**：`ChatListPage`（`chat_list_page.dart`）點擊聯絡人 → 開啟 `ChatRoomPage`。

#### 載入聯絡人清單
```
[ChatListPage initState]                                          port: 8000 + Appwrite
 └─ getContacts(userId)                                           matchmaking_api_service.dart:686
      └─ GET :8000/api/contacts?user_id=...                       chat.py:1521
           └─ 預設含 ai_assistant；查 matches_coll status=accepted → 加入聯絡人
 └─ 對每位聯絡人 AuthService.getUserProfile → Appwrite（取名字/大頭照）
```

#### 進入聊天室載入訊息
```
[點擊聯絡人 → ChatRoomPage]                                       port: 8000
 └─ ChatService.getMessages(currentUserId, otherUserId)           chat_service.dart:107
      └─ GET :8000/api/messages/{contactId}?user_id=...           chat.py:892
           └─ messages_coll.find({room_id}).sort(timestamp,1) → MongoDB
           └─ 回傳 {messages, active_match_proposal_id}
```
- `room_id = generate_room_id(u1, u2)`（`chat_service.py:4`，兩 id 排序後以 `_` 相連）。
- 只會讀到「已通過風險偵測、已 `save_message` 寫入 MongoDB」的訊息——即 receiver 只看得到審核通過的內容。

#### 發送訊息給配對對象（sender 樂觀顯示 / receiver 等審核）
```
[輸入框 送出]                                                      port: 8000 → 8001 + 9001(記憶)
 └─ ChatRoomPage._sendMessage                                     chat_room_page.dart:142
      ├─ (1) 樂觀插入：ChatMessage.local(...).copyWith(isPending:true)
      │        → setState insert(0) + AnimatedList.insertItem → 氣泡立刻出現（帶轉圈）
      └─ (2) await ChatService.sendMessage                         chat_service.dart:142
           └─ POST :8000/api/direct_chat {user_id, contact_id, message}  chat.py:909
                ├─ risk_client.check_risk → POST :8001/api/v1/risk/detect
                │    ├─ blocked → 回 {is_blocked:true}（訊息不寫入 MongoDB）
                │    └─ 通過 → 繼續下述流程
                ├─ save_message(user, message) → MongoDB          chat_service.py:7
                ├─ (背景) observe_user_memory(user, message, "pair_chat") → 9001 /api/memory/observe
                ├─ mark_post_chat_activity(match_doc, room_id)
                │    └─ matches_coll.update shared_message_count, last_chat_at
                ├─ (背景) 若 shared_message_count >= 6 → summarize_relationship
                │    └─ LLM 產生 shared_summary / common_topics → 存 relationship_memory
                └─ 回傳 {reply: None, message_saved, feedback_scheduled, risk_assessment, ui_priority}
      ├─ (3a) 成功 → 該訊息 copyWith(isPending:false)（轉圈消失）
      ├─ (3b) is_blocked → copyWith(isFailed:true) + SnackBar「訊息因安全考量未送出」
      │         氣泡變灰底+紅框+驚嘆號+刪除線（不擲例外）
      └─ (3c) 例外（網路/timeout）→ 同 (3b) 標記失敗 + SnackBar
```
- **sender 體驗**：送出瞬間就看到自己的訊息（pending 轉圈），不等 server；blocked 或斷線才標記失敗。
- **receiver 體驗**：完全由 server 端把關——`direct_chat` 同步呼叫 `check_risk` 通過後才 `save_message`，receiver 的 polling 只讀得到已寫入 MongoDB 的訊息，所以 b 等審核完畢才看得到。
- **ui_priority**：`risk`（warning/restricted/blocked）或 `coach`（可顯示 AI 教練 nudge）。
- **觸發服務**：`main_app`(:8000) → `risk_backend`(:8001) → `matchmaker_agent`(:9001, 記憶) → Ollama（摘要）。

#### 輪詢新訊息（保留 pending 氣泡）
```
_startPolling (Timer.periodic 3s)                                 chat_room_page.dart:105
 └─ _loadMessages                                                 chat_room_page.dart:65
      └─ ChatService.getMessages → GET :8000/api/messages/{contactId}
           └─ 回傳 server 端已確認的訊息清單
      ├─ 無 pending 本地訊息 → 走原本差異合併（first.id 比對 + insertItem）
      └─ 有 pending 本地訊息：
           ├─ 以 senderId + content 比對 server 清單（local id 與 MongoDB _id 不同）
           ├─ 已被 server 確認的 pending → 移除（server 版本會進入 toPrepend）
           ├─ 仍未確認的 pending → 保留在列表頂端（轉圈繼續）
           └─ toPrepend（server 新訊息、本地無對應 id）→ insertItem 動畫插入
```
- b（receiver）回覆的新訊息也會經由這條 polling 進來；b 端看到的內容同樣是 server 已審核通過的。

---

### 6.7 私下找媒人打聽 / 約會協調

**頁面**：`MediatorPrivateChatPage`（`mediator_private_chat_page.dart`）。
**入口**：`ChatRoomPage` 上的「媒人私聊」按鈕（mediatorUnreadCount 提示）。

#### 載入媒人私聊
```
[ChatRoomPage 媒人按鈕]                                           port: 8000
 └─ getMediatorPrivateMessages(userId, otherId)                  matchmaking_api_service.dart:550
      └─ GET :8000/api/mediator/private/{other_id}?user_id=...   chat.py:1582
           ├─ find_accepted_match (驗證已配對)
           ├─ save_message("ai_assistant", "這裡可以私下跟我打聽...")
           ├─ 清 private_unread
           └─ 回傳 {messages, other_id, unread_count, pending_step, probe_state, mediator_tone}
```

#### 在媒人私聊中發訊
```
[輸入框 送出]                                                      port: 8000 → 9001(記憶) + Ollama
 └─ sendMediatorPrivateMessage                                   matchmaking_api_service.dart:557
      └─ POST :8000/api/mediator/private  {user_id, other_id, message}  chat.py:1722
           ├─ save_message → MongoDB
           ├─ (背景) observe_user_memory(user, msg, "relationship_private", match_id) → 9001
           ├─ 偵測「約」字 → 進入約會協調流程
           │    └─ pending_date_coordination.stage = availability → activity → budget
           │    └─ 對方未填 → queue_mediator_event("date_coordination_request")
           │    └─ 雙方都填完 → date_overlap → queue_mediator_event("date_coordination_result")
           ├─ 偵測取消（「不要約/取消/改天」）→ date_coordination.status=cancelled
           ├─ pending_private_feedback（probe_answer / sentiment / consent）
           │    └─ classify_feedback / feedback_share_consent
           │    └─ 雙方都 positive+consent → queue_mediator_event("mutual_interest")
           ├─ is_relationship_probe_request？
           │    └─ request_relationship_probe → queue_mediator_event(other_id, "probe_question")
           └─ 否則：組 relationship_context prompt → Ollama → reply
```
- **觸發服務**：`main_app`(:8000) → `matchmaker_agent`(:9001, 記憶) → Ollama。
- **快捷按鈕**（`_quickActions`）：「幫我探探他的口風」「幫我問一件他的有趣小事」「幫我們協調約會」「你看完我們最近聊天，有什麼觀察？」。

#### 主動打聽（probe）
```
[快捷按鈕「幫我探探他的口風」]
 └─ matchmakingService? → POST :8000/api/mediator/probe          chat.py:1718
      └─ request_relationship_probe(user_id, other_id, kind)
           └─ queue_mediator_event(other_id, "probe_question", kind)
                → 對方在媒人私聊收到問題 → 回覆 → probe_result 回傳給請求者
```

---

### 6.8 默契小測驗 / 抽話題

**頁面**：`MediatorPrivateChatPage` 中的小遊戲區域（`_funState`）。

#### 開始測驗
```
[開始默契測驗按鈕]                                                port: 8000
 └─ startRelationshipQuiz                                        matchmaking_api_service.dart:580
      └─ POST :8000/api/relationship/quiz/start                  chat.py:2016
           ├─ 建立 quiz (status=active, expires_at=+7天, questions=QUIZ_QUESTIONS)
           └─ queue_mediator_event(other_id, "compatibility_quiz_invite")
```

#### 回答
```
[作答按鈕] POST :8000/api/relationship/quiz/answer               chat.py:2045
 └─ 雙方都答完 → 計算 match_count → save_message("ai_assistant", summary, mediator_card)
```

#### 抽話題
```
[抽話題按鈕] drawRelationshipTopic → POST :8000/api/relationship/topic  chat.py:2111
 └─ 更新 topic_box
```

#### 取消
```
POST :8000/api/relationship/quiz/cancel                          chat.py:2095
```

#### 狀態查詢
```
GET :8000/api/relationship/fun/{other_id}                        chat.py:2008
```

---

### 6.9 AI 聊天教練（Guidance Copilot）

**頁面**：`ChatRoomPage` 中的「AI 建議」按鈕（`_isLoadingSuggestion`）。
**後端有兩套並存**：

#### (A) main_app 本地 guidance（主要使用）
```
[AI 建議按鈕]                                                     port: 8000 + Ollama
 └─ getGuidanceSuggestion                                        matchmaking_api_service.dart:648
      └─ POST :8000/api/guidance/suggestion  {user_id, contact_id, input_text}  chat.py:1467
           ├─ 取最近 8 則 shared chat
           ├─ MEDIATOR_PERSONA prompt → generate_chat_completion (Ollama)
           └─ 回傳 {suggestion, ui_nudge, audit_trail, suggestion_id, role}
```
- **回報活動**：`POST :8000/api/guidance/activity`（`chat.py:1434`）。
- **狀態**：`GET :8000/api/guidance/status`（`chat.py:1451`）。

#### (B) ai_gen（掛在 /ai-gen，目前 App 未直接呼叫，保留）
```
POST :8000/ai-gen/track-message     ai_gen/app.py:233   訊號追蹤（latency/parity EMA）
POST :8000/ai-gen/semantic-plan     ai_gen/app.py:320   語意計畫 + 知識圖譜三元組（Ollama）
POST :8000/ai-gen/generate-suggestion  ai_gen/app.py:431 Gemini 產生 ui_nudge + audit_trail
```
- ai_gen 會依 `s_rat_ema`、`s_inv_penalties`、`h_lat_ema`、`h_par_ema` 動態切換角色：`FRIEND` / `ADVISER` / `MENTOR` / `FACILITATOR`。

---

### 6.10 風險偵測與介入（每一次訊息）

**觸發點**：**每次** `POST :8000/api/direct_chat` 與雙人聊天送出時，後端自動呼叫。

```
main_app chat.py:912                                             port: 8001
 └─ risk_client.check_risk(conversation_id, sender_id, receiver_id, content)
      └─ POST :8001/api/v1/risk/detect                            risk_detection.py:71
           STEP 0: guardrail_engine.check → 命中 hard block → 直接 blocked + 介入 + 寫 log
           STEP 1: chat_log_service.get_memory_context (Appwrite KB)
                    state_machine.get_user_state
                    delivered_history / behavior_history
                    TemporalFeatureService.calculate
           STEP 2: rule_engine.calculate → rule delta
           STEP 3: nlp_engine.analyze → nlp delta
           STEP 5: scenario_risk_layer.evaluate → bonus delta
           STEP 6: fusion.apply_scenario_bonus → final delta
           STEP 7: state_machine.update → new_state, risk_level (safe/warning/restricted/blocked)
           STEP 9: intervention_engine.execute → sender/receiver directive
           (背景) update_message_status / update_temporal_features / log_analysis_detail
                   background_judge_service.review_guardrail_context
                   handle_relationship_update → generate_rolling_summary
           回傳 {risk_level, should_intervene, intervention_command, risk_assessment, diagnostic_signals}
```
- **回前端**：`risk_client.attach_to_response` 加 `ui_priority = "risk" | "coach"`。
- **封鎖**：`is_blocked` 為真時，`/api/direct_chat` 直接回 `is_blocked:true`，訊息不送出。

#### 風險回饋
```
[使用者對介入按「不舒服/舒服」] 
 └─ POST :8000/api/risk/feedback  {triggered_by_msg_id, role, feedback}  chat.py:1424
      └─ risk_client.submit_feedback → POST :8001/api/v1/risk/feedback  risk_detection.py:338
```

#### 查風險狀態
```
GET :8001/api/v1/risk/state?conversation_id=...&user_id=...      risk_detection.py:309
```

---

### 6.11 通知 / 主動配對輪詢

**頁面**：`MatchingPage`（`matching_page.dart`）顯示邀請通知；`MatchChatPage` 主動輪詢。

#### 取得邀請通知
```
[MatchingPage initState]                                          port: 8000
 └─ getNotifications(userId)                                      matchmaking_api_service.dart:699
      └─ GET :8000/api/notifications?user_id=...                  system.py:160
           └─ 查 matches_coll status=pending, to_user=user → 回傳 from_user_big_five/context
```

#### 主動檢查（proactive check）
```
MatchChatPage _startProactivePolling (Timer)                      port: 8000
 └─ proactiveCheck(userId)                                        matchmaking_api_service.dart:615
      └─ GET :8000/api/proactive_check?user_id=...                chat.py
           └─ 從 mediator_inbox 取佇列事件 → {has_new, message, surface, type, matches, other_id}
```

#### 聯絡人清單（含媒人未讀）
```
getContacts → GET :8000/api/contacts                              chat.py:1521
```

---

### 6.12 設定頁

**頁面**：`SettingsPage`（`settings_page.dart`）。

#### 載入使用者資料
```
[SettingsPage initState]                                          port: Appwrite + 8000
 └─ AuthService.getCurrentUser / getUserProfile                  → Appwrite
 └─ getMatchingAnalysis → GET :8000/api/init                      system.py:21
```

#### 更新主動配對頻率
```
[儲存頻率按鈕]                                                     port: 8000
 └─ updateSettings                                               matchmaking_api_service.dart:712
      └─ POST :8000/api/settings  {user_id, proactive_frequency}  system.py:187
```

#### 更新媒人語氣 / probe 模式
```
POST :8000/api/settings/mediator  {user_id, mediator_tone, probe_mode}  system.py:197
```

#### 更新個人資料 / 大頭照
```
AuthService.updateUserProfile / uploadProfilePhoto               → Appwrite
```

#### 重置深層價值觀
```
POST :8000/api/reset_deep_profile  {user_id}                     system.py:305
```

#### 撤回近期情境
```
POST :8000/api/context/undo  {user_id}                           system.py:216
 └─ previous_context → current_context + 重新 embedding
```

#### 個人記憶管理
```
GET  :8000/api/profile/memories?user_id=...                      system.py:229
POST :8000/api/profile/memories/action  {user_id, key, action, value}  system.py:234
 └─ apply_memory_action → (內部) 9001 /api/memory/action
```

#### Debug profile state
```
GET :8000/api/debug/profile_state?user_id=...                    system.py:242
```

---

### 6.13 系統初始化 / 種子資料 / 清空

#### 初始化（App 開啟時）
```
MatchingPage / MatchChatPage initState
 └─ getMatchingAnalysis → GET :8000/api/init?user_id=...         system.py:21
      └─ 回傳 users, is_complete, my_context, my_bf_summary, deep_profile, proactive_frequency,
           mediator_tone, probe_mode, profile_memories, current_context_revision, match_search,
           onboarding_completed
```

#### 種子資料（開發用）
```
POST :8000/api/seed                                              system.py:112
 └─ 建立 10 個 seed_user_XX (big_five + deep_profile + context_embedding)
```

#### 清空所有資料
```
POST :8000/api/clear  {user_id}                                  system.py:144
 ├─ profiles_coll / matches_coll / messages_coll delete_many
 └─ POST :9001/api/clear_graph  (清空 Neo4j)
```

#### 完成 onboarding
```
POST :8000/api/onboarding/complete  {user_id}                    system.py:207
```

---

## 7. 後端關鍵執行流程（GitNexus 追蹤）

以下流程來自 GitNexus 知識圖譜的 process trace（`gitnexus://repo/Graduate_Project/process/{name}`）。

### 7.1 Build → MatchmakingApiException（配對頁載入失敗）
```
1. build                       DatingApp/lib/pages/matching_page.dart
2. _openPage                   matching_page.dart
3. _loadAnalysis               matching_page.dart:41
4. getMatchingAnalysis         matchmaking_api_service.dart:643
5. _get                        matchmaking_api_service.dart:802
6. MatchmakingApiException     matchmaking_api_service.dart:5
```
UI → 頁面邏輯 → service 層 → 統一錯誤類型。

### 7.2 InitState → MatchCandidate（配對結果頁）
```
1. initState                   match_results_page.dart:38
2. _initialize                 match_results_page.dart:43
3. _requestMatches             match_results_page.dart:69
4. _withDisplayName            match_results_page.dart:117
5. copyWith                    matchmaking_api_service.dart:387
6. MatchCandidate              matchmaking_api_service.dart:370
```

### 7.3 InitState → MatchmakingApiException（聊天頁 bootstrap）
```
1. initState                   match_chat_page.dart:37
2. _initializeUser             match_chat_page.dart:41
3. getAssistantMessages        matchmaking_api_service.dart:546
4. _getMessages                matchmaking_api_service.dart:789
5. _get                        matchmaking_api_service.dart:802
6. MatchmakingApiException     matchmaking_api_service.dart:5
```

### 7.4 Detect_risk → Get_appwrite_config（風險後端）
```
1. detect_risk                 Server/risk_backend/app/api/risk_detection.py:71
2. execute (intervention_engine)
3. get_interventions_by_level  kb_service.py
4. (Appwrite KB 讀取)
```
API → engine → service → Appwrite KB（已遷移自 MySQL）。

### 7.5 Trigger global reflection（跨服務）
```
routers/match.py accept_match(情境B)
 └─ (背景) POST :9001/api/global_reflection
      └─ agent.generate_global_reflection → 寫 Neo4j GlobalRule
```
證明 `main_app` 與 `matchmaker_agent` 透過 HTTP 溝通，非 in-process。

### 7.6 Receive feedback → generate graph reflection
```
/api/match/decline
 └─ (背景) POST :9001/api/feedback
      └─ agent.generate_graph_reflection → 寫 Neo4j HAS_PREFERENCE
```

### 7.7 Direct chat stream → save message（含 risk gate）
```
/api/direct_chat
 ├─ risk_client.check_risk → :8001
 ├─ save_message → MongoDB
 └─ (背景) observe_user_memory → :9001
```

### 7.8 Guidance copilot → audit log
```
/api/guidance/suggestion
 └─ generate_chat_completion → suggestion + audit_trail
```

---

## 8. 啟動方式

### 8.1 一鍵啟動（建議）
```bash
cd Server
./start_all.sh
```
`start_all.sh` 會：
1. 清理 8000 / 8001 / 9001 port。
2. 背景啟動 `risk_backend`（:8001）。
3. 背景啟動 `matchmaker_agent`（:9001）。
4. 前景啟動 `main.py`（:8000）。
5. 結束時自動 kill 背景服務。

啟動後：
- 前端頁面：`http://localhost:8000/`
- 主系統 API：`http://localhost:8000/api/`
- AI Gen：`http://localhost:8000/ai-gen/`
- 媒婆 Agent：`http://localhost:9001/`
- 風險偵測：`http://localhost:8001/`
- 健康檢查：`http://localhost:8000/health`

### 8.2 個別啟動
```bash
# 風險後端
cd Server/risk_backend && ../venv/bin/python main.py            # :8001

# 媒婆 Agent
cd Server/matchmaker_agent && ../venv/bin/python agent_api.py   # :9001

# 主伺服器（含 ai_gen）
cd Server && venv/bin/python main.py                            # :8000
```

### 8.3 MongoDB（目前用 Atlas 雲端，本地已停用）
後端透過 `Server/.env` 的 `MONGO_URI` / `AI_CHAT_MONGO_URI` 連到 **MongoDB Atlas 雲端**，不需啟動本地容器。

本地 MongoDB 容器（`local-mongo`）已停止並設為開機不自啟。若要重新啟用於本地開發：
```bash
cd Server/docker/local-mongo
docker compose up -d            # 啟動 local-mongo（含 $vectorSearch）
# 把 Server/.env 的 MONGO_URI / AI_CHAT_MONGO_URI 改成：
# mongodb://mongo_admin:localmongo123@localhost:27017/?authSource=admin&directConnection=true
python init_schema.py           # 建 schema + validator
python create_vector_index.py   # 建 vector index
# (可選) python migrate_from_atlas.py  從 Atlas 匯入
```
不使用時記得 `docker compose down` 並 `docker update --restart=no local-mongo` 避免開機自啟。

### 8.4 Flutter App
```bash
cd DatingApp
flutter pub get
flutter run -d <device>
# 若要指定後端位置：
flutter run --dart-define=MATCHMAKER_API_URL=http://127.0.0.1:8000
```

### 8.5 環境變數（`.env`）
- 頂層 `Server/.env` 會以 `override=True` 蓋過子資料夾 `.env`。
- 重要變數：`MONGO_URI` / `AI_CHAT_MONGO_URI`、`NEO4J_URI` / `NEO4J_USERNAME` / `NEO4J_PASSWORD`、`OLLAMA_FAST_CHAT_MODEL`、`RISK_SERVICE_URL`、`RISK_PORT` / `RISK_HOST`、`GEMINI_API_KEY`、`LLM_API_KEY` / `LLM_BASE_URL` / `AI_GEN_MODEL_ID`、`MATCH_AGENT_CANDIDATE_LIMIT`、`MEDIATOR_DEMO_FAST_PROBE`。

---

## 9. 功能區域（GitNexus 自動偵測）

| 區域 | Symbols | Cohesion | 角色 |
|---|---|---|---|
| **Pages** | 250 | 82% | Flutter UI 頁面（matching_page、match_chat_page、chat_room_page、mediator_private_chat_page、personality_chat_page、settings_page…） |
| **Services** | 115 | 85% | `main_app/services/*`：ai_service、chat_service、memory_service、mediator_event_service、risk_client、appwrite_service |
| **Routers** | 104 | 67% | `main_app/routers/*`：chat、match、system、frontend |
| **Tests** | 70 | 88% | 測試套件 |
| **Scripts** | 48 | 89% | 維護腳本 |
| **Matchmaker_agent** | 26 | 88% | `/matchmaker` API + `MatchmakerAgent` LLM（feedback、global reflection、memory） |
| **Runner** | 20 | 100% | risk-backend runtime |
| **Db_setup** | 15 | 100% | KB 遷移工具（`setup_kb_appwrite.py`） |
| **Ai_gen** | 14 | 87% | `/ai-gen` API（track-message、semantic-plan、generate-suggestion） |
| **Api** | 10 | 86% | risk API |
| **Theme** | 10 | 95% | Flutter 主題 |
| **Widgets** | 9 | 100% | Flutter 共用 widget（GlowAvatar、PressableScale、FloatingNavBar…） |
| **Flutter_linux** | 8 | 100% | Linux 平台整合 |
| **Local-mongo** | 6 | 100% | **已停用**。Docker Compose（MongoDB + mongot），原供本地開發 |
| **Writing-skills** | 5 | 100% | 文件 |

---

## 10. 備註

- **圖譜索引可能陳舊**：若看到 `social_demotest` / `NEW_AI_GEN` / `NEW0530` 路徑，它們實際對應 `main_app` / `ai_gen` / `risk_backend`。重新索引：`node .gitnexus/run.cjs analyze`。
- **`Server/NEW0530/`** 是已 git-deleted 的備份資料夾，請忽略圖譜中該路徑的結果。
- **無 MySQL / SQLite**：`risk_backend` 已遷移至 Appwrite KB；僅剩 legacy 測試名稱與 `db_setup/setup_kb_appwrite.py` 一次性遷移工具含 `mysql` 字樣。
- **跨服務內部呼叫**：後端內唯一的 inter-process HTTP 是 `main_app → matchmaker_agent`（`/matchmaker/global_reflection`、`/api/match`、`/api/feedback`、`/api/memory/observe`）與 `main_app → risk_backend`（`/api/v1/risk/detect`）。其餘在 `:8000` 內皆為 in-process router 組合。
- **統一儲存後端**：持久化只走 **MongoDB Atlas**（chat/match/AI）、Appwrite（auth/profiles/KB）或 Neo4j（偏好圖譜/全域法則）。本地 MongoDB 容器已停用。
- **索引新鮮度**：`gitnexus://repo/Graduate_Project/context`。

---

> 本 README 以 GitNexus 知識圖譜為骨架，並逐一閱讀 `Server/main.py`、`start_all.sh`、`main_app/routers/{chat,match,system}.py`、`main_app/services/risk_client.py`、`matchmaker_agent/agent_api.py`、`ai_gen/app.py`、`risk_backend/app/api/risk_detection.py`、`DatingApp/lib/{main.dart,pages/*,services/*}` 產生。