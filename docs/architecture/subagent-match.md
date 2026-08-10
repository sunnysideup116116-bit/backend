# Sub-agent：match（配對子代理）

> 本文說明使用者與阿月談「配對／牽線／對象」時，背後怎麼運作：match sub-agent 能做什麼、呼叫哪些 function、寫入如何確認與執行、媒婆如何介入。

## 1. 角色與能做什麼

**系統角色**（`v3/sub_agents/match_agent.py` 的 `_SYSTEM`）：

> 你是公開阿月的配對子代理：負責查詢配對狀態、對方摘要與發起搜尋。

能力：

- **查詢**本人的唯一單一 proposal／配對狀態（成功、接受、回覆、進度）。
- **讀取**目前有效或已接受配對的**單一公開**對象摘要（非 accepted 時自動匿名化：身份、共同點、摘要都不公開）。
- **發起搜尋**（`match.start_search`，需確認）。
- **對唯一可操作提案**表達有興趣或婉拒（`match.decide_active_proposal`，revision CAS）。

**不負責**：不讀對方私人資料；不用聊天紀錄猜配對結果（一律讀 canonical observation）；不列出或計算 accepted contacts aggregate，不把「等待回覆」寫成近期情境。

## 2. 可呼叫的工具（4 個）

| 工具 | risk | 用途 |
| --- | --- | --- |
| `match.get_status` | READ | 讀唯一正式配對狀態 snapshot |
| `match.get_counterparty_summary` | READ | 讀目前有效／已接受配對的單一公開對象摘要；不回答 accepted contacts aggregate |
| `match.start_search` | WRITE | 開始找新對象（先建立 confirmation） |
| `match.decide_active_proposal` | WRITE | 對唯一可操作提案表達有興趣／婉拒（runtime 注入 revision） |

### 2.1 `match.get_status`（READ，無參數）

回傳（`_MatchStatusOutput`）：`state`、`scope`、`is_terminal`、`chat_opened`、`counterparty`（公開名稱）、`revision`、`updated_at`、`reason_code`。

資料來自 `match_state_service.get_match_status_snapshot`（canonical read model），不是聊天紀錄。

範例（「他回覆了沒？」）：

```json
{"tool_name": "match.get_status", "arguments": {}}
```

### 2.2 `match.get_counterparty_summary`（READ，無參數）

回傳（`_CounterpartySummaryOutput`）：`found`、`match_state`、`display_name`、`safe_summary`、`recent_context`、`initial_interest`、`personality_summary`、`distinctive_tags`(≤4)、`verified_common_ground`、`recommendation_tier`、`chat_opened`。

隱私規則（`tools.py:_counterparty_summary`）：只有 `accepted` 才揭露 `display_name` 與完整摘要；`draft/pending` 期間用 `anonymize_counterparty_payload` 匿名化。配對機會未 ready 時 `missing_basis_question` 引導使用者先補資料。

### 2.3 `match.start_search`（WRITE）

Planner 參數：無。**不能由第一次 Planner decision 直接執行**：

```text
proposal → Guard(write_requires_confirmation) → prepare_write_confirmation
  → assess_match_opportunity(profile, user_id, explicit_search=True)
      - not_ready → 追問 profile basis（「我想先多了解你的方向…」）
      - active_match_blocked → 「你目前還有一段配對正在進行」
      - ready → 建立 pending confirmation（TTL 900s）
  → Synthesizer:「我會依你的近況、偏好和個性挑選，不會隨機配對。要我現在開始找就回覆『確認』…」
確認後 execute_write → _start_search → match_action_service.start_match_search
  → enqueue_match_search（job 佇列, idempotency_key=confirmation:{id}）
```

搜尋是**非同步 job**：`match_search_worker` claim job → 資格檢查與向量篩選 → 呼叫媒婆 `POST http://127.0.0.1:9001/api/match` → 產生**唯一 draft proposal** → mediator event「我翻到一位可以介紹給你的人…」。約 1–3 分鐘，使用者可繼續聊天。

### 2.4 `match.decide_active_proposal`（WRITE）

Planner 參數（`_ProposalDecisionArguments`）：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `decision` | `"interested"` \| `"declined"` | 有興趣／婉拒 |

**模型不填 match_id 或 revision**；executor 從 `turn.active_proposal` 注入（`_decide_active_proposal`）：

```text
proposal → Guard(write_requires_confirmation) → prepare_write_confirmation
  → preflight 只驗證 user_can_decide + decision 合法 → 建立 pending confirmation
確認後 execute_write → _decide_active_proposal
  → decide_active_proposal(user_id, decision, expected_revision=proposal_revision, idempotency_key)
  → match_action_service → apply_match_decision（CAS: status + proposal_revision）
      - stale → 回報最新狀態（accepted/declined/pending/draft 各對應文案），不覆寫
      - 成功 → apply_transition_effects（通知、開聊天室、GIF 慶祝、婉拒 feedback→媒婆）
```

## 3. 呼叫流程（背後怎麼運作）

```text
使用者: 「他回覆了沒？」
  │
  ▼ Planner → match task（depends_on: []）
  ▼ slice_for_agent("match"): message + recent_messages + active_proposal + latest_match_outcome + clock
  ▼ match_agent.run → LLM function calling
  ▼ Guard → READ 工具通過
  ▼ execute_tool → _match_status → get_match_status_snapshot
  ▼ Synthesizer: 「有，對方已經接受了，聊天室也開啟了。」
```

使用者主動想找人（「可以幫我找對象嗎？」）則走：

```text
Planner → match task → LLM 提 match.start_search（WRITE）
  → Guard: write_requires_confirmation → preflight（opportunity 檢查）
  → confirmation + preview → 使用者「確認」
  → job 佇列 → worker → 媒婆 9001 /api/match → draft proposal → mediator event
  → 阿月之後再以 status 工具讀回並回報
```

## 4. 主動牽線（opportunity）路徑

不經 match sub-agent 的另一條路：Planner 在 `decompose_tasks` 輸出 `opportunity: {signal: "social_opening", evidence_span, confidence}`。Scheduler 驗證（evidence_span 是原句連續子字串且 confidence ≥0.8）後：

- `assess_match_opportunity` 為 `ready` → 直接建立 `match.start_search` confirmation（`source=opportunity_guidance`），Synthesizer 以「你提到『…』。感覺這件事有人一起也不錯…」邀請。
- `not_ready` → 引導補 profile basis。

## 5. 狀態真相與隱私

- Canonical lifecycle：`draft → pending → accepted`；`declined` 為終態。`accepted` 是已建立的聯絡關係，不是進行中的提案；只有 live `draft/pending` 阻擋新 active proposal。
- 「對方是誰」等問題一律從 canonical tool observation 讀，禁止從聊天紀錄猜。
- 使用者面向文字用「對象／人選／對方／旅伴」，禁止稱人為「物件」。
- 配對結果、是否接受等問題由 `match.get_status`／`get_counterparty_summary` 的 typed projection 回答，不暴露內部 ID。

## 6. 端到端範例

**使用者**：「有人可以介紹給我嗎？」

1. Planner：match task + synthesizer。
2. match agent 提 `match.start_search` → Guard 攔截 → preflight：profile 已 ready → confirmation。
3. 阿月：「我會依你的近況、偏好和個性挑選，不會隨機配對。要我現在開始找就回覆『確認』；也可以先補充條件。」
4. 使用者：「確認」→ `_start_search` → job 入佇列 → 阿月：「好，我開始幫你找，通常約需要 1–3 分鐘。」
5. Worker：候選資格篩選 → 媒婆選出 1 位 → draft proposal → mediator event 通知使用者。
6. 使用者：「他是誰？」→ match agent 提 `match.get_counterparty_summary` → 回覆公開摘要（draft 期間匿名化）。
7. 使用者：「我有興趣」→ `match.decide_active_proposal(decision=interested)` → confirmation → CAS 轉 pending → 對方收到通知。
