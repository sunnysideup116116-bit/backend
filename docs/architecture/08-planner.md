# 08. Planner：任務拆解器

> 程式碼真相：`social_demotest/services/ayue_agent/v3/planner.py` 與 `v3/contracts.py`。Planner 只做語意規劃，不執行工具、不審核、不寫資料，也不直接保存 domain state。

## 1. 唯一輸出：`decompose_tasks`

Planner 透過一次 function calling 輸出 typed `Plan`；不解析自由文字 JSON。Plan 有三種 mode：

| mode | 用途 | 限制 |
| --- | --- | --- |
| `tasks` | 需要 domain tool、查證或 workflow | 1–4 個 task，必須恰有一個 terminal synthesizer |
| `direct_chat` | 不需要 App/domain 真相的一般聊天 | 不含 task、opportunity 或 product topics；只有 bounded reply/messages |
| `product_info` | 阿月身份、Public／Private 分工、能力與配對原理 | 不含 task；只輸出 allowlisted `product_info_topics` |

`presentation_mode` 預設 `default`；一日遊、半日遊或整天安排使用 `itinerary`，而且 tasks 中必須包含 Places。

## 2. Task 契約

每個 `SubTask` 只有：

```json
{
  "id": "t1",
  "agent": "web",
  "depends_on": [],
  "task_brief": "找鹽埕區近期可公開查證的新活動，保留日期、時間與場地",
  "evidence_policy": "casual_discovery"
}
```

- `agent`：`calendar | places | web | match | relationship | profile | synthesizer`
- `depends_on`：最多 3 個已存在 task id。
- `task_brief`：描述目標與限制，不填 tool arguments。
- `evidence_policy`：只允許 Web task 使用；`casual_discovery | strict_verification`。

Planner 永遠不能提供 `user_id`、match／event ID、revision、expected status 或 executor authority fields。

## 3. DAG 驗證

`Plan` 的純程式 validator 會拒絕：

- 重複 id、未知 dependency、自我依賴、重複 dependency 或 cycle。
- 沒有／多個 synthesizer，或 synthesizer 不是 terminal。
- terminal domain task 沒有被 synthesizer 依賴。
- 非 Web task 帶 `evidence_policy`。
- `direct_chat`／`product_info` 混入 tasks 或不相容欄位。
- itinerary 沒有 Places task。

任何 schema 或 function-call 錯誤、timeout、缺少 tool call 都使 `plan_turn` 回 `None`；Scheduler fail closed，不執行工具或副作用。

## 4. Routing ownership

Planner 依語意選 agent；程式不另建 keyword router：

- 本人行程、空檔、增修取消 → Calendar。
- 附近地點、距離、地圖卡 → Places。
- 近期／外部資訊、活動、新聞、公開文章或 URL → Web。
- 配對狀態、對象、開始搜尋或提案決策 → Match。
- 已接受聯絡人、`@` 對象與互動摘要 → Relationship。
- 本人 profile、近期情境、記憶、開始性格探索 → Profile。
- 無需工具的一般對話 → `direct_chat`；需要 domain synthesis 但沒有 read 的情況可用 synthesizer-only task。

明確要求開始／重試配對必須建立 Match task，不能只輸出 `opportunity.social_opening`。Opportunity 只是有原句 evidence span 且 confidence ≥ 0.8 的柔性建議，不建立 confirmation。

## 5. Web／Places／Itinerary

- 一般外部查詢：`web → synthesizer`。
- 地點候選需查目前條件：`places → web → synthesizer`。
- 未要求新活動的一般區域一日遊：`places → synthesizer` + `presentation_mode="itinerary"`。
- 找新活動並排整天：`web → places → web → synthesizer` + itinerary。

Web task 必須保留原始 answer target、地區／時間與證據需求；不能以相鄰背景資料代替答案。一般探索使用 `casual_discovery`，明確官方查證或高風險問題使用 `strict_verification`。詳細契約見 `subagent-web.md`。

## 6. Scheduler 如何使用 Plan

1. 特殊 assessment／confirmation 入口已在 Planner 前完成。
2. `_topological_layers` 依 dependency 分層，同層以 `AYUE_SUBAGENT_MAX_PARALLEL` 執行，預設且硬上限 2。
3. Domain task 只看自己宣告依賴的 prior observations；Synthesizer 彙整全部可用 observation。
4. 依賴沒有任何成功 observation 時，下游 task 標記 `SKIPPED/dependency_failed`。
5. 每個 domain sub-agent 再用自己的 function calling 產生具體 `ToolProposal`，由 Guard 與 Scheduler 審核／執行。

## 7. 測試

`test_v3_planner.py` 覆蓋三種 Plan mode、DAG、配對／assessment routing、Web evidence policy、Places 與 itinerary sequence，以及 invalid function call／schema／timeout 的 fail-closed 行為。`test_v3_contracts.py` 固定 validator；真實誤路由案例應再加入匿名 trajectory fixture。
