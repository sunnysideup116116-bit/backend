# Sub-agent：calendar（行事曆子代理）

> 本文說明使用者與阿月談「行程」時，背後怎麼運作：calendar sub-agent 能做什麼、呼叫哪些 function、參數長什麼樣、寫入如何確認、以及端到端範例。

## 1. 角色與能做什麼

**系統角色**（`v3/sub_agents/calendar_agent.py` 的 `_SYSTEM`）：

> 你是公開阿月的行事曆子代理：負責查看與管理本人行程。

能力：

- **查詢**本人行事曆（列表／找單筆），判斷空檔、日期與忙碌時段。
- **新增**私人行程（一次可新增多筆，同一回覆提出多個 `calendar.create_my_event` 呼叫）。
- **修改**一筆行程（私人行程直接改；共同約會提出「改期」並通知對方重新確認）。
- **取消**一筆或多筆行程（共同約會同步雙方行事曆並通知對方）。
- 相對時間詞（這個月、本週、明天…）由 agent 依 clock 自行換算成具體 `YYYY-MM-DD`。

**不負責**：不讀取對方行事曆；不猜測配對對象的行程。

## 2. 可呼叫的工具（6 個）

| 工具 | risk | 用途 |
| --- | --- | --- |
| `calendar.list_my_events` | READ | 列出指定區間的行程與忙碌時段 |
| `calendar.find_my_event` | READ | 找單筆行程（含候選與共同約會對象公開名稱） |
| `calendar.create_my_event` | WRITE | 新增私人行程（需確認） |
| `calendar.update_my_event` | WRITE | 修改一筆行程（需確認） |
| `calendar.cancel_my_event` | WRITE | 取消一筆行程（需確認） |
| `calendar.cancel_my_events` | WRITE | 取消多筆行程（需確認） |

### 2.1 `calendar.list_my_events`（READ）

Planner 參數（`_CalendarListArguments`）：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `start_date` | str（YYYY-MM-DD） | 查詢起日，可含過去 |
| `end_date` | str（YYYY-MM-DD） | 查詢迄日 |
| `date` | str（YYYY-MM-DD） | Legacy：單日 |
| `range_label` | str | Legacy：今天/本週/本月… |

不填範圍時系統預設未來 90 天。回傳 `{events: [{date, start_time, end_time, activity, status}], range}`。

範例（「這個月我有什麼行程？」）：

```json
{"tool_name": "calendar.list_my_events", "arguments": {"start_date": "2026-08-01", "end_date": "2026-08-31"}}
```

### 2.2 `calendar.find_my_event`（READ）

Planner 參數（`_CalendarFindArguments`）：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `event_hint` | str（必填, ≤120） | 描述「原本那筆行程」（活動名，可帶日期/時間） |
| `date_hint` | str（≤32） | 日期輔助 |
| `companion_hint` | str（≤30） | 已接受聯絡人公開名稱（縮小共同約會） |
| `limit` | int（1–30, 預設 10） | 候選上限 |

回傳 `{status: found|not_found|ambiguous, ...}`：`ambiguous` 時帶 `candidates[]`（activity/date/start_time/end_time/location/notes/event_kind），由 agent 逐一比對；`not_found` 的查詢會交給 Synthesizer 優雅回覆（不會當成寫入失敗）。

### 2.3 寫入工具（WRITE，先確認）

| 工具 | Planner 參數 | 說明 |
| --- | --- | --- |
| `calendar.create_my_event` | `title`(≤120, 必填)、`date`(YYYY-MM-DD)、`start_time`(HH:MM)、`end_time`(HH:MM)、`timezone`(預設 Asia/Taipei)、`location`(≤160)、`notes`(≤500) | 不填 `event_hint` |
| `calendar.get_next_my_event` | 無 | 回傳最近一筆安全 projection，並建立短期 server-owned recent reference |
| `calendar.update_my_event` | `event_hint` 或 `target_reference="recent_event"` + 可改欄位（title/date/start_time/end_time/timezone/location/notes） | 代名詞 follow-up 使用 recent reference，不重新組自然語言 hint |
| `calendar.cancel_my_event` | `event_hint` 或 `target_reference="recent_event"` | 同上 |
| `calendar.cancel_my_events` | `mode`(selected/all_upcoming) + `event_hints`(selected 需 2–10 筆) | all_upcoming 最多取消 10 筆未來行程 |

**agent 規則**（來自 `_SYSTEM`）：寫入只提交 authority-free typed commands，不會直接執行，系統會先做 deterministic preflight 與 confirmation。修改／取消不先拆 read task，也不把 read observation 重組成 `event_hint`；需要查詢內容時才提出 read。一次訊息中的多個 mutation 可放在同一 command batch，確認後依序執行。

## 3. 呼叫流程（背後怎麼運作）

```text
使用者: 「把 8/25 的雞排約會改到 8/15，順便新增一筆 8/20 看牙醫」
  │
  ▼ Planner (decompose_tasks)
  建立一個 calendar task，完整描述兩筆 mutation；Calendar Agent 產生 typed commands，server 再做唯一 preflight。
  └─ t1「寫入任務」: 修改雞排約會並新增 8/20 看牙醫 (depends_on: [])
  │
  ▼ Scheduler → slice_for_agent("calendar", ...)
  slice payload: message + recent_messages + clock + recent_context + prior_observations
  │
  ▼ calendar_agent.run → base.run_sub_agents
  LLM function calling (temperature=0)，一次可能輸出多個 tool calls
  │
  ▼ Guard（純程式碼）
  每個 proposal 驗證: registered / schema / duplicate / 步數上限 / write→confirmation
  │
  ▼ 唯讀工具（只有使用者真的詢問行程內容時）: executor_arguments_for_turn → tools.py:execute_tool
  calendar.list_my_events → _calendar_events → calendar_service.get_calendar_context
  calendar.find_my_event   → _calendar_find_event → calendar_service.find_owned_events
  │
  ▼ calendar.submit_commands → deterministic preflight
  canonicalize relative date（使用 authoritative clock）→ resolve target（需要時只做一次）→ 組 server-owned CalendarMutationPlan
  → 寫入 v3_pending_confirmations（TTL 900s；同 sub-task 多筆 calendar 寫入合併 batch）
  │
  ▼ Synthesizer 回覆使用者:
  「要把『8/25 17:00–20:00 雞排約會』改成 8/15 17:00–20:00「雞排約會」嗎？…
  要新增 8/20 09:00–10:00「看牙醫」嗎？回覆『確認』才會真的變更。」
  │
  ▼ 使用者回覆「確認」
  ConfirmationManager.execute_confirmed（CAS pending→executing）
  → execute_write → _calendar_execute（batch 逐筆, 每筆 agent_action_key 冪等）
      ├─ update_my_event → update_personal_event（私人）或 request_reschedule（共同約會）
      └─ create_my_event  → create_personal_event
  → Synthesizer 產出「已處理：…」回覆
```

## 4. 寫入確認的細節

- **Preflight（`v3/calendar_commands.py:preflight_calendar_commands`）**：
  - `create`：以 authoritative clock 將「今天／明天／後天」正規化，再驗證欄位、時間區間與 `conflicts_for_viewer`。
  - `update/cancel`：使用 typed recent reference 時直接 resolve canonical event；其他情況才以 `event_hint` 做唯一 resolver。`ambiguous`／`not_found`／缺少欄位／無法正規化日期都回傳 `needs_clarification`，不建立 confirmation。
  - `cancel_my_events`：保留 bounded selected/all_upcoming contract，產生多筆 server-owned plans。
  - ready 時只把不含 authority fields 的 preview 傳給使用者；canonical event ID/revision 留在 server-owned pending payload。
- **執行（`_calendar_execute`）**：
  - 私人行程：`create_personal_event` / `update_personal_event` / `cancel_event`（都帶 `expected_revision` 與 `agent_action_key`）。
  - 共同約會（`source_type=date`）：`request_reschedule`（提出改期，對方確認後才變更）或 `cancel_coordination_or_event`（同步取消雙方）。
  - 409（stale revision）→ 回「這筆行程剛剛有變動，我沒有覆寫它」；不覆寫終態。
- **Batch 合併**：同 sub-task 內多筆 calendar 寫入共用一個 confirmation_id（`batch` 陣列），一次「確認」執行全部；`ConfirmationManager` 也會把其他 pending 的 calendar confirmation 併入同一 batch。非 calendar 寫入每回合最多一筆。

## 5. 端到端範例

**使用者**：「我這星期天想和小李吃晚餐，幫我看看我那天有沒有空，有的話找附近牛排。」

**Planner** 拆 DAG（依 planner 規則）：

```json
{
  "tasks": [
    {"id": "t1", "agent": "calendar", "depends_on": [], "task_brief": "查詢這週日（8/9）本人行事曆是否有空檔"},
    {"id": "t2", "agent": "places", "depends_on": [], "task_brief": "搜尋三民區附近的牛排餐廳"},
    {"id": "t3", "agent": "synthesizer", "depends_on": ["t1", "t2"], "task_brief": "綜合行程衝突與餐廳推薦回覆"}
  ]
}
```

t1 與 t2 平行執行：

- **t1 (calendar)**：LLM 輸出 `{"tool_name": "calendar.list_my_events", "arguments": {"start_date": "2026-08-09", "end_date": "2026-08-09"}}` → Guard 通過 → `execute_tool` 回傳當天行程（例如 18:00–21:00 電影）。
- **t2 (places)**：見 `subagent-places.md`。
- **t3 (synthesizer)**：拿到 calendar observation（8/9 晚有電影）＋ places observation（牛排店清單），回覆：「這星期天 8/9 你晚上 6 點有電影，晚餐可能比較趕；附近有三民區的岩炙炭燒牛排（約 726 公尺）… 要先幫你改行程或換一天嗎？」

**使用者**：「那把電影改到 8/10 吧。」

- Planner 只建立一個 calendar mutation task；Calendar Agent 直接提出含自然語言 target hint 的 typed command，Scheduler preflight 負責唯一 resolve。
- preflight resolve 成 canonical event 後建立 server-owned `CalendarMutationPlan`；canonical event ID/revision 不會回到 LLM，也不會再由 observation 重組 `event_hint`。
- 阿月回：「要把『8/9 18:00–21:00 電影』改成 8/10 18:00–21:00「電影」嗎？回覆『確認』才會真的變更。」
- 確認後 `update_personal_event` 執行，回「已更新行程：8/10 18:00–21:00 電影。」

## Current V3 mutation contract

The current implementation uses `calendar.submit_commands` for Calendar Agent writes. The agent emits authority-free `CalendarCommand` values only. Scheduler preflight resolves each mutation target once and creates server-owned `CalendarMutationPlan` values containing canonical IDs and revisions; those plans never enter an LLM context. Missing fields, ambiguous targets, not-found targets, invalid provider command shapes, and stale recent references return normal clarification outcomes. A ready batch gets one confirmation and executes sequentially with stop-on-first-failure and no automatic rollback. The older `calendar.*` write payloads remain only as a compatibility path.

The typed boundary accepts canonical `action/title/target_hint/target_hints`. A closed compatibility adapter maps common provider spellings (`type`, `summary`, `event_hint`, `event_hints`) before strict validation. A unique Calendar read records a 15-minute server-owned recent reference, while incomplete create/update commands record an authority-free 15-minute draft. Both are projected to the model without event IDs or revisions; a follow-up such as 「這筆刪掉」uses the reference directly and never re-runs natural-language resolution.
The runtime persists these short-lived records in the bounded Mongo collections by default (`AYUE_CALENDAR_STATE_MONGO=on`) and always keeps an in-process fallback for tests/offline demos. A unique `calendar.get_next_my_event` read is the preferred path for “最近一筆／最近有啥行程”; it records the same server-owned recent reference used by a follow-up “他／她／它／這筆” mutation.
