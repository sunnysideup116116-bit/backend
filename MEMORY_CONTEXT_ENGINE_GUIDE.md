# Memory、Graph 與 Context Engine 擴充指南

本文件給負責「長期建議、Graph Memory、Context Engine」的開發者。開始修改前也必須閱讀 [`AGENTS.md`](./AGENTS.md) 與 [`AYUE_V3_ARCHITECTURE.md`](./AYUE_V3_ARCHITECTURE.md)。

核心原則：這三項能力可以共用 typed contracts 與安全 projection，但不能共用一個沒有邊界的資料池。

## 0. 外部設計參考：Hermes Agent

實作前建議閱讀 [NousResearch Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 Memory Provider 與 Context Engine 架構。它是設計參考，不是要把 Hermes runtime 或資料模型直接搬進阿月。

優先參考：

- [Persistent Memory](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory)：bounded、curated memory、容量限制、重複防護、安全掃描、使用者管理與 session search 的分工。
- [Memory Providers](https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers)：外部記憶 provider 的 prefetch、turn sync、session-end extraction、工具與 base-context injection lifecycle。
- [Memory Provider Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin/)：可替換 memory backend 的抽象介面與 hook。
- [Context Engine Plugin](https://hermes-agent.nousresearch.com/docs/developer-guide/context-engine-plugin)：`ContextEngine` lifecycle、單一 engine selection、per-turn selection／observation 與 contract tests。
- [Context Compression and Caching](https://hermes-agent.nousresearch.com/docs/developer-guide/context-compression-and-caching/)：token threshold、工具結果裁切、頭尾保護、tool-call/result 邊界、structured summary、re-compression 與 gateway safety net。
- [Sessions](https://hermes-agent.nousresearch.com/docs/user-guide/sessions/)：完整 session storage、按需搜尋與 active context 的分離；也要注意 compression 不是隱私刪除。

### 值得借鑑的模式

1. **Provider interface**：記憶儲存與 Agent runtime 分離，可替換 backend，但 runtime 只依賴 typed contract。
2. **Context engine lifecycle**：session start、每回合 observation、token update、compression decision、session end 都有明確 hook。
3. **Hot／cold separation**：少量關鍵 memory 常駐；舊 session 與大量記憶按需搜尋，不全部塞入 prompt。
4. **Bounded context**：每個 source 有容量上限、stable ordering、dedup 與 overflow 行為。
5. **Pre-compress hook**：壓縮或淘汰 context 前，讓 memory provider 保存真正需要跨 session 的 insight。
6. **Write governance**：記憶寫入可以要求 approval，且提供 pending／approve／reject／edit／delete 等可見管理流程。
7. **Contract tests**：替換 provider 或 engine 時，先通過共用 ABC／contract test，不讓 runtime 跟著改。

### 不可直接照搬的部分

- Hermes 內建 `MEMORY.md`／`USER.md` 適合單一使用者個人 Agent；阿月是多使用者交友 App，不能把文字記憶檔直接注入所有 prompt。
- Hermes 的完整 session history 與 external-provider context 不能直接套用到阿月；必須先經 owner、room、accepted relation 與 consent 隔離。
- Lossy conversation summary 只能協助對話連續性，不能成為配對狀態、行事曆、owner preference 或 proposal revision 的 source of truth。
- Context compression 不是資料刪除。任何隱私保留／刪除政策仍需作用於 Mongo、Neo4j、session store、projection 與 trace。
- Hermes external memory provider 是 additive；阿月若同時啟用多份 Graph／preview／provider，必須先定義唯一 source of truth、同步方向與 conflict policy。
- 不允許 Agent 自由編輯 owner memory。阿月仍要遵守 saved owner message、typed proposal、原文 evidence、subject validation 與 message-id idempotency。
- 不要因參考 Hermes 而新增 runtime dependency、改模型供應商或把 Public Ayue 變成 Hermes wrapper；任何第三方 provider 都必須是可選 adapter。

### 阿月對應方式

```text
Hermes MemoryProvider concept
→ Ayue DurableMemoryProvider
   read_owner_memories()
   apply_validated_proposals()
   disable() / restore() / correct()
   health()

Hermes ContextEngine concept
→ Ayue ContextEngineV1
   collect canonical sources
   project by privacy namespace
   retrieve / rank / deduplicate
   enforce per-source and total budgets
   return ContextBundleV1
```

本專案的 typed contracts、domain state、隱私 policy 與測試是最終真相。若 Hermes 的做法與阿月規則衝突，以阿月規則為準，並在設計文件記錄差異。

## 1. 先分清楚四種狀態

| 狀態 | 目前 owner | 用途 | 不可混入 |
| --- | --- | --- | --- |
| 本人近期情境 | Mongo profile 的 `recent_context_state`／`current_context` | 最近、正在或預計進行的本人現實活動 | 配對操作、他人狀態、長期人格判定 |
| 本人長期記憶 | Neo4j `User-[:PREFERS|AVOIDS|CURRENTLY_WANTS]->Concept` | 明確且可持續的本人偏好、排斥地雷與最新想要活動 | 系統建議、一次性活動、對方特徵 |
| 雙人關係語意 | Mongo `semantic_plans` 與 room-scoped KG triples | 已接受關係中的共同話題、互動節奏與 mediator strategy | 任一方私人悄悄話、跨房間資料 |
| Agent 每回合 context | `AgentTurnContextV2` | 讓 Planner 在有限 token 與隱私邊界內做當回合決策 | raw Mongo／Neo4j document、內部 ID、對方私人資料 |

「長期建議」是系統推導出的 recommendation，不是使用者記憶。即使建議來自 Graph Memory，也必須保存到獨立 read model，並保留來源、版本、有效期與可撤銷狀態；禁止寫成 `PREFERS`。

## 2. 現有資料流與 source of truth

### 2.1 近期情境

```text
Saved owner message
→ profile_skills.py typed extraction
→ evidence / subject / confidence validation
→ programmatic Traditional Chinese projection
→ Mongo revision CAS
→ profile.get_recent_context
→ AgentTurnContextV2
```

- Source of truth：`recent_context_state` 與 `current_context_revision`。
- `current_context` 是給 UI／Planner 的安全顯示 projection。
- 只接受 owner 已保存的原始訊息；assistant reply、history、tool result 與 match state 都不是寫入證據。
- 每個 evidence span 必須是該 owner message 的連續原文子字串。

### 2.2 長期偏好記憶（Concept 與 PREFERS / AVOIDS / CURRENTLY_WANTS）

```text
Saved owner message
→ profile_skills.py ProfileExtractionDecision.memories
→ typed validation
→ memory_service.apply_profile_memory_proposals
→ port 9001 /api/memory/apply
→ Neo4j (:User)-[:PREFERS|AVOIDS|CURRENTLY_WANTS]->(:Concept)
→ Mongo profile_memory_preview read projection & realtime sync
→ context_slicer.py (user_preferences) → Synthesizer prompt
```

- Neo4j 是 durable preference 的 source of truth，節點型別為 `Concept`（屬性 `{key, label, kind}`），廢除舊版 `Trait`。
- 關係類型：
  - 正向偏好：`(:User)-[:PREFERS]->(:Concept)`
  - 負向地雷：`(:User)-[:AVOIDS]->(:Concept)`
  - 短期意圖：`(:User)-[:CURRENTLY_WANTS {expires_at}]->(:Concept)`（寫入新意圖時自動清除舊有 `CURRENTLY_WANTS`，維持最新單一意圖，預設 30 天過期）。
- Mongo `profile_memory_preview`／`profile_memory_summary` 是 bounded read projection；前端「阿月記住的事」在開啟與載入時會即時與 Neo4j Concept 同步。
- `message_id` 是 observation idempotency key；同一 owner message 不得增加兩次 evidence count。
- `profile_memory_outbox` 只保存已驗證的 typed proposals 與 error code，不保存 raw chat。
- 使用者在設定中 disable／restore／correct 後，同步更新 Mongo projection。

### 2.3 對話壓縮與延續性（Compaction 機制）

- **觸發條件**：單一聊天室累積訊息超過 **30 句** 時觸發。
- **壓縮策略**：壓縮最舊 **10~11 句** 訊息為 6 大維度的結構化摘要（`active_topics`, `owner_goals`, `known_continuity`, `unresolved_questions`, `ayue_commitments`, `recent_decisions`），保留最新 **20 句** 未壓縮原始訊息作為即時上下文。
- **儲存位置**：MongoDB `conversation_compactions`，並帶有 `covered_through_message_id` watermark。

舊版 `/api/memory/observe`、主服務直接 Neo4j fallback 與自由文字 extractor 已移除；`profile_skills.py → /api/memory/apply` 是唯一 owner-memory extraction/write flow。不要再新增另一個自由文字 extractor。

### 2.3 雙人關係語意

`semantic_plan_service.py` 只處理 accepted pair room 中雙方真正傳送的訊息：

- `semantic_plans`：room-scoped summary、theme、strategy、role 與處理進度。
- room-scoped KG triples：共同聊天中可驗證的 entity relation。
- 不等於 owner durable memory，也不應自動回寫任一方的 `HAS_PREFERENCE`。
- 對 Public Ayue 或 Private Ayue 只能輸出 consented／shared projection，不能輸出 raw chat 或完整 strategy。
- Updater 只計算尚未處理的 pair-room 訊息，並以內容長度作保守 budget proxy；累積至少 600 字元單位才更新。短訊息數量本身不觸發更新，避免只因訊息筆數達門檻就呼叫模型。
- Private V2 由 `PrivateAgentTurnContextV2` 獨立 adapter 讀取 current accepted pair room。Planner projection 只允許 role、macro summary、theme、action plan、dynamic bounds 與最多 20 筆 triples；final composer 再縮小為 role、macro summary 與 triples。Raw plan/Graph 欄位與其他 room 的資料不得進 prompt。
- Neo4j relationship read 失敗時可退回同一 semantic plan 已保存的 bounded triples；兩者都沒有時回空集合，不得改抓 owner durable memory 或跨 room Graph。

### 2.4 Public Context

`services/ayue_agent/context.py` 是 Public V3 唯一 Context Builder。現在的 budget：

- 最近 12 則訊息，合計最多 6,000 字元。
- 本人近期情境一份。
- 本人長期記憶最多 8 筆。
- 唯一 live proposal 的安全狀態。
- 經 server 驗證的公開 mention。
- Asia/Taipei turn clock 與 capability version。

Context Builder 只組合安全 projection，不負責重新萃取、修正或寫入記憶。

### 2.5 已知技術債（不要沿用成新架構）

- Port 8000 `memory_service.py` 不再直接連 Neo4j；owner-memory 寫入、讀取與 action 都經由 port 9001 的 canonical API。
- 舊版 `/api/memory/observe` 與 `memory_service.py` 的 direct fallback 已刪除；新版 owner-message pipeline 只接受 `profile_skills.py` 產生的 validated proposals，再交給 `/api/memory/apply`。
- port 9001 memory API unavailable 時只回 bounded error／retry outbox，不得改抓主服務的 Graph credentials 或重新啟動另一條 writer。
- `/api/clear_graph` 是 destructive demo endpoint。正式環境必須停用或加上管理者授權與明確環境 guard；任何測試或 migration 不得呼叫它清正式 Graph。
- `/api/chat_triples` 目前可把 bounded evidence message content 寫入 Neo4j。正式化 relationship graph 前，應改成 message reference／hash 與受控 evidence projection，並提供舊資料 migration。
- `semantic_plan_service.py` 同時負責 metrics、LLM summary、Graph I/O 與 check-in effect，責任偏重。若重構，先以 contract/repository 分層，保持現有呼叫端相容。

## 3. 建議的 Context Engine 分層

不要把新的 Context Engine 寫成第二個聊天 router。建議維持以下分層：

```text
Canonical Sources
    Mongo profile / match / calendar
    Neo4j owner memory
    room-scoped relationship plan
        ↓
Privacy-safe Projectors
        ↓
Retriever / Ranker
        ↓
Budgeter + Deduplicator
        ↓
ContextBundleV1
        ↓
Public / Private runtime adapters
```

### 3.1 建議 contract

Context Engine 的輸出應是 provider-neutral typed bundle，而不是 prompt 字串：

```json
{
  "version": "context-bundle-v1",
  "owner": {
    "recent_context": "最近去游泳",
    "durable_memories": [
      {
        "key": "smoking",
        "label": "抽菸",
        "stance": "avoid",
        "confidence": 0.96,
        "source_type": "owner_message"
      }
    ]
  },
  "relationship": {
    "shared_facts": [],
    "counterparty_public_summary": null
  },
  "domain_state": {
    "match": null,
    "calendar": null
  },
  "retrieval": {
    "query_type": "casual",
    "selected_count": 1,
    "truncated": false
  }
}
```

實際送入 Planner 前仍要由 Public／Private adapter 套用各自的隱私 policy。Bundle 內部若需要 executor-only identifiers，必須放在 prompt 不可序列化的 server-side binding，不能混入可見 JSON。

### 3.2 Retrieval 規則

- 先依 owner／room／accepted relationship 做硬隔離，再做相關度排序。
- Rank 建議至少考慮：semantic relevance、confidence、recency、evidence count、active state。
- 最近訊息、近期情境、長期記憶與 relationship facts 分別計算 budget；不要讓某一類塞滿整個 prompt。
- 相同語意 memory 要 canonicalize／deduplicate，不要同時出現「喜歡游泳」「愛游泳」「偏好游泳」。
- 矛盾記憶不能由 LLM 靜默覆蓋。保留 evidence lineage，產生 conflict state，再由明確 owner 訊息或使用者設定處理。
- Context Engine 失敗時回 bounded empty sections 與 error code；不得改抓 raw profile、完整 Graph 或 legacy prompt。

## 4. Graph Memory 改善邊界

### 4.1 建議保留的最小 schema

```text
(User {id})
  -[HAS_PREFERENCE {
      stance,
      confidence,
      active,
      first_seen_at,
      last_seen_at,
      evidence_count,
      source,
      display_label_zh_tw,
      schema_version
    }]->
(Trait {key, name, category})

(MemoryObservation {
  message_id,
  owner_user_id,
  created_at
})
```

可以新增 evidence reference、conflict、superseded 或 decay metadata，但：

- 不保存完整 owner message；最多保存不可逆 hash、message ID 或 bounded evidence span。
- 所有 relation 必須 owner-scoped，禁止只靠 Trait key 反查後把 A 的偏好給 B。
- Protected／敏感屬性不可成為交友篩選記憶。
- 使用者修正或停用記憶時採 soft state，保留 auditability；不要直接刪除整個 Trait。
- Schema migration 預設 dry-run，顯示數量與去識別化範例；review 後才 `--apply`。

### 4.2 Matchmaker 使用方式

- `target_user.graph_memory` 只能代表發起者本人。
- 每個 candidate 的 `graph_memory` 只能代表該 candidate。
- Matchmaker 可以把記憶當 ranking signal，不能把低信心推論寫回 Graph。
- Hard conflict 必須在 LLM ranking 前做 deterministic qualification。
- 沒有 Graph 或 Neo4j timeout 時可降低 ranking evidence，但不得互換角色或發明共同點。
- 對外 proposal reason 只能使用已允許的安全 projection，不引用 key、ID、confidence 或 Graph 技術詞彙。

## 5. 長期建議的正確模型

若要加入「阿月長期建議」，建議建立獨立 `AdviceCardV1`：

```json
{
  "advice_id": "server-owned",
  "owner_user_id": "server-only",
  "topic": "conversation|dating|wellbeing|activity",
  "message_zh_tw": "給使用者看的短建議",
  "source_refs": [
    {"type": "memory_key", "ref": "server-only"},
    {"type": "recent_context_revision", "ref": "server-only"}
  ],
  "confidence": 0.84,
  "created_at": 0,
  "expires_at": 0,
  "generator_version": "advice-v1",
  "status": "active|dismissed|expired"
}
```

規則：

- 建議必須可解釋為「根據哪些 owner facts 推導」，但 UI 不顯示內部 reference。
- Advice 不回寫成 memory，也不能增加 Graph evidence count。
- 建議有 TTL、版本與 dismissed state；使用者不喜歡某建議，不代表他討厭該活動。
- 不使用對方私人資料產生本人建議。Relationship advice 只能依共同聊天室與已同意分享資訊。
- 醫療、法律、財務等高風險建議不得由這個一般交友 advice surface 自動生成。
- 主動推播 advice 前另做 frequency、grounding 與 duplication policy，不可直接重用 proactive care claim。

## 6. 允許的整合點

| 要改善的能力 | 建議修改位置 |
| --- | --- |
| Durable memory schema／讀寫 | `memory_service.py`、port 9001 memory endpoints、migration |
| Memory extraction contract | `profile_contracts.py`、`profile_skills.py`、`skills/memory/SKILL.md` |
| Public memory read tool | `ayue_agent/tools.py` 與 `tool_registry.py` |
| Public per-turn context selection | `ayue_agent/context.py`，或其下方新增獨立 context-engine package |
| Private pair context | `ayue_agent/private_v2.py` 的獨立 adapter |
| Relationship shared context | `semantic_plan_service.py` 與 consented projection |
| Matchmaker ranking | `matchmaker_agent/agent_api.py`、`matchmaker.py` |
| User correction UI/API | `/api/profile/memories` 與 `/api/profile/memories/action` |

禁止在 `routers/chat.py` 直接查 Neo4j、組 Graph prompt 或寫 memory；router 只做 API adapter 與背景工作協調。

## 7. 建議實作順序

1. 先寫現況 fixture：owner memories、矛盾、disable、Neo4j timeout、兩位使用者相同 Trait。
2. 定義 versioned memory／context contracts 與 privacy projection。
3. 將現有 Graph read/write 包在 repository 或 domain service，保留 API 相容。
4. 建立 retrieval／ranking／budgeting 的 deterministic tests。
5. 用 shadow mode 比較舊 `AgentTurnContextV2` 與新 bundle 的選取結果；shadow 只記 metadata，不記內容。
6. Public runtime adapter 驗證後才切換；V3 失敗仍 fail closed，不回 legacy。
7. 最後才讓 matchmaker 或 long-term advice 消費新 projection。

不要同時改 Graph schema、Context Builder、match ranking 與 UI。每階段都應能單獨 rollback。

## 8. 最低測試清單

- A、B 有相同 Trait key 時，讀取仍嚴格依 owner 隔離。
- 對方私人 memory 永不進 Public context、Private final composer 或 trace。
- Private relationship projection 僅限 current accepted pair room、allowlisted semantic-plan 欄位與最多 20 筆 triples；raw Mongo／Neo4j 欄位不進 Planner 或 composer。
- 未滿 600 字元單位的未處理短訊息不觸發 semantic update；達門檻後的 chat-log projection 必須保留實際 sender/content，而不是未展開的格式字串。
- 同一 message ID 重試不增加 evidence count。
- disable／restore／correct 會同步 Graph source 與 Mongo preview。
- 矛盾 evidence 不被後到的低信心訊息靜默覆蓋。
- 一次性活動只進近期情境，不進 durable memory。
- 長期偏好只進 durable memory，不污染近期情境。
- Advice 不會成為 memory，也不影響 evidence count。
- Context bundle 符合每區 budget、總字數、dedup 與 stable ordering。
- Neo4j unavailable 時 Context Engine bounded fallback，不洩漏 raw data、不阻塞一般聊天。
- Matchmaker 不互換 target/candidate memory，沒有 Graph 時不發明共同點。
- Trace 只記 source count、版本、latency、cache hit、truncation 與 error code，不記 memory text 或 Graph payload。

## 9. 交付要求

- 修改檔案與 schema migration 清單。
- Contract 版本與相容／rollback 方式。
- 完整 deterministic test 指令與結果。
- Shadow 指標只允許 count、latency、selected source type、truncated、error code。
- 去識別化範例，不提供真實 user ID、raw messages、Graph dump 或 prompt。
- 所有正式資料 migration 先 dry-run；由 reviewer 明確批准後才 apply。
# Demo Graph reset and degraded reads

Graph reads used by matching are optional signals. Empty or unavailable Graph
memory must remain a bounded degraded result and must not be treated as a
successful write or as a generic pipeline crash. The local Demo Graph reset is
explicitly guarded and returns no raw Graph data. Full Demo reset clears Graph,
Mongo app collections, and process-local fallback state in a fixed order; it
does not promise cross-store rollback.
