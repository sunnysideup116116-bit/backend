# Appwrite Schema Reference

> risk_backend 依賴的 Appwrite Chat Logs DB。9 個 collection，全部都在同一個 database 下。
> 部署新環境時依照本文件在 Appwrite Console 建立對應 collection 與 attribute。
>
> **若本文件與 `appwrite_schema_dump.json` 有出入**：以 `appwrite_schema_dump.json` 為準。
> 該 JSON 是從 live Appwrite 直接傾倒的機器產出，本 md 是人類整理的對照表，可能有疏漏。

---

## Setup 步驟

1. 建立 Appwrite Project（任意名稱，例如 `dating_safety`）
2. 建立 Database（任意名稱，例如 `chat_logs`）
3. 把 Project ID / Database ID / API Key 填到 `risk_backend/.env`：
   ```
   APPWRITE_ENDPOINT=https://your-appwrite-host/v1
   APPWRITE_PROJECT_ID=...
   APPWRITE_API_KEY=...
   APPWRITE_DB_ID=...
   ```
4. 在 Console 內依下列規格建立 9 個 collection 與 attribute

---

## Collections

### 1. `conversations`

| Attribute | Type | Notes |
|---|---|---|
| `user_a_id` | String | |
| `user_b_id` | String | |
| `last_activity` | DateTime | |

### 2. `messages`

| Attribute | Type | Notes |
|---|---|---|
| `conversation_id` | String | |
| `sender_id` | String | |
| `content` | String (max 5000) | |
| `timestamp` | DateTime | |
| `is_blocked` | Boolean | |
| `delivery_status` | String | pending_review / delivered / blocked |
| `reviewed_at` | DateTime | nullable |
| `delivered_at` | DateTime | nullable |
| `triggered_by_msg_id` | String | nullable, default null |

### 3. `temporal_features`

| Attribute | Type | Notes |
|---|---|---|
| `user_id` | String | |
| `conversation_id` | String | |
| `latency` | Float | 舊欄位（向後相容） |
| `frequency` | Float | |
| `message_burst_count` | Integer | |
| `last_message_time` | DateTime | |
| `unreplied_count` | Integer | |
| `consecutive_char_count` | Integer | |
| `message_ratio` | Float | |
| `volume_ratio` | Float | |
| `avg_chars_per_message` | Float | |
| `reply_latency_seconds` | Float | nullable |
| `idle_time_seconds` | Float | nullable |

### 4. `risk_analysis_logs_`

| Attribute | Type | Notes |
|---|---|---|
| `message_id` | String | |
| `conversation_id` | String | |
| `delta_rule` | String (JSON) | |
| `delta_nlp` | String (JSON) | |
| `delta_final` | String (JSON) | |
| `nlp_reasoning` | String (max 2000) | |
| `confidence` | Float | |
| `triggered_rules` | String (max 500) | |
| `triggered_scenarios` | String (max 500) | |
| `timestamp` | DateTime | |
| `max_score` | Float | |
| `spread_score` | Float | |
| `trend_score` | Float | |
| `composite_score` | Float | |
| `guardrail_flagged_words` | String (max 500) | Phase 3.1 |
| `guardrail_classifier_flag` | String (max 200) | Phase 3.1.1 |

### 5. `risk_state_history`

| Attribute | Type | Notes |
|---|---|---|
| `conversation_id` | String | |
| `user_id` | String | |
| `triggered_by_msg_id` | String | |
| `risk_state` | String (JSON) | |
| `risk_level` | String | safe / observation / warning / restricted / blocked |
| `risk_delta_total` | String (JSON) | |
| `decay_applied` | Boolean | |
| `timestamp` | DateTime | |

### 6. `intervention_logs`

| Attribute | Type | Notes |
|---|---|---|
| `conversation_id` | String | |
| `user_id` | String | |
| `risk_level` | String | |
| `action_taken` | String | |
| `user_response` | String | |
| `timestamp` | DateTime | |
| `triggered_by_msg_id` | String | |
| `sender_id` | String | |
| `receiver_id` | String | |
| `sender_action` | String | |
| `receiver_action` | String | |
| `decision_reason` | String | normal / critical_override |
| `primary_risk_type` | String | |
| `risk_state_snapshot` | String (JSON) | |
| `composite_score` | Float | |
| `max_score` | Float | |
| `spread_score` | Float | |
| `trend_score` | Float | |
| `sender_feedback` | String (Enum) | comfortable / uncomfortable / null |
| `receiver_feedback` | String (Enum) | comfortable / uncomfortable / null |
| `cooldown_seconds` | Integer | default 0, min 0 |

### 7. `conversation_summaries`

| Attribute | Type | Notes |
|---|---|---|
| `summary_content` | String (max 5000) | |
| `last_processed_msg_id` | String | |
| `conversation_id` | String | |
| `msg_count_snapshot` | Integer | |
| `updated_at` | DateTime | |
| `version` | Integer | |
| `intimacy_level` | Float | |
| `main_topics` | String (JSON) | |
| `tone_shift` | String | |
| `first_processed_msg_id` | String | |
| `conversation_summaries_reasoning` | String (JSON, max 2000) | |
| `self_disclosure_depth` | Float | |
| `emotional_intensity` | Float | |
| `exclusivity_framing` | Float | |
| `physical_intimacy_reference` | Float | |

### 8. `relationship_metrics`

| Attribute | Type | Notes |
|---|---|---|
| `conversation_id` | String | |
| `user_a_id` | String | |
| `user_b_id` | String | |
| `total_messages` | Integer | |
| `familiarity_score` | Float | |
| `conversation_balance` | Float | |
| `first_contact_at` | DateTime | |
| `last_contact_at` | DateTime | |
| `interaction_days` | Integer | |
| `intimacy_progression_rate` | Float | |
| `user_a_message_count` | Integer | |
| `user_b_message_count` | Integer | |
| `updated_at` | DateTime | |

### 9. `guardrail_context_reviews` (Phase 3.2)

| Attribute | Type | Notes |
|---|---|---|
| `conversation_id` | String | |
| `sender_id` | String | |
| `triggered_by_msg_id` | String | |
| `flagged_words` | String (JSON) | |
| `classifier_flag` | String (JSON) | |
| `judgment` | String (Enum) | healthy / concerning / unclear |
| `reasoning` | String (max 1000) | |
| `model` | String | 實際使用的模型名稱 |
| `timestamp` | DateTime | |

需要 Index：`conversation_id + sender_id + timestamp DESC`（`get_recent_guardrail_context_reviews()` 用）

---

## 注意事項

- 所有 collection 的 Permission 建議設 `Any: create / read / update / delete`（dev 環境）。Production 要改成 Server-only
- `String (JSON)` 表示存 JSON 字串，Python 端用 `json.dumps()` / `json.loads()` 處理
- 部分欄位（`message_id` 等）程式碼會自動生成 `ID.unique()`，不需要在 Appwrite 設 unique constraint
