# 公開阿月 V3 Sub-agent 架構設計

**日期**：2026-08-04
**狀態**：待審核
**取代**：V2 單一 agent loop（`AYUE_V2_ARCHITECTURE.md`）
**保留**：所有 `AGENTS.md` 安全、隱私與 domain 真相規則

---

## 1. 設計動機

V2 是單一 agent loop：`Context → Planner → Guard → Tool → Observation → Planner → Final`，`MAX_STEPS=5`，每步由同一個 Planner 決定下一步。在處理複雜或有順序性的任務時失誤率極高。

### 失敗案例

使用者訊息：「我這星期日想去和小李吃晚餐，想吃附近的牛排和再去吃甜點，你覺得如何？」

使用者週日晚其實已排電影。阿月實際回覆：

> 這星期日是 8/9，三民區附近有巖炙炭燒牛排（直線約 726 公尺）、大廚牛排（約 778 公尺）等，感覺都不錯。不過我目前認識的聯絡人只有小安，沒有小李的資料，他是你的朋友嗎？

### 失誤分析

| # | 子任務 | 阿月做了嗎 | 依賴 |
|---|--------|-----------|------|
| 1 | 確認日期（週日 = 8/9） | ✅ | — |
| 2 | 讀行事曆看當天有沒有衝突 | ❌ 沒做 | — |
| 3 | 確認小李是誰／是否為聯絡人 | ✅ | — |
| 4 | 搜尋附近牛排餐廳 | ✅ | — |
| 5 | 搜尋牛排店附近的甜點店 | ❌ 沒做 | 依賴 #4 |
| 6 | 綜合所有資訊給意見（含行程衝突提醒） | ❌ 沒做 | 依賴 #2 #4 #5 |

阿月做了 2.5/6。根因：單一 Planner 在 3 步讀取上限內傾向抓最顯眼的一步就收斂，無法可靠地先拆解再逐步執行。行事曆檢查因為 Planner 直接跳 `places` intent 而完全沒被觸發。

### 期望行為

- 推薦牛排與甜點餐廳
- 同時提醒當晚已有電影行程
- 小李是誰為次要，不阻塞主要資訊

---

## 2. 整體架構與角色

```
使用者訊息
     │
     ▼
┌──────────────────────────────────────────────────────┐
│  Scheduler / Orchestrator（純程式碼）                │
│                                                      │
│  每回合開頭先處理：                                   │
│  - 有效 confirmation → 直接執行寫入，不跑 Planner     │
│  - active/awaiting assessment session → 不跑 Planner  │
│  - 否則 → 跑 Planner                                 │
└──────┬───────────────────────────────────────────────┘
       │ 正常流程
       ▼
┌──────────────────────────────────────────────────────┐
│  Planner（輕量 LLM）                                  │
│                                                      │
│  只做任務拆解，產出靜態子任務 DAG：                    │
│  - 哪些 sub-agent 要跑                               │
│  - 平行還是有順序                                    │
│  - 哪個結果要傳給哪個 agent                           │
│  - 最後 synthesizer 綜合                             │
│                                                      │
│  不填 function args、不審核、不回覆使用者             │
└──────┬───────────────────────────────────────────────┘
       │ 子任務 DAG
       ▼
┌──────────────────────────────────────────────────────┐
│  Scheduler 執行                                       │
│                                                      │
│  ┌──────────────┐  ┌──────────────┐                   │
│  │ calendar_agent│  │ places_agent │  ← 平行          │
│  │ (LLM + tools) │  │ (LLM + tools)│                   │
│  └──────┬───────┘  └──────┬───────┘                   │
│         │ proposal        │ proposal                   │
│         ▼                 ▼                            │
│  ┌──────────────────────────────────────────┐         │
│  │        中央 Guard（純程式碼）              │         │
│  │  schema / args 安全 / 重複 /             │         │
│  │  步數 / 寫入 confirmation / CAS           │         │
│  └──────────┬───────────────────────────────┘         │
│         通過 │ 執行工具 → observation                │
│                                                      │
│  有順序依賴時：places(牛排)結果 →                      │
│  places(甜點，用牛排店位置)                           │
│                                                      │
│  全部完成 → synthesizer                               │
└──────────────────────────────────────────────────────┘
       │
       ▼
┌──────────────────────────────────────────────────────┐
│  Synthesizer（最終綜合 LLM）                          │
│                                                      │
│  拿所有 sub-agent 的 observation，產出使用者回覆。     │
│  不再 call function。                                │
│  處理「缺一塊」的情境。                                │
└──────┬───────────────────────────────────────────────┘
       │
       ▼
   使用者回覆
```

### 角色定論

| 角色 | 做什麼 | 不做什麼 |
|------|--------|----------|
| **Scheduler** | 管理 confirmation 入口、執行順序、平行、context slice 分配、步數計數 | 不決定語意 |
| **Planner** | 把請求拆成「需要哪些 sub-agent + 順序/依賴」 | 不填 function args、不審核、不回覆使用者 |
| **Sub-agent（6 個）** | 用 LLM + function calling 產出 tool call proposal | 不自己執行、不跨 domain |
| **中央 Guard** | 驗證 sub-agent 的 proposal，通過才執行 | 不花 LLM |
| **Synthesizer** | 拿所有 observation 產出使用者回覆 | 不再 call function |

**核心原則**：sub-agent 是「分工的 Planner」，不是「分工的 executor」。它們用 LLM 規劃並產出 tool call proposal，但執行由中央 Guard + Scheduler 統一控制。

---

## 3. 六個 Sub-agent

每個 sub-agent 收到 Scheduler 預先切好的 context slice（只含該 agent 該看的資料），加上當前時間（Scheduler 注入，不花 LLM）。agent 用 function calling 產出 tool call proposal，不自己執行。

| Sub-agent | 負責的意圖 | 擁有的工具 | 收到的 context slice |
|-----------|-----------|-----------|---------------------|
| **calendar_agent** | calendar / calendar_action | `calendar.list_my_events`、`calendar.find_my_event`、`calendar.create_my_event`*、`calendar.update_my_event`*、`calendar.cancel_my_event`*、`calendar.cancel_my_events`* | 本人完整行事曆、當前時間、對話歷史 |
| **places_agent** | places / web | `places.search_nearby`、`places.resolve_place`、`places.measure_distance`、`web.search`、`web.extract` | 本人粗略所在地、當前時間、對話歷史、前置 places observation（若有順序依賴） |
| **match_agent** | match_status / match_action | `match.get_status`、`match.get_counterparty_summary`、`match.start_search`*、`match.decide_active_proposal`* | 唯一 proposal 狀態、配對歷史、當前時間 |
| **relationship_agent** | relationship | `relationship.get_verified_evidence`、`relationship.get_mentioned_contact_summary`、`relationship.list_accepted_contacts` | @ 的聯絡人公開名稱、已接受聯絡人清單、對話歷史 |
| **profile_agent** | profile / assessment / memory | `profile.get_recent_context`、`profile.get_self_summary`、`profile.start_assessment`*、`memory.search_my_profile` | 本人 profile、近期情境、記憶、當前時間 |
| **synthesizer** | （無工具） | 無 | 所有 sub-agent 的 observation（含失敗 skip 標記）、當前時間、對話歷史 |

`*` = 寫入工具，需要 confirmation。agent 產出 proposal 後，Guard 擋下、Scheduler 建 pending confirmation，不直接執行。

### 設計要點

1. **time 不獨立成 agent**——當前時間/日期由 Scheduler 純程式碼注入每個 agent 的 prompt。任何 agent 需要時間資訊都能直接看到，零 LLM 成本。

2. **places_agent 同時擁有 web 工具**——因為「查餐廳 + 查網路評價」是常見組合，且兩者有順序依賴（先搜到店再查網路）。放同一個 agent 讓它自己決定要不要追加 web 搜尋，比拆成兩個 agent 再 handoff 簡單。

3. **synthesizer 不再 call function**——它的 context 是「所有 observation + 對話歷史」，職責是綜合產出回覆。如果發現缺資訊，它只能回覆「我這次沒查到 X，要不要我再看？」，讓使用者下一回合觸發，不在同一回合內 re-plan。

4. **寫入工具標記 `*` 的仍走 confirmation**——sub-agent 產出「我想 cancel event X」的 proposal，中央 Guard 識別這是寫入工具 → Scheduler 建立 pending confirmation → 回覆使用者「要取消嗎？」。同一回合若有多個寫入，各自建獨立 confirmation，執行時各自走 CAS，stale 就回報「狀態已變」。

5. **sub-agent 之間的 handoff**——有順序依賴時（例如 places 先查牛排店、再查附近甜點），Scheduler 把第一個 places observation 結果放進第二個 places_agent call 的 context slice 裡，讓第二個 agent 知道「牛排店在這裡，查它附近的甜點」。

---

## 4. Planner 的子任務 DAG 與 Scheduler 執行

### Planner 輸入（由 Scheduler 準備）

- 清理後的使用者訊息
- 最近對話歷史
- 當前時間/日期
- 六個 sub-agent 的名稱與一句話描述（讓 Planner 知道有哪些 agent 可派）
- 當前有效的 pending confirmation 清單（讓 Planner 知道哪些寫入已在等確認）

### Planner 輸出（typed 靜態 DAG）

```python
class SubTask:
    id: str
    agent: str          # "calendar" | "places" | "match" | "relationship" | "profile" | "synthesizer"
    depends_on: list[str]  # 前置 sub-task 的 id；空 = 可立刻跑
    task_brief: str      # 一句話描述這個子任務要查/做什麼

class Plan:
    tasks: list[SubTask]  # 含 synthesizer 作為終點
```

### 牛排例子 Planner 輸出

```json
{
  "tasks": [
    {"id": "t1", "agent": "calendar", "depends_on": [], "task_brief": "檢查使用者這週日是否有行程衝突"},
    {"id": "t2", "agent": "places", "depends_on": [], "task_brief": "搜尋使用者附近的三民區牛排餐廳"},
    {"id": "t3", "agent": "places", "depends_on": ["t2"], "task_brief": "搜尋 t2 牛排店附近的甜點店"},
    {"id": "t4", "agent": "synthesizer", "depends_on": ["t1", "t2", "t3"], "task_brief": "綜合行事曆衝突、牛排與甜點推薦，給使用者想法與提醒"}
  ]
}
```

Planner **不填** tool name、不填 args、不填 evidence span。它只說「需要 calendar agent 做這件事」，不說「call `calendar.list_my_events` 帶 date=2026-08-09」。

### Scheduler 執行流程

1. 收到 Plan
2. 拓樸排序 tasks，同一層可平行
   - 第 0 層（depends_on=[]）: t1, t2 → 平行跑
   - 第 1 層（depends_on=[t2]）: t3 → t2 完成後跑
   - 第 2 層（depends_on=[t1,t2,t3]）: t4 → 全部完成後跑
3. 對每個 sub-task：
   a. Scheduler 從 context.py 的完整 bounded context 切出該 agent 的 slice
   b. 若有 depends_on，把前置 observation 結果加入 slice
   c. 呼叫該 sub-agent（LLM + 它的工具集 + function calling）
   d. sub-agent 產出 tool call proposal
   e. 中央 Guard 驗證 proposal：
      - schema 合法？
      - args 沒偷渡 user_id/match_id/revision？
      - 同 tool+args 沒重複跑過？
      - 該 sub-agent 步數未超？
      - 是寫入工具 → 建立 confirmation，不執行
   f. Guard 通過 → 執行工具 → observation
      Guard 不過 → 標記 failed（含 Guard code）
4. synthesizer task：
   a. 收集所有已完成 sub-task 的 observation（含失敗 skip 的標記）
   b. 呼叫 synthesizer LLM 產出回覆
   c. 語言/安全驗證 → 回覆使用者

### 靜態規劃，不 re-plan

Planner 一開始就決定完整子任務圖。sub-agent 回報結果（例如行事曆衝突）後，不回頭改計畫，由 synthesizer 拿到所有結果後自己決定怎麼講。例如「附近有巖炙牛排不錯，不過你週日晚有電影，要不要改午餐？」

synthesizer 能處理「順帶提醒衝突 + 給替代方案」，不需要「發現衝突就重跑一輪」。

---

## 5. 中央 Guard（純程式碼，零 LLM）

中央 Guard 是純 Python `if/return`，毫秒級，零成本。不花任何模型呼叫。

### 檢查項目

| 檢查 | 怎麼做 | 範例 |
|------|--------|------|
| schema 合法嗎 | Pydantic 驗證 args 符合 tool 的 arg schema | args 缺必填欄位 → 擋 |
| args 安全嗎 | 黑名單欄位檢查 | args 出現 `user_id`/`match_id`/`revision` → 擋 |
| 寫入有確認嗎 | 查 pending confirmation 是否存在且未過期 | 寫入工具無 confirmation → 建 confirmation，不執行 |
| 重複呼叫嗎 | 算 `tool + normalized args` hash，比對該 sub-agent 已跑清單 | 同樣查過牛排店 → 沿用舊結果 |
| 步數超了嗎 | 該 sub-agent 計數器，最多 3 次唯讀 | 超過 → 擋，標記 failed |

### ID 注入原則不變

sub-agent 產出 proposal 時**不能填** `user_id`、`match_id`、`event_id`、`revision`、`expected_status`。中央 Guard 驗證這些欄位不在 args 裡。執行時由 executor 根據登入者與 canonical state 注入。

---

## 6. 步數額度

每個 sub-agent **各自最多 3 次唯讀**，整回合不再有全局唯讀上限。寫入無數量上限，但每個寫入 proposal 都必須各自建立獨立 confirmation（見第 7 節）。同一 sub-agent 在一回合內可產出多個寫入 proposal，各自走 confirmation 流程。

| Sub-agent | 唯讀上限 |
|-----------|---------|
| calendar_agent | 3 |
| places_agent | 3 |
| match_agent | 3 |
| relationship_agent | 3 |
| profile_agent | 3 |
| synthesizer | 0（不 call function） |

**代價**：多個 agent 同時跑、各自額度內可跑 2-3 次，總讀取可能到 8-12 次。延遲與成本比現有 V2 高。這是 sub-agent 架構換取失誤率降低的代價。

---

## 7. Confirmation、寫入與失敗處理

### Confirmation 流程（多筆獨立）

```
使用者：「取消週日電影，然後約小李吃牛排」

Planner 拆解：
  t1: calendar_agent → cancel_my_event（寫入）
  t2: match_agent     → start_search（寫入）
  t3: synthesizer     → 綜合

Scheduler 執行 t1：
  calendar_agent 產出 proposal「cancel event X」
  中央 Guard 識別=寫入 → 不執行，建立 confirmation #1
Scheduler 執行 t2：
  match_agent 產出 proposal「start search」
  Guard 識別=寫入 → 不執行，建立 confirmation #2
t3 synthesizer 拿到「兩個寫入待確認」→ 回覆：
  「要取消週日的電影嗎？另外我也幫你約小李吃牛排，要開始找人嗎？」

下一回合使用者回「都好」：
  Scheduler 開頭檢查 → 有 2 個有效 confirmation
  confirmation #1：重讀 canonical state → CAS 執行 cancel_my_event
  confirmation #2：重讀 canonical state → CAS 執行 start_search
  各自獨立，任一 stale 就回報「X 的狀態已變，要重新確認」
  全部處理完 → synthesizer 綜合結果回覆
```

### Confirmation 規則

- 每個寫入 proposal 各自建立獨立 confirmation，各自有 15 分鐘效期
- confirmation 期間使用者可問別的事，不會被汙染
- 執行時各自重讀 canonical state + CAS，不共用 revision 快照，不連動失效
- stale 就回報「狀態已變，要重新確認」，不覆寫終態

### Pending confirmation 入口（Scheduler 開頭，純程式碼）

每回合 Scheduler 先跑這段判斷：

```
有有效 confirmation？
├─ 訊息是「確認」→ 逐一執行所有有效 confirmation（各自 CAS，順序不影響結果）
│                 任一 stale 就回報「X 的狀態已變，要重新確認」（不覆寫終態）
│                 全部處理完 → synthesizer 綜合所有執行結果回覆
├─ 訊息是「取消」→ 清除所有有效 confirmation → 回覆「已取消」
├─ 訊息是別的問題 → 保留 confirmation，跑正常 Planner 流程
│                 （confirmation 不過期就不受影響）
└─ 沒有 confirmation → 正常 Planner 流程

有 active/awaiting assessment session？
├─ 是 → 接管 session，不跑 Planner（同現在 V2）
└─ 否 → 正常流程
```

### 失敗處理（skip 失敗 agent，synthesizer 處理缺口）

```
Scheduler 跑 t1 calendar_agent → ✅ observation「週日晚有電影」
Scheduler 跑 t2 places_agent    → ❌ LLM 逾時
Scheduler 標記 t2 = failed（含失敗原因 code）
Scheduler 跑 t3（依賴 t2）→ 因為 t2 失敗，t3 也無法跑 → 標記 t3 = skipped
Scheduler 跑 t4 synthesizer → 收到：
  - t1 observation
  - t2 failed（原因：llm_timeout）
  - t3 skipped（原因：dependency_failed）
synthesizer 回覆：「你週日晚有電影，不過餐廳我這次沒查到，要不要我等一下再查？」
```

### 失敗分類

| 失敗類型 | 來源 | 處理 |
|---------|------|------|
| LLM 逾時/壞 JSON/低信心 | sub-agent 產出階段 | 標記 failed，不重試 |
| Guard 擋下（schema/args/重複/步數） | 中央 Guard | 標記 failed，含 Guard code，不重試。sub-agent 不會收到 Guard 的回饋再修正——這保持「LLM 建議、程式驗證」的單向流程，避免 sub-agent 與 Guard 之間形成 loop |
| 工具執行錯誤（API 失敗等） | 工具層 | 標記 failed，含 error code |
| 依賴的 sub-task failed | Scheduler | 標記 skipped，連帶不跑 |

synthesizer 的 prompt 會收到每個 task 的狀態（ok / failed + code / skipped + reason），讓它產出合理的部分回覆。

---

## 8. Context 建立與隱私邊界

### Context 來源仍單一

`context.py` 仍是唯一 Context Builder，負責產出完整 bounded context（跟現在 V2 一樣：最近 12 則對話、近期情境、記憶、proposal 狀態、@ 聯絡人、當前時間）。刻意排除的東西不變：Mongo `_id`、對方私人記憶、對方行事曆細節、內部 ID、revision。

### Scheduler 切 context slice（程式碼，deterministic）

| Sub-agent | 從完整 context 切出的 slice | 刻意不給 |
|-----------|---------------------------|---------|
| calendar | 本人完整行事曆、當前時間、對話歷史 | 配對狀態、對方資料、places 資料 |
| places | 本人粗略所在地、當前時間、對話歷史、前置 places observation | 行事曆細節、配對、對方 |
| match | 唯一 proposal 狀態、配對歷史、當前時間 | 行事曆細節、對方私人記憶 |
| relationship | @ 聯絡人公開名稱、已接受聯絡人清單、對話歷史 | 行事曆、places、配對細節 |
| profile | 本人 profile、近期情境、記憶、當前時間 | 配對、對方、行事曆 |
| synthesizer | 所有 sub-agent observation、當前時間、對話歷史 | 不再讀資料 |

### 為什麼由 Scheduler 切而不是各 agent 自己拉

1. **隱私邊界集中維護**——一份切法邏輯，改規則只改一個地方
2. **符合 `AGENTS.md` 規則 6**——「Context 只能由 context.py 建立」，Scheduler 切 slice 是 context.py 的延伸，不是新 source of truth
3. **sub-agent 收到的是已安全 slice**——它不用判斷「對方行事曆能不能看」，因為根本沒拿到
4. **省一輪 LLM**——如果 agent 自己拉資料，要先 call function 才有 context，等於多一輪。Scheduler 預先切好，agent 一次就有足夠 context 產出 proposal

### 有順序依賴時的 context 注入

places_agent 跑 t2（搜牛排）→ t3（搜甜點，依賴 t2）。Scheduler 跑 t3 時：

```
t3 的 places_agent context slice =
  基礎 slice（所在地、時間、對話歷史）
  + t2 的 observation（牛排店名、位置、距離）
```

t3 的 prompt 會看到「前一個查詢找到巖炙炭燒牛排在三民區，請查它附近的甜點店」。agent 據此產出 `places.search_nearby` 的 proposal，帶入 t2 結果衍生的位置參數。

### 對方資訊的隱私守則（維持 V2 既有）

- 對方行事曆細節永不進任何 agent 的 slice
- 對方私人記憶永不進任何 agent 的 slice
- 跨使用者 availability 只用產品允許的安全 projection（busy/free）
- @ 聯絡人只用公開顯示名稱，內部 ID 留在 executor side
- trace 不保存 prompt、args、observation、ID、revision

---

## 9. Trace、觀測性與 Public Event

### Trace（privacy-safe，allowlisted only）

Trace 仍是 privacy-safe、allowlisted only，保存於 `agent_runs`。不保存完整 prompt、args、observation、ID、revision、對方資料。

| 欄位 | 內容 | 是否新增 |
|------|------|---------|
| `planner_plan` | Planner 拆解的子任務 DAG（agent 名稱 + depends_on + task_brief，**不含 args**） | 新增 |
| `subtask_results` | 每個 sub-task 的狀態（ok/failed/skipped）+ Guard result code + 工具名 + error code | 新增 |
| `guard_results` | 中央 Guard 的每個驗證結果 code | 沿用 |
| `synthesizer_outcome` | synthesizer 的 outcome code（planner_reply / llm_reply / fallback） | 沿用 |
| `model_ms` | 各 LLM 呼叫延遲（Planner + 各 sub-agent + synthesizer） | 沿用 |
| `tool_ms` | 各工具執行延遲 | 沿用 |

**禁止保存**：sub-agent 的完整 prompt、tool arguments、observation 內容、使用者原句、對方資料。只存 code 與 metadata。

### Public event（NDJSON stream）

維持現有 allowlist：`run_started`、`tool_started`、`tool_finished`、`final`、`error`。不新增 event 類型。

`tool_started` 的 `text` 仍只顯示安全 progress 文字，不顯示 sub-agent 名稱或 tool name。`tool_finished` 只更新狀態，不新增訊息。

進度流程範例：

```
run_started
  ├─ (平行) tool_started「我看一下你的行事曆…」
  ├─ (平行) tool_started「我搜一下附近的牛排…」
  ├─ (平行) tool_finished (calendar ok)
  ├─ (平行) tool_finished (places 牛排 ok)
  ├─ tool_started「再查一下甜點…」
  ├─ tool_finished (places 甜點 ok)
final
```

UI 仍只顯示單一暫時 progress bubble，新進度覆蓋舊的。final/error/斷線時清除 bubble。

---

## 10. Runtime flags

| Flag | 用途 |
|------|------|
| `AYUE_AGENT_V3_MODE=on\|off` | Sub-agent 架構或人工 legacy rollback（V2 既有 flag 保留供緊急 rollback） |
| `AYUE_AGENT_V3_USER_ALLOWLIST` | 漸進式指定使用者 |
| `AYUE_SUBAGENT_MAX_READS` | 每個 sub-agent 的唯讀上限，預設 3 |
| `AYUE_SUBAGENT_TIMEOUT_MS` | 單一 sub-agent LLM 呼叫逾時，預設沿用現有 |
| `AYUE_AGENT_MAX_STEPS` | （移除全局唯讀上限，改由 per-agent 額度取代） |

**V2 → V3 並存期**：`AYUE_AGENT_V2_MODE` 與 `AYUE_AGENT_V3_MODE` 可同時存在。V3 on 時走 sub-agent 架構，off 時回 V2。緊急 rollback 路徑跟現有一致——人工切換 flag，不是 request-level fallback。

---

## 11. 測試與交付標準

### 新增測試類別

1. **Planner DAG 測試**：各種請求 → Planner 產出的 DAG 是否包含該跑的 agent + 正確依賴
2. **Sub-agent proposal 測試**：各 agent 收到 slice 後，產出的 tool call proposal 是否合法（schema、無偷渡 ID）
3. **中央 Guard 測試**：各種違規 proposal 是否被擋（schema 不符、偷渡 ID、重複、寫入無 confirmation）
4. **Scheduler 拓樸排序測試**：DAG 是否正確排序、平行層是否同時跑、依賴是否等待
5. **Confirmation 多筆獨立測試**：同回合多寫入、各自 CAS、stale 不連動
6. **失敗 skip 測試**：sub-agent 失敗 → 標記 → 依賴 skip → synthesizer 部分回覆
7. **隱私測試**：各 agent slice 不含禁止欄位、trace 不含 prompt/args/observation
8. **Trajectory 測試**：牛排例子等真實案例 → 完整流程跑通

### 既有的不變量全部保留

| `AGENTS.md` 規則 | V3 對應 |
|-----------------|---------|
| 規則 1：唯一 orchestrator | Scheduler 取代 V2 runtime，不造第二套 |
| 規則 2：LLM 做語意、程式做安全 | Planner/sub-agent 用 LLM，中央 Guard 純程式碼 |
| 規則 3：Tool Registry 單一入口 | sub-agent 的工具來自同一份 registry |
| 規則 4：副作用走 Domain Service | 不變 |
| 規則 5：所有寫入先確認 | 不變，且支援多筆獨立 confirmation |
| 規則 6：Context 只能由 context.py 建立 | Scheduler 切 slice 是延伸 |
| 規則 7：Profile Pipeline 分離 | 不變 |
| 規則 8：配對狀態真相 | 不變 |
| 規則 9：API/UI 相容性 | `/api/direct_chat` JSON 與 stream contract 不變 |
| 規則 10：Trace allowlist | 擴充但仍只存 code 與 metadata |

### 交付檢查

- Python source 必須 compile；主服務與 matchmaker 必須能啟動
- `GET /` 與 port 9001 health endpoint 必須為 200
- Test harness 必須使用 stub config 或 local test database；自動測試不得連線或修改正式 MongoDB Atlas／Neo4j
- 修改 runtime contract、tool list、state machine、環境旗標或 App migration 步驟時，必須同步更新 `AYUE_V2_ARCHITECTURE.md`

---

## 12. 檔案結構（預期）

```
social_demotest/services/ayue_agent/
├── runtime.py              # 保留 V2 loop（緊急 rollback）
├── v3/
│   ├── __init__.py
│   ├── scheduler.py        # Scheduler / Orchestrator（純程式碼）
│   ├── planner.py          # 輕量 Planner（LLM，產 DAG）
│   ├── guard.py            # 中央 Guard（純程式碼）
│   ├── synthesizer.py     # 最終綜合（LLM）
│   ├── context_slicer.py  # 從完整 context 切各 agent slice（純程式碼）
│   ├── sub_agents/
│   │   ├── __init__.py
│   │   ├── calendar_agent.py
│   │   ├── places_agent.py
│   │   ├── match_agent.py
│   │   ├── relationship_agent.py
│   │   └── profile_agent.py
│   └── contracts.py       # SubTask / Plan / SubTaskResult 等 typed contract
├── context.py             # 保留（唯一 Context Builder）
├── tool_registry.py       # 保留（單一 Tool Registry）
└── tools.py                # 保留（tool executor）
```