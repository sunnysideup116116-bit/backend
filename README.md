# 阿月交友 Demo

這個 repository 是目前 Demo 使用的阿月 V3。公開阿月採用 sub-agent 架構：Planner 拆解任務 DAG、Sub-agents 以 function calling 執行工具、Synthesizer 彙整回覆；程式端負責權限、確認、狀態轉移、隱私與冪等性。

```text
Context Builder
→ Planner (decompose DAG)
→ Sub-agents (Guard + Typed Tool)
→ Verified Observations
→ Synthesizer
→ Final Reply
```

目前包含：

- 公開阿月 V3 sub-agent runtime 與 NDJSON progress streaming
- 配對搜尋、提案、接受／婉拒與 revision CAS
- 本人行事曆 CRUD、共同約會改期／取消
- 本人近期情境與長期記憶 extraction
- 主動關心 scheduler
- `@` 已接受聯絡人的公開資訊查詢
- Tavily 網路搜尋，以及 OpenStreetMap／Overpass 附近地點、距離與可預覽的地點卡
- 與公開阿月隔離的阿月悄悄話
- 離線 deterministic tests 與 trajectory fixtures

## 架構文件（docs/architecture/）

| 文件 | 內容 |
| --- | --- |
| [01-project-overview.md](./docs/architecture/01-project-overview.md) | 系統定位、兩個服務、公開阿月 vs 悄悄話、主要資料流 |
| [02-python-modules.md](./docs/architecture/02-python-modules.md) | 每個 Python 模組在做什麼（routers / services 對照表） |
| [03-v3-runtime-lifecycle.md](./docs/architecture/03-v3-runtime-lifecycle.md) | 一回合完整生命週期：Planner → Guard → 工具 → Synthesizer、確認流程、trace |
| [04-tool-registry.md](./docs/architecture/04-tool-registry.md) | 23 個工具的契約、執行流程與新增工具檢查清單 |
| [05-matchmaker-and-memory.md](./docs/architecture/05-matchmaker-and-memory.md) | port 9001 媒婆、Neo4j 圖記憶、profile pipeline、配對狀態真相 |
| [06-testing.md](./docs/architecture/06-testing.md) | 測試指令、分類與必覆蓋面向 |
| [07-guard.md](./docs/architecture/07-guard.md) | Central Guard：審核什麼、GuardResultCode 全表、被拒絕後的處理 |
| [08-planner.md](./docs/architecture/08-planner.md) | Planner：decompose_tasks 契約、拆解規則、fail closed、與 sub-agents 分工 |
| [subagent-calendar.md](./docs/architecture/subagent-calendar.md) | 行事曆子代理：能做什麼、呼叫哪些 function、端到端範例 |
| [subagent-match.md](./docs/architecture/subagent-match.md) | 配對子代理：狀態查詢、搜尋 job、提案 CAS、主動牽線 |
| [subagent-places.md](./docs/architecture/subagent-places.md) | 地點子代理：附近地點、距離、地點卡、OSM/Google provider |
| [subagent-relationship.md](./docs/architecture/subagent-relationship.md) | 關係子代理：@ 驗證、已接受聯絡人、可驗證互動摘要 |
| [subagent-profile.md](./docs/architecture/subagent-profile.md) | 個人檔案子代理：self summary、記憶、性格探索 session |

其他規範文件：

- [AYUE_V3_ARCHITECTURE.md](./AYUE_V3_ARCHITECTURE.md)：實際 runtime、tool、state、API 與 App 遷移方式（內容為 V3 sub-agent 架構）
- [AGENTS.md](./AGENTS.md)：後續 coding agent 必須遵守的邊界與擴充規則
- [MEMORY_CONTEXT_ENGINE_GUIDE.md](./MEMORY_CONTEXT_ENGINE_GUIDE.md)：長期建議、Neo4j Graph Memory 與 Context Engine 的資料邊界、Hermes Agent 參考方式和實作順序
- [docs/](./docs/)：其他詳細設計與變更說明（Google Maps 遷移計畫、行事曆已知問題、地點菜系支援等）

## 專案結構

| 路徑 | 用途 |
| --- | --- |
| `social_demotest/` | FastAPI 主服務、Web Demo、Public/Private Ayue 與測試（port 8000） |
| `matchmaker_agent/` | 候選排序、Neo4j 記憶與 feedback service（port 9001） |
| `docs/architecture/` | 架構導覽與 sub-agent 流程文件（本文檔） |
| `docs/` | 其他詳細設計與技術規格說明 |
| `skills/` | 近期情境、長期記憶與性格探索的 versioned policy |
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

附近地點預設使用 OpenStreetMap／Overpass，不需要 Google API key。若要選擇性升級成 Google Places UI Kit 卡片，再設定 `AYUE_GOOGLE_PLACE_CARDS_ENABLED=on`、後端 Places key 與受 HTTP referrer 限制的瀏覽器 Maps key；未設定或載入失敗時仍會保留 OSM 自製卡片。

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

- Public V3 只有 `services/ayue_agent/v3/scheduler.py` 一個 orchestrator。
- LLM 做語意判斷；Guard 只做安全、schema、狀態與權限驗證。
- 新能力必須先進 typed tool registry；寫入必須走 canonical domain service。
- 模型不得提供 user、proposal、match、event ID 或 revision。
- 所有寫入預設需要 confirmation、ownership、CAS 與 idempotency。
- V3 失敗必須 fail closed，不得自動掉回 legacy。
- 修真實失敗案例時先新增匿名 trajectory，再修 contract、projection 或 prompt；不要堆疊中文 keyword regex。
- 多輪地點推薦會保存 15 分鐘的 bounded search draft；條件已足夠或使用者把選擇交給阿月時，必須直接查詢，不能重複追問料理類型。
