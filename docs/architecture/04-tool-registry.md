# 04. Tool Registry：能力與安全契約

> 本篇說明公開阿月的全部工具契約。**`social_demotest/services/ayue_agent/tool_registry.py` 是能力的唯一入口**：新增任何能力都必須先在此註冊，並同步更新 `tools.py`（唯讀 facade）或 `write_executors.py`（寫入執行）與 Planner prompt。

## 1. ToolSpec 欄位

每個工具以 `ToolSpec` 定義：

| 欄位 | 意義 |
| --- | --- |
| `name` | 工具名稱（`domain.action` 形式） |
| `risk` | `ToolRisk.READ` 或 `ToolRisk.WRITE` |
| `executor_key` | 對應 `tools.py:execute_tool` 分派表中的 key，或 `write_executors.py` 的執行器 |
| `description` | 給模型的描述（Planner/sub-agent 用它決定何時呼叫） |
| `progress_text` | 執行時顯示的 progress bubble 文案 |
| `requires_confirmation` | 寫入工具一律 `True` |
| `planner_arguments_model` | 模型可提供的 arguments schema（**禁止**含 ID/revision 欄位） |
| `executor_arguments_model` | executor 實際使用的 schema（由 runtime 注入 ID/revision 與 mention 綁定） |
| `output_model` | 工具結果必須通過的 typed projection（`extra="forbid"`） |
| `argument_source` | 參數來源：`NONE`、`PLANNER_GROUNDED`、`MENTIONED_RELATIONSHIP`、`MENTIONED_CONTACTS` |
| `reuse_success_within_turn` | 同 sub-task 內相同結果是否重用 |

**禁止模型提供的欄位**（`FORBIDDEN_ARG_FIELDS`）：`user_id`、`match_id`、`event_id`、`revision`、`expected_status`。

## 2. 唯讀工具（16 個）

| 工具 | executor_key | Planner 參數 | 回傳（output_model） | argument_source |
| --- | --- | --- | --- | --- |
| `system.get_current_time` | `current_time` | 無 | clock（台北時間＋相對日期） | NONE |
| `calendar.list_my_events` | `calendar_events` | `start_date`/`end_date`（YYYY-MM-DD，可含過去；不填預設未來 90 天）、legacy `date`/`range_label` | events[]（date/start_time/end_time/activity/status）+ range | PLANNER_GROUNDED |
| `calendar.find_my_event` | `calendar_event_find` | `event_hint`、`date_hint`、`companion_hint`、`limit`(1–30) | found/not_found/ambiguous + candidates[]（含 companion 公開名稱） | PLANNER_GROUNDED |
| `match.get_status` | `match_status` | 無 | 唯一配對狀態 snapshot（state/scope/chat_opened/revision 等） | NONE |
| `match.get_counterparty_summary` | `counterparty_summary` | 無 | 目前有效或已接受配對的公開對象摘要（非 accepted 時匿名化） | NONE |
| `profile.get_recent_context` | `recent_context` | 無 | 本人已保存近期情境 + revision | NONE |
| `profile.get_self_summary` | `self_profile` | 無 | 本人 profile 投影（Big Five、deep profile、偏好、missing_sections） | NONE |
| `relationship.get_verified_evidence` | `relationship_evidence` | 無（executor 注入 `other_id`） | 已接受配對的可驗證互動摘要 | MENTIONED_RELATIONSHIP |
| `relationship.get_mentioned_contact_summary` | `mentioned_contact_summary` | 無（executor 注入 `other_ids` ≤3） | @ 對象的公開摘要 | MENTIONED_CONTACTS |
| `relationship.list_accepted_contacts` | `accepted_contact_list` | 無 | 已接受聯絡人最小公開清單（≤8 + truncated） | NONE |
| `memory.search_my_profile` | `memory_profile` | 無 | 本人記憶摘要 + 近期情境 + preferences(≤8) | NONE |
| `web.search` | `web_search` | `query`(2–300)、`recency`、`use_saved_location` | results[]（title/url/snippet/published_date） | PLANNER_GROUNDED |
| `web.extract` | `web_extract` | `urls`(1–2)、`query` | pages[]（url/content/truncated） | PLANNER_GROUNDED |
| `places.search_nearby` | `places_nearby` | `anchor`、`categories`(1–3, 5 種)、`cuisine`、`radius_m`(300–5000)、`limit`(1–10)、`use_saved_location` | 地點卡 list（name/category/distance_m/map_url/place_id/photo_url 等） | PLANNER_GROUNDED |
| `places.measure_distance` | `places_distance` | `origin`、`destination`、`use_saved_origin` | 距離 + 耗時文字（reuse_success_within_turn） | PLANNER_GROUNDED |
| `places.resolve_place` | `places_resolve` | `query`(2–160) | 單一地點卡（found + place + attribution） | PLANNER_GROUNDED |

唯讀工具只能回傳完成問題所需的最小 typed projection；不傳 Mongo document、raw profile 或內部 ID。

## 3. 寫入工具（7 個，全部需要確認）

| 工具 | executor_key | Planner 參數 | 確認後執行路徑 |
| --- | --- | --- | --- |
| `match.start_search` | `start_search` | 無 | `_start_search` → `start_match_search`（入 job 佇列） |
| `match.decide_active_proposal` | `decide_active_proposal` | `decision`(interested/declined) | `_decide_active_proposal` → revision CAS |
| `profile.start_assessment` | `assessment_start` | `kind`(basic/deep) | `_start_assessment` → session 啟動 |
| `calendar.create_my_event` | `calendar_create` | `title`、`date`、`start_time`、`end_time`、`timezone`(預設 Asia/Taipei)、`location`、`notes` | `_calendar_execute` → `create_personal_event` |
| `calendar.update_my_event` | `calendar_update` | `event_hint` + 可改欄位 | `_calendar_execute` → 私人：`update_personal_event`；共同約會：`request_reschedule` |
| `calendar.cancel_my_event` | `calendar_cancel` | `event_hint` | `_calendar_execute` → `cancel_event` / `cancel_coordination_or_event` |
| `calendar.cancel_my_events` | `calendar_cancel` | `mode`(selected/all_upcoming) + `event_hints`(2–10) | `_calendar_execute`（batch）→ 逐筆取消，先驗證 `cancel_targets_are_current` |
| `calendar.submit_commands` | `calendar_commands` | `commands`（1–10 個 authority-free `CalendarCommand`） | Scheduler deterministic preflight → server-owned `CalendarMutationPlan` → `_execute_calendar_mutation_plans`，依序執行、stop-on-failure |

## 4. 工具可見性（哪些 agent 看得到哪些工具）

工具可見性由各 sub-agent 的 `_TOOLS` 決定（見各 `subagent-*.md`）：

- `READ_ONLY_TOOLS`：預設公開的唯讀面（排除 web/places 三工具與 mention summary）。
- `WEB_TOOLS` = `{web.search, web.extract}`；`PLACES_TOOLS` = `{places.search_nearby, places.measure_distance, places.resolve_place}`。
- 只有 `places_agent` 同時有 web + places 工具。
- `relationship.get_mentioned_contact_summary` 只在 server 驗證過 accepted @ mention 的回合才可見（`planner_tool_names(can_read_mentioned_contacts=...)`）。
- 明確配對請求（例如「再配對一次」）必須由 Planner 建立 `match` task；`opportunity.social_opening` 只產生短期溫和提議 observation，不建立 confirmation。

## 5. 執行與驗證流程

```text
sub-agent LLM function calling 輸出 {tool_name, arguments}
  → base.py: _repair_categories（僅 places.search_nearby）
  → planner_arguments_allowed(spec, args)（planner schema 驗證）
  → ToolProposal（攔截 FORBIDDEN_ARG_FIELDS）
  → Guard（registry/schema/duplicate/steps/write）
  → 唯讀: executor_arguments_for_turn → execute_tool → output_model 驗證
  → 寫入: prepare_write_confirmation（preflight）→ pending → 確認後 execute_write
```

相同 `tool + normalized arguments` 在同一 sub-task 內不得重跑：`tool_call_key(spec, safe_args)`（executor 參數的排序 JSON）在 lock 內檢查並加入 `seen_keys`。每個 sub-agent 最多三個唯讀步驟；每回合最多一筆待確認副作用（calendar 例外：同 sub-task 多筆可合併進同一 confirmation 的 `batch` 欄位）。

## 6. 新增工具檢查清單

唯讀：

1. `tool_registry.py` 建立嚴格 Pydantic input/output schema 與 `ToolSpec`（`READ`）。
2. `tools.py` 建立最小、privacy-safe projection；資料真相由既有 domain service 提供。
3. 更新 Planner prompt 讓模型知道何時使用（不加 keyword visibility router）。
4. 新增 registry、tool、planner sequence、privacy、duplicate-call 與 trajectory tests。

寫入：

1. 先建立或擴充 canonical domain service。
2. 定義 ownership、合法 state transition、revision/CAS、idempotency 與 stale response。
3. Registry 標記 `WRITE` + `requires_confirmation=True`。
4. `write_executors.py` 註冊執行器（preflight + execute）。
5. 加入重複請求、stale revision、雙方並發、終態不可覆寫與 effect failure tests。

修改 runtime contract、tool list 或 state machine 時，必須同步更新 `AYUE_V3_ARCHITECTURE.md` 與本文件。

## Current Calendar Agent mutation contract

`calendar.submit_commands` is the registered typed intent entry point for the V3 Calendar Agent. Its `CalendarCommand` schema has no `user_id`, `event_id`, `revision`, `expected_revision`, or `coordination_id`. Scheduler preflight resolves targets once and creates the server-owned `CalendarMutationPlan`; plans never return to the model. Missing fields, ambiguous, and not-found outcomes are normal clarification results. Legacy calendar write tools remain only for compatibility.

`calendar.get_next_my_event` is a bounded read for “最近一筆／最近有啥行程”. Its output is a safe event projection; the executor stores the canonical event reference privately so a subsequent pronoun follow-up can use `target_reference="recent_event"` without another natural-language lookup.
