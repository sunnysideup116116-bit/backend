# Sub-agent：relationship（關係子代理）

> 本文說明使用者與阿月談「已建立聯絡的人／@ 對象」時，背後怎麼運作：relationship sub-agent 能做什麼、呼叫哪些 function、@ 綁定如何驗證。

## 1. 角色與能做什麼

**系統角色**（`v3/sub_agents/relationship_agent.py` 的 `_SYSTEM`）：

> 你是公開阿月的關係子代理：負責查看已建立聯絡的對象與提及聯絡人摘要。

能力：

- **列出與計數**本人已接受／已建立聯絡人（最小公開清單；若 `total_count` 有值可回答精確總數）。
- **比較與推薦**現有已接受聯絡人；推薦範圍只限這份 bounded 清單能支持的對象。
- **查詢 @ 對象**的公開摘要（本回合 server 驗證過的 accepted @ mention）。
- **讀取**已接受配對的可驗證互動摘要（relationship evidence）。

**不負責**：不讀對方私人資料、行事曆或目前 availability；不能用公開 relationship projection 推測對方現在是否有空或正在做什麼；`@` 不是每次自動讀資料——只有語意需要公開資料時才呼叫工具。

## 2. 可呼叫的工具（3 個）

| 工具 | risk | 用途 | argument_source |
| --- | --- | --- | --- |
| `relationship.list_accepted_contacts` | READ | 列出已接受聯絡人最小公開摘要 | NONE |
| `relationship.get_mentioned_contact_summary` | READ | 讀本回合 @ 的已接受聯絡人公開摘要 | MENTIONED_CONTACTS |
| `relationship.get_verified_evidence` | READ | 讀已接受配對的可驗證互動摘要 | MENTIONED_RELATIONSHIP |

### 2.1 `relationship.list_accepted_contacts`（READ，無參數）

回傳（`_AcceptedContactListOutput`）：`contacts[]`（≤8）＋`truncated`＋可能存在的 `total_count`。每個 contact：`display_name`、`recent_context`、`initial_interest`、`personality_summary`、`safe_match_reason`、`verified_common_ground`、`distinctive_tags`。

適用：「我已經配到哪些人？」、「這幾位已建立聯絡的人中誰適合看電影？」——只回最小公開投影，不能拿來推測對方目前是否有空。

Bounded-list semantics：

- `truncated=false` 時，清單代表目前可回傳的全部 accepted contacts，可在此範圍內比較或推薦。
- `truncated=true` 時，`total_count` 有值即可回答精確總數；比較／推薦只能表述為「在目前返回的聯絡人中」，不能宣稱是所有 accepted contacts 中的最佳結果。若使用者要求全體最佳，應先說明範圍限制或請使用者縮小範圍。

範例：

```json
{"tool_name": "relationship.list_accepted_contacts", "arguments": {}}
```

### 2.2 `relationship.get_mentioned_contact_summary`（READ，參數由 server 注入）

Planner 的參數面是空的；executor 以 `other_ids`（≤3，本回合 @ 的 accepted 聯絡人）注入。回傳（`_MentionedContactSummaryOutput`）：`contacts[]`（欄位同上）。

**@ 綁定流程**（關鍵安全邊界）：

```text
client 傳 mentioned_other_ids / mentioned_other_id（任意字串）
  → public_chat.py: validated_mentioned_contact_ids(user_id, ids)
      → 只保留「canonical accepted relation」內的使用者（public_relationship_projection.py）
      → 非 accepted 的 ID 全部丟棄；超過 3 位 → mention_overflow
  → scheduler 把驗證過的 ids 放入 turn._mentioned_ids
  → Guard 通過後、執行前再檢查：
      若 tool.argument_source ∈ {MENTIONED_RELATIONSHIP, MENTIONED_CONTACTS}
      且 turn 無 verified mentioned_ids → 失敗 error_code=mentioned_required
  → executor_arguments_for_turn 注入 other_ids（≤3）/ other_id
```

因此**任何 client mention ID 都必須重新依 canonical accepted relation 驗證**；Planner context、顯示訊息、trace 與回覆只使用公開名稱（`display_name`），最多三位，超過時請使用者縮小範圍。

### 2.3 `relationship.get_verified_evidence`（READ，參數由 server 注入）

executor 注入 `other_id`（來自 @ 綁定）。回傳（`_RelationshipOutput`）：`relationships[]` 每筆含 `counterparty`（公開名稱）、`summary`（shared_summary）、`shared_message_count`。

非 accepted 配對 → `relationship_not_accepted` 失敗：「這位目前不是已接受配對，我不能把他的資料當成已確認資訊。」

## 3. 呼叫流程（背後怎麼運作）

```text
使用者: 「@小安 我們有哪些已驗證的共同點？」
  │
  ▼ public_chat: 驗證 @ 綁定 → mentioned_contact_refs → 顯示訊息前置 @小安
  ▼ Planner → relationship task（depends_on: []）
  ▼ slice_for_agent("relationship"): message + recent_messages
       + mentioned_contacts(公開 refs) + mentioned_contact_overflow + clock
  ▼ relationship_agent.run → LLM function calling
      {"tool_name": "relationship.get_mentioned_contact_summary", "arguments": {}}
  ▼ Guard → 通過；scheduler 檢查 mentioned_ids 非空 → OK
  ▼ executor 注入 other_ids=["<verified id>"] → execute_tool
      → mentioned_contact_summary → public_relationship_projection.mentioned_contact_summary
  ▼ Synthesizer 以公開名稱回覆（不得暴露內部 ID）
```

使用者問「我認識的人裡誰適合週末爬山？」：

```text
relationship task → LLM 提 relationship.list_accepted_contacts
  → execute_tool → accepted_contact_summaries(user_id)（≤8 + truncated）
  → Synthesizer 依公開摘要推薦（阿月不會把此當成對方同意）
```

## 4. 工具可見性

`relationship.get_mentioned_contact_summary` **不在** `READ_ONLY_TOOLS`（沒有安全目標就不能外露），只在 server 驗證過 accepted @ mention 的回合由 `planner_tool_names(can_read_mentioned_contacts=True)` 加入；calendar agent 也關閉 mention 工具（`can_read_mentioned_contacts=False`）。places 等 agent 看不到 relationship 工具。

## 5. 端到端範例

**使用者**：「@小安 和小美，誰和我的公開共同點比較多？」

1. `@` 驗證：小安、小美必須都是 canonical accepted contact，否則被丟棄並提示。
2. Planner：relationship task + synthesizer。
3. LLM 提 `relationship.get_mentioned_contact_summary`（一次呼叫，executor 注入兩位）。
4. 回覆以公開摘要比較（近期情境、initial_interest、verified_common_ground 等 typed 欄位）；若其中一位非 accepted → 該位不顯示，阿月說明「小美目前不是已接受配對，我不能讀取她的資料」。
