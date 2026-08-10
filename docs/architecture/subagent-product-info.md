# Sub-agent：product_info（產品資訊子代理）

> 現行 owner：`services/ayue_agent/v3/sub_agents/product_info_agent.py`。ProductInfo 是 Public V3 DAG 的 first-class read-only specialist；它不是 Planner 的 topic mode，也不是直接回覆使用者的 FAQ router。

## 1. 責任邊界

ProductInfo 處理阿月或 App 本身的問題：

- 阿月能做什麼、Public／Private surface 如何分工。
- 產品可見流程、限制與隱私邊界。
- 配對如何使用既有 profile、如何開始搜尋、為何仍需確認。
- Calendar 與 assessment 的產品行為。

Planner 只建立一個 `agent="product_info"` 的 `SubTask`，把使用者 proposition 放進 `task_brief`。Planner 不知道 section ID，也不直接輸出產品答案。

```text
Planner
  -> product_info task
ProductInfoAgent
  -> bounded product knowledge retrieval
  -> product_info.v1 observation
Synthesizer
  -> user-facing reply
```

## 2. Input interface

ProductInfo 的 `AgentContextSlice` 只包含：

- 當回合 message（bounded）。
- 最多四則 recent messages。
- server-owned product knowledge catalog。

它不接收 owner profile、行事曆內容、relationship-private context、raw database document 或內部 ID。

正式 runner 介面遵守 `RuntimeRegistration`：

```python
run(context_slice, *, task, services)
    -> tuple[TaskRunnerResult, SubAgentMetrics]
```

ProductInfo 不產生 `ToolProposal`；它回傳 `TaskRunnerResult.from_completed(...)`。

## 3. Retrieval interface

產品真相只來自 `services/ayue_agent/capabilities.py` 的 allowlisted product knowledge。Retrieval：

- 最多 2 rounds。
- 最多 6 個 knowledge sections。
- deterministic、read-only，不使用 Web/RAG/vector search。
- section hint 只存在 ProductInfo 內部，不是 Planner taxonomy，也不能變成全域 keyword router。

若沒有 authoritative section，回 `coverage="insufficient"` 與 `failure_code="product_knowledge_insufficient"`，不得憑常識補造產品能力。

## 4. Output interface：`product_info.v1`

核心 observation：

```json
{
  "product_info": {
    "schema_version": "product_info.v1",
    "question_understanding": {
      "request_shape": "product_behavior_question",
      "domains": [],
      "retrieval_rounds": 1
    },
    "facts": {},
    "knowledge_sections": [],
    "unknown_sections": [],
    "coverage": "sufficient|insufficient",
    "failure_code": null,
    "retrieval": {"rounds": [], "max_rounds": 2}
  }
}
```

`manifest_version` 與 `topics` 是 rollout compatibility fields；新 consumer 應使用 `knowledge_sections`、section-keyed `facts` 與 `coverage`。

## 5. Progress 與 debug

`before_run`／`after_run` 會投影一個虛擬 process step：

```text
product_info.process
```

Public stream 只看到 allowlisted `tool_started`／`tool_finished` envelope；localhost debug 才能看到 `process="bounded_product_knowledge"`、task brief 與安全 input projection。這個 process 只是可觀測性，不是 Tool Registry capability，也不消耗 Web/tool budget。

## 6. Compatibility boundary

`Plan.mode="product_info"` 與 `product_info_topics` 是舊 provider payload 的相容輸入。`normalize_plan_for_execution()` 會把它轉成：

```text
product_info -> synthesizer
```

新 Planner prompt、fixture、測試與文件不得產生 task-free ProductInfo mode。
