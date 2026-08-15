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
- 公開阿月替已接受聯絡人建立空白約會邀請卡（確認後才建立）
- Tavily 網路搜尋，以及 OpenStreetMap／Overpass 附近地點、距離與可預覽的地點卡
- 與公開阿月隔離的阿月悄悄話
- 離線 deterministic contract／trajectory／state／privacy tests

## 架構文件（docs/architecture/）

| 文件 | 內容 |
| --- | --- |
| [01-project-overview.md](./docs/architecture/01-project-overview.md) | 系統定位、兩個服務、公開阿月 vs 悄悄話、主要資料流 |
| [02-python-modules.md](./docs/architecture/02-python-modules.md) | 每個 Python 模組在做什麼（routers / services 對照表） |
| [03-v3-runtime-lifecycle.md](./docs/architecture/03-v3-runtime-lifecycle.md) | 一回合完整生命週期：Planner → Guard → 工具 → Synthesizer、確認流程、trace |
| [04-tool-registry.md](./docs/architecture/04-tool-registry.md) | 23 個現行工具的契約、執行流程與新增工具檢查清單 |
| [05-matchmaker-and-memory.md](./docs/architecture/05-matchmaker-and-memory.md) | port 9001 媒婆、Neo4j 圖記憶、profile pipeline、配對狀態真相 |
| [06-testing.md](./docs/architecture/06-testing.md) | 測試指令、分類與必覆蓋面向 |
| [07-guard.md](./docs/architecture/07-guard.md) | Central Guard：審核什麼、GuardResultCode 全表、被拒絕後的處理 |
| [08-planner.md](./docs/architecture/08-planner.md) | Planner：decompose_tasks 契約、拆解規則、fail closed、與 sub-agents 分工 |
| [09-runtime-interfaces.md](./docs/architecture/09-runtime-interfaces.md) | HTTP、Context、Planner、RuntimeRegistration、Tool/Guard、Observation 與寫入介面 |
| [subagent-calendar.md](./docs/architecture/subagent-calendar.md) | 行事曆子代理：能做什麼、呼叫哪些 function、端到端範例 |
| [subagent-match.md](./docs/architecture/subagent-match.md) | 配對子代理：狀態查詢、搜尋 job、提案 CAS、主動牽線 |
| [subagent-places.md](./docs/architecture/subagent-places.md) | 地點子代理：附近地點、距離、地點卡、OSM/Google provider |
| [subagent-web.md](./docs/architecture/subagent-web.md) | Web 子代理：Tavily 查詢、證據等級、活動探索與 Places 串接 |
| [subagent-relationship.md](./docs/architecture/subagent-relationship.md) | 關係子代理：@ 驗證、已接受聯絡人、可驗證互動摘要與空白約會邀請卡 |
| [subagent-profile.md](./docs/architecture/subagent-profile.md) | 個人檔案子代理：self summary、記憶、性格探索 session |
| [subagent-product-info.md](./docs/architecture/subagent-product-info.md) | 產品資訊子代理：bounded knowledge retrieval、`product_info.v1` observation 與 progress/debug |

其他規範文件：

- [AYUE_V3_ARCHITECTURE.md](./AYUE_V3_ARCHITECTURE.md)：實際 runtime、tool、state、API 與 App 遷移方式（內容為 V3 sub-agent 架構）
- [AGENTS.md](./AGENTS.md)：後續 coding agent 必須遵守的邊界與擴充規則
- [MEMORY_CONTEXT_ENGINE_GUIDE.md](./MEMORY_CONTEXT_ENGINE_GUIDE.md)：長期建議、Neo4j Graph Memory 與 Context Engine 的資料邊界、Hermes Agent 參考方式和實作順序
- [FLUTTER_V1_TO_V3_AGENT_GUIDE.md](./docs/FLUTTER_V1_TO_V3_AGENT_GUIDE.md)：從舊 V1 Demo 搬到目前 V3 時，給 Flutter 整合 agent 的替換策略、API 差異與驗收清單
- [docs/architecture/](./docs/architecture/)：以現行程式為準的架構與 sub-agent 文件；已完成的 migration／Phase 計畫不保留為現況文件

版本名稱約定：`Public V3` 是目前公開阿月架構；`Private V2` 是仍在使用且隔離的悄悄話 runtime。`public-v1`、`web_research.v1`、`product_info.v1` 等名稱是 typed payload 的 schema version，不代表舊 Public runtime。已移除的 Public V1/V2 文件與 prompt 範例不再保留；需要理解層與層之間的 contract 時，以 [09-runtime-interfaces.md](./docs/architecture/09-runtime-interfaces.md) 為入口。

## 專案結構

| 路徑 | 用途 |
| --- | --- |
| `social_demotest/` | FastAPI 主服務、Web Demo、Public/Private Ayue 與測試（port 8000） |
| `matchmaker_agent/` | 候選排序、Neo4j 記憶與 feedback service（port 9001） |
| `docs/architecture/` | 架構導覽與 sub-agent 流程文件（本文檔） |
| `docs/` | 現行架構與技術契約文件 |
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

附近地點預設使用 OpenStreetMap／Overpass，不需要 Google API key。`AYUE_GOOGLE_PLACE_CARDS_ENABLED=on` 只啟用 Google Places／Routes 後端資料來源；對 Public 回傳 `place_cards`／`presentation_blocks` 還必須另外開啟 `AYUE_PUBLIC_PLACE_CARDS_ENABLED=on`。目前 Demo 的 public card 開關預設關閉，因此 Flutter 應先以文字／Markdown 回覆為正式介面，不能假設一定會收到卡片。未設定 Google keys 或 provider 失敗時，地點查詢仍可回退 OSM。

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
- Web Demo health：<http://127.0.0.1:8000/api/health>
- Matchmaker API 文件：<http://127.0.0.1:9001/docs>
- Matchmaker health：<http://127.0.0.1:9001/health>

`9001/docs` 是後端 API 文件，不是 App 畫面。
冷啟動健康檢查預設等待 90 秒；較慢的本機環境可用
`-StartupTimeoutSeconds 120` 調整。健康檢查使用固定 JSON service identity，
不依賴首頁標題或畫面文字。

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
- Scheduler 只透過 `RuntimeRegistration`／`TaskRunnerResult` dispatch sub-agent；domain loop、clarification 與 typed assembly 留在擁有它的 runtime。
- LLM 做語意判斷；Guard 只做安全、schema、狀態與權限驗證。
- 新能力必須先進 typed tool registry；寫入必須走 canonical domain service。
- 模型不得提供 user、proposal、match、event ID 或 revision。
- 所有寫入預設需要 confirmation、ownership、CAS 與 idempotency。
- V3 失敗必須 fail closed，不得自動掉回 legacy。
- 修真實失敗案例時先加入對應 owner 的匿名 deterministic trajectory test，再修 contract、projection 或 prompt；不要堆疊中文 keyword regex。
- 多輪地點推薦由 Public V3 直接依當回合 bounded context 判斷；條件已足夠或使用者把選擇交給阿月時，必須直接查詢，不能重複追問料理類型。
