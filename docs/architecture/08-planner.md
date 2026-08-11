# 08. Planner：任務拆解器

> 程式碼真相：`social_demotest/services/ayue_agent/v3/planner.py` 與 `v3/contracts.py`。Planner 只做語意 routing 與靜態 DAG 拆解，不執行工具、不審核、不保存 domain state，也不直接回答需要產品或 domain 真相的問題。

## 1. 唯一 function interface：`decompose_tasks`

Planner 透過一次 function calling 輸出 typed arguments，不解析自由文字 JSON。新 Planner 只產生兩種正常形狀：

| mode | 用途 | 限制 |
| --- | --- | --- |
| `tasks` | 需要 domain truth、查證、workflow 或 Synthesizer | 1–5 個 tasks；最多 4 個 domain tasks；恰有 1 個 terminal synthesizer |
| `direct_chat` | 不需要 App/domain/private/external truth 的一般聊天 | `tasks=[]`；`direct_reply` 或 `direct_messages` 恰好一種；不能帶 opportunity |

`presentation_mode` 只能是 `default|itinerary`。一日遊、半日遊或整天安排使用 `itinerary`，且 DAG 必須包含 Places。

Provider 每次都必須輸出 `write_intent`。一般請求為 `none`；明確建立約會邀請卡為 `relationship.date_invitation.v1`，且只允許 root Relationship 加 terminal Synthesizer。Canonical `Plan` 保留 `none` default 供 server-side／舊 fixture 建構相容，但這個 default 不存在於 provider-facing required schema。

Planner 的 system prompt 使用 compact-v3 版本：保留 routing ownership、direct-chat 邊界、Places／Web／Calendar／Match／Relationship／Profile／ProductInfo 的歧義規則、DAG invariants，以及具體日期＋既有聯絡人＋新活動＋附近晚餐的五節點範例；不注入完整 public persona、voice few-shots 或 Synthesizer reply contract。Planner 專用 context 只送最近 4 則／2,000 字元歷史、精簡 clock 與非空 server projection；這是 input budget 優化，不是第二個 router。Regression budget 為 system prompt ≤6,000 字元、provider schema ≤3,500 字元、兩者合計 ≤9,500 字元；這些不是 provider context-window 上限。

`Plan` model 仍接受 `mode="product_info"` 與 `product_info_topics`，但那只是舊 provider payload 的 compatibility input；`normalize_plan_for_execution()` 會立即轉成正常的 `product_info -> synthesizer` DAG。新 prompt、fixture 與文件不得輸出 task-free ProductInfo mode。

## 2. `SubTask` contract

```json
{
  "id": "t1",
  "agent": "web",
  "depends_on": [],
  "task_brief": "找鹽埕區近期可公開查證的新活動，保留日期、時間與場地",
  "evidence_policy": "casual_discovery"
}
```

| 欄位 | 規則 |
| --- | --- |
| `id` | 1–64 字元，同一 DAG 唯一 |
| `agent` | `calendar|places|web|match|relationship|profile|product_info|synthesizer` |
| `depends_on` | 最多 4 個同一 DAG 的 task IDs；只代表資料依賴 |
| `task_brief` | 1–500 字元；描述目標、限制與 evidence class，不是 tool arguments |
| `evidence_policy` | 只有 Web 可用：`casual_discovery|strict_verification` |
| `outcome_contract` | 只有 Calendar availability task 可用：`calendar.availability.v1` |
| `run_if` | 控制依賴；`task.finished` 或 allowlisted Calendar outcome，不傳遞上游 observation |

未使用的 optional 欄位要省略；不可用空字串代替 enum，也不可送出不完整的 `{}` `run_if`。Provider boundary 只會把 known agent 的精確空 placeholder 視為省略；空白字串、非空無效值、不完整 condition 與 graph／DAG drift 仍由 canonical validator 拒絕，最多 retry once 後 fail closed。

Planner 永遠不能提供 `user_id`、match/proposal/event ID、revision、expected status 或其他 executor authority fields。

具體日期的個人外出／約會建議，即使使用者說不要新增行程，仍先建立唯讀 Calendar availability task。一般 precheck 使用 `run_if.required_outcome="task.finished"`；只有明確說「有事就算了／沒事才繼續」才使用 `calendar.no_scheduled_events`。Calendar mutation 仍只在使用者明確要求保存、建立、修改或取消時建立。

「具體日期＋從目前認識／accepted contacts 中挑一位＋找新活動＋附近晚餐」固定使用 `calendar`、平行 gated 的 `relationship`／`web`、消費 Web activity venue 的 `places`，以及 terminal `synthesizer`。這是 4 個 domain tasks 加 Synthesizer；不得改成 Match、direct chat 或 provider-authored synth-only plan。

## 3. DAG validator

`Plan` 的純程式 validator 會拒絕：

- 重複 ID、未知 dependency、自我依賴、重複 dependency 或 cycle。
- 沒有或有多個 synthesizer。
- synthesizer 不是 terminal，或沒有依賴所有 terminal domain tasks。
- 非 Web task 帶 `evidence_policy`。
- Provider 產生的 `mode="tasks"` 只有 Synthesizer、且沒有 `social_opening` opportunity。
- `relationship.date_invitation.v1` 不是精確的 root Relationship → terminal Synthesizer DAG，或混入 precheck／其他 presentation／opportunity。
- `direct_chat` 混入 task、domain opportunity 或不相容欄位。
- `itinerary` 沒有 Places task。

Planner 無 tool call、function name 錯誤或 schema 不符時最多重試一次；schema retry 只提供 allowlisted 欄位規則，例如 required `write_intent`、`evidence_policy` 僅限 Web、`outcome_contract` 僅限 Calendar availability，不回送錯誤值。無效或空 DAG 不再靜默改成 Synthesizer-only；兩次仍失敗或 provider timeout 時 fail closed，不執行任何工具或副作用。舊 ProductInfo envelope 的少量 protocol drift 只能在 planner compatibility boundary 做 bounded repair；不能用自然語言 regex 猜 intent。

Compatibility normalization 一律先複製 provider arguments，且只接受三類封閉修復：known agent 上錯置但值合法的 `evidence_policy`／Calendar availability `outcome_contract`、精確空 optional placeholder，以及 Relationship 將精確 `relationship.date_invitation.v1` 放到 `outcome_contract` 的單一 relocation case。其他 agent/value、衝突 root intent、`depends_on`／`run_if` drift、unknown agent 與 DAG invariant 不修復。Repair 不消耗 retry，只記 allowlisted code 到 localhost ephemeral debug；normalized payload、owner text 與 raw exception 不進 durable trace 或 public events。

## 4. Routing ownership

Planner 依完整語意選 agent；Python 不另建 keyword router：

- 本人行程、空檔、增修取消 → `calendar`。
- 附近地點、距離、地址、地圖卡，以及 Places 可投影的結構化地點事實（營業／目前開放、價位、評分、步行距離／時間）→ `places`。
- 近期／外部資訊、活動、新聞、公開文章、論壇、社群或 URL → `web`。
- singleton active-proposal/search lifecycle（這一輪最多一筆提案的搜尋、進度、狀態、決策或單一對象摘要）→ `match`。
- accepted／已建立聯絡對象 aggregate（清單、總數、比較、挑選、`@` 對象與互動摘要）→ `relationship`。「我目前配對到哪些人」「我現在有配到誰」「總共幾位」均屬此類，不因含「目前／配對」改送 `match`。
- 本人 profile、近期情境、記憶、開始／重做性格探索 → `profile`。
- 阿月／App 的能力、流程、限制、隱私、媒合、Calendar 或 assessment 產品行為 → `product_info`。
- 一般對話 → `direct_chat`。Synthesizer-only 只保留為 Scheduler 拒絕 direct fast path 後的內部安全降級，不是 Planner provider 可自行輸出的正常 route。

ProductInfo 是正常 domain task。Planner 只在 `task_brief` 保留使用者 proposition，不選 knowledge section、不輸出產品答案；詳見 `subagent-product-info.md`。

明確要求開始／重試配對必須建立 Match task，不能只輸出 `opportunity.social_opening`。Opportunity 只是有原句 evidence span 且 confidence ≥0.8 的柔性建議，不建立 confirmation。

Match／Relationship 的路由先看資料形狀，不看「配對」單一詞彙：0～多位已建立聯絡對象的 aggregate query 由 Relationship 擁有；這一輪唯一 active proposal 的 lifecycle query 由 Match 擁有。Planner 只做此語意分流，Scheduler 不以中文 keyword／regex 重寫 route。

### Relationship date-card routing

使用者明確要求邀請一位已接受聯絡人並建立共同約會卡時，Planner 必須送 `write_intent="relationship.date_invitation.v1"`，且 DAG 只能是 root Relationship → terminal Synthesizer。Match／Calendar／Places／Web precheck、非 default presentation 或 opportunity 都會使 plan 無效。驗證成功後，Planner 以 server-owned brief 取代 model-written task brief；Relationship Runtime 只看到 `relationship.start_date_coordination`，最多做兩次 function-call protocol attempt，不能先讀聯絡人清單。

Target 只能來自一個已驗證 mention、current message 的連續名稱 evidence span，或同 owner 15 分鐘 recent-contact reference。唯一 accepted target 才能產生一筆 confirmation；模糊、未知、過期或非 accepted 都 fail closed。確認後只建立空白卡片，日期、地點、活動與 notes 由雙方之後填寫。這條 intent／reference channel 是 server-only ephemeral state，不進 prompt、trace 或 public events。

## 5. Web／Places／Itinerary DAG

- 一般外部查詢：`web -> synthesizer`。
- Places 可直接建立的結構化條件（`hours`、`price`、`rating`、`walking`）：`places -> synthesizer`，即使使用者指定今晚／目前也不自動建立 Web task。
- 只有 Places 無法建立的非結構化／目前公開主張（優惠、特殊菜單、活動、臨時歇業公告、社群貼文等）：`places -> web -> synthesizer`。
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
6. 依賴沒有成功 observation 時，下游 task 標記 `SKIPPED/dependency_failed`；條件不符合或來源無 outcome 時標記 `condition_not_met`／`condition_unavailable`。

完整 runner/result interface 見 `09-runtime-interfaces.md`。

## 7. 測試

- `test_v3_planner.py`：Planner function schema、direct/task routing、ProductInfo DAG、Web evidence policy、Places/itinerary sequence、invalid provider output fail closed。
- `test_v3_contracts.py`：DAG validator、agent allowlist、evidence policy、compatibility normalization。
- `test_v3_scheduler.py`：topological execution、registration dispatch、dependency skip、direct-chat blockers 與 final projection。
- `test_v3_relationship_date_invite.py`：typed write intent、exact DAG、受限 function surface、target resolution 與 fail closed。
- 真實誤路由案例應以匿名資料加入擁有該 contract 的現行 deterministic test／fixture；不要保留不再由測試讀取的舊 trajectory 格式，也不要新增中文 keyword router。
