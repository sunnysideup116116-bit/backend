# 07. Central Guard：審核什麼、怎麼運作

> 本篇專門說明 V3 架構中的 Central Guard：它是誰、它**審核**哪些部分、它**不**做什麼，以及被拒絕後會發生什麼。程式碼真相在 `social/services/ayue_agent/v3/guard.py`。

## 1. Guard 是什麼

Guard 是 V3 sub-agent 架構中的**純程式碼安全閘門**，介於 Sub-agents（LLM）與工具執行之間：

```text
Planner (LLM, 拆 DAG)
  → Sub-agent (LLM, 提出 tool proposals)
  → Central Guard (純程式碼, 逐 proposal 審核)   ← 本篇主角
  → runtime-specific guarded adapter 注入 executor 參數 → execute_tool
  → Calendar Runtime 的 command/preflight path → prepare_write_confirmation
```

**核心定位**：

- **零 LLM**：Guard 不呼叫任何模型，不看語意、不猜意圖。它只做確定性（deterministic）檢查。
- **逐個 proposal 審核**：sub-agent 一次回覆可以產出多個 tool calls（例如同時查牛排與甜點），proposal runner 或 Web Runtime 對**每一個** proposal 呼叫一次 `guard_proposal`，一個失敗不影響其他。
- **拒絕即停止**：Guard 拒絕的 proposal 不會執行，也不會有 confirmation、progress 或副作用。

## 2. 審核哪些部分（GuardResultCode 全表）

`guard_proposal(proposal, *, agent_name, seen_keys, step_count, max_reads)` 依序執行以下檢查，**第一個失敗即回傳對應 code**：

| 順序 | 檢查 | 通過條件 | 失敗 code |
| --- | --- | --- | --- |
| 1 | 工具註冊 | tool 名稱存在於 `TOOL_REGISTRY` | `tool_not_registered` |
| 2 | Schema 驗證 | arguments 通過該工具的 `planner_arguments_model`（`planner_arguments_allowed`） | `schema_invalid` |
| 3 | 重複呼叫 | 同一 Public run 的 shared `seen_keys` 內沒有相同 `tool + normalized arguments`（執行前再以 executor arguments lock re-check） | `duplicate_call` |
| 4 | 步數上限 | 該 agent 的唯讀呼叫次數 < `max_reads`（`AYUE_SUBAGENT_MAX_READS`，預設 3） | `step_limit_exceeded` |
| 5 | 寫入確認 | READ 工具直接通過；**WRITE 工具一律拒絕**，改由 Scheduler 或 domain runtime 建立 confirmation | `write_requires_confirmation` |

### 額外的禁止欄位防線（Guard 之外、進入 Guard 之前）

- `FORBIDDEN_ARG_FIELDS = {user_id, match_id, event_id, revision, expected_status}` 由 `ToolProposal` 的 Pydantic validator 在**提案建立時**攔截（`contracts.py`）——Guard 的 schema 檢查是第二道防線（測試 `test_rejects_forbidden_arg_field` 證明 validator 先拒絕，Guard 做 backstop）。
- 這些欄位只能由 Scheduler／executor 從登入者與 canonical state 注入（`executor_arguments_for_turn`），模型永遠看不到。

## 3. Guard 不審核什麼（刻意不做）

| 不審核 | 原因 |
| --- | --- |
| 使用者意圖／自然語言 | 那是 Planner 與 sub-agents（LLM）的職責；Guard 不得累積 keyword regex |
| arguments 的「內容正確性」 | 例如日期合不合理、地點存不存在——那由 tool facade 與 domain service 判斷 |
| ID/revision 的實際值 | Guard 只確保模型**沒有**提供；值的注入與 CAS 是 Scheduler／executor 的事 |
| 隱私投影 | context slice（`context_slicer.py`）在 Guard 之前已切好，工具輸出驗證在 Guard 之後由 `output_model` 做 |
| Domain semantic equivalence | Guard 只比較 `tool_call_key`，不猜兩句自然語言是否等價；shared `seen_keys` 會跨同一 Public run 的 tasks 阻擋完全相同 key |

## 4. Guard 被拒絕之後（runtime 的處理）

一般 proposal runner 仍由 `scheduler.py:_run_sub_task` 對每個 proposal 處理；Calendar
由 `v3/calendar_runtime.py` 協調 command/preflight，Web 則由
`v3/guarded_execution.py:GuardedReadExecutor` 在 Web Runtime 內處理：

The registered runtime boundary uses one `TaskRunnerResult`: proposal results
return to this central guard, while completed Calendar/Web/ProductInfo results
already contain typed `SubTaskResult` observations. Scheduler does not inspect
domain command or preflight fields.

```text
decision = guard_proposal(proposal, agent_name=task.agent, seen_keys=..., step_count=..., max_reads=...)
trace["guard_results"].append(decision.code.value)   # 只記 code，不記 args

if not decision.ok:
    if code == WRITE_REQUIRES_CONFIRMATION:
        → prepare_write_confirmation（preflight）→ 建立 pending confirmation
        → observation = {pending_confirmation: True, preview}
    else:
        → SubTaskResult(FAILED, guard_code=decision.code)
        → 其他 proposals 照常執行；task 只要有任一 OK 即為 ok
```

**WRITE 的「拒絕」是設計行為**，不是錯誤：Guard 讓寫入永遠不可能直接執行，必須先確認。

## 5. Guard 之外的 runtime 檢查（同樣純程式碼）

Guard 通過後，runtime 還做三道確定性檢查（一般 runner 在 Scheduler，Calendar
command/preflight 在 `v3/calendar_runtime.py`，Web runner 在
`v3/guarded_execution.py`），也屬於「程式審核」範圍：

| 檢查 | 時機 | 失敗行為 |
| --- | --- | --- |
| `@` 綁定存在 | 工具 `argument_source ∈ {MENTIONED_RELATIONSHIP, MENTIONED_CONTACTS}` 但本回合無 server 驗證過的 mentioned_ids | `FAILED/mentioned_required` |
| 同 task 內結果重用 | 工具標 `reuse_success_within_turn`（目前 `places.measure_distance`）且已存在等價成功 observation | 標 `OK (reused prior observation)`，不重跑 |
| `web.extract` URL 綁定 | URL 不是「本回合 web.search 結果或 owner 原句提供的公開 URL」；Web Runtime 使用 `GuardedReadExecutor` | `FAILED/web_extract_url_not_bound` |

## 6. 測試

`tests/test_v3_guard.py` 覆蓋全部 GuardResultCode：pass、unknown tool、forbidden arg field（validator 層）、schema invalid、duplicate、step limit、write requires confirmation；`tests/test_v3_web_research.py` 另驗證 Web guarded adapter 的 executor projection、URL binding 與 runtime dispatch。新增任何 Guard 規則時必須同步補測試。

## 7. 總結

Guard 是「LLM 做語意判斷、程式做安全判斷」這條原則的具體落地：它保證模型輸出**結構上合法、不重複、不超限、不越權**，但永遠不試圖理解模型「想做什麼」。安全與狀態的最終裁決（確認、CAS、冪等）則交給 Scheduler 與 domain services。
