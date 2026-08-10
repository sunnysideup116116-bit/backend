# 08. Planner：任務拆解器

> 程式碼真相：`social_demotest/services/ayue_agent/v3/planner.py` 與 `v3/contracts.py`。Planner 只做語意 routing 與靜態 DAG 拆解，不執行工具、不審核、不保存 domain state，也不直接回答需要產品或 domain 真相的問題。

## 1. 唯一 function interface：`decompose_tasks`

Planner 透過一次 function calling 輸出 typed arguments，不解析自由文字 JSON。新 Planner 只產生兩種正常形狀：

| mode | 用途 | 限制 |
| --- | --- | --- |
| `tasks` | 需要 domain truth、查證、workflow 或 Synthesizer | 1–4 個 tasks；最多 3 個 domain tasks；恰有 1 個 terminal synthesizer |
| `direct_chat` | 不需要 App/domain/private/external truth 的一般聊天 | `tasks=[]`；`direct_reply` 或 `direct_messages` 恰好一種；不能帶 opportunity |

`presentation_mode` 只能是 `default|itinerary`。一日遊、半日遊或整天安排使用 `itinerary`，且 DAG 必須包含 Places。

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
| `depends_on` | 最多 3 個同一 DAG 的 task IDs |
| `task_brief` | 1–500 字元；描述目標、限制與 evidence class，不是 tool arguments |
| `evidence_policy` | 只有 Web 可用：`casual_discovery|strict_verification` |

Planner 永遠不能提供 `user_id`、match/proposal/event ID、revision、expected status 或其他 executor authority fields。

## 3. DAG validator

`Plan` 的純程式 validator 會拒絕：

- 重複 ID、未知 dependency、自我依賴、重複 dependency 或 cycle。
- 沒有或有多個 synthesizer。
- synthesizer 不是 terminal，或沒有依賴所有 terminal domain tasks。
- 非 Web task 帶 `evidence_policy`。
- `direct_chat` 混入 task、domain opportunity 或不相容欄位。
- `itinerary` 沒有 Places task。

Planner 無 tool call、function name 錯誤、schema 不符或 provider timeout 時，Scheduler fail closed，不執行任何工具或副作用。對明確嘗試 `direct_chat`／舊 ProductInfo envelope 的少量 protocol drift，只能在 planner compatibility boundary 做 bounded repair；不能用自然語言 regex 猜 intent。

## 4. Routing ownership

Planner 依完整語意選 agent；Python 不另建 keyword router：

- 本人行程、空檔、增修取消 → `calendar`。
- 附近地點、距離、地址、地圖卡 → `places`。
- 近期／外部資訊、活動、新聞、公開文章、論壇、社群或 URL → `web`。
- 配對狀態、目前提案、開始搜尋或提案決策 → `match`。
- 已接受聯絡人、`@` 對象與互動摘要 → `relationship`。
- 本人 profile、近期情境、記憶、開始／重做性格探索 → `profile`。
- 阿月／App 的能力、流程、限制、隱私、媒合、Calendar 或 assessment 產品行為 → `product_info`。
- 一般對話 → `direct_chat`；若 direct fast path 不安全或需一般組句，可使用 synthesizer-only DAG。

ProductInfo 是正常 domain task。Planner 只在 `task_brief` 保留使用者 proposition，不選 knowledge section、不輸出產品答案；詳見 `subagent-product-info.md`。

明確要求開始／重試配對必須建立 Match task，不能只輸出 `opportunity.social_opening`。Opportunity 只是有原句 evidence span 且 confidence ≥0.8 的柔性建議，不建立 confirmation。

## 5. Web／Places／Itinerary DAG

- 一般外部查詢：`web -> synthesizer`。
- 地點候選需查目前條件：`places -> web -> synthesizer`。
- 未要求新活動的一般區域一日遊：`places -> synthesizer` + `presentation_mode="itinerary"`。
- 找一個有直接證據的新活動並排整天：`web -> places -> web -> synthesizer` + itinerary。

Web task 必須保留原始 answer target、地區／時間與證據需求，不能用相鄰背景資料取代答案。一般探索使用 `casual_discovery`；明確官方查證或醫療／法律／金融／安全風險問題使用 `strict_verification`。

Places 不擁有 Web tools。需要 current/public criterion 時，Planner 以依賴關係建立獨立 Web task；Web 只能研究 server 投影的 bounded candidate refs，不能發明新 place candidate。

## 6. Scheduler 如何使用 Plan

1. Assessment／confirmation 等特殊入口已在 Planner 前處理。
2. `_topological_layers` 依 dependency 分層；同層以 `AYUE_SUBAGENT_MAX_PARALLEL` 平行執行，預設且硬上限 2。
3. Scheduler 依 `RuntimeRegistration` 呼叫統一 runner；不依 agent 名稱重新設計 domain loop。
4. Proposal runner 回 `ToolProposal`，再走中央 Guard 與 tool execution；Calendar、Web、ProductInfo 等 specialist runtime 回 completed typed results。
5. Domain task 只收到自己宣告依賴的 prior observations；Synthesizer 收集所有可用 observation。
6. 依賴沒有成功 observation 時，下游 task 標記 `SKIPPED/dependency_failed`。

完整 runner/result interface 見 `09-runtime-interfaces.md`。

## 7. 測試

- `test_v3_planner.py`：Planner function schema、direct/task routing、ProductInfo DAG、Web evidence policy、Places/itinerary sequence、invalid provider output fail closed。
- `test_v3_contracts.py`：DAG validator、agent allowlist、evidence policy、compatibility normalization。
- `test_v3_scheduler.py`：topological execution、registration dispatch、dependency skip、direct-chat blockers 與 final projection。
- 真實誤路由案例應加入匿名 trajectory fixture；修 contract、context projection 或 prompt，不新增中文 keyword router。
