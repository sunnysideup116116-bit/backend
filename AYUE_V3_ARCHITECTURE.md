# 公開阿月 V3：現行架構

> 本文件只描述目前程式，不保存已完成的 Public V1/V2 migration 計畫或舊 prompt 快照。層與層之間的精確 typed interface 請見 [`docs/architecture/09-runtime-interfaces.md`](./docs/architecture/09-runtime-interfaces.md)；各 domain 行為請見 `docs/architecture/subagent-*.md`。

## 1. Current baseline

- Public Ayue 永遠走 V3 sub-agent runtime；唯一 orchestrator 是 `social/services/ayue_agent/v3/scheduler.py`。
- Event discovery 不在 Public Ayue request 內執行：8000 只寫入 Mongo singleton job，`social/event_worker.py` 獨立處理。Worker 以 MongoDB Change Stream 即時接收 queued transition，並以低頻 reconciliation 恢復漏失通知、斷線或租約到期工作，不做固定 2 秒 polling。手動 `discovery` job 只搜尋與建圖；`EVENT_WEEKLY_CYCLE_ENABLED=on` 時，每週一的 `weekly_cycle` job 由同一 Worker 依序執行 scoped Event reset、未來 30 天 discovery（每類最低 4、目標與上限 6）、等待 Concept embedding/relevance ready，再執行 invitation scan。Event identity 由 9001 以 Unicode NFKC 正規化的類別／標題／場地／日期管理，來源 URL 只作證據；全部批次完成後執行 Event-only reconciliation 與每類上限收斂，不碰 User。等待逾時時 fail closed 為 partial 並跳過邀請。Reset 只刪 Event 與因此孤立的 Concept，將未決定活動邀請轉為 expired，保留 User、一般配對及 accepted/declined 歷史。正式 `start_all.sh` 不另開第五個 Event process；Social FastAPI startup 會呼叫 `start_event_discovery_worker()`，在 :8000 進程內建立 daemon background thread。Worker 不阻塞 request path，也不依賴使用者聊天。
- Public 失敗時 fail closed。Rollback 只能透過 deployment／commit rollback，不存在 request-level legacy fallback、rollout allowlist 或第二套 public router。
- Private Ayue 是仍在使用、與 Public 隔離的 current V2 runtime，由 `routers/private_mediator.py`、`services/ayue_agent/private_v2.py` 與 `private_calendar.py` 擁有。
- `public-v1`、`web_research.v1`、`product_info.v1`、`TurnClockV1` 等名稱是 typed payload/schema version，不是 Public V1 runtime。
- `runtime.py`、`legacy_match_routing.py`、`private_runtime.py` 與舊 Public 單一-loop prompt 文件均已移除；不得恢復。

## 2. 系統定位

公開阿月是交友 App 內協助使用者認識人、牽線與整理行動的 AI 媒人，不是另一位使用者，也不是外部服務。產品身份與語氣的程式真相位於 `services/ayue_agent/product_identity.py` 與 `capabilities.py`。

```text
HTTP adapter
  -> Context Builder
  -> Planner（靜態 DAG）
  -> Runtime registrations / Sub-agents
       -> Central Guard
       -> Typed tools / domain services
  -> Verified observations
  -> Synthesizer
  -> AgentResult / Final
```

核心分工：

- LLM：理解語意、拆任務、提出 authority-free tool proposal、根據 observation 組回覆。
- 程式：ownership、schema、budget、URL/mention binding、confirmation、CAS、idempotency、privacy projection 與 trace allowlist。
- Domain service：canonical read/write truth；Router、Scheduler、Agent 與 UI 不重複實作同一狀態轉移。

## 3. Repository owners

| Owner | 路徑 | 責任 |
| --- | --- | --- |
| Public HTTP | `social/routers/public_chat.py` | JSON/NDJSON adapter、訊息唯一保存、mention 驗證、呼叫 V3 |
| Context | `services/ayue_agent/context.py` | 建立唯一 prompt-safe `PublicAgentTurnContext` |
| Planner | `services/ayue_agent/v3/planner.py` | `decompose_tasks` function calling，輸出 direct chat 或靜態 DAG |
| Contracts | `services/ayue_agent/v3/contracts.py` | `Plan`、`SubTask`、`ToolProposal`、`SubTaskResult` |
| Orchestrator | `services/ayue_agent/v3/scheduler.py` | 特殊入口、DAG、registration dispatch、共用 budgets、confirmation、trace、final |
| Runtime interface | `services/ayue_agent/v3/runtime_registry.py` | `RuntimeRegistration`、`TaskRunnerResult` |
| Guard adapter | `services/ayue_agent/v3/guarded_execution.py` | Guard→executor args→typed tool；Web URL binding |
| Calendar runtime | `services/ayue_agent/v3/calendar_runtime.py` | clarification、draft/reference、reads、commands、preflight、confirmation preparation |
| Web runtime | `services/ayue_agent/v3/web_runtime.py` | bounded research/finish phases 與 `web_research.v1` assembly |
| Relationship runtime | `services/ayue_agent/v3/relationship_runtime.py` | 一般 read proposal 與 date-card write-intent 的受限 dispatch |
| ProductInfo | `services/ayue_agent/v3/sub_agents/product_info_agent.py` | bounded allowlisted product knowledge 與 `product_info.v1` observation |
| Guard | `services/ayue_agent/v3/guard.py` | 純程式碼 registry/schema/duplicate/budget/write-confirmation validation |
| Tool registry | `services/ayue_agent/tool_registry.py` | ToolSpec、三層 schema、risk、argument source、progress |
| Tool reads | `services/ayue_agent/tools.py` | 唯讀 capability facade 與 typed output projection |
| Confirmed writes | `services/ayue_agent/v3/write_executors.py` | 已確認寫入的唯一執行入口 |
| Synthesizer | `services/ayue_agent/v3/synthesizer.py` | 根據 verified observations 產出 user-facing response |
| Public debug | `services/ayue_agent/v3/debug_trace.py` | localhost-only ephemeral debug events |

## 4. Public request lifecycle

### 4.1 HTTP input

Public UI 使用 `POST /api/direct_chat/stream`；`POST /api/direct_chat` 保留 JSON contract。`routers/public_chat.py`：

1. 驗證 public contact 與最多三個 accepted mentions。
2. 保存 owner message 一次。
3. 組 `AgentTurnContext`，呼叫 `run_public_agent_turn_v3()`。
4. 保存 final assistant message 一次。
5. 非 assessment owner message 才背景排入 profile extraction。

### 4.2 Planner 前的特殊入口

Scheduler 先處理：

- assessment commit 的 room-scoped 泡泡按鈕／active session；
- typed `assessment_action="cancel"`；
- 純對話寫入的 `bubble_buttons_v1`（精確 `choice_id`）與既有媒人卡片隔離的 `legacy_text` 協議；
- 逾期、stale 或已被其他 worker claim 的 confirmation。

這些狀態不進 Planner，避免一般對話重新解讀 authority-bearing action。

### 4.3 Planner

Planner 只呼叫 `decompose_tasks`：

- `mode="direct_chat"`：不需要 App/domain/private/external truth 的 bounded conversational reply。
- `mode="tasks"`：1–5 個 `SubTask`，最多四個 domain tasks，恰好一個 terminal synthesizer。
- `write_intent` 是 provider-required typed decision：一般請求為 `none`；約會邀請卡為 `relationship.date_invitation.v1`，且 canonical contract 只接受 `relationship -> synthesizer`，不接受 Match 或其他 precheck。

Agent allowlist：

```text
calendar | places | web | match | relationship | profile | product_info | synthesizer
```

`task_brief` 保存目標與限制，不是 tool arguments。只有 Web 可帶 `evidence_policy=casual_discovery|strict_verification`；Calendar availability task 可帶 `outcome_contract=calendar.availability.v1`，下游可用 bounded `run_if` control edge 等待結果而不接收 Calendar observation。

Planner 只有在下游會消費上游 typed observation、candidate ref 或其他明確 contract 時才建立 `depends_on`；獨立請求放在同一層平行執行，不為了排列順序製造依賴。需要等待但不傳遞資料時使用 bounded `run_if` control edge。

Planner 使用 compact prompt projection：保留最近 4 則且最多 2,000 字元的歷史，移除已在 `message` 中重複的最新 user message；clock 只投影本地日期／時間、時區、星期與實際出現的相對日期 reference。缺少的 optional state 不送入 prompt，active proposal 不包含 revision。這只縮短 Planner input，不改 PublicAgentTurnContext 的全域 12 則／6,000 字元 budget 或其他 specialist slice。

目前 compact-v3 prompt 將「具體日期＋從既有聯絡人挑一位＋新活動＋附近晚餐」明定為 `calendar`、平行 gated 的 `relationship`／`web`、依賴 Web activity venue 的 `places`，以及 terminal `synthesizer`。任一子需求需要 domain state 或 external truth 時，整回合不得使用 direct chat 或 provider-authored synth-only plan。Planner schema 失敗最多重試一次，並只附加欄位級 allowlisted 修正提示；無效／空 DAG 不靜默降級成 Synthesizer-only。

舊 provider 可能送出 task-free `mode="product_info"`／`product_info_topics`；compatibility boundary 只把它正規化成 `product_info -> synthesizer` DAG。新 Planner、fixture 與文件不得產生舊 envelope。

Provider compatibility normalization 只處理已列入 allowlist 的編碼漂移：known agent 上錯置但值合法的 `evidence_policy`／Calendar `outcome_contract`、精確空 placeholder，以及 Relationship 將 `relationship.date_invitation.v1` 放錯到 `outcome_contract` 的單一 relocation case。Unknown agent、非空無效值、graph／DAG invariant 與衝突 intent 仍 retry once 後 fail closed；repair code 只進 localhost ephemeral debug。Compact-v3 system prompt ≤6,000 字元、provider schema ≤3,500 字元、合計 ≤9,500 字元。

### 4.4 DAG execution

Scheduler 依 data dependency 與 `run_if` control edge 做 topological layers；同層最多平行 2 個 task。每個 domain task：

1. `context_slicer.py` 產生最小 `AgentContextSlice`。
2. 透過 `RuntimeRegistration` 呼叫統一 runner。
3. Runner 回 `TaskRunnerResult.proposals` 或 `completed_results`，兩者恰好一種。
4. Proposal 逐筆走 Central Guard 與 typed tool；completed result 由 owning specialist runtime 完成 bounded interpretation/assembly，Scheduler 不重做 domain 判斷。
5. 依賴沒有成功 observation 的下游 task 標記 `SKIPPED/dependency_failed`；`run_if` 不符合或來源無法產生 outcome 時標記 `condition_not_met`／`condition_unavailable`，不啟動 runner。

正式 runner interface：

```python
run(context_slice, *, task, services)
    -> tuple[TaskRunnerResult, SubAgentMetrics | None]
```

Calendar、Web、ProductInfo 回 completed results；Places、Match、Profile 使用 proposal runner。Relationship 透過自己的 runtime 依已驗證 `write_intent` 切換一般 READ 或 date-card WRITE proposal surface，兩者仍交由 Scheduler 中央 Guard。

### 4.5 Final composition

一般 DAG 的 user-facing wording 只由 Synthesizer 產生。例外只限 server-owned deterministic outputs：confirmation、confirmed result、typed clarification、assessment lifecycle、安全/錯誤 fallback。

Synthesizer 只能使用本回合 verified observations。Map URL、source URL、place candidate ref、Web source ref 由 server-owned catalog 綁定；模型不能新增未觀察到的 reference。

格式提示依資訊量自適應：多候選、比較、步驟或清楚分組可用輕量 Markdown；簡單答案維持自然 prose，不要求 Places/Web/itinerary 固定標題。`presentation_mode="itinerary"` 只是 editorial hint，仍使用 ordinary compose contract。Server-owned mutation verification／pending preview 只有在 exclusive transaction 時 bypass Synthesizer；混合回合先組合其他 observations，再附加鎖定回覆，避免安全回覆丟失同回合結果。

LLM routing 使用兩級模型：Planner 與 Places／Match／Relationship／Profile proposal runner 要求 `OLLAMA_FAST_CHAT_MODEL`（未設定時回退 main）；Calendar、Web、Synthesizer 使用 main；ProductInfo bounded path 不呼叫 LLM。Planner 對 function-call protocol failure 最多 retry 一次；provider error 不重試，兩次仍失敗時 fail closed。`llm_call_count` 由每個 owner 真實累計，Scheduler 以 metrics 加總，不以 agent 節點數猜測；retry/failure attempt details 只存在 localhost ephemeral debug。

`compose_public_reply.presentation_class` 的正式 enum 不變。Synthesizer boundary 只對 provider drift 做窄幅相容：舊的 `itinerary` 正規化為 `grounded_recommendation`，其他未知值改用既有安全預設 `conversation`，避免丟棄其餘已通過驗證的自然文字。這項相容不放寬 candidate refs、Web URL/source refs、internal IDs 或 mutation authority 的驗證。

## 5. Context 與 privacy

`services/ayue_agent/context.py` 是 Public Context 唯一 owner：

- recent messages ≤12；總字元 ≤6,000；relevant memories ≤8。
- 已通過驗證的 `ConversationSummaryV1` 只作 owner-scoped 對話延續；watermark 之前的訊息不再重複送入 prompt。摘要不可取代 Profile、Match、Calendar 或 Memory 的 canonical state。
- 一般配對與活動邀請分別投影為 `active_proposal` 與 `active_event_invitation`；兩者可同時存在，且 specialist slice 不含 match ID、event ID 或其他 authority-bearing identity。
- 不輸出 raw Mongo/Neo4j document、`seed_user_*`、未公開 ID、對方私人記憶或對方行事曆。
- 只有已接受關係且 server 驗證的 public display projection 能進 context。
- Calendar、Web、ProductInfo 等 agent 只收到 `context_slicer.py` 明列欄位。
- ProductInfo 不收到 owner profile/calendar/relationship private state。

Public 與 Private 不交換完整 prompt/history。Private 只讀其 current accepted room 允許的 bounded relationship context；Private owner messages不自動進 Public profile/memory pipeline。

Private V2 透過自己的 `PrivateAgentTurnContextV2` adapter 呼叫 `get_relationship_semantic_context(match_doc, pair_room)`，不使用 Public Context Builder。Planner 只取得 relationship role、macro summary、theme、action plan、dynamic bounds 與最多 20 筆 room-scoped triples；final composer 只取得 role、macro summary 與同一份 bounded triples。Raw semantic-plan document、未列入 projection 的 strategy 欄位與 Neo4j raw payload 不得進 prompt。Neo4j 讀取不可用時只可使用該 room 已保存的 bounded triples 或空集合，不得讀取其他 room 或 owner durable memory。

## 6. Tool Registry、Guard 與 budgets

所有公開 capability 必須先註冊 `ToolSpec`：

```text
name / risk / executor_key / description / progress_text
planner_arguments_model / executor_arguments_model / output_model
argument_source / confirmation / reuse policy
```

模型永遠不能提供 `user_id`、match/proposal/event ID、revision 或 expected status。

Guard 檢查：

- registered tool；
- planner schema；
- shared Public-run duplicate key；
- per-task read budget（預設 3），以及每回合最多 9 次實際唯讀執行；
- WRITE 一律回 `write_requires_confirmation`。

同一 `tool + normalized executor arguments` 在同一 Public run 不重跑。每回合最多建立一筆 pending side effect。

目前共註冊 24 個 capabilities，其中 6 個 WRITE。完整表見 [`docs/architecture/04-tool-registry.md`](./docs/architecture/04-tool-registry.md)。

## 7. Confirmation 與 writes

```text
WRITE proposal / Calendar command batch
  -> schema + domain preflight
  -> prepared confirmation（TTL 900s，preview + room bound）
  -> AI 訊息保存後 pending
  -> owner 以泡泡 choice_id 或既有卡片介面確認
  -> ConfirmationManager CAS pending -> executing
  -> write_executors
  -> canonical domain service（CAS + idempotency）
```

`calendar.submit_commands`、`match.start_search`、`profile.start_assessment`、探索結果 commit 與卡片建立前的約會協調使用一般 AI 泡泡內按鈕；`match.decide_active_proposal`／`match.decide_active_event_invitation` 已有媒人卡片，維持原卡片與文字備援。一般文字會自動取消同房泡泡選擇並繼續進 Planner，不會被當成確認權限。

目前 WRITE capabilities：

- `relationship.start_date_coordination`
- `match.start_search`
- `match.decide_active_proposal`
- `match.decide_active_event_invitation`
- `profile.start_assessment`
- `calendar.submit_commands`

Calendar mutation 只使用 `calendar.submit_commands`。一至十筆 authority-free commands 可形成一筆 confirmation；Calendar Runtime resolve canonical target 一次，建立不進 LLM 的 `CalendarMutationPlan`。舊 create/update/cancel tool names 不再註冊，stale confirmation fail closed。

`relationship.start_date_coordination` 只在 validated `write_intent="relationship.date_invitation.v1"` 的精確 `relationship -> synthesizer` DAG 可見。Relationship Runtime 接受 mention、連續原句 name evidence 或同 owner 15 分鐘 recent-contact reference，僅在唯一 accepted contact 可解析時建立一筆確認；確認後透過 `date_coordination_service.create_invite` 建立空白卡片，日期、地點與活動由雙方之後填寫。Target ID、revision 與 resolution metadata 不進 prompt、trace 或 public events。

`match.decide_active_event_invitation` 只處理 `proposal_namespace="event_invitation"` 的 live proposal。Planner 只能提出 `interested|declined`；Runtime 從 `active_event_invitation` 綁定 canonical revision，確認後由 `write_executors.py` 呼叫 namespace-aware match domain service。一般 `relationship_match` 與活動邀請各有一個獨立 live slot；任一方接受既有聯絡人的活動邀請時沿用聊天室，不重新建立關係。

## 8. Domain specialists

### Calendar

擁有本人的行程 reads、typed mutation commands、15 分鐘 draft/recent-reference、deterministic preflight 與 post-mutation verification。不可讀對方行事曆。共同約會的 mutation 仍由 `date_coordination_service.py` 處理雙方同步與通知。

具體日期的個人外出建議可建立 `calendar.availability.v1` 唯讀 precheck；成功時只由 Calendar Runtime 產生 `calendar.no_scheduled_events` 或 `calendar.has_scheduled_events` allowlisted outcome。Scheduler 只評估 `run_if`，不解析 events，也不把控制 edge 的行程內容傳給其他 domain。availability task 僅允許一次 `calendar.list_my_events`，失敗不偽造 availability outcome。

### Places

只擁有 structured nearby search、hours、price、rating、walking、distance 與 provider-neutral place projection。Search radius 對 OSM 與 Google 都是 hard bound；保存位置只允許粗粒度城市／行政區。`search_nearby` 可選 `rating|hours|price|walking` enrichment，`resolve_place` 可選前三者；預設不要求昂貴欄位，缺少或 partial price 不推測。Walking 以一筆 bounded Routes matrix 呼叫處理最多八個 destination，單一 element 失敗不拖垮其他候選；`measure_distance` 預設 `DRIVE`，明確要求時可用 `WALK`。只有優惠、特殊菜單、活動、臨時歇業公告、社群貼文等 Places 欄位無法建立的目前公開主張才由獨立 Web task 查證。Public place-card rendering 由 `AYUE_PUBLIC_PLACE_CARDS_ENABLED` 控制，目前 demo 預設關閉；關閉時 candidate refs、provider IDs、map URLs 與 Web grounding 仍只在 server-side runtime 內保留。

### Web

Web Runtime 擁有 research/finish phases，research 最多三個 tool calls、extract 最多一次；finish-only phase 不消耗 Web tool budget。Web Agent 只依 `phase`／`available_actions` 行動，`round_index` 僅供診斷。

Search rows 有獨立 projection cap；達到 search cap 不會停止掃描後續 observations，因此 late extract 仍可進 finalization。URL 必須綁定本回合搜尋結果或 owner 原句提供的安全公開 URL。

`AYUE_V3_WEB_PLACE_BOOTSTRAP_FAST_PATH` 開啟且 task 為 `casual_discovery`、已有 Places candidates 時，runtime 可先經 Guard 對最多兩個 server-anchored candidates 搜尋，再進既有 bounded loop；strict verification、無候選與 Web disabled 不受影響。

### ProductInfo

ProductInfo 是 first-class read-only DAG specialist。它最多兩輪、最多六個 allowlisted knowledge sections，輸出 `product_info.v1` observation；section hints 只屬其內部 retrieval，不是 Planner taxonomy 或全域 keyword router。Progress 使用 `product_info.process` 虛擬步驟；它不是 Tool Registry capability，也不消耗 Web/tool budget。

### Match / Relationship / Profile

- Match 讀 canonical 的 singleton proposal/status／單一對象摘要，搜尋與決策走 confirmation/CAS；不擁有 accepted contacts aggregate 清單或總數。
- Relationship 只讀 accepted relations 與 server 驗證 mentions 的 public projection；擁有已接受／已建立聯絡人的清單、總數、現有聯絡人比較與 bounded 範圍內的推薦。
- Relationship 的唯一寫入面是建立空白約會邀請卡。它需要 Planner 的 typed write intent、最多兩次 function-call protocol attempt、唯一 accepted target 與一次 confirmation；一般 Relationship task 仍只看三個 READ functions。
- Planner 依資料形狀分流這兩個 owner：「目前配到哪些人／總共幾位／現在有配到誰」仍是 Relationship aggregate；只有這一輪唯一 proposal 的搜尋、進度、狀態或決策才是 Match。Scheduler 不以中文 keyword／regex 重寫 route。
- Profile 讀本人資料與 assessment start；owner memory extraction 由獨立 `profile_skills.py` pipeline 擁有，不由聊天 agent 寫入。

## 9. Match state

Canonical lifecycle：

```text
draft -> pending -> accepted
             \-> declined
draft/pending -> expired
```

- 只有 live `draft/pending` 阻擋新的 active proposal。
- `accepted` 是已建立聯絡關係，不是 active proposal。
- `match_decision_service.py` 擁有 status+revision CAS；stale 回最新狀態，不覆寫終態。
- `match_action_service.py` 只在 transition 成功後執行通知、聊天室、feedback 等 effects；effect 失敗不讓已提交 transition 被重送。
- `POST /api/match/decision` 與 `GET /api/match/state` 的 HTTP adapter 只在 canonical match 已 `accepted`、具有 `has_verified_acceptance` 證據且 caller 是 participant 時，增加導航用 `other_id`。此欄位不進 tool observation、Public prompt、stream 或 Event snapshot；導航讀取失敗也不改變已提交的接受結果。
- Flutter 活動卡保留 canonical `proposal_namespace`、公開 `event` 與 `chat_reused`。日期以台灣時間呈現並尊重 date/datetime 精度；雙方同意後直接開啟對應聊天室，既有 pair 沿用原聊天室。後端以 match-scoped event key 在 canonical pair room 冪等保存安全的 Event 開場 system card，讓新／既有 pair 都能從同一份活動 snapshot 接著聊，且不建立第二個 relationship anchor。舊的卡片終態與 revision 保護不變。
- 提案 HTTP state 與 AI 聊天歷史提供 viewer-bound、UI-only `counterparty_nickname`，讓一般／活動配對理由介紹對方暱稱。由 `public_nickname_service.py` 唯讀 Appwrite 公開 `name`，缺資料／不可用時回退 Mongo seed/legacy 公開稱呼，不同步或寫入 profile。歷史訊息只投影、不重存；名字不進新 model context、Graph 或婉拒原因，導航仍需雙方同意。
- 婉拒原因由 viewer-bound `decline_reason_options` 提供；使用者可只婉拒、不記錄。只有本人勾選並同意記錄的 `explicit_reasons` 才進既有 feedback normalizer，再共用 `/api/memory/apply` 寫入 `AVOIDS -> Concept` 與 Social preference facts。空選擇、撤回、stale 不寫偏好，不從對方特質推論；UI 的「已送出」不等同 Graph 成功收據。

## 10. Profile、Memory 與 Context Engine

Owner message pipeline：

```text
saved owner message
  -> profile_skills.py typed extraction + contiguous evidence span
  -> memory_service.apply_profile_memory_proposals
  -> matchmaker /api/memory/apply
  -> Neo4j owner-scoped durable memory
  -> Mongo profile_memory_preview（read projection only）
```

- assistant reply、conversation history、tool result、match state 或對方資料不能成為 profile write source。
- 已移除 `/api/memory/observe` 與主服務 direct Neo4j fallback；9001 failure 進 bounded outbox，不能改抓 raw data。
- Memory outbox 由 lease worker bounded retry；9001 將 observation marker 與 edges 原子提交。Disable／restore／correct 保留 owner relation 語意，設定頁以 status-aware Graph snapshot 刷新 bounded Mongo projection。
- `semantic_plans`／room chat triples 是雙人關係 context，不自動轉成任一方 durable preference。
- Relationship semantic updater 以尚未處理的 pair-room 訊息內容長度作 bounded budget proxy；累積未達 600 字元單位時不更新，不能只因短訊息數量多就觸發。更新後的 shared projection 才能由 Private V2 adapter 消費。
- 未來 Context Engine 只能輸出 bounded versioned typed bundle，先做 owner/room/accepted-relation hard isolation，再做 ranking/budget/dedup。
- Conversation compaction 對 legacy 與新版 Public AI room 使用同一 server-owned owner validator；generation／evaluation 消費 `ChatResult.content`，通過評估的 room-scoped continuity 才能進 Public context。

完整規則見 [`MEMORY_CONTEXT_ENGINE_GUIDE.md`](./MEMORY_CONTEXT_ENGINE_GUIDE.md)。

## 11. HTTP、UI、progress 與 debug

Public NDJSON 對外 event allowlist：

```text
run_started | tool_started | tool_finished | final | error
```

- Public UI 只顯示一個暫時 progress bubble；final/error/disconnect 時清除。
- Progress、debug event、tool name 不存成聊天訊息。
- `reply` 保留相容性；`messages` 最多三個；`place_cards`、`sources`、`presentation_blocks` 是 additive typed projection。`AYUE_PUBLIC_PLACE_CARDS_ENABLED` 關閉時，Places/Web 仍回傳文字或 Markdown，但 public result 不產生 place cards 或 card presentation blocks。
- Debug 只對 loopback/localhost 開放，使用 `agent_run_id` 關聯 ephemeral run。Execution debug 顯示各 owner 的 model/tier/call count、整回合 total、Planner retry/failure code 與 bounded attempts；Public trace 只存 allowlisted metadata，不存 prompt、owner 原句、arguments/result、raw exception、ID 或 revision。

## 12. Testing baseline

所有行為修改必須有 deterministic test；自動測試不得連線或修改正式 MongoDB Atlas、Neo4j、Tavily 或 Google API。

最低覆蓋：

- Planner function schema、DAG、routing 與 fail closed。
- `RuntimeRegistration`／`TaskRunnerResult` contract。
- Context slices 與 Public/Private privacy isolation。
- Guard codes、tool schema/output、duplicate、budgets。
- Confirmation、CAS、idempotency、stale、concurrency。
- Web research/finish、late extract projection、URL/source/subject binding。
- Calendar command/preflight/draft/reference/verification。
- ProductInfo retrieval/observation/progress/debug。
- JSON/NDJSON compatibility、trace/event allowlists。

標準指令見 [`docs/architecture/06-testing.md`](./docs/architecture/06-testing.md)。

## 13. Current documentation index

- [`docs/architecture/01-project-overview.md`](./docs/architecture/01-project-overview.md)：onboarding 與服務邊界。
- [`docs/architecture/02-python-modules.md`](./docs/architecture/02-python-modules.md)：模組 owner map。
- [`docs/architecture/03-v3-runtime-lifecycle.md`](./docs/architecture/03-v3-runtime-lifecycle.md)：單回合 lifecycle。
- [`docs/architecture/04-tool-registry.md`](./docs/architecture/04-tool-registry.md)：23 個 ToolSpec。
- [`docs/architecture/05-matchmaker-and-memory.md`](./docs/architecture/05-matchmaker-and-memory.md)：9001、Neo4j 與 profile memory pipeline。
- [`docs/architecture/06-testing.md`](./docs/architecture/06-testing.md)：測試策略。
- [`docs/architecture/07-guard.md`](./docs/architecture/07-guard.md)：Central Guard。
- [`docs/architecture/08-planner.md`](./docs/architecture/08-planner.md)：Planner DAG contract。
- [`docs/architecture/09-runtime-interfaces.md`](./docs/architecture/09-runtime-interfaces.md)：HTTP、Context、Planner、runner、Tool/Guard、write 與 observation interfaces。
- `docs/architecture/subagent-*.md`：各 domain specialist 的現行行為。

修改 runtime contract、tool list、state machine、stream/debug envelope 或 environment flag 時，必須同步更新本文件、interfaces 文件與對應 domain 文件。
