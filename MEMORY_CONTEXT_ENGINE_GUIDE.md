# Memory、Graph 與 Context Engine 擴充指南

本文件給負責「長期建議、Graph Memory、Context Engine」的開發者。開始修改前也必須閱讀 [`AGENTS.md`](./AGENTS.md) 與 [`AYUE_V2_ARCHITECTURE.md`](./AYUE_V2_ARCHITECTURE.md)。

核心原則：這三項能力可以共用 typed contracts 與安全 projection，但不能共用一個沒有邊界的資料池。

## 1. 先分清楚四種狀態

| 狀態 | 目前 owner | 用途 | 不可混入 |
| --- | --- | --- | --- |
| 本人近期情境 | Mongo profile 的 `recent_context_state`／`current_context` | 最近、正在或預計進行的本人現實活動 | 配對操作、他人狀態、長期人格判定 |
| 本人長期記憶 | Neo4j `User-[:HAS_PREFERENCE]->Trait` | 明確且可持續的本人偏好／限制 | 系統建議、一次性活動、對方特徵 |
| 雙人關係語意 | Mongo `semantic_plans` 與 room-scoped KG triples | 已接受關係中的共同話題、互動節奏與 mediator strategy | 任一方私人悄悄話、跨房間資料 |
| Agent 每回合 context | `AgentTurnContextV2` | 讓 Planner 在有限 token 與隱私邊界內做當回合決策 | raw Mongo／Neo4j document、內部 ID、對方私人資料 |

「長期建議」是系統推導出的 recommendation，不是使用者記憶。即使建議來自 Graph Memory，也必須保存到獨立 read model，並保留來源、版本、有效期與可撤銷狀態；禁止寫成 `HAS_PREFERENCE`。

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

### 2.2 長期偏好記憶

```text
Saved owner message
→ profile_skills.py ProfileExtractionDecision.memories
→ typed validation
→ memory_service.apply_profile_memory_proposals
→ port 9001 /api/memory/apply
→ Neo4j owner-scoped relationship
→ Mongo profile_memory_preview read projection
→ memory.search_my_profile / Context Builder
```

- Neo4j 是 durable preference 的 source of truth。
- Mongo `profile_memory_preview`／`profile_memory_summary` 是 bounded read projection，不是另一份可獨立修改的真相。
- `message_id` 是 observation idempotency key；同一 owner message 不得增加兩次 evidence count。
- `profile_memory_outbox` 只保存已驗證的 typed proposals 與 error code，不保存 raw chat。
- 使用者在設定中 disable／restore／correct 後，必須同步更新 Mongo projection。

目前仍有 `/api/memory/observe` 與 direct fallback 等相容路徑。後續重構目標應是讓 `profile_skills.py → /api/memory/apply` 成為唯一 extraction/write flow；不要再新增另一個自由文字 extractor。

### 2.3 雙人關係語意

`semantic_plan_service.py` 只處理 accepted pair room 中雙方真正傳送的訊息：

- `semantic_plans`：room-scoped summary、theme、strategy、role 與處理進度。
- room-scoped KG triples：共同聊天中可驗證的 entity relation。
- 不等於 owner durable memory，也不應自動回寫任一方的 `HAS_PREFERENCE`。
- 對 Public Ayue 或 Private Ayue 只能輸出 consented／shared projection，不能輸出 raw chat 或完整 strategy。

### 2.4 Public Context

`services/ayue_agent/context.py` 是 Public V2 唯一 Context Builder。現在的 budget：

- 最近 12 則訊息，合計最多 6,000 字元。
- 本人近期情境一份。
- 本人長期記憶最多 8 筆。
- 唯一 live proposal 的安全狀態。
- 經 server 驗證的公開 mention。
- Asia/Taipei turn clock 與 capability version。

Context Builder 只組合安全 projection，不負責重新萃取、修正或寫入記憶。

### 2.5 已知技術債（不要沿用成新架構）

- Port 8000 `memory_service.py` 與 port 9001 memory endpoint 目前有重複 Cypher／fallback writer；應逐步收斂成一個 canonical Graph repository，而不是再增加第三條寫入路徑。
- `/api/memory/observe` 仍可自行呼叫 LLM extraction；新版 owner-message pipeline 已由 `profile_skills.py` 負責。新功能只接 `/api/memory/apply` 的 validated proposals。
- `memory_service._agent_graph_config()` 直接讀取 matchmaker `.env` 是部署耦合；未來改由 service configuration 或 port 9001 API 邊界提供，不能讓更多主服務模組直接讀另一服務的 secret file。
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
6. Public runtime adapter 驗證後才切換；V2 失敗仍 fail closed，不回 legacy。
7. 最後才讓 matchmaker 或 long-term advice 消費新 projection。

不要同時改 Graph schema、Context Builder、match ranking 與 UI。每階段都應能單獨 rollback。

## 8. 最低測試清單

- A、B 有相同 Trait key 時，讀取仍嚴格依 owner 隔離。
- 對方私人 memory 永不進 Public context、Private final composer 或 trace。
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
