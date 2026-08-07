# 05. 媒婆服務、圖記憶與 Context Engine

> 本篇說明 port 9001 媒婆服務（candidate 排序＋Neo4j 記憶）與主服務的記憶／profile pipeline。完整資料邊界請見根目錄 `MEMORY_CONTEXT_ENGINE_GUIDE.md`。

## 1. 媒婆服務（matchmaker_agent, port 9001）

### 角色

媒婆是候選排序與 Neo4j 記憶服務，不是聊天 runtime。主服務把「已通過資格檢查的候選集合」與發起者資料送來，媒婆用 LLM 選出 **0 或 1 位**最值得牽線的人，回傳嚴格 JSON。

### API 端點（`agent_api.py`）

| 端點 | 方法 | 用途 |
| --- | --- | --- |
| `/health` | GET | Process-level readiness（不讀 profile／Neo4j） |
| `/api/match` | POST | 候選排序：`{target_user, candidates, target_deep_profile}` → `{outcome: selected\|no_suitable_candidate, matches: [1 筆]}` |
| `/api/feedback` | POST | 婉拒／接受後的反思：LLM 產生 graph reflection → 寫入 `HAS_PREFERENCE` relationships |
| `/api/global_reflection` | POST | 從一對（from_big_five→to_big_five）歸納全域法則 `GlobalRule`（相似度合併，weight 遞增） |
| `/api/memory/observe` | POST | 從本人第一人稱訊息萃取長期偏好（confidence ≥0.90），message_id 冪等 |
| `/api/memory/apply` | POST | 寫入已驗證的 memory proposals（不重新萃取），message_id 冪等 |
| `/api/memory/{user_id}` | GET | 讀某使用者 active 偏好（owner-scoped） |
| `/api/memory/action` | POST | disable／restore／correct 一筆偏好 |
| `/api/chat_triples` | POST/GET | 雙人聊天室的三元組（session-scoped，**不**自動成為任一方 durable preference） |
| `/api/clear_graph` | POST | Demo 專用：清空 Neo4j |

### 排序決策（`matchmaker.py:MatchmakerAgent`）

權重：近期情境 context 30% + 雙方 graph memory 25% + deep_profile/價值觀 20% + Big Five 15% + 立即可聊話題 10%。硬性規則：任一方 `DISLIKES_TRAIT` 命中對方特質原則上不推薦；沒有值得誠實推薦的人時回 `no_suitable_candidate`，不得硬選。

`/api/match` 的處理流程（`agent_api.py:match_endpoint`）：

1. 平行讀取發起者 graph memory、候選人 graph memories、全域法則（Neo4j 讀取失敗時回傳占位文字，不中斷）。
2. 每個 candidate 附加自己的 `graph_memory` 欄位（**candidate 的記憶只代表 candidate**）。
3. `agent.match(...)` 呼叫 LLM；解析 JSON 失敗或格式不符 → HTTP 502 `Invalid matchmaker response`（呼叫端不能把 provider 失敗當成「沒有合適人選」）。
4. `matches` 最多保留 1 筆，`matched_user_id` 必須存在。

## 2. Neo4j 圖記憶模型

```
(:User {id}) -[HAS_PREFERENCE {stance, type, confidence, active, evidence_count, source}]-> (:Trait {key, name, category})
(:Agent {name:"System"}) -[LEARNED_RULE {weight}]-> (:GlobalRule {content, category})
(:MemoryObservation {message_id})   ← message_id 冪等（每則訊息最多萃取一次）
(:ChatEntity {key}) -[IS_A|HAS|LIKES|...]-> (:ChatEntity {key})   ← 雙人聊天 triples
```

- 偏好必須 **owner-scoped**：`MEMORY_OBSERVATION.message_id` 唯一約束保證同一訊息不會重複寫入。
- `stance` ∈ {like, dislike, require, avoid}；`type` 由 stance 推導（LIKES_TRAIT/DISLIKES_TRAIT）。
- 敏感內容（種族、宗教、性傾向、疾病等）在寫入前被正則擋掉（`agent_api.py` 的 `protected`）。
- **系統產生的長期建議是 recommendation，不是使用者事實**：禁止寫成 `HAS_PREFERENCE`（需獨立 versioned contract + TTL + dismissed state，見 `MEMORY_CONTEXT_ENGINE_GUIDE.md`）。

## 3. 主服務的記憶 pipeline

### 3.1 Profile extraction（`profile_skills.py`）

- 只接受**已保存的 owner 原始訊息**（`public_chat.py` 在保存後背景排程）；同一 `message_id` 最多處理一次（`_claim_profile_message` 的 `$setOnInsert` upsert）。
- 禁止使用 assistant reply、conversation history、tool result、match state 或對方資料作為寫入來源。
- LLM 只提出 typed `ProfileExtractionDecision`（`profile_contracts.py`）＋原句 `evidence_span`；evidence 必須是 owner message 的連續原文子字串（`_valid_evidence_span`），否則拒絕該欄位。
- 近期情境只保存本人現實活動；找人、配對、提案、等待回覆不得成為近期情境。長期記憶只保存明確且可持續的本人偏好（confidence ≥0.90，`subject=owner`）。
- 使用者可描述近期想做的事而沒有時間單位；不得把「缺時間詞」當作拒絕理由。
- 顯示摘要由程式投影組合，不直接儲存模型自由文字摘要。

### 3.2 Durable memory facade（`memory_service.py`）

- `apply_profile_memory_proposals`：validated durable-memory write facade（主路徑走媒婆 `/api/memory/apply`，Neo4j 直接連線為 fallback；兩條路都做 message_id 冪等）。
- `get_user_graph_memories`：讀回 active 偏好並投影成 `profile_memory_preview`（Mongo read projection，**不是第二個 source of truth**）。
- `observe_user_memory` / `apply_memory_action`：observe 與 disable/restore/correct 操作。
- `_sync_memory_projection`：把圖記憶壓縮成 ≤12 筆的 Mongo 投影與摘要。

### 3.3 Context Engine 邊界（現況）

`context.py:build_agent_turn_context_v2` 每回合組 bounded context（`relevant_memories` ≤8 筆等）。新 Context Engine 若建置，只能輸出 bounded、versioned typed bundle；Public／Private runtime 各自套用 privacy adapter。Retrieval 必須先做 owner／room／accepted-relation 硬隔離，再做相關度排序、budget、dedup；失敗時回 bounded empty projection 與 error code，不得改抓 raw data。

## 4. 配對狀態真相（canonical lifecycle）

- Lifecycle：`draft → pending → accepted`；`declined` 為終態。
- `match_decision_service.py:apply_match_decision` 是唯一 CAS 轉移：`status + proposal_revision` 條件更新（`find_one_and_update`），stale 回報最新狀態且不覆寫；`idempotency_key` 存於 `last_decision`，重放回 `idempotent: true`。
- 只有 live `draft/pending` 阻擋新的 active proposal；`accepted` 是已建立的聯絡關係。
- 效果（通知、開聊天室、GIF、feedback）只在 transition 成功後執行（`match_action_service.apply_transition_effects`）；effect 失敗不讓已提交 transition 被重送。

## 5. 雙人關係 context（不可誤用）

- `semantic_plan_service.py`：accepted pair room 的 shared semantic plan 與 chat triples 屬於雙人關係 context，**不得**自動轉成任一方 durable preference。
- `relationship.get_verified_evidence`、`get_mentioned_contact_summary`：只能讀 canonical accepted relation 的公開投影。
- 媒婆的 `/api/chat_triples` 以 session_id 隔離，只服務雙人聊天室的關係脈絡。
