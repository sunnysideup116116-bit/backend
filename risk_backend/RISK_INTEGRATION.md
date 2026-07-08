# 風險偵測整合 v2 — 更新通知 & API 整合說明

> 本文件兼具三種用途：
> - **給組員的 v2 更新通知**：看 §1 - §3
> - **給前端的 API 整合說明**：看 §4 - §8
> - **給後端 deploy 負責人**：看 §9

---

## 1. 新增的東西

### 1.1 新資料夾 `risk_backend/`
- 風險偵測 microservice，獨立跑在 :8001
- 啟動：`cd risk_backend && python -m uvicorn app.main:app --port 8001`
- 第一次需要 `pip install -r requirements.txt` + 從 `.env.example` 建 `.env` 填 API key

### 1.2 `risk_backend/db_setup/`（部署用 DB 資源）
- `dating_safety.sql` — MySQL Knowledge Base 完整 schema + 種子資料（5/30 版，含 Phase 3.1 `trigger_mode` 欄位）
- `APPWRITE_SCHEMA.md` — Appwrite Chat Logs 9 個 collection 完整 attribute 規格（含 Phase 3.2 `guardrail_context_reviews`）
- `README.md` — 新環境部署步驟教學
- 既有環境 huangchunming 在維護，當文件參考即可

### 1.3 `social_demotest/services/risk_client.py`
- HTTP 客戶端，由 chat.py 呼叫去打 :8001
- 含 timeout（1.5s）+ 失敗 fallback（:8001 掛掉時 return None 不擋主流程）

---

## 2. 改的東西

### 2.1 `social_demotest/routers/chat.py`
`direct_chat()` 函式內加 4 處：
- 開頭呼叫 `risk_client.check_risk(...)` 取得風險評估
- 若 `risk_level == "blocked"` 直接 return（訊息不存進 MongoDB）
- 3 個 `return res_data` 點各加一行 `risk_client.attach_to_response()`，讓 response 多出兩個欄位給前端用：
  - `ui_priority`: `"coach"` 或 `"risk"`
  - `risk_assessment`: 完整風險評估物件
- 其他既有邏輯都沒動；degraded 模式（:8001 掛掉時）會 fallback 回原本的 `check_boundary_guard`

### 2.2 `social_demotest/.env`
加 2 行：
```
RISK_SERVICE_URL=http://localhost:8001
RISK_TIMEOUT_SEC=1.5
```

### 2.3 `social_demotest/requirements.txt`
加 `httpx>=0.27`

---

## 3. Demo 啟動順序

```bash
Terminal 1: cd risk_backend && python -m uvicorn app.main:app --port 8001
Terminal 2: cd social_demotest && python main.py     # :8000
Browser:    localhost:8000
```

兩個服務獨立跑，任一邊掛了另一邊仍可運作（risk service 掛 → 自動 fallback 回原本的 boundary guard）。

---

## 4. 系統架構（給前端）

```
Browser
   │
   ▼
:8000  Unified Server (social_demotest)
   │
   ▼  HTTP call (server-to-server)
:8001  Risk Detection Service (risk_backend)
```

- :8001 必須在運作中，整合才有完整功能
- 若 :8001 掛掉：response 的 `risk_assessment` 會是 `null`，前端 fallback 到原本的 coach UI
- 連線超時上限 1.5 秒，超過視為服務不可用

---

## 5. `/api/direct_chat` Response 新欄位

每次呼叫 direct_chat，response 都會比原本多兩個欄位：

| 欄位 | 型別 | 說明 |
|---|---|---|
| `ui_priority` | `"coach"` \| `"risk"` | 前端用這個決定要顯示教練 nudge 還是風險警告 |
| `risk_assessment` | object \| null | 風險偵測完整結果（null 表示服務不可用） |
| `is_blocked` | bool（僅 blocked 時才有） | 訊息是否被攔截（true 時 reply 為 null） |

### 5.1 ui_priority 決策表

| risk_level | ui_priority | 前端該做什麼 |
|---|---|---|
| `safe` | `coach` | 顯示 AI Copilot 對話建議（與原本流程相同） |
| `observation` | `coach` | 顯示 AI Copilot 建議 + 一個低調的安全 icon 在 receiver 那邊 |
| `warning` | `risk` | **隱藏** AI Copilot 建議，**改顯示** sender 的反思 banner |
| `restricted` | `risk` | **隱藏** AI Copilot，顯示 sender modal warning + cooldown |
| `blocked` | `risk` | **完全隱藏** AI Copilot，顯示 blocked notice |

**原則:安全永遠優先於體驗。** 觸發 warning 以上時，提醒對方比給對話建議更重要。

---

## 6. Response 範例

### 6.1 safe / observation 例（最常見）

```json
{
  "reply": "喝咖啡聽起來真愜意！...",
  "is_locked": false,
  "ui_priority": "coach",
  "risk_assessment": {
    "risk_level": "safe",
    "composite_score": 0.02,
    "intervention_command": {
      "sender_directive": {"action": "none"},
      "receiver_directive": {"action": "none"}
    }
  }
}
```

→ 前端：照原本流程渲染，可選擇是否秀 risk score 在 debug panel。

### 6.2 warning（顯示反思 banner）

```json
{
  "reply": "...AI 回覆內容...",
  "ui_priority": "risk",
  "risk_assessment": {
    "risk_level": "warning",
    "composite_score": 0.42,
    "intervention_command": {
      "sender_directive": {
        "action": "show_reflection_banner",
        "content": {
          "body": "頻繁的訊息有時會讓對方感到不舒服",
          "primary_risk_type": "harassment"
        }
      },
      "receiver_directive": {
        "action": "show_safety_info_card",
        "content": {
          "body": "系統偵測到對話中可能有不當模式，已對對方啟動提醒"
        }
      }
    }
  }
}
```

→ 前端：
- Sender 視窗底部顯示 banner，文字取自 `intervention_command.sender_directive.content.body`
- Receiver 視窗顯示資訊卡片，文字取自 `intervention_command.receiver_directive.content.body`
- 不顯示 AI Copilot nudge

### 6.3 blocked（訊息根本沒送出）

```json
{
  "reply": null,
  "is_blocked": true,
  "ui_priority": "risk",
  "risk_assessment": {
    "risk_level": "blocked",
    "intervention_command": {
      "sender_directive": {
        "action": "block_message",
        "cooldown_seconds": 1800,
        "require_acknowledgment": true,
        "content": {
          "title": "訊息未送出",
          "body": "這則訊息因為安全考量暫時無法送出"
        }
      },
      "receiver_directive": {
        "action": "show_blocked_notice",
        "content": {
          "body": "系統攔截了一則可能對你造成不適的訊息"
        }
      }
    }
  }
}
```

→ 前端：
- Sender 顯示 modal「訊息未送出」+ disabled 輸入框 30 分鐘
- Receiver 完全看不到原訊息，只看到「系統攔截了一則訊息」通知
- 完全不顯示 AI Copilot

### 6.4 risk service 不可用（fallback）

```json
{
  "reply": "...AI 回覆內容...",
  "is_locked": false,
  "ui_priority": "coach",
  "risk_assessment": null
}
```

→ 前端：照原本流程渲染（等同 safe）。後端會 print warning 到 server log。

---

## 7. Receiver Feedback Endpoint（可選）

當 receiver 收到 observation / warning / restricted 等級的提示時，可以給回饋。後端已整合：

```http
POST /api/feedback
Content-Type: application/json

{
  "triggered_by_msg_id": "<從 risk_assessment 取得>",
  "role": "receiver",
  "feedback": "comfortable" | "uncomfortable"
}
```

回饋會影響下一次該 sender 的風險判定（trust / alert / neutral）。

---

## 8. UI 樣式建議

| Level | 顏色 | Icon | 動作 |
|---|---|---|---|
| `safe` | - | - | 無 |
| `observation` | 淺灰 | ⚪ | Receiver 端 ambient icon |
| `warning` | 黃 | ⚠️ | Sender 底部黃色 banner |
| `restricted` | 橘 | 🟠 | Sender modal + 60 秒冷卻 |
| `blocked` | 紅 | 🛑 | Sender 全螢幕 modal + 30 分鐘冷卻 |

僅供前端配色參考，可自由調整。

---

## 9. 後端 deploy 負責人

若要 setup 全新環境（例如教室機器），讀 `risk_backend/db_setup/README.md` 建 MySQL + Appwrite。

既有環境 huangchunming 在維護，直接跑兩個 server 即可。

---

## 各角色任務速查

- **前端**：讀 §4 - §8，改 `frontend.html` / `script.js`
- **後端 deploy 負責人**：讀 `risk_backend/db_setup/README.md`
- **huangchunming（風險偵測）**：已測通 `:8001 /api/v1/risk/detect` 回應正常（cold start ~16s、warm ~1-3s）

## 驗證狀態

- ✅ risk service `:8001` 跑得起來、回應格式正確
- ⏳ 尚未做端到端 `:8000 → :8001` 串接驗證
- ⏳ 尚未驗證 chat.py patch 在 social_demotest 啟動時不會 import error

任何 import error / 啟動失敗 / response 怪怪的，貼我
