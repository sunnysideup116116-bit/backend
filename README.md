# 阿月交友平台 — Server 後端

交友平台「阿月」的 Python FastAPI 後端，由 **主 API（social）**、**媒婆 Agent（matchmaker_agent）**、**風險偵測（risk_backend）** 與可選的 **Guardrail（llama.cpp）** 四個服務組成。

```text
┌──────────────┐   HTTP    ┌────────────────────────────────────────┐        ┌────────────────┐
│  DatingApp   │ ─────────▶│          social  (FastAPI :8000)       │◀──────▶│  MongoDB Atlas │
│  (Flutter)   │           │  前端 HTML + Public/Private 阿月 + 配對 │ pymongo│  (雲端 Cluster) │
└──────────────┘           └───────────────────┬────────────────────┘        └────────────────┘
                                               │ HTTP (localhost)
              ┌────────────────────────────────┼───────────────────────────────┐
              ▼                                ▼                                ▼
   ┌────────────────────┐           ┌────────────────────┐           ┌────────────────────┐
   │  matchmaker_agent  │           │    risk_backend    │           │  guardrail (可選)   │
   │      (Neo4j)       │           │  (Appwrite KB)     │           │  llama.cpp :8081    │
   │        :9001       │           │        :8001       │           │  llama-guard-3-1b   │
   └────────────────────┘           └────────────────────┘           └────────────────────┘
```

## Port 總覽

| Port | 服務 | 入口 | 說明 |
|------|------|------|------|
| **8000** | social 主 API | `social/main.py` | 前端頁面 `/`、核心 API `/api/*`、健康檢查 `/api/health` |
| **8001** | risk_backend 風險偵測 | `risk_backend/main.py` | `/api/v1/risk/detect`、`/api/v1/risk/feedback`、`/api/v1/risk/state` |
| **9001** | matchmaker_agent 媒婆 | `matchmaker_agent/agent_api.py` | `/api/match`、`/api/feedback`、`/api/global_reflection`、`/api/memory/*`、`/api/chat_triples`、`/api/clear_graph` |
| **8081** | guardrail（可選） | `scripts/run_ayue_guardrail.sh` | llama.cpp server + llama-guard-3-1b，供 risk_backend 的 `llm_classifier` 使用 |

## 檔案架構

```
Server/
├── start_all.sh               # 統一啟動入口（guardrail → risk → matchmaker → social）
├── .env                       # 共用環境變數（所有服務共用）
├── .gitignore
│
├── social/                    # ★ 主 API（port 8000）
│   ├── main.py                #   FastAPI app：router 掛載 + startup index 建立 + 背景 worker
│   ├── config.py              #   環境變數讀取（Mongo/Ollama/Google/Tavily/Giphy…）
│   ├── database.py            #   MongoDB 連線（profiles / matches / messages）
│   ├── models.py              #   Pydantic request models
│   ├── frontend.html          #   Web Demo 前端（視覺參考實作）
│   ├── images/                #   前端靜態圖檔（已 gitignore）
│   ├── routers/               #   HTTP 層（thin adapter，邏輯在 services）
│   │   ├── chat.py            #     /api/* 聚合 router（leaf routers 組合成一個 Chat tag）
│   │   ├── chat_onboarding.py #     個性測驗（big_five / deep_profile）
│   │   ├── chat_messages.py   #     訊息讀取
│   │   ├── public_chat.py     #     公開阿月 V3（direct_chat / NDJSON stream）+ 風險閘道
│   │   ├── private_mediator.py#     私聊媒人 Private V2
│   │   ├── match.py           #     配對搜尋/提案/接受/婉拒（CAS revision）
│   │   ├── calendar.py        #     本人行事曆 CRUD
│   │   ├── relationship_dates.py #  共同約會協調
│   │   ├── relationship_quiz.py  #  默契測驗
│   │   ├── proactive.py       #     主動關心
│   │   ├── demo.py            #     Demo 工具
│   │   ├── system.py          #     健康檢查/init/seed/設定/通知
│   │   └── frontend.py        #     前端頁面路由
│   ├── services/              #   領域邏輯
│   │   ├── ayue_agent/        #     阿月 V3 sub-agent runtime
│   │   │   ├── v3/            #       scheduler / planner / sub_agents / synthesizer / guard…
│   │   │   ├── router.py      #       runtime 註冊
│   │   │   ├── tools.py / tool_registry.py
│   │   │   ├── maps_client.py / google_places_client.py / web_tools.py
│   │   │   ├── private_v2.py  #       Private V2 協調
│   │   │   └── proactive_scheduler.py / proactive_care.py
│   │   ├── ai_service.py      #   LLM 呼叫（Ollama / Gemini）、測驗分析、約會協調
│   │   ├── risk_policy_service.py # 配對訊息風險閘道（HTTP → risk_backend :8001）
│   │   ├── semantic_plan_service.py # 關係語意計畫 + chat triples（→ matchmaker :9001）
│   │   ├── memory_service.py  #   阿月記憶（→ matchmaker :9001 /api/memory/*）
│   │   ├── match_*_service.py #   配對決策 / 理由 / 狀態 / 搜尋 worker
│   │   ├── calendar_service.py / date_coordination_service.py
│   │   ├── profile_skills.py / skill_loader.py / profile_task_service.py
│   │   ├── mediator_event_service.py / mediator_context_service.py
│   │   ├── chat_service.py / demo_cleanup_service.py / giphy_service.py
│   │   └── …
│   ├── scripts/               #   一次性維護腳本（migrate / audit / cleanup）
│   ├── migrate_*.py           #   資料遷移工具
│   ├── requirements.txt
│   └── tests/                 #   social 單元/契約測試（64 檔）
│
├── matchmaker_agent/          # ★ 媒婆 Agent（port 9001）
│   ├── agent_api.py           #   FastAPI：配對決策、回饋反思、全域法則、記憶、chat triples
│   ├── matchmaker.py          #   LLM 媒婆邏輯（Neo4j 圖譜）
│   ├── test.py / requirements.txt
│   └── .env                   #   NEO4J_* / LLM_*（provision 腳本產生）
│
├── risk_backend/              # ★ 風險偵測（port 8001）
│   ├── main.py                #   uvicorn 啟動
│   ├── app/
│   │   ├── main.py            #   FastAPI app + CORS
│   │   ├── api/risk_detection.py # /detect /state /feedback /reset
│   │   ├── core/              #   rule_engine / nlp_engine / risk_fusion / scenario_risk_layer
│   │   │                      #   risk_state / intervention_engine / guardrail_engine / llm_adapters
│   │   ├── services/          #   kb_service / chat_log_service / relationship_service / …
│   │   ├── models/ utils/
│   │   └── config.py
│   ├── db_setup/              #   部署用 DB 資源（Appwrite schema / MySQL 歷史）
│   ├── tests/                 #   98 個測試
│   └── RISK_INTEGRATION.md
│
├── skills/                    # 技能指令包（profile_skills 動態載入）
│   ├── recent-context/        #   ✅ runtime 載入
│   ├── memory/                #   ✅ runtime 載入
│   ├── basic-profile-assessment/   # 契約文件（未載入）
│   └── deep-profile-assessment/    # 契約文件（未載入）
│
├── scripts/                   # 啟動/驗證/環境腳本
│   ├── run_ayue_social.sh     #   啟動 social（:8000）
│   ├── run_ayue_risk.sh       #   啟動 risk_backend（:8001）
│   ├── run_ayue_matchmaker.sh #   啟動 matchmaker_agent（:9001）
│   ├── run_ayue_guardrail.sh  #   啟動 llama.cpp guardrail（:8081）
│   ├── start_ayue_services.sh #   一次啟動全部 + health check
│   ├── check_ayue_services.sh #   健康檢查 4 服務
│   ├── provision_ayue_v3_env.sh    #   產生 social/matchmaker 的 .env
│   └── validate_ayue_v3_environment.py
│
├── tests/                     # 跨服務契約測試（contracts/）
│   ├── conftest.py            #   social 加入 sys.path
│   ├── contracts/             #   UI/API/風險契約（fixtures/ 放參考實作）
│   └── test_*.py              #   launch scripts / chat triples / UI contract / env validator
│
├── docs/                      # 文件
│   ├── AGENTS.md              #   開發規則
│   ├── AYUE_V3_ARCHITECTURE.md
│   ├── MEMORY_CONTEXT_ENGINE_GUIDE.md
│   ├── architecture/          #   子系統文件（01-project-overview … subagent-*）
│   ├── api/                   #   JSON schema + 契約文件
│   ├── ayue-v3-*.md           #   環境矩陣 / UI 契約 / 匯入紀錄
│   └── superpowers/           #   開發計畫紀錄
│
├── venv/                      # risk_backend / tests 用（Python 3.12）
└── .local-venv/               # social（:8000）與 matchmaker_agent（:9001）用（已 gitignore）
```

## 快速啟動

```bash
cd Server
./start_all.sh          # 依序啟動：guardrail → risk → matchmaker → social（前景，Ctrl+C 停止）
```

個別啟動：

```bash
./scripts/run_ayue_guardrail.sh    # :8081（可選，llama.cpp）
./scripts/run_ayue_risk.sh         # :8001
./scripts/run_ayue_matchmaker.sh   # :9001
./scripts/run_ayue_social.sh       # :8000
./scripts/check_ayue_services.sh   # 健康檢查
```

啟動後：
- 前端頁面：`http://localhost:8000/`
- 主 API：`http://localhost:8000/api/`
- 媒婆 Agent：`http://localhost:9001/health`
- 風險偵測：`http://localhost:8001/health`
- Guardrail：`http://localhost:8081/v1/models`

## 服務間連線

| 來源 | → 目標 | 用途 |
|------|--------|------|
| social | risk_backend (:8001) | `risk_policy_service.py` 配對訊息風險閘道（public_chat 保存前） |
| social | matchmaker (:9001) | `/api/match` 配對決策、`/api/memory/*` 記憶、`/api/chat_triples` 語意三元組、`/api/clear_graph`、`/api/feedback` |
| risk_backend | guardrail (:8081) | `GUARDRAIL_PROVIDER=llm_classifier` 時呼叫 llama.cpp（llama-guard-3-1b） |

## 環境變數（`.env`）

頂層 `Server/.env` 為共用設定；`social/.env` 與 `matchmaker_agent/.env` 由 `scripts/provision_ayue_v3_env.sh` 產生。

重點變數：

| 類別 | 變數 |
|------|------|
| MongoDB | `MONGO_URI`、`MONGO_DB_NAME`（Atlas 雲端，含 `$vectorSearch`） |
| Neo4j | `NEO4J_URI`、`NEO4J_USERNAME`、`NEO4J_PASSWORD`、`NEO4J_DATABASE` |
| LLM | `OLLAMA_HOST`、`OLLAMA_API_KEY`、`OLLAMA_CHAT_MODEL`、`OLLAMA_FAST_CHAT_MODEL`、`GOOGLE_AI_STUDIO_API_KEY`、`GOOGLE_EMBEDDING_MODEL` |
| 工具 | `TAVILY_API_KEY`、`TAVILY_PROJECT`、`GIPHY_API_KEY`、`GOOGLE_PLACES_SERVER_API_KEY`、`GOOGLE_MAPS_BROWSER_API_KEY` |
| 風險 | `RISK_SERVICE_URL`、`RISK_TIMEOUT_SEC`、`GUARDRAIL_PROVIDER`、`GUARDRAIL_BASE_URL`、`GUARDRAIL_MODEL_PATH`、`GUARDRAIL_SERVER_BIN`、`GUARDRAIL_PORT` |
| 媒婆 | `MATCH_AGENT_CANDIDATE_LIMIT`、`MATCH_VECTOR_QUALIFICATION_MIN` |
| 行為開關 | `AYUE_V3_SIMPLE_CHAT_FAST_PATH`、`AYUE_MAPS_ENABLED`、`AYUE_GOOGLE_PLACE_CARDS_ENABLED`、`AYUE_PROFILE_SKILLS_MODE` … |

## 測試

| 套件 | 位置 | 執行 |
|------|------|------|
| 跨服務契約 | `Server/tests/` | `Server/venv/bin/python -m pytest tests/`（49 個） |
| social | `social/tests/` | `.local-venv/social/bin/python -m pytest tests/`（800+ 個） |
| risk_backend | `risk_backend/tests/` | `Server/venv/bin/python -m pytest risk_backend/tests/`（98 個） |

> `social/tests/test_profile_skills.py` 有 17 個既有失敗（profile_skills 功能問題），與檔案結構無關。

## 資料層

| 資料 | 儲存 |
|------|------|
| 配對 profile / matches / messages / context_embedding | MongoDB Atlas `profiling_db` |
| 使用者身份 / 個人資料 / 大頭照 / 風險知識庫 | Appwrite cloud |
| 偏好圖譜 / 全域法則 / chat triples | Neo4j |
| 風險狀態 / 介入 log / 訊息 log | Appwrite KB database |

> 專案不使用 MySQL / SQLite；本地 MongoDB（`docker/local-mongo`）已移除。
