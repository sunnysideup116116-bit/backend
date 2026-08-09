# 01. 專案總覽

> 本篇為工程師 onboarding 的第一站：說明這個 repo 是什麼、有哪兩個服務、各自負責什麼，以及公開阿月與阿月悄悄話的關係。詳細的 Python 模組拆解、runtime 生命週期與 sub-agent 流程請見本目錄其他文件。

## 1. 這是什麼

這是一個交友 App 的後端與前端 Demo，核心是「公開阿月」——一位協助使用者認識人、牽線的 AI 媒人。阿月不是另一位使用者，也不把這個 App 當成外部服務；她是在 App 內提供媒合、行事曆、地點推薦、記憶與關係脈絡等能力的 assistant。

阿月採用 **V3 sub-agent 架構**：

```text
Context Builder
→ Planner (LLM, 拆解任務成 DAG)
→ Sub-agents (LLM + function calling，提出工具呼叫)
→ Central Guard (純程式碼驗證)
→ Typed Tool (唯讀 facade / domain service)
→ Verified Observation
→ Synthesizer (LLM, 綜合所有 observation 產出最終回覆)
→ Final Reply
```

核心原則（詳見根目錄 `AGENTS.md`）：

- **LLM 做語意判斷**（拆任務、選工具、寫回覆），**程式做安全與狀態判斷**（schema、重複呼叫、步數上限、確認、CAS、冪等、隱私投影）。
- **Tool Registry 是能力的唯一入口**：所有工具必須先註冊於 `tool_registry.py`，Planner 永遠不能提供 `user_id`、match/event ID 或 revision。
- **所有寫入先確認**：寫入工具只建立 pending confirmation，使用者回覆「確認」後才由 `write_executors.py` 透過 canonical domain service 執行。
- **失敗必須 fail closed**：模型輸出無效、逾時或工具失敗時，不執行未確認的副作用，也不掉回 legacy router。

## 2. 兩個服務

| 服務 | 目錄 | 執行 | 責任 |
| --- | --- | --- | --- |
| 主服務（social_demotest） | `social_demotest/` | FastAPI，port 8000 | 產品後端：使用者、聊天、配對狀態、行事曆、地點、記憶、公開阿月 V3 runtime、Web Demo |
| 媒婆服務（matchmaker_agent） | `matchmaker_agent/` | FastAPI，port 9001 | 候選人排序（LLM 評估）、Neo4j 圖記憶讀寫、feedback 反思、全域法則歸納 |

兩者透過 HTTP 互動：

- 主服務在配對搜尋 job 中呼叫 `POST http://127.0.0.1:9001/api/match` 取得候選人推薦（見 `routers/match.py` 的 candidate pipeline 與 `match_search_job_service.py`）。
- 配對婉拒後，主服務呼叫 `POST /api/feedback` 回饋給媒婆學習（見 `match_action_service.py:apply_transition_effects`）。
- 主服務的 `memory_service.py` 呼叫媒婆的 `/api/memory/observe`、`/api/memory/apply`、`/api/memory/{user_id}` 等端點，把已驗證的偏好寫進 Neo4j 或讀回投影。

Neo4j 只在需要完整媒婆記憶功能時設定；沒有 Neo4j 時媒婆仍可依 profile 資料排序（graph memory 讀取失敗時回傳「無法讀取圖譜記憶」，不會中斷配對）。

## 3. Repository 結構

| 路徑 | 責任 |
| --- | --- |
| `social_demotest/main.py` | FastAPI app、routers 註冊、startup 時啟動 background workers |
| `social_demotest/frontend.html` | 現行 Web UI（單檔 HTML + JS），消費 NDJSON stream 與卡片 API |
| `social_demotest/routers/` | HTTP adapters：`chat.py` 是 `/api` aggregate，leaf routers 各管一類端點 |
| `social_demotest/services/` | Domain services：配對、行事曆、記憶、profile、媒人、悄悄話等 |
| `social_demotest/services/ayue_agent/v3/` | 公開阿月 V3 runtime：scheduler、planner、guard、sub-agents、synthesizer、confirmation、write executors |
| `social_demotest/services/ayue_agent/` | V3 之外的公開阿月元件：context builder、tool registry、tools facade、web/maps clients、proactive care、private runtimes |
| `social_demotest/tests/` | 離線 deterministic contract／trajectory／state／privacy tests + fixtures |
| `matchmaker_agent/` | 媒婆服務：`agent_api.py`（FastAPI adapters）、`matchmaker.py`（LLM 評估 agent） |
| `docs/` | 設計與變更文件；本目錄 `docs/architecture/` 是架構導覽 |
| `skills/` | 近期情境、記憶、性格探索的 versioned prompt policy |
| `start_ayue.ps1` / `start_ayue.cmd` | Windows 啟動與 health check |

## 4. 公開阿月 vs 阿月悄悄話

兩者是 **sibling runtimes**，不是 parent/subagent 關係：

| | 公開阿月 | 阿月悄悄話 |
| --- | --- | --- |
| 身分 | 對使用者本人的 AI 媒人助手 | 已接受配對的雙人聊天室內的專屬媒人視角 |
| Runtime | `services/ayue_agent/v3/scheduler.py`（V3 sub-agent） | `services/ayue_agent/private_v2.py` + `private_calendar.py` |
| HTTP 入口 | `/api/direct_chat`、`/api/direct_chat/stream` | `/api/mediator/private*`（`routers/private_mediator.py`） |
| Context / Tool policy / Trace | 獨立 namespace | 獨立 namespace |
| 資料邊界 | 本人 profile、行事曆、配對狀態、記憶 | 只讀雙人關係內可公開／已同意的 projection |

公開阿月不會把 history 或 prompt 傳給悄悄話，對方私人阿月內容維持不可讀。除非任務明確要求，兩者不得合併，公開阿月也不得任意 spawn private agent。

## 5. 主要資料流（一回合）

1. 使用者送出訊息 → `POST /api/direct_chat`（JSON）或 `/api/direct_chat/stream`（NDJSON）。
2. `routers/public_chat.py` 保存使用者訊息、組 `AgentTurnContext`，呼叫 `run_public_agent_turn_v3`。
3. Scheduler 依序處理：assessment session → confirmation → Planner（拆 DAG）→ 拓撲分層平行執行 sub-agents → Guard → 工具執行 → Synthesizer 產出最終回覆。
4. 回覆保存為唯一一筆 assistant message；assessment 回答不會進 profile 記憶 pipeline。
5. 背景：`profile_skills.py` 以保存的 owner 原始訊息做近期情境／記憶 extraction；`proactive_scheduler.py` 依使用者的「AI 關心頻率」定期產生主動關心。

完整流程與每層職責見 `03-v3-runtime-lifecycle.md`。

## 6. 下一步閱讀

- 想了解 Python 各模組在做什麼 → `02-python-modules.md`
- 想了解一回合的完整生命週期 → `03-v3-runtime-lifecycle.md`
- 想了解 22 個現行工具契約與寫入確認 → `04-tool-registry.md`
- 想了解媒婆與記憶 → `05-matchmaker-and-memory.md`
- 想了解測試策略 → `06-testing.md`
- 想了解 Guard 審核什麼 → `07-guard.md`
- 想了解 Planner 怎麼拆 DAG → `08-planner.md`
- 想了解某個 sub-agent 能做什麼、呼叫哪些 function → `subagent-calendar.md`、`subagent-match.md`、`subagent-places.md`、`subagent-web.md`、`subagent-relationship.md`、`subagent-profile.md`
