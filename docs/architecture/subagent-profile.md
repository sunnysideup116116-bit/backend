# Sub-agent：profile（個人檔案子代理）

> 本文說明使用者與阿月談「你是誰／你了解我多少／我的記憶」以及基本性格、深層探索時，背後怎麼運作：profile sub-agent 能做什麼、呼叫哪些 function、assessment session 如何運作。

## 1. 角色與能做什麼

**系統角色**（`v3/sub_agents/profile_agent.py` 的 `_SYSTEM`）：

> 你是公開阿月的個人檔案子代理：負責查看本人的 profile、近期情境與記憶搜尋。

能力：

- **讀取**本人已完成的基礎資料、深度資料、偏好、近期情境與粗略地區（`profile.get_self_summary`）。
- **讀取**本人已儲存的近期情境（`profile.get_recent_context`）。
- **搜尋**本人已儲存的偏好與近期情境（`memory.search_my_profile`）。
- **啟動**基本性格（Big Five）或深層探索 session（`profile.start_assessment`，需確認）。

**不負責**：不讀對方資料；不直接寫入 profile（extraction 一律走 `profile_skills.py` 的 owner-only pipeline）；不把 assessment 答案存成近期情境或記憶。

## 2. 可呼叫的工具（4 個）

| 工具 | risk | 用途 |
| --- | --- | --- |
| `profile.get_self_summary` | READ | 本人 profile 完整投影 |
| `profile.get_recent_context` | READ | 本人已保存近期情境 |
| `memory.search_my_profile` | READ | 本人記憶摘要＋偏好 |
| `profile.start_assessment` | WRITE | 開始基本／深層探索（需確認） |

### 2.1 `profile.get_self_summary`（READ，無參數）

回傳（`_SelfProfileOutput`）：`display_name`、`initial_interest`、`personality_summary`、Big Five 五項分數（O/C/E/A/N）、`values`、`life_goals`、`relationship_needs`、`stress_coping`、`ideal_future`、`deep_profile_summary`、`recent_context`、`location`、`preferences`(≤8)、`missing_sections`。

**隱私**：只投影「本人」已完成的欄位；`clean_profile_text` 過濾內部 ID；`missing_sections` 列出未完成區塊（興趣、基礎性格、深層資料、近期情境）供阿月引導補齊。

適用：「我是誰？」「你了解我多少？」「我適合什麼樣的人？」

### 2.2 `profile.get_recent_context`（READ，無參數）

回傳（`_RecentContextOutput`）：`current_context`、`revision`、`exists`。資料來自已保存的 `current_context`（`safe_recent_context`），不會重新分析。

### 2.3 `memory.search_my_profile`（READ，無參數）

回傳（`_MemoryOutput`）：`summary`（profile_memory_summary）、`current_context`、`preferences`（`profile_memory_preview` ≤8，typed 渲染：喜歡/不喜歡/需要/避免 前綴）。

### 2.4 `profile.start_assessment`（WRITE，需確認）

Planner 參數（`_AssessmentStartArguments`）：

| 欄位 | 型別 | 說明 |
| --- | --- | --- |
| `kind` | `"basic"` \| `"deep"` | 基本性格（Big Five）／深層探索 |

流程：

```text
proposal → Guard(write_requires_confirmation) → prepare_write_confirmation
  → kind 驗證 → preview:「要重新開始{基本性格|深層探索}嗎？新的結果完成前，原本的資料會保留。
     請選擇是否開始。」＋ AI 泡泡內「取消／確認」
按鈕確認後 execute_write → _start_assessment
  → assessment_session_service.start_assessment_session(user_id, kind, idempotency_key=confirmation:{id}:{kind})
```

## 3. Assessment session（特殊入口）

`profile.start_assessment` 確認後，Scheduler 的**階段 0** 接管，不再跑 Planner：

| 狀態 | 行為 |
| --- | --- |
| `active`（探索進行中） | 每則訊息作為 session 答案：`advance_assessment_session`；回覆「取消」可中斷 |
| `awaiting_commit`（探索完成） | 完成訊息泡泡提供 `choice_id` 按鈕：「確認」→ `commit_assessment_session`（CAS revision）覆寫正式資料；「取消」或繼續同房對話 → 保留原本資料 |
| session 過期 | `expire_assessment_session` |

Assessment 相關回合回傳 `assessment_state/kind/revision`；**assessment 答案不會進入 profile extraction pipeline**（`public_chat.py` 以 `profile_write_reason == "assessment"` 排除），也不會成為近期情境或 durable memory evidence。

## 4. Profile 資料怎麼來（與聊天 Agent 分離）

profile sub-agent 只**讀**；寫入由獨立的 owner-only pipeline 負責：

```text
使用者訊息 → 保存（唯一一次）→ public_chat 背景排程
  → profile_task_service → profile_skills.py
      - message_id 冪等（每則訊息最多處理一次）
      - 只用「已保存的 owner 原始訊息」；禁用 assistant reply/history/tool result/match state/對方資料
      - LLM 提出 typed ProfileExtractionDecision + 原句 evidence_span
      - evidence 必須是 owner message 連續原文子字串，否則拒絕
      - 近期情境：只保存本人現實活動（找人/配對/等待回覆不存）
      - 長期記憶：confidence ≥0.90、subject=owner、label 無敏感內容
  → memory_service.apply_profile_memory_proposals → 媒婆 /api/memory/apply（Neo4j, message_id 冪等）
  → profile_memory_preview（Mongo read projection）
```

顯示摘要（如 `get_self_summary` 的 personality_summary 與記憶 preferences）由程式 typed projection／template 組合，不直接儲存模型自由文字摘要。

## 5. 呼叫流程（背後怎麼運作）

```text
使用者: 「你覺得我適合什麼樣的人？」
  │
  ▼ Planner → profile task（depends_on: []）
  ▼ slice_for_agent("profile"): message + recent_messages + recent_context + relevant_memories + clock
  ▼ profile_agent.run → LLM function calling
      {"tool_name": "profile.get_self_summary", "arguments": {}}
  ▼ Guard → execute_tool → _self_profile → _SelfProfileOutput 驗證
  ▼ Synthesizer 依 Big Five/deep profile/偏好回覆建議
      （配對建議屬 recommendation，不是使用者事實，不會寫入記憶）
```

使用者想重新做性格測驗：「我想重新做深層探索」

```text
Planner → profile task → LLM 提 profile.start_assessment {kind: "deep"}
  → Guard: write_requires_confirmation → preflight → confirmation + preview
  → 使用者「確認」→ session 啟動 → 進入 assessment 對話模式
  → 完成後「確認」→ commit 覆寫正式資料（revision CAS）
```

## 6. 端到端範例

**使用者**：「你記得我最近在準備考試嗎？」

1. Planner：profile task。
2. LLM 提 `profile.get_recent_context`（或 `memory.search_my_profile`）。
3. 回傳 `current_context`（若近期情境已保存「準備考試」且未過期）→ Synthesizer：「有，你最近在準備考試…」；若未保存 → 誠實說「我還沒有記到這一段，你跟我說我就會記下來」。
4. 背景 pipeline 已把該訊息保存為證據（message_id 冪等），之後同一訊息不會重複萃取。
