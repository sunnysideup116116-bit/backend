# 08. Planner：任務拆解器

> 程式碼真相：`social/services/ayue_agent/v3/planner.py` 與 `v3/contracts.py`。Planner 只做語意 routing 與靜態 DAG 拆解，不執行工具、不審核、不保存 domain state，也不直接回答需要產品或 domain 真相的問題。

## 1. 唯一 function interface：`decompose_tasks`

Planner 透過一次 function calling 輸出 typed arguments，不解析自由文字 JSON。新 Planner 只產生兩種正常形狀：

| mode | 用途 | 限制 |
| --- | --- | --- |
| `tasks` | 需要 domain truth、查證、workflow 或 Synthesizer | 1–5 個 tasks；最多 4 個 domain tasks；恰有 1 個 terminal synthesizer |
| `direct_chat` | 不需要 App/domain/private/external truth 的一般聊天 | `tasks=[]`；`direct_reply` 或 `direct_messages` 恰好一種；不能帶 opportunity |

`presentation_mode` 只能是 `default|itinerary`。一日遊、半日遊或整天安排使用 `itinerary`，且 DAG 必須包含 Places。

Provider 每次都必須輸出 `write_intent`。一般請求為 `none`；只有明確要阿月現在建立／送出空白約會邀請才用 `relationship.date_invitation.v1`。「想約某人，幫我找店」的邀約是背景，@ 只是 entity binding。Date write 需一個 root Relationship task 與 terminal Synthesizer，可保留 typed read-only siblings。Canonical `Plan` 保留 `none` default 只供 server-side／舊 fixture 相容。

Planner 的 system prompt 使用 compact-v3 版本：保留 routing ownership、direct-chat 邊界、Places／Web／Calendar／Match／Relationship／Profile／ProductInfo 的歧義規則、DAG invariants，以及具體日期＋既有聯絡人＋新活動＋附近晚餐的五節點範例；不注入完整 public persona、voice few-shots 或 Synthesizer reply contract。Planner 專用 context 只送最近 8 則／4,000 字元歷史、精簡 clock、server 產生的 temporal candidates 與非空 projection；這是 input budget 優化，不是第二個 router。Regression budget 為 system prompt ≤6,500 字元、provider schema ≤4,000 字元、兩者合計 ≤10,500 字元；這些不是 provider context-window 上限。

`Plan` model 仍接受 `mode="product_info"` 與 `product_info_topics`，但那只是舊 provider payload 的 compatibility input；`normalize_plan_for_execution()` 會立即轉成正常的 `product_info -> synthesizer` DAG。新 prompt、fixture 與文件不得輸出 task-free ProductInfo mode。

## 2. `SubTask` contract

```json
{
  "id": "t1",
  "agent": "web",
  "depends_on": [],
  "task_brief": "找鹽埕區近期可公開查證的新活動，保留日期、時間與場地",
  "web_mode": "public_lookup",
  "evidence_policy": "casual_discovery"
}
```

| 欄位 | 規則 |
| --- | --- |
| `id` | 1–64 字元，同一 DAG 唯一 |
| `agent` | `calendar|places|web|match|relationship|profile|product_info|synthesizer` |
| `depends_on` | 最多 4 個同一 DAG 的 task IDs；只代表資料依賴 |
| `task_brief` | 1–500 字元；描述目標、限制與 evidence class，不是 tool arguments |
| `place_mode` | Places 必填：`discover` 新推薦、`details` 單店結構化資料、`reviews` 單店口碑；非 Places 省略 |
| `web_mode` | Web 新輸出必填：`public_lookup|place_verification|place_hours_fallback`；舊 payload 省略時由 dependency shape 相容推導 |
| `evidence_policy` | 只有 Web 可用：`casual_discovery|strict_verification` |
| `outcome_contract` | 只有 Calendar availability task 可用：`calendar.availability.v1` |
| `run_if` | Server canonicalizer 產生的控制邊；provider-facing task schema 不暴露此欄位 |

未使用的 optional 欄位要省略；不可用空字串代替 enum，也不可送出不完整的 `{}` `run_if`。Provider boundary 只會把 known agent 的精確空 placeholder 視為省略；空白字串、非空無效值、不完整 condition 與 graph／DAG drift 仍由 canonical validator 拒絕，最多 retry once 後 fail closed。

Planner 永遠不能提供 `user_id`、match/proposal/event ID、revision、expected status 或其他 executor authority fields。

具體日期的個人外出／約會建議，即使使用者說不要新增行程，仍先建立唯讀 Calendar availability task。Planner 另以頂層 `availability_policy` 選擇 `fit_around_events` 或 `abort_if_any_event`；前者是預設並 canonicalize 為 `task.finished`，後者只允許明確「有任何行程就停止」的當前句連續 `evidence_span`，並 canonicalize 為 `calendar.no_scheduled_events`。Scheduler 不再從中文重新判斷。

相對日期由 Server 先生成 bounded candidates，Planner 再以頂層 `temporal_bindings` 選擇 `future_planning|stated_period|needs_clarification`。`future_planning` 不得綁到過去候選；source text、candidate ID 與 Calendar／Web task IDs 皆由 Server 驗證。選定後的 `ResolvedTemporalTarget` 是 provider 不可填寫的執行事實，Calendar read window 與 Web prompt 必須使用同一 ISO date；真模糊時產生 typed clarification observation，零 domain tool。Calendar mutation 仍只在使用者明確要求保存、建立、修改或取消時建立。

「具體日期＋從目前認識／accepted contacts 中挑一位＋找新活動＋附近晚餐」固定使用 `calendar`、平行 gated 的 `relationship`／`web`、消費 Web activity venue 的 `places`，以及 terminal `synthesizer`。這是 4 個 domain tasks 加 Synthesizer；不得改成 Match、direct chat 或 provider-authored synth-only plan。

## 3. DAG validator

`Plan` 的純程式 validator 會拒絕：

- 重複 ID、未知 dependency、自我依賴、重複 dependency 或 cycle。
- 沒有或有多個 synthesizer。
- synthesizer 不是 terminal，或沒有依賴所有 terminal domain tasks。
- 非 Web task 帶 `evidence_policy`。
- `public_lookup` 依賴 Places，或另外兩種 Web mode 沒有 Places dependency。
- Provider 產生的 `mode="tasks"` 只有 Synthesizer、且沒有 `social_opening` opportunity。
- `relationship.date_invitation.v1` 不是精確的 root Relationship → terminal Synthesizer DAG，或混入 precheck／其他 presentation／opportunity。
- `direct_chat` 混入 task、domain opportunity 或不相容欄位。
- `itinerary` 沒有 Places task。
- availability policy 的 task IDs、outcome contract、strict-abort evidence 或產生後的控制邊不合法。
- temporal binding 的 source／candidate／task IDs 不合法，或 `future_planning` 選到過去日期。

Planner 無 tool call、function name 錯誤、schema 不符或 provider error 時最多重試一次；schema retry 只提供 allowlisted 欄位規則，包含 `availability_policy` 與 `temporal_bindings` 的精確形狀，不回送錯誤值。Provider retry 仍使用同一 requested model tier，不自動切換 main。無效或空 DAG 不再靜默改成可執行的 Synthesizer-only plan；兩次仍失敗時 fail closed，不執行 domain tool 或副作用，但會把 typed no-action fact 交給 Synthesizer 寫最後回覆。舊 ProductInfo envelope 的少量 protocol drift 只能在 planner compatibility boundary 做 bounded repair；不能用自然語言 regex 猜 intent。

Compatibility normalization 先複製 provider arguments，只處理 allowlisted 且不授權的格式漂移：known agent 上錯置的 scoped field、精確空 optional placeholder、date intent relocation、呈現模式降級，以及已由 owner+room snapshot 唯一解析時 Places `details|reviews` task 上錯置的字串／空 `place_reference`。Temporal 另允許兩種封閉 repair：將誤放在 `interpretation` 的 `next_occurrence` 還原為 candidate ID，以及從 temporal task IDs 移除明確不屬 Calendar／Web 的 task；兩者都不讀中文、不接受 provider date，也不增加寫入權限。其他 agent、容器型 reference、衝突 intent、`depends_on`／`run_if` drift、unknown agent 與 DAG invariant 不修復。Repair 不消耗 retry，只記 allowlisted code 到 localhost ephemeral debug。

## 4. Routing ownership

Planner 依完整語意選 agent；Python 不另建 keyword router：

- 本人行程、空檔、增修取消 → `calendar`。
- 附近地點、距離、地址、地圖卡，以及 Places 可投影的結構化地點事實（營業／目前開放、價位、評分、步行距離／時間）→ `places`。
- Places task 的 `place_mode` 由 Planner 依完整語意決定：推薦多店用 `discover`；第二間怎樣／地址／營業時間用 `details`；好不好吃／好不好喝／口碑用 `reviews`。後兩者沿用 server-owned selected place，不重新 search nearby。
- 近期／外部資訊、活動、新聞、公開文章、論壇、社群或 URL → `web`。
- 搜尋、進度、多卡片牽線收件匣狀態或受限單一對象摘要 → `match`；提案決策引導到 Hub。搜尋 task 的 `match_search_request` 區分 general/activity、綁定本句 topic 與代送 invitation_evidence；預設先看提案，不以 topic 自動授權送邀請。
- accepted／已建立聯絡對象 aggregate（清單、總數、比較、挑選、`@` 對象與互動摘要）→ `relationship`。「我目前配對到哪些人」「我現在有配到誰」「總共幾位」均屬此類，不因含「目前／配對」改送 `match`。
- 本人 profile、近期情境、記憶、開始／重做性格探索 → `profile`。
- 阿月／App 的能力、流程、限制、隱私、媒合、Calendar 或 assessment 產品行為 → `product_info`。
- 一般對話 → `direct_chat`。Synthesizer-only 只保留為 Scheduler 拒絕 direct fast path 後的內部安全降級，不是 Planner provider 可自行輸出的正常 route。

ProductInfo 是正常 domain task。Planner 只在 `task_brief` 保留使用者 proposition，不選 knowledge section、不輸出產品答案；詳見 `subagent-product-info.md`。

明確要求開始／重試配對必須建立 Match task，不能只輸出 `opportunity.social_opening`。Opportunity 只是有原句 evidence span 且 confidence ≥0.8 的柔性建議，不建立 confirmation。

Match／Relationship 的路由先看資料形狀，不看「配對」單一詞彙：0～多位已建立聯絡對象的 aggregate query 由 Relationship 擁有；搜尋與多張牽線提案的 lifecycle query 由 Match 擁有。Planner 只做此語意分流，Scheduler 不以中文 keyword／regex 重寫 route。

### Relationship date-card routing

使用者明確要阿月現在建立邀請時，Planner 送 `write_intent="relationship.date_invitation.v1"`。DAG 必須含唯一 root Relationship write task 與 terminal Synthesizer；其他 task 只允許 Places／Web／ProductInfo、typed Calendar availability 或唯讀 Match intent。Relationship brief 改成 server-owned write brief，其他 domain brief 保留；混合回合將唯讀結果與 locked confirmation 同時呈現。

Target 只能來自一個已驗證 mention、current message 的連續名稱 evidence span，或同 owner 15 分鐘 recent-contact reference。唯一 accepted target 才能產生一筆 confirmation；模糊、未知、過期或非 accepted 都 fail closed。確認後只建立空白卡片，日期、地點、活動與 notes 由雙方之後填寫。這條 intent／reference channel 是 server-only ephemeral state，不進 prompt、trace 或 public events。

## 5. Web／Places／Itinerary DAG

- 一般外部查詢：`web -> synthesizer`。
- Places 可直接建立的結構化條件（`price`、`rating`、`walking`）：`places -> synthesizer`。店家營業資訊使用 `places(details) -> web(place_hours_fallback) -> synthesizer`；有完整星期營業資料時 Web 以 typed no-op 跳過。
- 若 provider 對明確營業時間請求只輸出唯一 Places `details -> synthesizer`，Scheduler 會以封閉規則補上同店的 `place_hours_fallback`；已有 Web task、一般地址／詳細資料或多個 Places task 時不補。
- 區域／場館／品牌的公開活動查詢：`web(public_lookup) -> synthesizer`，不先建立 Places 搜尋；延續問句由 recent messages 保留活動命題。
- 只有 Places 無法建立的非結構化／目前公開主張（優惠、特殊菜單、活動、臨時歇業公告、社群貼文等）：`places -> web -> synthesizer`。
- `reviews` 固定使用 `places -> web -> synthesizer`，Web 預設 `casual_discovery`；部落格／社群食記的具體口味描述可作為 source-attributed direct finding，分歧或單一來源保留 `partial` 與限制。
- 未要求新活動的一般區域一日遊：`places -> synthesizer` + `presentation_mode="itinerary"`。
- 找一個有直接證據的新活動並排整天：`web -> places -> web -> synthesizer` + itinerary。

Web task 必須保留原始 answer target、地區／時間與證據需求，不能用相鄰背景資料取代答案。一般探索使用 `casual_discovery`；明確官方查證或醫療／法律／金融／安全風險問題使用 `strict_verification`。

Places 不擁有 Web tools。需要 Places 無法建立的非結構化／目前公開 criterion 時，Planner 以依賴關係建立獨立 Web task；Web 只能研究 server 投影的 bounded candidate refs，不能發明新 place candidate。

## 6. Scheduler 如何使用 Plan

1. Assessment／confirmation 等特殊入口已在 Planner 前處理。
2. `_topological_layers` 依 data dependency 與 `run_if` control edge 分層；同層以 `AYUE_SUBAGENT_MAX_PARALLEL` 平行執行，預設且硬上限 2。
3. Scheduler 依 `RuntimeRegistration` 呼叫統一 runner；不依 agent 名稱重新設計 domain loop。
4. Proposal runner 回 `ToolProposal`，再走中央 Guard 與 tool execution；Calendar、Web、ProductInfo 等 specialist runtime 回 completed typed results。Relationship 由自己的 runtime 依 typed write intent 切換 READ／WRITE proposal surface。Scheduler 在啟動 runner 前先評估 `run_if`。
5. Domain task 只收到自己宣告依賴的 prior observations；Synthesizer 收集所有可用 observation。
6. Scheduler 將每個 non-synth task 投影為 `completed|condition_stopped|upstream_unavailable|failed` 的 `ExecutionOutcome`。條件未成立是正常中止；未啟動的下游不得說成 Web／Places 執行失敗。所有 outcome 與成功 observation 都交給 Synthesizer。

完整 runner/result interface 見 `09-runtime-interfaces.md`。

## 7. 測試

- `test_v3_planner.py`：Planner function schema、direct/task routing、ProductInfo DAG、Web evidence policy、Places/itinerary sequence、invalid provider output fail closed。
- `test_v3_contracts.py`：DAG validator、agent allowlist、evidence policy、compatibility normalization。
- `test_v3_scheduler.py`：topological execution、registration dispatch、dependency skip、direct-chat blockers 與 final projection。
- `test_v3_relationship_date_invite.py`：typed write intent、exact DAG、受限 function surface、target resolution 與 fail closed。
- 真實誤路由案例應以匿名資料加入擁有該 contract 的現行 deterministic test／fixture；不要保留不再由測試讀取的舊 trajectory 格式，也不要新增中文 keyword router。
