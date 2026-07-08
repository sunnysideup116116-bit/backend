# 🔌 Unified AI Social Platform - API 呼叫與函數對照手冊 (API Reference Manual)

本手冊提供整個 `Unified AI Social Platform` 專案所有 API 端點的詳細說明。  
本系統基於統一的 **FastAPI** 伺服器啟動，透過 URL 前綴區分並路由到三個不同的核心子系統。

---

## 📋 服務模組與前綴對照

| 模組名稱 (Tag) | 訪問前綴 | 實作檔案 | 職責說明 |
| :--- | :--- | :--- | :--- |
| **🏠 Frontend & Health** | `/` 及 `/health` | [frontend.py](file:///home/sunny/桌面/Project/social_demotest/routers/frontend.py) / [main.py](file:///home/sunny/桌面/Project/main.py) | 前台介面呈現與伺服器健康度監控 |
| **💬 Chat Service** | `/api` | [chat.py](file:///home/sunny/桌面/Project/social_demotest/routers/chat.py) | 性格分析對話、聊天紀錄存取與主動關心 |
| **💘 Match Service** | `/api/match` | [match.py](file:///home/sunny/桌面/Project/social_demotest/routers/match.py) | 配對流程狀態機 (Draft ➔ Pending ➔ Accepted) |
| **🧠 Guidance Service** | `/api/guidance` | [guidance.py](file:///home/sunny/桌面/Project/social_demotest/routers/guidance.py) | 實時 Copilot 聊天對話建議與閒置監測 |
| **⚙️ System Utilities** | `/api` | [system.py](file:///home/sunny/桌面/Project/social_demotest/routers/system.py) | 系統初始化、假資料填充 (Seeding) 及資料重設 |
| **💍 Matchmaker Agent** | `/matchmaker` | [agent_api.py](file:///home/sunny/桌面/Project/matchmaker_agent/agent_api.py) | 媒婆 Multi-Agent 配對決策與 Neo4j 記憶反思 |
| **🤖 AI Gen Service** | `/ai-gen` | [app.py](file:///home/sunny/桌面/Project/NEW_AI_GEN/app.py) | 聊天建議生成 (Agent 2) 與語意規則劃分 |

---

## 1. 🏠 Frontend & Health (前台與系統健康)

### 📌 Serve Frontend
* **API 端點**: `GET /`
* **對接函數**: `serve_frontend()`
* **實作位置**: [frontend.py](file:///home/sunny/桌面/Project/social_demotest/routers/frontend.py#L7-L12)
* **說明**: 提供互動式社交前台頁面。
* **回應 (Response)**: HTML 網頁字串。

### 📌 Health Check
* **API 端點**: `GET /health`
* **對接函數**: `health_check()`
* **實作位置**: [main.py](file:///home/sunny/桌面/Project/main.py#L43-L53)
* **說明**: 檢查整個統一服務的狀態以及路由掛載的確認。
* **回應範例**:
  ```json
  {
      "status": "ok",
      "services": {
          "social_demotest": "mounted at /api",
          "matchmaker_agent": "mounted at /matchmaker",
          "ai_gen": "mounted at /ai-gen"
      }
  }
  ```

---

## 2. 💬 Chat Service API (主系統聊天服務)

本模組定義在 [chat.py](file:///home/sunny/桌面/Project/social_demotest/routers/chat.py)，主要處理與 AI 小助手的性格分析問答、以及和配對伴侶的直接聊天。

### 📌 性格特質分析與問答 (Big Five / Deep Value)
* **API 端點**: `POST /api/chat`
* **對接函數**: `chat_endpoint(req: ChatRequest)`
* **實作位置**: [chat.py](file:///home/sunny/桌面/Project/social_demotest/routers/chat.py#L11-L80)
* **要求參數 (Request Body)**:
  * 參照 `ChatRequest` 模型 ([models.py](file:///home/sunny/桌面/Project/social_demotest/models.py#L9-L13))
  ```json
  {
      "user_id": "user_01",
      "message": "我平常很喜歡去大自然露營和爬山",
      "state": "big_five",            // "big_five" 或 "deep_profile"
      "initial_interest": "戶外活動"  // 選擇性輸入，首輪興趣
  }
  ```
* **回應範例**:
  ```json
  {
      "status": "success",
      "big_five": {
          "O": 8, "C": 7, "E": 9, "A": 8, "N": 3,
          "summary": "外向、熱愛冒險..."
      },
      "reply": "哇！露營聽起來超棒的！那你最近...",
      "is_complete": false
  }
  ```

### 📌 重置分析對話狀態
* **API 端點**: `POST /api/chat/reset`
* **對接函數**: `reset_chat_state(req: ResetRequest)`
* **實作位置**: [chat.py](file:///home/sunny/桌面/Project/social_demotest/routers/chat.py#L82-L94)
* **要求參數 (Request Body)**:
  * 參照 `ResetRequest` 模型 ([models.py](file:///home/sunny/桌面/Project/social_demotest/models.py#L36-L39))
  ```json
  {
      "user_id": "user_01",
      "state": "big_five" // 或 "deep_profile"
  }
  ```
* **回應範例**:
  ```json
  {
      "status": "success"
  }
  ```

### 📌 取得聊天室歷史訊息
* **API 端點**: `GET /api/messages/{contact_id}`
* **對接函數**: `get_messages(contact_id: str, user_id: str)`
* **實作位置**: [chat.py](file:///home/sunny/桌面/Project/social_demotest/routers/chat.py#L96-L106)
* **路徑參數 (Path Parameter)**: `contact_id` (聊天對象 ID，若為助理則傳 `"ai_assistant"`)
* **查詢參數 (Query Parameter)**: `user_id` (當前使用者 ID)
* **說明**: 若聊天室為空且對象是小助手，會自動初始化開場白。
* **回應範例**:
  ```json
  {
      "messages": [
          {
              "room_id": "user_01_ai_assistant",
              "sender_id": "ai_assistant",
              "content": "哈囉！👋 歡迎來到 MatchApp...",
              "timestamp": 1716800000
          }
      ]
  }
  ```

### 📌 直接聊天 (傳送訊息給 AI 助理或真人配對對象)
* **API 端點**: `POST /api/direct_chat`
* **對接函數**: `direct_chat(req: DirectChatRequest, background_tasks: BackgroundTasks)`
* **實作位置**: [chat.py](file:///home/sunny/桌面/Project/social_demotest/routers/chat.py#L108-L279)
* **說明**: 
  1. 送出訊息。
  2. 使用 `check_boundary_guard` 進行敏感邊界過濾。
  3. 若對象是 `ai_assistant`，會啟動三輪對話鎖定機制，於第三輪結束並萃取使用者的最新情境與 Embedding。
  4. 若對象是配對用戶，會以該用戶的大五性格設定為 Prompt 基礎，模擬真人聊天回覆。
  5. 異步觸發 Copilot 的對話緩衝區更新。
* **要求參數 (Request Body)**:
  * 參照 `DirectChatRequest` 模型 ([models.py](file:///home/sunny/桌面/Project/social_demotest/models.py#L26-L30))
  ```json
  {
      "user_id": "user_01",
      "contact_id": "ai_assistant", // 或 "seed_user_03"
      "message": "今天天氣很好，想去喝咖啡！"
  }
  ```
* **回應範例**:
  ```json
  {
      "reply": "喝咖啡聽起來真愜意！最近有推薦的咖啡廳嗎？",
      "is_locked": false,
      "welcome_back_draft": null // 若剛從閒置狀態回來會附帶歡迎草稿
  }
  ```

### 📌 取得聯絡人清單
* **API 端點**: `GET /api/contacts`
* **對接函數**: `get_contacts(user_id: str)`
* **實作位置**: [chat.py](file:///home/sunny/桌面/Project/social_demotest/routers/chat.py#L281-L307)
* **查詢參數**: `user_id`
* **說明**: 返回使用者的聯絡人，包含預設的 `AI 小助手` 以及所有**已接受配對 (accepted)** 的聯絡人。
* **回應範例**:
  ```json
  {
      "contacts": [
          {
              "id": "ai_assistant",
              "name": "AI 小助手",
              "role": "system",
              "context": "幫助您分析性格與配對",
              "is_locked": false
          },
          {
              "id": "seed_user_02",
              "name": "seed_user_02",
              "role": "user",
              "context": "週末想去圖書館看書"
          }
      ]
  }
  ```

### 📌 AI 助手主動發起關心機制 (Proactive Check)
* **API 端點**: `GET /api/proactive_check`
* **對接函數**: `proactive_check(user_id: str)`
* **實作位置**: [chat.py](file:///home/sunny/桌面/Project/social_demotest/routers/chat.py#L310-L354)
* **說明**: 依據使用者設定的頻率 (低/中/高秒數)，時間到時會由 AI 助手主動發起關心話題（例如對他「上次記錄的情境」進行追問），並解鎖小助手聊天室。
* **查詢參數**: `user_id`
* **回應範例**:
  ```json
  {
      "has_new": true,
      "message": "哈囉！上次聽你說你想去大自然露營，後來去成了嗎？有沒有拍到漂亮的帳篷照片呀？"
  }
  ```

---

## 3. 💘 Match Service API (主系統配對服務)

本模組定義在 [match.py](file:///home/sunny/桌面/Project/social_demotest/routers/match.py)，主要包含向媒婆 Agent 索取配對、接受邀請和婉拒偏好回饋。

### 📌 搜尋並索取配對名單 (向量檢索 + 媒婆 Agent 篩選)
* **API 端點**: `POST /api/match`
* **對接函數**: `match_endpoint(req: MatchRequest)`
* **實作位置**: [match.py](file:///home/sunny/桌面/Project/social_demotest/routers/match.py#L12-L159)
* **說明**: 
  1. 使用使用者的 context_embedding 在 MongoDB Atlas 進行向量匹配，篩選出前 50 個候選人。
  2. 提取 Top 5 最相近的對象。
  3. 將卷宗、大五性格與深層價值觀包裝，直接呼叫內部 `matchmaker_agent` 核心邏輯進行深度思考與配對。
  4. 產出 `draft` (草稿) 配對單寫入資料庫。
* **要求參數 (Request Body)**:
  * 參照 `MatchRequest` 模型 ([models.py](file:///home/sunny/桌面/Project/social_demotest/models.py#L15-L16))
  ```json
  {
      "user_id": "user_01"
  }
  ```
* **回應範例**:
  ```json
  {
      "status": "success",
      "matches": [
          {
              "match_id": "6654abcde...",
              "matched_user_id": "seed_user_05",
              "contrast_label": "極致互補",
              "distinctive_tags": ["高行動力", "冷靜理性"],
              "recommendation_reason": "因為你在大五性格中偏向內斂，而他行動力極強，兩人在週末看書與運動的安排上可以..."
          }
      ],
      "debug_info": []
  }
  ```

### 📌 接受配對 (狀態轉變機：Draft ➔ Pending ➔ Accepted)
* **API 端點**: `POST /api/match/accept`
* **對接函數**: `accept_match(req: AcceptRequest, background_tasks: BackgroundTasks)`
* **實作位置**: [match.py](file:///home/sunny/桌面/Project/social_demotest/routers/match.py#L161-L217)
* **說明**: 
  * **情境 A (發起人點選發送)**: 狀態由 `draft` ➔ `pending`。
  * **情境 B (接收人點選接受)**: 狀態由 `pending` ➔ `accepted`。並在背景觸發 **AI 破冰訊息** 與 **全域抽象化反思** (寫入 Neo4j 全域法則)。
* **要求參數 (Request Body)**:
  * 參照 `AcceptRequest` 模型 ([models.py](file:///home/sunny/桌面/Project/social_demotest/models.py#L21-L24))
  ```json
  {
      "user_id": "user_01",
      "match_id": "6654abcde..."
  }
  ```
* **回應範例**:
  ```json
  {
      "status": "success",
      "new_status": "accepted"
  }
  ```

### 📌 婉拒配對 (偏好與地雷回饋)
* **API 端點**: `POST /api/match/decline`
* **對接函數**: `decline_match(req: AcceptRequest, background_tasks: BackgroundTasks)`
* **實作位置**: [match.py](file:///home/sunny/桌面/Project/social_demotest/routers/match.py#L219-L274)
* **說明**: 
  * 點擊婉拒會將 match 狀態改為 `declined`。
  * 背景呼叫 `do_feedback`，將使用者明確勾選的婉拒特質 (`explicit_reasons`) 傳送給媒婆 Agent，媒婆大腦經過反思後，會直接在 **Neo4j** 圖形資料庫中畫出該使用者的偏好與地雷關係 (例如：`DISLIKES_TRAIT`)。
* **要求參數 (Request Body)**:
  * 參照 `AcceptRequest` 模型，包含選擇的婉拒原因
  ```json
  {
      "user_id": "user_01",
      "match_id": "6654abcde...",
      "explicit_reasons": ["對象生活哲學過於及時行樂", "外向度太高"]
  }
  ```
* **回應範例**:
  ```json
  {
      "status": "success",
      "new_status": "declined",
      "context": "initiator_declined_draft"
  }
  ```

---

## 4. 🧠 Guidance Service API (Copilot 導引與狀態服務)

本模組定義在 [guidance.py](file:///home/sunny/桌面/Project/social_demotest/routers/guidance.py)，為用戶提供即時的 Copilot 對話建議與狀態追蹤。

### 📌 獲取聊天即時 Copilot 建議
* **API 端點**: `POST /api/guidance/suggestion`
* **對接函數**: `get_guidance_suggestion(req: SuggestionRequest)`
* **實作位置**: [guidance.py](file:///home/sunny/桌面/Project/social_demotest/routers/guidance.py#L21-L27)
* **要求參數 (Request Body)**:
  ```json
  {
      "user_id": "user_01",
      "contact_id": "seed_user_03",
      "input_text": "我今天在想是不是該..." // 使用者目前在輸入框輸入的暫存文字
  }
  ```
* **回應範例**:
  ```json
  {
      "ui_nudge": "你可以分享你對這部電影最深刻的情節，並問問他喜歡哪一幕。",
      "audit_trail": "對象偏好靜態活動。檢測到高打字遲延。策略：引導探討深度話題。"
  }
  ```

### 📌 回報活動狀態 (更新活躍度 / 消除閒置)
* **API 端點**: `POST /api/guidance/activity`
* **對接函數**: `report_activity(req: ActivityRequest)`
* **實作位置**: [guidance.py](file:///home/sunny/桌面/Project/social_demotest/routers/guidance.py#L29-L33)
* **說明**: 當偵測到鍵盤或滑鼠事件時呼叫，更新活躍時間點。若使用者是從閒置狀態回來，可能回傳溫暖的歡迎回歸草稿。
* **要求參數 (Request Body)**:
  ```json
  {
      "user_id": "user_01",
      "contact_id": "seed_user_03"
  }
  ```
* **回應範例**:
  ```json
  {
      "status": "active",
      "welcome_back_draft": "不好意思，剛剛手邊有點事情！我們剛剛聊到..."
  }
  ```

### 📌 查詢 Guidance 狀態 (含閒置監測)
* **API 端點**: `GET /api/guidance/status`
* **對接函數**: `get_guidance_status(user_id: str, contact_id: str)`
* **實作位置**: [guidance.py](file:///home/sunny/桌面/Project/social_demotest/routers/guidance.py#L35-L67)
* **說明**: 取得當前 AI 的戰術決策角色、以及檢查雙方是否進入 `idle` (閒置) 狀態。若對方剛進入閒置，會異步在資料庫插入一條系統閒置通知訊息。
* **查詢參數**: `user_id` / `contact_id`
* **回應範例**:
  ```json
  {
      "current_role": "FACILITATOR",
      "current_user_idle_state": {
          "is_idle": false,
          "is_new_notification": false
      },
      "partner_idle_state": {
          "is_idle": true,
          "is_new_notification": true,
          "system_notification_text": "[AI Copilot] 對方暫時離開囉，你可以稍後再試。"
      }
  }
  ```

### 📌 查詢建議審計日誌
* **API 端點**: `GET /api/guidance/audit/{suggestion_id}`
* **對接函數**: `get_audit_log(suggestion_id: str)`
* **實作位置**: [guidance.py](file:///home/sunny/桌面/Project/social_demotest/routers/guidance.py#L69-L74)
* **路徑參數**: `suggestion_id`
* **回應**: 完整的 MongoDB 審計日誌物件（包含 AI 的推論路徑）。

---

## 5. ⚙️ System & Seeding Utilities API (系統與資料初始化工具)

本模組定義在 [system.py](file:///home/sunny/桌面/Project/social_demotest/routers/system.py)，提供開發測試用的後台接口。

### 📌 系統初始化 (載入使用者狀態)
* **API 端點**: `GET /api/init`
* **對接函數**: `init_system(user_id: str)`
* **實作位置**: [system.py](file:///home/sunny/桌面/Project/social_demotest/routers/system.py#L9-L66)
* **說明**: 回傳系統中所有使用者名單，並提供當前 `user_id` 的大五性格分析狀態、深層價值觀狀態及設定參數，供前端頁面載入。
* **查詢參數**: `user_id`
* **回應範例**:
  ```json
  {
      "users": ["demo_user", "seed_user_01"],
      "is_complete": true,
      "my_context": "我想喝咖啡",
      "my_bf_summary": "外向樂觀的性格...",
      "is_deep_complete": false,
      "my_deep_summary": "尚無深層價值觀分析資料"
  }
  ```

### 📌 假資料填充 (Seed 10 位用戶)
* **API 端點**: `POST /api/seed`
* **對接函數**: `seed_data()`
* **實作位置**: [system.py](file:///home/sunny/桌面/Project/social_demotest/routers/system.py#L68-L98)
* **說明**: 自動在資料庫建立 10 位具備完整 `big_five`、`deep_profile`、`current_context` 與 `context_embedding` 的虛擬用戶。
* **回應**: `{"status": "success", "message": "10 seed profiles created."}`

### 📌 清空所有資料
* **API 端點**: `POST /api/clear`
* **對接函數**: `clear_data(req: ClearRequest)`
* **實作位置**: [system.py](file:///home/sunny/桌面/Project/social_demotest/routers/system.py#L100-L108)
* **說明**: 一鍵清除 MongoDB 中的 `profiles`、`matches`、`messages`、`semantic_plans`、`knowledge_graph_edges` 與 `audit_logs` 所有集合。
* **要求參數 (Request Body)**: `{"user_id": "user_01"}`
* **回應**: `{"status": "success"}`

### 📌 查詢待回應配對邀請
* **API 端點**: `GET /api/notifications`
* **對接函數**: `get_notifications(user_id: str)`
* **實作位置**: [system.py](file:///home/sunny/桌面/Project/social_demotest/routers/system.py#L110-L130)
* **查詢參數**: `user_id`
* **說明**: 撈取狀態為 `pending` 且被配對對象為自己 (`to_user`) 的邀請清單。
* **回應範例**:
  ```json
  {
      "notifications": [
          {
              "match_id": "6654abcde...",
              "from_user": "seed_user_01",
              "reason": "配對原因分析...",
              "from_user_big_five": {},
              "from_user_context": "想去郊外看書"
          }
      ]
  }
  ```

### 📌 更新系統設定
* **API 端點**: `POST /api/settings`
* **對接函數**: `update_settings(req: SettingsRequest)`
* **實作位置**: [system.py](file:///home/sunny/桌面/Project/social_demotest/routers/system.py#L132-L140)
* **要求參數 (Request Body)**:
  ```json
  {
      "user_id": "user_01",
      "proactive_frequency": "60" // 秒數或是 "none"
  }
  ```
* **回應**: `{"status": "success", "proactive_frequency": "60"}`

### 📌 重置深層價值觀分析
* **API 端點**: `POST /api/reset_deep_profile`
* **對接函數**: `reset_deep_profile(req: ClearRequest)`
* **實作位置**: [system.py](file:///home/sunny/桌面/Project/social_demotest/routers/system.py#L142-L149)
* **說明**: 移除資料庫中的 `deep_profile` 欄位與對話輪數，讓用戶可以重新開啟深層分析。
* **要求參數**: `{"user_id": "user_01"}`
* **回應**: `{"status": "success"}`

---

## 6. 💍 Matchmaker Agent API (媒婆 Agent 微服務)

本模組定義在 [agent_api.py](file:///home/sunny/桌面/Project/matchmaker_agent/agent_api.py)，掛載前綴為 `/matchmaker`。主要負責與大語言模型和 **Neo4j** 圖譜通訊。

### 📌 媒婆配對決策 (核心)
* **API 端點**: `POST /matchmaker/match`
* **對接函數**: `match_endpoint(req: MatchRequest)` (實質呼叫 `do_match`)
* **實作位置**: [agent_api.py](file:///home/sunny/桌面/Project/matchmaker_agent/agent_api.py#L321-L323)
* **說明**:
  1. 讀取 Neo4j 當前使用者的偏好與地雷資料 (`get_user_graph_memory`)。
  2. 讀取系統權重前三高的全域法則 (`get_global_rules`)。
  3. 餵入 LLM 計算候選人性格之契合度，產出推薦原因、給接收者的原因、對立標籤與亮點標籤。
* **要求參數 (Request Body)**:
  ```json
  {
      "target_user": { "user_id": "user_01", "big_five": {} },
      "candidates": [ { "user_id": "seed_user_01", "big_five": {} } ],
      "target_deep_profile": {}
  }
  ```
* **回應範例**:
  ```json
  {
      "matches": [
          {
              "matched_user_id": "seed_user_01",
              "contrast_label": "性格互補",
              "recommendation_reason": "...",
              "receiver_reason": "...",
              "distinctive_tags": ["高行動力"]
          }
      ]
  }
  ```

### 📌 婉拒與接受的行為回饋 (更新個體地雷圖譜)
* **API 端點**: `POST /matchmaker/feedback`
* **對接函數**: `receive_feedback(req: FeedbackRequest)` (實質呼叫 `do_feedback`)
* **實作位置**: [agent_api.py](file:///home/sunny/桌面/Project/matchmaker_agent/agent_api.py#L336-L338)
* **說明**: 當用戶點選配對卡片的 accept/decline 時，觸發此端點。若累積婉拒，會觸發 LLM 提煉反思，並在 Neo4j 圖資料庫中動態 MERGE 使用者與特質節點，建立帶有 reason 的 `HAS_PREFERENCE` 關聯。
* **要求參數 (Request Body)**:
  ```json
  {
      "user_id": "user_01",
      "target_id": "seed_user_01",
      "action": "decline",
      "target_traits": { "summary": "超級活潑外向" },
      "explicit_reasons": ["對方外向度太高，聊天有壓力"]
  }
  ```
* **回應**: `{"status": "success", "message": "媒婆已將此事記在心上。"}`

### 📌 全域反思抽象化 (歸納成功配對法則)
* **API 端點**: `POST /matchmaker/global_reflection`
* **對接函數**: `global_reflection_endpoint(req: GlobalReflectionRequest)` (實質呼叫 `do_global_reflection`)
* **實作位置**: [agent_api.py](file:///home/sunny/桌面/Project/matchmaker_agent/agent_api.py#L347-L349)
* **說明**: 當雙方成功達成配對時呼叫。利用 LLM 歸納出該次成功配對的通用抽象法則，並在 Neo4j 中以 `LEARNED_RULE` 關聯寫入或增強系統全域法則之 Weight 權重。
* **要求參數 (Request Body)**:
  ```json
  {
      "from_big_five": { "summary": "內斂穩健" },
      "from_context": "週末想在圖書館看書",
      "to_big_five": { "summary": "溫柔可靠" },
      "to_context": "想去安靜的地方"
  }
  ```
* **回應範例**:
  ```json
  {
      "status": "success",
      "abstract_rule": "內向且偏好安靜閱讀的個體，與溫柔且尋求安穩的傾聽者配對，容易在靜態環境中建立深度共感。",
      "category": "情境型"
  }
  ```

---

## 7. 🤖 AI Gen API (AI 聊天建議微服務)

本模組定義在 [app.py](file:///home/sunny/桌面/Project/NEW_AI_GEN/app.py)，掛載前綴為 `/ai-gen`。主要處理對話指標計算與即時 Suggestions 生成。

### 📌 對話追蹤與訊號指標計算
* **API 端點**: `POST /ai-gen/track-message`
* **對接函數**: `track_message(request: Request)`
* **實作位置**: [app.py](file:///home/sunny/桌面/Project/NEW_AI_GEN/app.py#L233-L318)
* **說明**:
  * 每當有新訊息，將其加入對話歷史。
  * 更新使用者的 `s_rat_ema` (主動點擊建議比例)。
  * 計算**延遲指標 (Latency)** 與**對稱度指標 (Parity)** 並更新其滑動窗口與指數移動平均 (EMA)，作為評估當前對話是否健康的依據。
* **要求參數 (Request Body)**:
  ```json
  {
      "sessionId": "user_01_seed_user_03",
      "message": {
          "sender": "user_01",
          "text": "我也喜歡那部片！",
          "timestamp": "2026-05-27T13:40:00Z"
      }
  }
  ```
* **回應**: `{"status": "tracked"}`

### 📌 戰術意圖更新 (Read-Revise-Rewrite)
* **API 端點**: `POST /ai-gen/semantic-plan`
* **對接函數**: `semantic_plan(request: Request)`
* **實作位置**: [app.py](file:///home/sunny/桌面/Project/NEW_AI_GEN/app.py#L320-L429)
* **說明**: 
  * 讀取當前對話歷史，使用 LLM 進行「讀取-修訂-覆寫」循環。
  * 更新背景戰術意圖、主題、行動方案與**內容限制邊界 (Dynamic Content Bounds)**。
  * 從對話中提煉三元組，進行**知識圖譜實體關係抽取**。
  * 評估對話健康度，決定下一次建議時 AI 扮演的決策角色 (`FRIEND`/`ADVISER`/`MENTOR`/`FACILITATOR`)。
* **要求參數 (Request Body)**:
  ```json
  {
      "sessionId": "user_01_seed_user_03",
      "messages": [
          { "sender": "user_01", "text": "我也喜歡那部片！" }
      ]
  }
  ```
* **回應範例**:
  ```json
  {
      "session_id": "user_01_seed_user_03",
      "current_role": "FRIEND",
      "strategy": {
          "strategic_intent": "促進雙方探討電影細節",
          "theme": "電影愛好",
          "dynamic_content_bounds": ["STRICT RULE: 不要劇透結局"]
      },
      "knowledge_graph_triples": [
          { "subject": "User 01", "predicate": "LIKES", "object": "電影" }
      ]
  }
  ```

### 📌 即時對話建議生成 (Agent 2)
* **API 端點**: `POST /ai-gen/generate-suggestion`
* **對接函數**: `generate_suggestion(request: Request)`
* **實作位置**: [app.py](file:///home/sunny/桌面/Project/NEW_AI_GEN/app.py#L431-L516)
* **說明**:
  * 整合大五性格背景、目前對話記錄、**當前被指派的角色 (Role)** 與**動態內容限制邊界**。
  * 根據「不透漏具體台詞與不抄襲法則 (Anti-Plagiarism Law)」，產出富含引導意義且極度簡短 (少於 25 字) 的建議行動。
* **要求參數 (Request Body)**:
  ```json
  {
      "sessionId": "user_01_seed_user_03",
      "messages": [
          { "sender": "user_01", "text": "你也喜歡這部片嗎？" }
      ],
      "t_invoke": 2.5,          // 觸發時的輸入打字時間
      "input_text": "我也很",   // 目前正在輸入的草稿
      "force_assist": false     // 是否強制干預
  }
  ```
* **回應範例**:
  ```json
  {
      "ui_nudge": "探討他們喜歡劇中角色的哪些特質，並引導他們問你的看法。",
      "audit_trail": "對象為內向型。打字時間短。策略：角色 Friend，給予共鳴以維持流暢感。"
  }
  ```

---

## 💡 開發人員使用提示 (Developer Tips)

1. **互動式 Swagger UI 文件**:  
   專案啟動後，請訪問 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) 以查看由 FastAPI 自動生成的交互式 Swagger 介面。您可以在此進行真實的端點發送與測試。
2. **CORS 設定**:  
   統一服務之 [main.py](file:///home/sunny/桌面/Project/main.py#L34-L40) 已配置跨來源資源共用 (CORS) 中介軟體並預設允許所有網域 (`allow_origins=["*"]`)，以利外網或前端直接串接呼叫。
3. **資料庫依賴**:  
   * **MongoDB**: 用於儲存對話紀錄、性格分析、配對草稿與對話策略檔。
   * **Neo4j**: 用於儲存性格地雷的偏好圖譜與配對反思得到的全域法則。請確保 `.env` 配置正確以防寫入失敗。
