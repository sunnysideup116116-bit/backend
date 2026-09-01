# risk_backend Database Setup

> 部署 `risk_backend` 到新環境（教室機器、新主機）時，依照本資料夾的檔案建立 DB 環境。
> 既有環境（已 setup 過）直接 maintain live DB 即可，不用重跑這些檔案。

---

## 統一使用 Appwrite

後端所有資料皆存於 Appwrite，分為兩個 database：

| Appwrite Database | 角色 | Setup 檔案 |
|---|---|---|
| `chat_logs`（或 `.env` 的 `APPWRITE_DB_ID`） | Chat Logs DB（對話 / 風險歷史 / 介入紀錄 / 記憶） | `setup_appwrite.py` + `appwrite_schema_dump.json` |
| `kb`（或 `.env` 的 `APPWRITE_KB_DB_ID`） | Knowledge Base（規則 / Prompt / 場景 / 配置） | `setup_kb_appwrite.py` + `dating_safety.sql` |

> 知識庫原本存放於 MySQL/SQLite，現已遷移至 Appwrite 的 `kb` database，統一儲存層，省去額外關聯式 DB 依賴。

---

## KB Setup（知識庫）

```bash
# 1. 確認 .env 已設定 Appwrite 連線（APPWRITE_ENDPOINT / PROJECT_ID / API_KEY）
#    與 APPWRITE_KB_DB_ID（預設為 kb）

# 2. 在 Appwrite 建立 kb database 與 7 個 collections，並從 dating_safety.sql 匯入種子資料
python db_setup/setup_kb_appwrite.py

# 3. 驗證
#    透過 Appwrite Console 檢查 kb database 內各 collection 的 document 數量：
#      kb_configs: 2, kb_features: 26, kb_hard_blocks: 11, kb_interventions: 13,
#      kb_prompts: 2, kb_rules: 4, kb_scenario_rules: 14
```

`setup_kb_appwrite.py` 會：
1. 建立 `kb` database 與 7 個 KB collections（含 attributes / indexes）。
2. 解析 `dating_safety.sql` 中的 INSERT 語句，將資料寫入對應 collections。
3. 補寫高擬真 regex patterns 至 `kb_features.regex_pattern`（對齊舊版 `init_sqlite.py` 的行為）。

腳本為 idempotent：重複執行會以 PK 為 documentId 進行 upsert，已存在的 database / collection / attribute / index 會被跳過。

`dating_safety.sql` 保留作為 KB 種子資料來源（MySQL dump 格式，由腳本解析後寫入 Appwrite），包含以下表的 schema + 內容：
- `kb_configs` — 融合權重、風險閾值、衰退設定
- `kb_features` — 特徵 delta 表（26 個特徵）
- `kb_hard_blocks` — Step 0 第一層禁詞（含 `trigger_mode` 欄位）
- `kb_interventions` — 五等級介入模板
- `kb_prompts` — risk_analysis_v2 + memory_summary_v1
- `kb_rules` — Rule engine 行為規則
- `kb_scenario_rules` — 14 條情境規則

---

## Chat Logs Setup

依照 `setup_appwrite.py` 在 Appwrite 建立 chat logs database 與 9 個 collection：

1. `conversations`
2. `messages`
3. `temporal_features`
4. `risk_analysis_logs_`
5. `risk_state_history`
6. `intervention_logs`
7. `conversation_summaries`
8. `relationship_metrics`
9. `guardrail_context_reviews` (Phase 3.2)

完整 attribute 規格見 `APPWRITE_SCHEMA.md`，`appwrite_schema_dump.json` 為 live Appwrite 的 JSON 備份對照。

既有環境如果是舊 Schema，不要直接刪除 required attribute。改用非破壞式遷移：

```bash
# 預覽：不寫入 Appwrite
python db_setup/migrate_appwrite_schema.py

# 建立新 DB、套用 Schema、搬移相容文件並驗證
python db_setup/migrate_appwrite_schema.py --apply
```

預設目標是 `chat_logs_v2_20260815`。腳本對來源 DB 只讀，不含 delete/update；驗證通過後才將 `APPWRITE_DB_ID` 切換到新 DB。`risk_analysis_logs_` 的尾端底線是 collection ID 的一部分。

---

## 環境變數（`.env`）

```
APPWRITE_ENDPOINT=https://your-appwrite-endpoint/v1
APPWRITE_PROJECT_ID=your_appwrite_project_id
APPWRITE_API_KEY=your_appwrite_api_key
APPWRITE_DB_ID=your_chat_logs_database_id
APPWRITE_KB_DB_ID=kb
```
