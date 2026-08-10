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

每個 sub-agent 只能收到 `context_slicer.py` 對該 agent 明確列出的欄位。新增 agent 時，必須新增獨立 slice 與 privacy test；不得把整個 `PublicAgentTurnContext` 直接交給 agent。

## 4. Planner interface

Planner 只呼叫一個 function：`decompose_tasks`。目前可產生兩種正常執行形狀：

```text
mode=direct_chat
  tasks=[]
  direct_reply 或 direct_messages（二擇一）

mode=tasks
  1..4 個 SubTask
  最多 3 個 domain task
  恰好 1 個 terminal synthesizer
```

`SubTask` contract：

```json
{
  "id": "t1",
  "agent": "calendar|places|web|match|relationship|profile|product_info|synthesizer",
  "depends_on": [],
  "task_brief": "完整目標與限制",
  "evidence_policy": null
}
```

只有 Web task 可帶 `evidence_policy=casual_discovery|strict_verification`。Planner 不選 tool arguments、不選 ProductInfo section、不執行工具，也不寫 final domain answer。

`Plan.mode="product_info"` 與 `product_info_topics` 只是在 `v3/contracts.py` 保留的 provider compatibility input；`normalize_plan_for_execution()` 會立刻轉成正常的 `product_info -> synthesizer` DAG。新 prompt、fixture 與文件不得產生這個舊 envelope。

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
| `relationship` | proposal runner | `sub_agents/relationship_agent.py` |
| `profile` | proposal runner | `sub_agents/profile_agent.py` |
| `product_info` | completed read-only specialist | `sub_agents/product_info_agent.py` |

`before_run`／`after_run` hook 只能做 bounded progress/debug projection，不能執行 domain write 或改寫 runner result。`direct_chat_blocker` 與 `confirmed_result_projector` 也必須留在擁有該語意的 runtime registration 旁。

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

Guard 只驗證 schema、註冊、重複呼叫、步數與 write-confirmation；不判斷自然語言 intent。相同 `tool + normalized executor arguments` 在同一 Public run 的 shared `seen_keys` 內不得重跑；唯讀 budget 依 task id 計算。

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

## 8. Observation and synthesis interface

`SubTaskResult` 只能是：

- `OK`：帶 bounded `observation`。
- `FAILED`：帶 allowlisted `error_code`／`guard_code`。
- `SKIPPED`：帶 `skip_reason`。

Observation 不是 user-facing prose。Synthesizer 是一般 DAG 的唯一 final wording owner，且只能使用它實際收到的 verified observations。Web URL、place card 與 subject reference 由 server-owned catalog 綁定；模型不能創造新的 reference。

ProductInfo 固定輸出 `product_info.v1` observation；Web 固定輸出 `web_research.v1` observation。這些 `.v1` 是 payload schema，不代表舊 runtime。

## 9. 新增或修改 interface 的交付要求

1. 先確認 owner，避免把 domain policy搬回 Scheduler 或 router。
2. 修改 typed contract，再修改 runtime/adapter；不要以 prompt 自由文字代替 schema。
3. 補 Planner trajectory、runtime contract、Guard、projection、privacy、budget 與 fallback tests。
4. 同步更新本文件、`AYUE_V3_ARCHITECTURE.md`、相關 sub-agent 文件與 `README.md` 索引。
5. 若保留 compatibility adapter，必須寫明接受什麼舊輸入、在哪一層正規化、何時可移除；新程式不得繼續產生舊形狀。
