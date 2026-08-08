# 08. Planner：任務拆解器

> 本篇說明 V3 架構中的 Planner：它是誰、怎麼把一句話變成子任務 DAG、輸出契約、失敗行為，以及與其他層的關係。程式碼真相在 `social_demotest/services/ayue_agent/v3/planner.py`。

## 1. Planner 是什麼

Planner 是 V3 架構中的**輕量 LLM**，職責只有一個：把使用者這一回合的訊息拆解成一張**靜態子任務 DAG**。

```text
使用者訊息 → Scheduler → Planner (LLM) → Plan (DAG) → Scheduler 執行
```

**刻意不做**的事：

- 不填工具參數（arguments 由 sub-agents 各自以 function calling 提出）。
- 不審核（審核是 Central Guard 的職責，見 `07-guard.md`）。
- 不回覆使用者（回覆是 Synthesizer 的職責）。
- 不提供 `user_id`、match/event ID、revision 或 `expected_status`（這些欄位在 `Plan`/`SubTask`/`ToolProposal` 契約中根本不存在，模型無法輸出）。

## 2. 輸出契約：decompose_tasks

Planner 透過**單一 function calling** `decompose_tasks` 輸出，tool call 的 arguments 就是 typed `Plan`（`v3/contracts.py`），不做自由文字 JSON 解析：

```json
{
  "tasks": [
    {"id": "t1", "agent": "calendar", "depends_on": [], "task_brief": "查詢這週日本人行事曆是否有空檔"},
    {"id": "t2", "agent": "places", "depends_on": [], "task_brief": "搜尋三民區附近的牛排餐廳"},
    {"id": "t3", "agent": "synthesizer", "depends_on": ["t1", "t2"], "task_brief": "綜合行程衝突與餐廳推薦回覆"}
  ],
  "opportunity": {"signal": "none", "evidence_span": "", "confidence": 0.0}
}
```

- `agent` 只能是：`calendar | places | match | relationship | profile | synthesizer`。
- 每個 task 有 `id`、`agent`、`depends_on[]`、`task_brief`（給該 sub-agent 的簡短中文指示）。
- `opportunity` 為選用：`signal`（`none`/`social_opening`）、`evidence_span`（原句連續子字串）、`confidence`（0.0–1.0）。

### Plan 的 DAG 驗證（純程式碼，`contracts.py:Plan`）

模型輸出先經 `_DecomposeTasksArguments.model_validate`，再過 `Plan` 的 model_validator：

1. task id 不可重複。
2. `depends_on` 必須指向存在的 task id。
3. synthesizer 必須是**終端**：不能被任何其他 task 依賴。
4. 至少一個 task。

任何一項失敗 → `plan_turn` 回 `None` → Scheduler **fail closed**（不執行任何工具，直接回覆「我現在沒辦法判斷這個請求要不要執行」）。

## 3. Planner 的輸入（_planner_prompt）

`planner.py:_planner_prompt` 組出 prompt，內含：

| 欄位 | 內容 |
| --- | --- |
| `message` | 本次 owner 原始訊息 |
| `recent_messages` | 最近對話（已清理） |
| `recent_context` | 本人已保存近期情境 |
| `user_location` | 本人手動保存的粗略所在地 |
| `relevant_memories` | 本人相關記憶（≤8） |
| `clock` | 本回合 Asia/Taipei clock 與相對日期解析 |
| `active_proposal` | 唯一可操作提案的安全狀態（無內部 ID） |
| `mentioned_contacts` | 本回合 server 驗證過的 @ 已接受聯絡人公開名稱 |
| `pending_confirmations` | 該使用者目前有效的 pending confirmation 摘要 |

Prompt 中列出六個 sub-agent 的用途描述（`_AGENT_DESCRIPTIONS`），並給 14 條拆解規則，重點包括：

1. **平行原則**：可平行時 `depends_on=[]`；彼此不能互相依賴。
2. **synthesizer 必為終端**：必須依賴所有需要彙整的 task。
3. **agent 選擇**：行程→calendar；地點→places；配對→match；@ 對象/關係→relationship；偏好/近期情境或開始／重新開始 assessment→profile；不確定→只回 synthesizer。
4. **新增行程**：單一 calendar task 即可，不必先查詢（task_brief 直接描述要新增的行程）。
5. **修改/取消行程**：單一 calendar task 直接描述完整 mutation；由 Calendar Agent 產生 typed command，server preflight 唯一 resolve target。
6. **Opportunity**：使用者表達想有人陪、想認識人或獨自參加不舒服時，在 `opportunity` 填 `social_opening` + 原句 `evidence_span` + `confidence ≥ 0.8`；單純旅行、寒暄或負面情緒一律 `none`。
7. **Assessment routing**：做／開始／重新做基本性格或深層探索時，必須建立 profile task，不得只建立 synthesizer task。
8. **只呼叫 `decompose_tasks`**，不輸出其他文字。

## 4. 呼叫流程（plan_turn 內部）

```text
Scheduler: plan_turn(turn_ctx, pending_confirmations=...)

Current contract corrections:

- An explicit request to start/retry/search for a match (for example, 「再配對一次」) must produce a `match` task. It must not be represented only as `opportunity.social_opening`.
- `opportunity.social_opening` is a soft, non-actionable suggestion. Scheduler may show it only as an observation; it does not create a confirmation and must not claim that matching started.
- Calendar update/cancel follow-ups use the server-owned `calendar_recent_reference` when the user refers to the event just shown. The Planner/Calendar Agent may emit `target_reference="recent_event"`; it never emits authority fields.
  → 組 prompt（含本回合 context）
  → generate_chat_completion_with_tools(prompt, [decompose_tasks schema], temperature=0)
  → 檢查 tool_calls:
      - 無 tool call            → (None, metrics)  → fail closed
      - 名稱不是 decompose_tasks → (None, metrics)  → fail closed
      - arguments 無法通過 _DecomposeTasksArguments 驗證 → (None, metrics) → fail closed
      - 例外（逾時等）           → (None, metrics)  → fail closed
  → 組 Plan（含 opportunity）→ 回傳
```

## 5. Scheduler 如何使用 Plan

1. **Opportunity 驗證**：`signal == "social_opening"` 且 `confidence ≥ 0.8` 且 `evidence_span` 是原句連續子字串才採用；`ready` 時只產生短期 soft-offer observation，不建立 confirmation。明確開始／重試配對（例如「再配對一次」）必須建立 `match` task，走一般 confirmation。
2. **拓撲分層**：`_topological_layers` 依 `depends_on` 分層，同層平行執行（`AYUE_SUBAGENT_MAX_PARALLEL` 預設 5）。
3. **Prior observation 只給依賴**：`_prior_observations_for` 只回傳該 task 宣告的 `depends_on` 的結果——同層無依賴的任務互不可見；synthesizer 例外（彙整全部）。
4. **依賴失敗**：該 task 標記 `SKIPPED (dependency_failed)`，不執行。

## 6. 與 Sub-agents 的分工

| | Planner | Sub-agents |
| --- | --- | --- |
| 輸入 | 整回合 context（含記憶、clock、提案） | 自己的 context slice + task_brief + prior observations |
| 輸出 | `Plan`（誰來做、順序、做什麼） | `ToolProposal` 列表（具體呼叫哪個工具、填什麼參數） |
| LLM 呼叫 | 一次（decompose_tasks） | 每 task 一次（可多 tool calls） |
| 失敗 | fail closed，整回合不執行 | 單一 task 失敗不影響其他 task |

## 7. 測試

`tests/test_v3_planner.py` 覆蓋：牛排範例產生 calendar+places+synthesizer DAG、簡單聊天只產 synthesizer、無 tool call / 錯工具名 / invalid args / timeout → `None`、opportunity 攜帶與驗證。新增拆解規則時必須同步補 planner sequence 測試（不得只靠真實模型手測）。

## 8. 總結

Planner 是 V3「LLM 做語意判斷」的**最小**實現：它只回答「這句請求需要誰、以什麼順序做」，不接觸工具參數、不審核、不回覆。參數由 sub-agents 填、審核由 Guard 做、執行由 Scheduler 編排、回覆由 Synthesizer 產生——每一層的職責都不可越界。
