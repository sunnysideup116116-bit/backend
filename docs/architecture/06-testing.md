# 06. 測試策略

> 本篇說明測試怎麼跑、覆蓋哪些契約。**所有行為修改都必須有 deterministic test**；不可只用真實模型手測。Test harness 使用 stub config 或 local test database，自動測試不得連線或修改正式 MongoDB Atlas／Neo4j。

## 1. 執行指令

在 `social_demotest/` 下：

```powershell
$env:AYUE_SKIP_DOTENV = "1"
$env:MONGO_URI = "mongodb://127.0.0.1:27017/?serverSelectionTimeoutMS=50&connectTimeoutMS=50"
..\.project-venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

Python compile 檢查：

```powershell
..\.project-venv\Scripts\python.exe -m compileall -q .
```

## 2. 測試分類（`social_demotest/tests/`，46 檔）

### V3 runtime（9 檔）

| 檔案 | 覆蓋 |
| --- | --- |
| `test_v3_contracts.py` | `Plan`/`SubTask`/`ToolProposal` typed contracts、DAG 驗證、forbidden fields |
| `test_v3_planner.py` | `plan_turn`：牛排範例產生 calendar+places+synthesizer DAG、assessment intent routing 到 profile、synthesizer-only、無 tool call／錯工具名／invalid args／timeout → None、opportunity 攜帶 |
| `test_v3_guard.py` | `guard_proposal` 全部 code：pass、unknown tool、schema、duplicate、step limit、write requires confirmation |
| `test_v3_context_slicer.py` | 各 agent 的 privacy-safe slice 欄位 |
| `test_v3_scheduler.py` | Scheduler 編排：拓撲層、平行、依賴失敗 skip、assessment profile confirmation、confirmation 入口、fail closed |
| `test_v3_sub_agents.py` | 各 sub-agent 的 tool 集合與 system prompt 行為，含 profile.start_assessment 與 Calendar typed command rejection |
| `test_v3_synthesizer.py` | Synthesizer 綜合 observation、typed clarification／confirmed domain reply 不經 LLM 改寫、place card 決策、內部欄位剝離 |
| `test_v3_web_research.py` | Web bounded loop 的 search→observation→extract 順序、evidence relevance、query budget、URL projection、insufficient/unavailable fallback |
| `test_v3_confirmation.py` | ConfirmationManager：CAS claim、batch 合併、多 confirmation 獨立性 |
| `test_v3_write_executors.py` | 寫入執行器：start_search 冪等重放、decide_active_proposal revision CAS / stale、assessment、calendar batch（混合 update+cancel） |
| `test_v3_trajectories.py` | 匿名 trajectory fixtures：修真實失敗案例時先新增 trajectory，再修 contract／projection／prompt |

### 公開阿月元件（8 檔）

| 檔案 | 覆蓋 |
| --- | --- |
| `test_ayue_agent_context.py` | Context builder 限制（12 則訊息／6000 字元）、內部 ID 清理 |
| `test_ayue_agent_registry.py` | ToolSpec 契約、write tool 不能 bypass runtime executor、output model 驗證 |
| `test_ayue_agent_maps_tools.py` | OSM/Google 地點工具：saved location 只在要求時使用、Google 失敗回退、distance 無 origin 不猜位置 |
| `test_ayue_agent_mentions.py` | @ 驗證：公開欄位 only、非 accepted 拒絕 |
| `test_ayue_agent_stream.py` | NDJSON stream 事件形狀與 sanitize |
| `test_ayue_agent_time_context.py` | Turn clock 與相對日期解析 |
| `test_ayue_agent_web_tools.py` | Tavily 搜尋／extract、URL 安全、saved location |
| `test_ayue_match_opportunity.py` | 配對機會評估：basis、block、fingerprint |

### Domain services（其餘）

- 配對：`test_match_action_service.py`、`test_match_decision_service.py`（CAS/stale/idempotency/並發）、`test_match_state_service.py`、`test_match_qualification.py`、`test_match_search_job_service.py`、`test_accepted_match_integrity.py`
- 行事曆／約會：`test_calendar_event_matching.py`、`test_date_coordination_service.py`
- 記憶／profile：`test_memory_service.py`、`test_profile_skills.py`、`test_profile_task_service.py`、`test_assessment_session_service.py`
- 悄悄話：`test_private_ayue_agent.py`、`test_private_mediator_extraction.py`、`test_private_v2.py`
- 主動關心：`test_proactive_care.py`、`test_proactive_delivery_service.py`、`test_proactive_scheduler.py`
- 關係：`test_relationship_engagement_service.py`、`test_relationship_quiz_service.py`
- Router 相容：`test_chat_leaf_routers.py`、`test_chat_router_characterization.py`、`test_chat_service_system_messages.py`
- 其他：`test_giphy_service.py`、`test_google_places_client.py`、`test_google_maps_live_smoke.py`（live smoke 需真實 key，預設不執行）、`test_demo_tools.py`

## 3. 必覆蓋面向（AGENTS.md 交付標準）

至少覆蓋：

- planner sequence（DAG 拆解）
- guard（全部 GuardResultCode）
- tool schema / output（registry + tools facade）
- trajectory（匿名失敗案例）
- privacy（context、tool projection、trace allowlist、mention 邊界）
- profile evidence（evidence_span 驗證、message_id 冪等）
- state/concurrency（CAS、stale、雙寫入並發、終態不可覆寫）
- stream 與 JSON compatibility（`/api/direct_chat` contract 只加 optional fields）
- Product identity/surface contract：same-Ayue identity、bounded cross-surface context、Private 不回流 Public profile/memory、Private 新配對 redirect。
- Presentation contract：`Plan.mode=product_info`、`AyueReplyPresentation` bubble limits、`AgentResult.messages` additive projection、單回合單筆 assistant storage、onboarding version/idempotency。
- `tests/fixtures/ayue_voice_eval_v1.json` 提供 40 個 deterministic voice/privacy/product cases；eval 應檢查 hard-fail privacy/transaction/unverified-success 規則，不比對 LLM 完整字串。

## 4. 新增 fallback／result code 時

同步更新 trace allowlist（`scheduler.py:_persist_trace` 允許的 metadata）與 privacy test（`test_ayue_agent_stream.py` 等）。Trace 不得包含 arguments、tool result、prompt、raw exception、ID 或 revision。

Synthesizer 另須覆蓋：capability/general chat 的 synthesizer-only path 不呼叫 domain tool；`reply_source` 能區分 capability、verified observation、LLM 與 fallback；provider error／空 content／被拒絕的模型內容標為 `degraded`，且 Final 必須與 Synthesizer 節點結果逐字一致。Local debug 的 `模型 Function Calls=[]` 代表該回合沒有需要的卡片決策工具，不是 function failure。

## 5. 交付檢查清單

- Python source compile 通過。
- 主服務與 matchmaker 可啟動，`GET /` 與 port 9001 `/health` 回 200。
- 修改 runtime contract、tool list、state machine、環境旗標或 App migration 步驟時，同步更新 `AYUE_V3_ARCHITECTURE.md`。
- 交付清單列出修改檔案、測試指令／結果、未解決問題；不交付 `.env`、venv、log、cache 或真實 trace。
`test_v3_calendar_commands.py` covers the current typed Calendar mutation contract: authority-field rejection, missing-field clarification, unique/ambiguous/not-found preflight, personal/shared-date routing metadata, one confirmation, one-time resolution, sequential stop-on-failure, and stale cancellation batches.

The regression suite also covers the `calendar.get_next_my_event` safe projection, pronoun follow-up via `target_reference="recent_event"`, soft match-opportunity observations (including explicit acceptance into normal confirmation), and stable match-search failure codes/stages for unavailable vector or matchmaker services.

Places/Web regression coverage also includes dependency projection, ephemeral
candidate-ref binding, wrong-place source rejection, query anchoring, partial
evidence, Places-only versus Places->Web routing, and retrieval/display count
separation. Presentation tests cover curated 2-3 card recommendations,
broad browse, explicit five-card requests, partial A/C verification, text/card
mismatch, simple short replies, and the bounded
`grounded_recommendation` envelope.
