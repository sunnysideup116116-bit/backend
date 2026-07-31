# 阿月交友 Demo

這個 repository 是目前 Demo 使用的阿月 V2。公開阿月採用單一、事件化的 agent loop，由 LLM 理解語意與選擇 typed tool；程式端負責權限、確認、狀態轉移、隱私與冪等性。

```text
Context Builder
→ LLM Planner
→ Deterministic Guard
→ Progress Event
→ Typed Tool / Domain Service
→ Verified Observation
→ Final Reply
```

目前包含：

- 公開阿月 V2 與 NDJSON progress streaming
- 配對搜尋、提案、接受／婉拒與 revision CAS
- 本人行事曆 CRUD、共同約會改期／取消
- 本人近期情境與長期記憶 extraction
- 主動關心 scheduler
- `@` 已接受聯絡人的公開資訊查詢
- Tavily 網路搜尋，以及 OpenStreetMap／Overpass 附近地點與距離查詢
- 與公開阿月隔離的阿月悄悄話 V2
- 離線 deterministic tests 與 trajectory fixtures

架構與擴充規範：

- [AYUE_V2_ARCHITECTURE.md](./AYUE_V2_ARCHITECTURE.md)：實際 runtime、tool、state、API 與 App V1 → V2 遷移方式
- [AGENTS.md](./AGENTS.md)：後續 coding agent 必須遵守的邊界與擴充規則
- [MEMORY_CONTEXT_ENGINE_GUIDE.md](./MEMORY_CONTEXT_ENGINE_GUIDE.md)：長期建議、Neo4j Graph Memory 與 Context Engine 的資料邊界和實作順序

## 專案結構

| 路徑 | 用途 |
| --- | --- |
| `social_demotest/` | FastAPI 主服務、Web Demo、Public/Private Ayue 與測試 |
| `matchmaker_agent/` | 候選排序、Neo4j 記憶與 feedback service |
| `skills/` | 近期情境與長期記憶的 versioned extraction policy |
| `start_ayue.ps1` | Windows 啟動與 health check |
| `start_ayue.cmd` | PowerShell 啟動腳本的 cmd wrapper |

## 本機設定

需求：

- Python 3.11+
- 可使用的 MongoDB
- Ollama 相容 Chat API 或專案既有 LLM 設定
- Google AI Studio API key（embedding）
- Neo4j 僅在需要完整 matchmaker 記憶功能時設定
- Tavily 僅在需要即時網路搜尋時設定

建立專案虛擬環境並安裝兩個服務的依賴：

```powershell
py -3.11 -m venv .project-venv
.\.project-venv\Scripts\python.exe -m pip install -r .\social_demotest\requirements.txt
.\.project-venv\Scripts\python.exe -m pip install -r .\matchmaker_agent\requirements.txt
```

分別複製兩個服務的環境範例後填入自己的值：

```powershell
Copy-Item .\social_demotest\.env.example .\social_demotest\.env
Copy-Item .\matchmaker_agent\.env.example .\matchmaker_agent\.env
```

`.env`、虛擬環境、log、cache 與真實 trace 都不會進 Git。不要把 API key 寫進 Python、HTML、Markdown 或測試 fixture。

## 啟動

Windows：

```powershell
.\start_ayue.ps1 -Background
```

或：

```bat
start_ayue.cmd -Background
```

啟動腳本會啟動並檢查：

- Web Demo：<http://127.0.0.1:8000/>
- Matchmaker API 文件：<http://127.0.0.1:9001/docs>

`9001/docs` 是後端 API 文件，不是 App 畫面。

## 測試

離線 deterministic tests 不應連線或修改正式 MongoDB Atlas／Neo4j：

```powershell
Set-Location .\social_demotest
$env:AYUE_SKIP_DOTENV = "1"
$env:MONGO_URI = "mongodb://127.0.0.1:27017/?serverSelectionTimeoutMS=50&connectTimeoutMS=50"
..\.project-venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Python compile：

```powershell
Set-Location .\social_demotest
..\.project-venv\Scripts\python.exe -m compileall -q .
```

正式資料修復腳本預設只做 dry-run。未經 review 不要加 `--apply`：

```powershell
Set-Location .\social_demotest
..\.project-venv\Scripts\python.exe scripts\cleanup_invalid_current_context.py
..\.project-venv\Scripts\python.exe scripts\repair_directional_match_reasons.py
```

## 開發原則

- Public V2 只有 `services/ayue_agent/runtime.py` 一個 orchestrator。
- LLM 做語意判斷；Guard 只做安全、schema、狀態與權限驗證。
- 新能力必須先進 typed tool registry；寫入必須走 canonical domain service。
- 模型不得提供 user、proposal、match、event ID 或 revision。
- 所有寫入預設需要 confirmation、ownership、CAS 與 idempotency。
- V2 失敗必須 fail closed，不得自動掉回 legacy。
- 修真實失敗案例時先新增匿名 trajectory，再修 contract、projection 或 prompt；不要堆疊中文 keyword regex。
