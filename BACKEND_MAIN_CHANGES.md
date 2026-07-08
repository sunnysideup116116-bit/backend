# backend-main 變更整理

檢查日期：2026-07-08  
檢查範圍：`backend-main`

## 摘要

本次整理的主要成果，是在 `backend-main` 下新增一個整合後端工作區：

```text
backend-main\Project
```

這個 `Project` 把目前可用的新後端核心與獨立功能線集中到同一個資料夾，但保留多服務啟動邊界。

目前預期服務邊界：

```text
8000 Project/main.py -> social_demotest + NEW_AI_GEN
8001 Project/risk_backend
9001 Project/matchmaker_agent
```

## 新增的 Project 結構

目前 `backend-main\Project` 實際結構，依照github上專案目錄結構：

```
Project/
├── docker/
├── matchmaker_agent/
├── NEW_AI_GEN/
├── risk_backend/
├── social_demotest/
├── venv/
├── __pycache__/
├── .env
├── .gitignore
├── CURRENT_AYUE_MATCHMAKER_HANDOFF.md
├── main.py
├── README.md
└── requirements.txt
```

其中 `venv/` 與 `__pycache__/` 是本機執行產物，不應視為正式原始碼架構，也不應提交。

## 是否符合預期專案架構

原先預期架構：

```text
Project/
├── main.py
├── requirements.txt
├── .env
├── social_demotest/
├── matchmaker_agent/
├── NEW_AI_GEN/
└── venv/
```

目前實際狀態：大致符合，並額外保留了必要輔助模組。

符合項目：

```text
Project/main.py
Project/requirements.txt
Project/.env
Project/social_demotest/
Project/matchmaker_agent/
Project/NEW_AI_GEN/
Project/venv/
```

額外保留但合理的項目：

```text
Project/risk_backend/
Project/docker/
Project/README.md
Project/.gitignore
Project/CURRENT_AYUE_MATCHMAKER_HANDOFF.md
```

差異說明：

- `risk_backend/` 是從 `backend-main` 獨立功能線搬入，仍作為 8001 服務。
- `docker/` 是原 `backend-main` 的輔助環境設定，先保留。
- `README.md` 是本次新增的操作說明。
- `.gitignore` 是避免提交 `.env`、venv、cache 等本機或敏感檔案。
- `CURRENT_AYUE_MATCHMAKER_HANDOFF.md` 是目前 matchmaker 後端交接與邏輯說明文件。

## Project/main.py

新增檔案：

```text
backend-main\Project\main.py
```

用途：

- 作為 8000 統一入口。
- 掛載 `social_demotest` 的 router：
  - `frontend`
  - `chat`
  - `match`
  - `system`
- 嘗試掛載 `NEW_AI_GEN` router 到：

```text
/ai-gen
```

- 提供健康檢查：

```text
GET /health
```

服務邊界：

- `matchmaker_agent` 不被直接合併進同一 process，仍由 9001 提供。
- `risk_backend` 不被直接合併進同一 process，仍由 8001 提供。

這樣做是為了降低整合風險，避免一次改動 8000、8001、9001 三個服務的執行模型。

## Project/requirements.txt

新增檔案：

```text
backend-main\Project\requirements.txt
```

用途：

- 合併 `social_demotest`
- 合併 `matchmaker_agent`
- 合併 `risk_backend`
- 合併 `NEW_AI_GEN`

目前包含主要依賴：

```text
fastapi
uvicorn
pymongo
python-dotenv
openai
google-generativeai
google-genai
ollama
neo4j
appwrite
httpx
requests
certifi
pydantic
pytz
Flask
Flask-CORS
PyPDF2
pytest
```

## Project/.env

新增/合併檔案：

```text
backend-main\Project\.env
```

合併來源：

```text
backend-main\.env
DatingApp\Project\social_demotest\.env
DatingApp\Project\matchmaker_agent\.env
```

處理方式：

- 合併 key。
- 重複 key 只保留第一次出現版本。
- 後續發現 `MONGO_URI` 被保留成 localhost，已改回 `social_demotest` 原本使用的 MongoDB Atlas URI。
- 不在本文件揭露任何 secret 值。

已確認存在的主要 key 類型：

```text
MONGO_URI
AI_CHAT_MONGO_URI
NEO4J_URI
NEO4J_USERNAME
NEO4J_PASSWORD
NEO4J_DATABASE
GOOGLE_AI_STUDIO_API_KEY
GEMINI_API_KEY
OLLAMA_HOST
OLLAMA_API_KEY
OLLAMA_CHAT_MODEL
OPENAI_API_KEY
LLM_API_KEY
LLM_BASE_URL
LLM_MODEL_ID
APPWRITE_ENDPOINT
APPWRITE_PROJECT_ID
APPWRITE_API_KEY
APPWRITE_DB_ID
RISK_SERVICE_URL
```

已驗證：

```text
MONGO_PING=OK
LLM_API_KEY=SET
LLM_BASE_URL=SET
LLM_MODEL_ID=SET
MATCHMAKER_AGENT_INIT=OK
```

## Project/README.md

新增檔案：

```text
backend-main\Project\README.md
```

內容包含：

- 架構說明
- 資料夾結構
- 初次建立 venv 指令
- 三服務啟動順序
- 8000 / 8001 / 9001 服務邊界說明
- 說明 `main_app` 不再作為主版本使用

## social_demotest

位置：

```text
backend-main\Project\social_demotest
```

用途：

- 目前 8000 主後端核心。
- 管理社交分析、媒合流程、聊天、系統初始化、guidance、memory、risk client 等。

目前主要檔案：

```text
main.py
config.py
database.py
models.py
frontend.html
requirements.txt
```

目前 routers：

```text
routers/chat.py
routers/frontend.py
routers/match.py
routers/system.py
```

目前 services：

```text
services/ai_service.py
services/chat_service.py
services/mediator_event_service.py
services/memory_service.py
services/risk_client.py
```

已做的重要修正：

- `config.py` 改為先讀 `Project\.env`，再讀子資料夾 `.env` 覆蓋。
- `main.py` 已具備 CORS 設定。
- `chat.py` 已加入 guidance endpoints：
  - `POST /api/guidance/activity`
  - `GET /api/guidance/status`
  - `POST /api/guidance/suggestion`
- `chat.py` 的訊息讀取會回傳穩定 `id`。
- `chat.py` 已接入 `risk_client`。
- `match.py` 仍會呼叫 9001 matchmaker agent。
- `system.py` 保留系統初始化、設定、清除資料、profile memory 等功能。

## matchmaker_agent

位置：

```text
backend-main\Project\matchmaker_agent
```

用途：

- 9001 媒婆 Agent 認知模組。
- 管理 Neo4j graph memory、候選人決策、A/B 推薦、feedback learning。

目前主要檔案：

```text
agent_api.py
matchmaker.py
requirements.txt
test.py
```

已做的重要修正：

- `agent_api.py` 改為先讀 `Project\.env`，再讀 `matchmaker_agent\.env` 覆蓋。
- `matchmaker.py` 改為先讀 `Project\.env`，再讀 `matchmaker_agent\.env` 覆蓋。
- 已驗證 `MatchmakerAgent` 初始化不再因 missing credentials 失敗。

啟動方式：

```powershell
cd backend-main\Project\matchmaker_agent
..\venv\Scripts\python.exe agent_api.py
```

預期服務：

```text
http://127.0.0.1:9001
```

## risk_backend

位置：

```text
backend-main\Project\risk_backend
```

用途：

- 8001 風險偵測服務。
- 提供對話風險判斷、intervention、Appwrite risk history / feedback 等功能。

目前主要結構：

```text
main.py
requirements.txt
RISK_INTEGRATION.md
app/
db_setup/
tests/
```

已做的重要修正：

- `app/core/appwrite_config.py` 改為先讀 `Project\.env`，再讀 `risk_backend\.env` 覆蓋。
- Appwrite endpoint 缺失時有預設值，避免 `None.startswith` 啟動錯誤。

啟動方式：

```powershell
cd backend-main\Project\risk_backend
..\venv\Scripts\python.exe main.py
```

預期服務：

```text
http://127.0.0.1:8001
```
也就是配對的網頁，可以切換使用者帳號進行測試，使用者帳號包括10位seed_user、demo_user、以及兩個註冊帳號

## NEW_AI_GEN

位置：

```text
backend-main\Project\NEW_AI_GEN
```

用途：

- 即時對話建議與 scaffold UI nudge 模組。
- 可被 `Project/main.py` 掛載到 `/ai-gen`。

目前主要檔案：

```text
app.py
index.html
script.js
style.css
requirements.txt
test_agent.py
verify.py
```

另含 PDF 與文字說明資料。

已做的重要修正：

- `app.py` 已改為讀頂層 `Project\.env`。
- 移除對 `mongoDB_string.txt` 的依賴。
- Gemini key 缺失時不會在 import 階段直接 crash。

## docker

現在位置：

```text
backend-main\Project\docker
```

來源：

```text
backend-main\docker
```

用途：

- 保留原本 backend-main 的 local infra 輔助資料。
- 目前未強制依賴 docker 啟動。

## 不納入 Project 主版本的既有 backend-main 內容

以下仍保留在 `backend-main` 根目錄，但不是新的 `Project` 主版本：

```text
backend-main\main_app
backend-main\matchmaker_agent
backend-main\risk_backend
backend-main\ai_gen
backend-main\main.py
backend-main\requirements.txt
```

原因：

- `main_app` 與新的 `Project\social_demotest` 重複，且目前以 `social_demotest` 為主。
- 根目錄的 `matchmaker_agent`、`risk_backend`、`ai_gen` 是原 backend-main 版本，已將整合後版本放入 `Project`。
- 根目錄 `main.py` 是舊的統一入口，新的入口是 `Project\main.py`。

## 目前應避免提交的項目

以下項目已存在或可能存在，但不應提交：

```text
backend-main\.env
backend-main\Project\.env
backend-main\venv/
backend-main\Project\venv/
backend-main\__pycache__/
backend-main\Project\__pycache__/
*.pyc
*.pyo
```

目前根目錄 `.gitignore` 已涵蓋這些類型。