# 06. 測試策略

> 所有行為修改都必須有 deterministic test；自動測試不得連線或修改正式 MongoDB Atlas、Neo4j、Tavily 或 Google APIs。測試檔清單以 `social/tests/test_*.py` 為準，不在文件固定容易過期的檔案數量。

## 1. 基本指令

在 `social/` 下執行：

```powershell
$env:AYUE_SKIP_DOTENV = "1"
$env:MONGO_URI = "mongodb://127.0.0.1:27017/?serverSelectionTimeoutMS=50&connectTimeoutMS=50"
..\.project-venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
..\.project-venv\Scripts\python.exe -m compileall -q .
```

列出目前測試：

```powershell
rg --files tests -g "test_*.py" | Sort-Object
```

`test_google_maps_live_smoke.py`、`test_ayue_persona_live.py` 與其他標示 live 的案例需要真實 provider 設定，預設不得成為離線 CI 的必要條件。

## 2. Public V3 核心

| 測試 | 主要契約 |
| --- | --- |
| `test_v3_contracts.py` | `Plan` modes、DAG、`run_if`／Calendar outcome contract、evidence policy、forbidden authority fields |
| `test_v3_planner.py` | `decompose_tasks` function calling、direct chat／product info、Web／Places／itinerary routing、fail closed |
| `test_v3_guard.py` | registry、schema、duplicate、每 task／每回合 read budget、write confirmation |
| `test_v3_context_slicer.py` | Calendar/Places/Web/Match/Relationship/Profile/ProductInfo 與 Synthesizer 的 privacy-safe slice |
| `test_v3_scheduler.py` | 拓撲層、最大並發 2、dependency skip、confirmation、assessment、fail closed |
| `test_v3_sub_agents.py` | 各 sub-agent 可見工具與 proposal contract |
| `test_v3_synthesizer.py`、`test_v3_public_reply.py`、`test_v3_product_surface.py` | grounded reply、presentation、product identity、place-card selection |
| `test_v3_web_research.py` | bounded Web loop、證據分級、來源綁定、Places 協作與 itinerary |
| `test_v3_product_info_agent.py` | ProductInfo first-class runtime、bounded retrieval、`product_info.v1` observation、progress/debug hooks |
| `test_v3_relationship_date_invite.py` | date-card Planner intent、受限 Relationship runtime、accepted target resolution、confirmation 與 canonical write path |
| `test_v3_calendar_commands.py`、`test_v3_calendar_verification.py`、`test_v3_calendar_runtime.py` | typed Calendar mutation、availability outcome、preflight、clarification、recent mutation verification、runtime boundary and authority isolation |
| `test_v3_confirmation.py`、`test_v3_write_executors.py` | pending CAS、batch、idempotency、stale 與 domain write path |
| `test_v3_debug_trace.py`、`test_ayue_agent_stream.py` | trace／NDJSON allowlist 與隱私 |

## 3. Domain 與 API

- 配對／Event：`test_match_*`、`test_event_*`、`test_inline_match_proposals.py`、`test_proposal_nickname.py`、`test_accepted_match_integrity.py`，覆蓋 qualification、job、一般／Event 獨立 slot、同頁 progress、viewer hydration、directional reason、暱稱、婉拒選項、CAS／stale／終態、既有聊天重用與單一 Event 開場卡。
- 行事曆／共同約會：`test_calendar_*`、`test_date_coordination_service.py`、`test_private_calendar.py`。
- Profile／Memory：`test_profile_*`、`test_memory_service.py`、`test_memory_outbox_service.py`、`test_conversation_compaction_service.py`、`test_assessment_session_service.py`，特別驗證 owner evidence span、message-id idempotency、bounded retry、relation-preserving restore，以及 legacy／新版 owner room 的 watermark 與 continuity gate。
- Private Ayue：`test_private_v2.py`、`test_private_context_projection.py`、`test_private_mediator_extraction.py`、`test_private_redirect.py`、`test_semantic_plan_service_fixes.py`；覆蓋 relationship semantic projection、600 字元單位更新門檻與 raw Graph 欄位隔離，且不得和 Public V3 混用 context 或 runtime。
- 主動關心：`test_proactive_*`；驗證 atomic claim、grounding 與不重複投遞。
- HTTP／相容性：`test_chat_leaf_routers.py`、`test_chat_router_characterization.py`（launcher 測試已隨 start_ayue 移除）；保護 JSON contract、NDJSON final 與 health identity。
- 外部工具：`test_ayue_agent_web_tools.py`、`test_ayue_agent_maps_tools.py`、`test_google_places_client.py`；一律 stub provider，驗證 timeout、URL／位置安全與 bounded output。

## 4. 必覆蓋的不變量

- Planner sequence、DAG 與 fail-closed 行為。
- Guard result code、tool schema／output、重複呼叫與額度。
- Confirmation、ownership、CAS、idempotency、stale 與 concurrency。
- 一般與 Event proposal namespace 不互相佔名額；terminal state 不被較舊 polling/cache 復活；Event accepted opener 以 match-scoped key 最多保存一次。
- Match search 與 recent-context 的競態必須覆蓋：第一次 revision mismatch 原子重排同一 job 並維持公開 queued 狀態、第二次不再重排且投遞一次可見失敗、cancel／lease handoff 不得被誤判為可重試。
- 只有使用者 opt-in 勾選的婉拒原因能形成 `AVOIDS`；空選擇、取消或 stale decision 不寫記憶。
- Context／trace／stream／mention 的隱私邊界。
- Profile evidence 只來自已保存 owner 原句。
- Public V3 與 Private V2 的 runtime／context 隔離。
- `POST /api/direct_chat` JSON 相容性與 public NDJSON event allowlist。
- Web／Places 的來源綁定、卡片 projection、partial evidence 與 itinerary presentation。
- Synthesizer adaptive Markdown、無固定 Places/Web/itinerary headings、mixed server-owned reply partition，以及 cards disabled 時 zero public cards。
- Provider model-tier selection、真實 LLM call counters、Planner dependency policy，以及可選 Places -> Web bootstrap 的 off/strict/no-candidate guards。
- Planner compact-v3 prompt budget：system prompt ≤6,000 chars、provider schema ≤3,500 chars、合計 ≤9,500 chars；Planner history 最多 4 則／2,000 字元；最新 current message 不得在 history 重複；active proposal 不得帶 revision。
- Calendar busy/free/unknown branch：hard-gated downstream runner/tool_started 次數分別為 0/執行/0；普通 `task.finished` precheck 在 Calendar busy 或技術失敗後仍可繼續，且 control edge 不把 raw events 傳入下游。
- `RuntimeRegistration` runner signature、`TaskRunnerResult` 互斥結果形狀，以及 compatibility input 只在 boundary 正規化。

新增 fallback、result code 或 trace 欄位時，必須同步更新 allowlist 與 privacy test。修真實失敗案例時，先把案例匿名化加入 trajectory fixture，再修 contract、projection 或 prompt；不要新增自然語言 keyword router。

## 5. 交付檢查

- 相關 deterministic tests 通過，並記錄完整 suite 是否有既有環境型失敗。
- Python compile 通過。
- 主服務 `GET /`、`GET /api/health` 與 matchmaker `/health` 回 200。
- 修改 runtime contract、tool list、state machine 或環境旗標時，同步更新 `AYUE_V3_ARCHITECTURE.md`、`09-runtime-interfaces.md` 與對應 domain 文件。
- 不交付 `.env`、venv、log、cache、真實 trace 或 provider raw payload。
