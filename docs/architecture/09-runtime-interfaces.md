# 09. Public V3 Runtime Interfaces

> 本篇定義目前 Public Ayue V3 各層之間可以交換什麼資料、誰擁有哪個決策，以及新增 runtime 時必須遵守的介面。程式碼仍是最終真相；主要來源是 `services/ayue_agent/contracts.py`、`v3/contracts.py`、`v3/runtime_registry.py`、`v3/guarded_execution.py` 與 `tool_registry.py`。

## 1. 版本名稱先分清楚

- **Public V3**：目前唯一公開阿月 runtime 架構。
- **Private V2**：目前仍在使用的阿月悄悄話 runtime，不是 Public 的 fallback，也不是待移植的舊 Public 架構。
- `public-v1`、`web_research.v1`、`product_info.v1`、`TurnClockV1`：typed payload 的 schema version；它們不是 Public V1 runtime。
- 相容欄位或 adapter 只存在於明確標示的 compatibility boundary。新程式不得以它們作為主要設計。

## 2. HTTP interface

Public 入口由 `routers/public_chat.py` 擁有：

| 入口 | 用途 | 穩定性 |
| --- | --- | --- |
| `POST /api/direct_chat` | JSON compatibility endpoint | 必須保留；只可增加 optional fields |
| `POST /api/direct_chat/stream` | Public UI 的 NDJSON endpoint | Public Ayue 優先入口 |

Request 使用 `DirectChatRequest`。登入者與 canonical state 必須由 server 驗證；模型不能提供 `user_id`、match/proposal/event ID 或 revision。

Public stream 對外只允許：

```text
run_started | tool_started | tool_finished | final | error
```

這是未協商能力時的預設 contract。只有明確送出 `X-Ayue-Stream-Tokens: v1` 的 client 會另外收到 bounded `token` event；未送 header 的既有 client 不受影響。

`subagent_started`、prompt、tool arguments、tool result、raw exception 與 debug payload 都不是 public HTTP interface。它們不得被前端保存成聊天訊息。

Final response 使用 `AgentResult`：

- `reply` 是相容文字投影。
- `messages` 最多三個顯示氣泡。
- `sources`、`place_cards`、`presentation_blocks` 都是 bounded typed projection。
- `agent_run_id` 是不含內容的 opaque correlation ID，只能用來對應 localhost debug run。

## 3. Context interface

HTTP 層先組 `AgentTurnContext`；`services/ayue_agent/context.py` 再建立唯一 prompt-safe `PublicAgentTurnContext`。

```text
AgentTurnContext（server/private control input）
  -> build_public_agent_turn_context
PublicAgentTurnContext（bounded prompt-safe state）
  -> slice_for_agent
AgentContextSlice（每個 specialist 的最小視圖）
```

固定 budget：最近 12 則訊息、合計 6,000 字元、記憶最多 8 筆。Context Builder 不得輸出 raw Mongo/Neo4j document、內部 ID、對方私人記憶或對方行事曆。

Public conversation compaction 使用 `ConversationSummaryV1` 的 bounded owner-scoped projection。Context Builder 只載入通過 evaluation、source hash 與 room/owner 驗證的最新摘要，並以 watermark 排除已涵蓋的舊訊息。這份 continuity 只協助延續話題；Match、Profile、Calendar、accepted relationship 與 durable memory 一律重新讀 canonical domain state。

Match slice 可同時包含兩份互不覆蓋的最小狀態：`active_proposal` 對應一般 `relationship_match`，`active_event_invitation` 對應活動牽線。兩份 projection 只提供 stage、公開對象稱呼、是否可決定、活動標題等必要資訊；ID 與 revision 僅留在 server-side turn context / confirmation payload，不由 Planner 提供。

Planner 在取得 `PublicAgentTurnContext` 後另做 bounded prompt projection：最多最近 4 則、合計 2,000 字元，若最新 history item 就是本回合 `message` 則只保留一份；clock 只送 timezone、local date/time、weekday 與實際存在的 temporal references。Optional state 為空時省略；active proposal 只送公開 status、counterparty、user_can_decide，不送 proposal revision。此 projection 不改全域 Context Builder budget，也不改其他 sub-agent slice。

每個 sub-agent 只能收到 `context_slicer.py` 對該 agent 明確列出的欄位。新增 agent 時，必須新增獨立 slice 與 privacy test；不得把整個 `PublicAgentTurnContext` 直接交給 agent。

### 3.1 Private V2 relationship context adapter

Private V2 不使用 `PublicAgentTurnContext` 或 Public sub-agent slices。`build_private_turn_context_v2()` 在 router 已驗證 current accepted relation 後，以 canonical pair room 呼叫 `get_relationship_semantic_context()`，把結果保存於 `PrivateAgentTurnContextV2.relationship_semantic_context`。

這個內部欄位不是 raw document 的 prompt contract。Private planner 只投影 `current_role`、`context.macro_summary`、`strategy.theme`、`strategy.action_plan`、`strategy.dynamic_content_bounds` 與最多 20 筆只含 `subject`／`predicate`／`object` 的 triples；final composer 不取得 strategy，只取得 role、macro summary 與同一 bounded triples。Neo4j read 不可用時只能退回同 room 已保存的 bounded triples 或空集合。這條 adapter 不得讀取 Public history、owner durable memory、其他 room 或 authority-bearing match identifiers。

## 4. Planner interface

Planner 只呼叫一個 function：`decompose_tasks`。目前可產生兩種正常執行形狀：

```text
mode=direct_chat
  write_intent=none
  tasks=[]
  direct_reply 或 direct_messages（二擇一）

mode=tasks
  write_intent=none | relationship.date_invitation.v1
  1..5 個 SubTask
  最多 4 個 domain task
  恰好 1 個 terminal synthesizer
```

`SubTask` contract：

```json
{
  "id": "t1",
  "agent": "calendar|places|web|match|relationship|profile|product_info|synthesizer",
  "depends_on": [],
  "task_brief": "完整目標與限制"
}
```

Provider-facing schema 將 `write_intent` 設為 required；一般請求必須明確送 `none`。Canonical `Plan` 的 `none` default 只供 server-side 與舊 fixture 建構相容，不替 provider 補值。

未使用的 optional task 欄位應省略；不可用空字串代替 enum，也不可送出不完整的 `{}` `run_if`。Provider boundary 只會把 known agent 的精確空 placeholder 視為省略；空白字串、非空無效值、不完整 condition 與 graph／DAG drift 仍由 canonical validator 拒絕，最多 retry once 後 fail closed。

只有 Web task 可帶 `evidence_policy=casual_discovery|strict_verification`。Calendar availability task 可帶 `outcome_contract="calendar.availability.v1"`，但它只有在 graph 中至少有一個下游 `run_if` consumer 時才有控制意義；若 provider 把它放在沒有 consumer 的普通 Calendar 查詢或 mutation task，contract normalization 會移除這個無效 metadata，而不是拒絕整張 plan。`run_if` 是控制 edge，必要欄位為 `source_task_id` 與 `required_outcome`（`task.finished` 或 allowlisted Calendar outcome）。這是 DAG 結構 normalization，不是 Calendar intent parser：Planner 不選 command、tool arguments、event ID 或 confirmation，也不選 ProductInfo section、不執行工具、不寫 final domain answer。

Planner 只有在下游會消費上游的 typed observation、candidate ref 或其他明確 contract 時才建立 `depends_on`；獨立的 domain request 放在同一層平行執行。這個規則是 DAG 的資料依賴，不是為了排列顯示順序。只需等待、不需傳遞上游資料時使用 `run_if`；Scheduler 只在來源完成後評估它。

具體日期的個人外出建議必須先建立 read-only Calendar availability task，即使使用者說不要新增行程。一般 precheck 使用 `task.finished`，明確的「有事就算了／沒事才繼續」才使用 `calendar.no_scheduled_events`。Calendar Runtime 對成功的單次 `calendar.list_my_events` 產生 `calendar.no_scheduled_events` 或 `calendar.has_scheduled_events`；失敗不偽造 outcome，也不把 `run_if` 控制 metadata 投影給下游 domain prompt。

Provider 輸出的 `mode="tasks"` 必須包含至少一個 domain task，除非同時帶有合法的 `social_opening` opportunity；普通聊天使用 `direct_chat`。具體日期＋既有聯絡人推薦＋新活動＋附近晚餐使用 `calendar`、平行 gated 的 `relationship`／`web`、依賴 Web activity venue 的 `places` 與 terminal `synthesizer`。Planner 遇到 contract mismatch 時最多重試一次，只提示 required `write_intent` 或 `evidence_policy`／`outcome_contract`／`run_if` 的封閉規則，不把錯誤值回送模型；無效 DAG 不轉成 provider-authored synth-only route。

`Plan.mode="product_info"` 與 `product_info_topics` 只是在 `v3/contracts.py` 保留的 provider compatibility input；`normalize_plan_for_execution()` 會立刻轉成正常的 `product_info -> synthesizer` DAG。新 prompt、fixture 與文件不得產生這個舊 envelope。

### 4.1 Provider compatibility boundary

Planner 在 canonical validation 前先複製 provider arguments，且只處理三類 allowlisted drift：known agent 上錯置但值合法的 `evidence_policy`／Calendar availability `outcome_contract`、精確空 optional placeholder，以及 known Relationship task 將精確 `relationship.date_invitation.v1` 放到 `outcome_contract` 的單一 relocation case。其他 agent/value、衝突 root intent、unknown agent、`depends_on`／`run_if` drift 與 DAG invariant 不修復。成功修復不消耗 retry，只把 bounded repair code 投影到 localhost ephemeral debug；normalized payload、owner text 與 raw exception 不進 durable trace 或 public events。

## 5. Runtime registration interface

Scheduler 只透過 `RuntimeRegistration` dispatch domain task。正式 runner 的統一介面是：

```python
run(
    context_slice: AgentContextSlice,
    *,
    task: SubTask,
    services: GuardedReadExecutor,
) -> tuple[TaskRunnerResult, SubAgentMetrics | None]
```

`TaskRunnerResult` 必須恰好使用一種結果形狀：

| 形狀 | 使用者 | 後續責任 |
| --- | --- | --- |
| `from_proposals(list[ToolProposal])` | 一般 function-calling sub-agent | Scheduler 逐筆 Guard、注入 executor args、執行 tool |
| `from_completed(list[SubTaskResult])` | 擁有 bounded loop/typed assembly 的 specialist runtime | Scheduler 只收 completed projection，不重新解讀 domain state |

目前 registration：

| Agent | Runtime 類型 | Owner |
| --- | --- | --- |
| `calendar` | completed specialist runtime | `calendar_runtime.py` |
| `places` | proposal runner | `sub_agents/places_agent.py` |
| `web` | completed specialist runtime | `web_runtime.py` |
| `match` | proposal runner | `sub_agents/match_agent.py` |
| `relationship` | intent-aware proposal runtime | `relationship_runtime.py` → `sub_agents/relationship_agent.py` |
| `profile` | proposal runner | `sub_agents/profile_agent.py` |
| `product_info` | completed read-only specialist | `sub_agents/product_info_agent.py` |

`before_run`／`after_run` hook 只能做 bounded progress/debug projection，不能執行 domain write 或改寫 runner result。`direct_chat_blocker` 與 `confirmed_result_projector` 也必須留在擁有該語意的 runtime registration 旁。

Relationship Runtime owns already-accepted／established contacts: their bounded
list, exact count when `total_count` is available, comparison, and recommendation
among existing contacts. A `truncated=true` list cannot support claiming that a
recommendation is best among all accepted contacts. In ordinary mode it returns
READ proposals; only a validated date-card `write_intent` switches it to the
single confirmed WRITE proposal surface. Match owns the singleton pending/live
proposal status, counterparty summary, and start/retry/decision flow; an accepted
contact is not an active proposal. A current Match observation never supplies
aggregate accepted-contact count or roster authority.

The Planner-facing `SubTask.agent` schema describes this as an aggregate-versus-
singleton boundary. Colloquial questions such as 「我目前配對到哪些人」、
「我現在有配到誰」 or 「總共幾位」 require Relationship even though
they contain 「目前」 or 「配對」. Match is reserved for the one active
proposal/search lifecycle. This is semantic Planner ownership; Scheduler does
not add a keyword or regex rerouter.

## 6. Tool and Guard interface

每個公開能力都必須先有 `ToolSpec`：

```text
name + risk + executor_key + description + progress_text
+ planner_arguments_model
+ executor_arguments_model
+ output_model
+ argument_source
+ confirmation policy
```

三個 schema 的責任不可混合：

- `planner_arguments_model`：模型可提出的 authority-free 欄位。
- `executor_arguments_model`：server 注入 ownership／mention binding 後的欄位。
- `output_model`：可送入 observation 的最小安全 projection。

`GuardedReadExecutor` 是 specialist runtime 的唯一 Guard→argument injection→tool execution adapter。Runtime 可以擁有 action/round/budget policy，但不能繞過 `guard_proposal()` 或直接呼叫第三方 provider。

Guard 只驗證 schema、註冊、重複呼叫、步數與 write-confirmation；不判斷自然語言 intent。相同 `tool + normalized executor arguments` 在同一 Public run 的 shared `seen_keys` 內不得重跑；唯讀 budget 依 task id 計算，並受每回合 9 次實際唯讀執行的 shared ceiling 約束。

## 7. Write interface

所有副作用先建立 `v3_pending_confirmations`，由使用者使用封閉協議確認後，才進 `write_executors.py` 與 canonical domain service。

```text
ToolProposal(WRITE)
  -> Guard: write_requires_confirmation
  -> domain preflight / authority resolution
  -> pending confirmation（preview-bound）
  -> 使用者確認
  -> write_executors
  -> canonical domain service（CAS + idempotency）
```

Calendar 的唯一公開 mutation capability 是 `calendar.submit_commands`。Calendar Runtime 擁有 clarification、draft/reference、command validation、preflight 與 confirmation preparation；舊的 create/update/cancel tool names 不是可用 interface。

`match.decide_active_event_invitation` 是 Match-owned confirmed write。Planner 只能提出 `decision=interested|declined`；pending confirmation 由 server 綁定當下 `event_invitation` revision，確認後才呼叫 namespace-aware match domain service。一般配對與活動邀請使用獨立 live namespace，因此可同時存在；同一 pair 的明確婉拒仍共用 pair cooldown，避免短期重複推薦。

### 7.1 Relationship date-card write

`relationship.start_date_coordination` 是 Relationship-owned confirmed write。它只在 provider-required `write_intent="relationship.date_invitation.v1"` 通過 exact `relationship -> synthesizer` DAG 驗證後可見；Match 不是合法 precheck。Scheduler 只透過 task-bound `GuardedReadExecutor.runtime_state["planner_write_intent"]` 傳遞這個 server-only ephemeral intent，不把它放進 prompt、trace 或 public event。

Date-card 模式只暴露這一個 function，最多兩次 provider attempt，只接受一個 grounded target proposal。Target 來源限 validated mention、current-message 的連續 name evidence span，或同 owner 15 分鐘 recent-contact reference；server 只在 accepted contacts 中做 bounded unique resolution。模糊、未知、過期或非 accepted 都 fail closed。Pending preview 不含模型提供的 ID／revision；確認後由 canonical `date_coordination_service.create_invite` 建立空白卡片，雙方之後自行填寫細節。

## 8. Observation and synthesis interface

`SubTaskResult` 只能是：

- `OK`：帶 bounded `observation`。
- `FAILED`：帶 allowlisted `error_code`／`guard_code`。
- `SKIPPED`：帶 `skip_reason`。

`SubTaskResult.outcome_codes` 是 server-owned 的 bounded control metadata，目前只允許 `calendar.no_scheduled_events` 與 `calendar.has_scheduled_events`。Calendar Runtime 才能產生它；Scheduler 僅做 exact-match `run_if` 判定，不解析 `observation.events`。`task.finished` 是 Scheduler 對來源 `OK`／`FAILED` 的技術完成判定，不是 domain outcome。條件不符合時下游在 runner 前標記 `SKIPPED/condition_not_met` 或 `SKIPPED/condition_unavailable`，不發出 `tool_started`。

Observation 不是 user-facing prose。Synthesizer 是一般 DAG 的唯一 final wording owner，且只能使用它實際收到的 verified observations。Web URL、place card 與 subject reference 由 server-owned catalog 綁定；模型不能創造新的 reference。

### Provider model tier and call telemetry

`OLLAMA_FAST_CHAT_MODEL` 是可選的 fast tier；未設定時回退到 `OLLAMA_CHAT_MODEL`。Planner 與 Places／Match／Relationship／Profile proposal runners 要求 fast tier；Calendar、Web、Synthesizer 使用 main tier。ProductInfo retrieval 本身是 bounded typed path；Synthesizer 可用該 projection 搭配使用者的實際問題自然組合回答，固定產品文案只作 provider failure fallback。Runtime model override 優先於這些 tier，且不自動在 fast 失敗後重試 main。

每個 LLM owner 的 metrics 都回報 `llm_call_count` 與 `requested_model_tier`。Planner 對 `missing_tool_call`、`wrong_function_name`、`invalid_arguments` 最多做一次 bounded protocol retry；`provider_error` 不重試，兩次 protocol failure 後仍 fail closed。Planner 的 `retry_count`、`retry_reason`、`failure_code` 與 bounded `attempts` 只投影到 localhost ephemeral debug；durable trace 不保存 prompt 或 raw output。Web 的 bounded retry／finish attempts 會累計真實 provider call 數，Scheduler 的 `trace.llm_call_count` 是 Planner、所有 sub-agent 與 Synthesizer counters 的總和；它不再以 agent 節點數估算呼叫次數。

Domain failure 可以在既有 `SubTaskResult` 形狀內帶 bounded `observation` projection，例如 `{"failure": {"code": "location_not_found", "subject": "...", "message": "..."}}`。這只適用於 domain 明確 allowlist 的 user-facing failure；`subject` 必須來自已驗證的 planner/executor arguments，`message` 必須是 server-owned 固定文字。不得放入 raw exception、stack trace、provider detail 或 internal ID。Scheduler 只傳遞這個 projection，不負責解讀 Places 或其他 domain 的 failure semantics。

Grounded Places/Places+Web success observations normally go through the
Synthesizer `compose_public_reply` contract. `candidate_cards` is a bounded
server-owned evidence pool; `selected_candidate_refs` is the public
presentation set. Deterministic domain formatters are post-composition
degradation paths and must not run before normal composition or discard
successful sibling-domain observations.

For ordinary Places/Places+Web recommendations, the active compose contract
contains only `messages: list[str]`, `presentation_class`, `card_intent`,
`selected_candidate_refs`, `recommended_candidate_refs`, and
`discussed_candidate_refs`. Model-authored `blocks` and `card_mode` are not
part of this ordinary schema. The server derives `card_mode` from
`card_intent` and validated refs, then emits the card-only presentation
projection from its own candidate catalog. A legacy top-level `blocks` field
may be discarded for compatibility, but its nested shape is never used as an
ordinary card binding. Itinerary uses the same ordinary compose contract as
other presentation modes; `presentation_mode="itinerary"` is only an editorial
hint and does not require a block-based rendering schema. Selected refs are
the only model-to-server binding for optional cards; map URLs and other card
fields remain server-owned. `messages` are public reply strings, not chat
transcript objects. A narrow compatibility adapter may retain only bounded
`role="assistant"` string content from an older provider shape;
user/system/tool/unknown-role content is discarded.
Schema drift is reported as `compose_schema_invalid`.
The supported `presentation_class` enum is unchanged. At the Synthesizer
boundary only, legacy `itinerary` is normalized to `grounded_recommendation`;
any other unsupported value uses the existing safe `conversation` default so
otherwise valid public messages are not discarded. This metadata compatibility
does not relax candidate-ref relations, Web URL/source-ref binding, internal-ID
filtering, or mutation-authority validation.
`presentation_mode="itinerary"` is a Synthesizer prompt hint, not a rigid
rendering schema. It uses the ordinary compose contract so the model may use
natural prose/Markdown without a mandatory stop count or time window. Dates,
times, and schedule details are included only when supported by the request or
typed observations; the server does not invent default itinerary times.
Qualitative atmosphere, quality, and date-suitability claims remain grounded
by the Synthesizer evidence contract: affirmative claims need matching typed
evidence, while an explicit limitation is valid when confirmation is not
available. This is not a general natural-language claim parser.

Synthesizer formatting is adaptive: multiple candidates, comparisons, steps,
or clearly separated information groups may use lightweight Markdown such as
bullets, numbering, short bold labels, or an occasional descriptive heading.
Simple answers remain natural prose; no Places, Web, or itinerary heading set
is mandatory.

Web-only `web_research.v1` observations, including answered, partial,
insufficient-evidence, degraded, and unavailable outcomes, reach the
Synthesizer first for natural-language composition. The typed result fixes the
facts, status, limitations, URLs, and source refs; it does not prescribe
headings, bullet counts, or sections such as 已確認／尚未確認. `_web_research_fallback`
is reserved for provider, compose, grounding, or presentation validation
degradation and returns only a minimal bounded claim/limitation reply.
All Places/Web deterministic fallbacks likewise return short recovery text,
without fixed headings, candidate dumps, or automatic place-card presentation.

Public place-card rendering is controlled by
`AYUE_PUBLIC_PLACE_CARDS_ENABLED`, which is off for the current demo. When it
is off, Scheduler returns zero `place_cards` and `presentation_blocks` for
Places/Web replies, while the bounded candidate projection, candidate refs,
provider IDs, map URLs, and Web grounding bindings remain available inside the
server-side runtime.

When a turn contains both a server-owned mutation/confirmation reply and other
observations, Synthesizer bypasses the provider only for an exclusive
transaction. In a mixed turn it removes the locked reply from the model prompt,
composes the remaining observations, and appends the locked reply afterward.
Unknown list items remain in the cloned observation; arbitrary observation
`message` fields never become server-owned replies.

`AYUE_V3_WEB_PLACE_BOOTSTRAP_FAST_PATH` is an opt-in latency optimization for
`casual_discovery` Places -> Web turns. If enabled, Web performs at most two
server-anchored candidate searches through `GuardedReadExecutor` before its
normal bounded research loop. Strict verification, no-candidate, Web-disabled,
and ordinary Places-only flows are unchanged.

ProductInfo 固定輸出 `product_info.v1` observation；Web 固定輸出 `web_research.v1` observation。這些 `.v1` 是 payload schema，不代表舊 runtime。

## 9. Places typed enrichment interface

Places tool arguments remain authority-free and optional: `enrichments=[]`
defaults to no expensive Google fields; `search_nearby` supports `rating`,
`hours`, `price`, and `walking`, while `resolve_place` supports `rating`,
`hours`, and `price`.
The executor normalizes/deduplicates these enums before cache keys and provider
calls. The typed place projection may include rating/count, bounded current
opening-hours data, validated price level/range endpoints, and per-candidate
walking distance/duration. Missing or partial price data is omitted and never
inferred. These fields remain in the bounded observation passed to the AI; the existing card
projection/UI is unchanged. Google failure preserves the existing OSM fallback,
which does not fabricate price fields.

Nearby walking enrichment uses Routes `computeRouteMatrix` with one origin and
the bounded place-ID destinations. Matrix elements are independently mapped by
`destinationIndex`; an element error leaves only that candidate without
walking fields. The local cap is eight destinations, so the request is one
HTTP call and at most eight billed origin-destination elements. Single-distance
`travel_mode` defaults to `DRIVE` and may be `WALK` without changing the
existing default behavior.

Planner keeps structured hours, price, rating, and walking requests in
`places -> synthesizer`; only unstructured/current public claims that Places
cannot establish use a separate `places -> web -> synthesizer` DAG. Places
does not call Web itself.

## 10. 新增或修改 interface 的交付要求

1. 先確認 owner，避免把 domain policy搬回 Scheduler 或 router。
2. 修改 typed contract，再修改 runtime/adapter；不要以 prompt 自由文字代替 schema。
3. 補 Planner trajectory、runtime contract、Guard、projection、privacy、budget 與 fallback tests。
4. 同步更新本文件、`AYUE_V3_ARCHITECTURE.md`、相關 sub-agent 文件與 `README.md` 索引。
5. 若保留 compatibility adapter，必須寫明接受什麼舊輸入、在哪一層正規化、何時可移除；新程式不得繼續產生舊形狀。
