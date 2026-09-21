> **架構更新註記**：本文保留其 domain／歷史內容；其中公開 V3 Planner、Scheduler、subagent 或 DAG 的描述已被 Pi 正式架構取代。
>
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
| 本人長期記憶 | Neo4j `User-[:PREFERS\|AVOIDS]->Concept`，Mongo preference_facts 保存證據／生命週期 | 明確且可持續的本人偏好與排斥地雷；CURRENTLY_WANTS 另屬有期限的近期意圖 | 系統建議、一次性活動、對方特徵 |
| 雙人關係語意 | Mongo `semantic_plans` 與 room-scoped KG triples | 已接受關係中的共同話題、互動節奏與 mediator strategy | 任一方私人悄悄話、跨房間資料 |
| Agent 每回合 context | `PublicAgentTurnContext` | 讓 Planner 在有限 token 與隱私邊界內做當回合決策 | raw Mongo／Neo4j document、內部 ID、對方私人資料 |

「長期建議」是系統推導出的 recommendation，不是使用者記憶。即使建議來自 Graph Memory，也必須保存到獨立 read model，並保留來源、版本、有效期與可撤銷狀態；禁止寫成 `PREFERS`、`AVOIDS` 或 `CURRENTLY_WANTS`。

## 2. 現有資料流與 source of truth

### 2.1 近期情境

```text
Saved owner message
→ profile_skills.py typed extraction
→ evidence / subject / confidence validation
→ programmatic Traditional Chinese projection
→ Mongo revision CAS
→ profile.get_recent_context
→ PublicAgentTurnContext
```

- Source of truth：`recent_context_state` 與 `current_context_revision`。
- `current_context` 是給 UI／Planner 的安全顯示 projection。
- `recent_context_expires_at` 是 canonical active boundary；明確過期或格式無效時，所有 read projection 必須清空 `current_context`、typed signals 與 embedding，不可再用於 candidate retrieval、reasoning 或 UI。沒有 expiry 的舊 profile 暫時維持相容讀取；本階段不調整 TTL 數字。
- 只接受 owner 已保存的原始訊息；assistant reply、history、tool result 與 match state 都不是寫入證據。
- 每個 evidence span 必須是該 owner message 的連續原文子字串。
- Public message 的 `metadata.message_use`（`message-use-v1`）是 profile、compaction 與 proactive surface 共用的用途標記。`ordinary` 才可重用；`calendar_operation`、`assessment`、`no_memory` 與未標記的 `unknown` 一律排除。行事曆草稿、補充、確認、取消與失敗回合都沿用同一排除標記。
- 用途標記在 server 完成 V3 turn 後寫回原始 owner message；background coverage、profile extractor、compaction 與 care delivery 都再次驗證，不能只信 router 當次的結果。

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

- 正式 Graph 節點型別為 `Concept`（屬性 `{key, label, kind}`），關係只使用 `PREFERS`、`AVOIDS` 與 `CURRENTLY_WANTS`。`Trait`／`HAS_PREFERENCE` 只是 migration compatibility，新程式不得再產生。
- 正向偏好使用 `PREFERS`；負向地雷使用 `AVOIDS`；短期意圖使用帶 `expires_at` 的 `CURRENTLY_WANTS`。
- 每個 durable memory candidate 只表示一個 atomic concept。明確列舉可拆多筆；描述性名詞片語不可用標點或斷詞粗暴拆分。
- 含明確正負 polarity 的單一複合 candidate fail closed；模型必須用 item-level evidence 分成獨立 candidates，避免把其中一邊的 stance 丟掉。
- `concept_identity.py` 是 server-owned identity boundary：已知 K-pop variants 收斂成 `k_pop`／`K-pop`，未知概念使用穩定 label-derived identity；provider 提議的 key 不能直接成為 Graph identity。
- `韓國流行音樂`／`韓流音樂` 等 semantic synonym 不在 P0 alias 表內，仍是不同 identity；semantic Concept retrieval／合併留給 P1。
- 每訊息 limit 由 `DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE` 統一管理（預設 6、硬上限 8），套用於 extraction、outbox、registration 與 9001 writer。
- Mongo `profile_memory_preview`／`profile_memory_summary` 是 bounded read projection；Graph metadata 與 evidence／lifecycle 不得藉由舊 `HAS_PREFERENCE` properties 重複儲存。
- `message_id` 是 observation idempotency key；同一 owner message 不得增加兩次 evidence count。
- `profile_memory_outbox` 只保存已驗證的 typed proposals 與 error code，不保存 raw chat。
- Profile extraction 的 `profile_skill_runs` 以 message-id 唯一去重，保留 processing lease 與 bounded attempt state；provider 暫時失敗由 Social `profile-retry-worker` 依 30／120 秒退避最多重試三次，政策排除與未標記 source 不重試。
- `memory_outbox_service.py` 以 Mongo lease 領取失敗寫入、指數退避並最多嘗試八次；舊資料的 `next_attempt_at=null` 與欄位不存在都視為立即可重試。成功、重複 delivery 與 terminal failed 都有明確狀態，不會重新萃取 raw chat。
- 9001 將 `MemoryObservation` marker 與該訊息的全部 Concept edges 放在同一 Neo4j transaction；marker 不得早於 edge 單獨 commit。
- 使用者 disable 時以 owner-scoped `MEMORY_DISABLED` 保存原 relation；restore 依原 `PREFERS`／`AVOIDS`／未過期 `CURRENTLY_WANTS` 恢復，correct 將該 owner edge 搬到新的 canonical Concept，不修改其他 owner 共用的 Concept 關係。完成後同步 Mongo projection。
- 設定頁每次讀取都嘗試用 status-aware Graph snapshot 刷新 bounded Mongo projection；Graph 明確為空可清除 stale cache，Graph unavailable 才沿用 cache。

舊版 `/api/memory/observe`、主服務直接 Neo4j fallback 與自由文字 extractor 已移除；`profile_skills.py → /api/memory/apply` 是唯一 owner-memory extraction/write flow。不要再新增另一個自由文字 extractor。

### 2.2.1 使用者勾選的婉拒回饋

另有明確註冊表單來源的 [註冊興趣 bootstrap](REGISTRATION_GRAPH_BOOTSTRAP.md)：
註冊初始化排入既有 outbox，重新驗證 Appwrite，本人興趣沿用 typed extractor 與 memory facade，
只對沒有任何既有／停用記憶的帳號 insert-only 補 PREFERS。User 以 id 唯一識別、name 顯示暱稱。
此例外不放寬聊天來源限制，不修改近期情境，不另建自由文字 extractor。

提案視窗的 `explicit_reasons` 是本人明確選擇並同意記錄的結構化回饋，不是從 match state 或對方特質推測本人的偏好：

```text
使用者選擇「記錄原因並婉拒」
→ /api/match/decision 的既有 CAS 成功
→ match_action_service 的 optional feedback effect
→ 9001 /api/feedback 的既有 normalizer（只正規化這次勾選清單）
→ 共用 /api/memory/apply → User-[:AVOIDS]->Concept
→ Social upsert_preference_facts（match_feedback source + match/message evidence）
```

- 「只婉拒，不記錄」、空清單或撤回邀請不啟動此流程；不能用對方 Big Five 或舊的 process-local feedback history 補出未勾選的偏好。
- Client 不自行按「近期情境／興趣／個性」分類刪除選項。Normalizer 的舊 `DISLIKES_TRAIT` output 只作為相容輸入，轉成 typed `stance=avoid`；Graph 不產生 Trait／DISLIKES_TRAIT 舊模型。
- 寫入沿用既有 memory validation，不另維護 feedback Cypher writer；單次 feedback 超過共用 runtime limit 時整次 fail closed，不分批繞過上限，也不靜默截掉後續本人已勾選原因。Request 本身另有 hard max 8。
- `/api/feedback` 沒有勾選時回 `skipped`，有效但空的正規化結果回 `no_preferences`；正規化失敗回 502，Graph 不可用回 503，不假裝寫入成功。Feedback 失敗不撤銷已提交的 decline，也不能重送 decision 作為偏好重試。
- API 回傳 `memories` 供 Social 寫入既有偏好 facts。畫面上的「已送出」不是 Graph 成功收據；此 optional effect 尚非 durable retry queue。

### 2.3 對話壓縮與延續性（Compaction 機制）

目前有 operator 批准的受控全域試行、owner-only 進度 API 與持久重建 worker。獨立合成 benchmark 可作為明確的 operator 批准依據，但不灌入線上評估數據；未批准時 `*` 才沿用原 readiness 門檻。批准後低使用量僅反映健康資料不足／過期，足夠線上樣本顯示品質退化才自動暫停。政策、模型或程式指紋改變須重新批准，逐份摘要驗證不變。詳見 [操作指南](SUMMARY_ROLLOUT_OPERATIONS.md)。

#### 2026-09-15 目前部署狀態

目前以 `conversation_compaction_policy_v5` 在 Public Pi runtime 服務。受控全域試行已由 operator 以 70 例／14 類合成 benchmark 批准，並綁定 exact policy、model、provider 與 algorithm fingerprint。compaction producer 仍使用 `COMPACTION_MODE=shadow` 的安全 shadow-run 標籤；summary consumer 則由 `CONTEXT_MODE=on`、`CONTEXT_USER_ALLOWLIST=*` 與有效 approval 啟用，因此不是 shadow-only。46/46 現有 profile 通過 eligibility，新註冊 owner 也走同一 wildcard 規則。

每回合 Context 仍只注入通過 owner／room／policy／source／evaluation 檢查的 room-specific continuity。長對話 compaction 以完整訊息連續前綴工作：最多 9,000 字元來源、最多 11 則舊訊息、保留最新 20 則；支援 Mongo `ObjectId` 與合法 `system-event:<sha256>` ID，system-event 內容不進摘要。欄位超限會要求有界 contract retry；品質 review 會以具體 issue codes 做一次 semantic repair，仍不合格就保留上一份合格摘要與 watermark。

重建已改為持久 job：owner／room scope、atomic claim、lease renewal、bounded retry、worker restart recovery、status API 和去重都保留。`GET /api/conversation/summary-status` 只回狀態／數量，`POST /api/conversation/summary-rebuild` 只接受 owner JWT 與 room ID；兩者不回摘要原文或 rollout authority。Context Builder 仍是 read-only，不直接改 memory 或 application state。

目前是「受控全帳號可用」而非全量品質門檻完成：最後監測快照為 45 筆 live evaluations、pass 0.9333、review 0.0667，原定 50 筆／95% observational gate 尚未通過；review-held room 不會被強制注入。短對話使用近期歷史，不需要產生摘要。完整修復與部署證據見 [global stability acceptance](../../artifacts/summary-global-stability-2026-09-15.md)、[mixed-ID repair](../SUMMARY_MIXED_ID_FIX.md) 與 [review repair](../SUMMARY_REVIEW_REPAIR.md)。

#### 2026-09-14 指定帳號歷史 canary 驗收

這是當時以 Public V3 DAG、指定 demo 帳號完成的真實摘要生成／評估與公開聊天 API canary；它保留作為歷史證據，不代表目前 Pi runtime、全帳號部署或最新 review repair。當時的 12-message／6,000-character budget 也不是現行 compaction budget。現行狀態請以本節 2026-09-15 段落及 [global stability acceptance](../../artifacts/summary-global-stability-2026-09-15.md) 為準。正式 `.env` 不納入版本庫。

- **觸發條件**：單一聊天室累積訊息超過 **30 句**時觸發。
- **壓縮策略**：從最舊最多 **11 則**訊息選取符合 9,000 字元來源預算的完整連續前綴，保留至少最新 **20 則**未壓縮訊息。不再逐則截前900字；生成與評估使用同一份完整來源。單則超過來源預算時回 `source_over_budget` 並延後，不略過該則，也不推進其 watermark。
- **儲存位置**：MongoDB `conversation_compactions`，並帶 `covered_through_message_id` watermark。
- **聊天室範圍**：永久 legacy room 與 `ai_rooms` 中經 server 驗證屬於 owner 的新版聊天室各自壓縮；刪除、偽造或他人 room fail closed。壓縮前的 profile coverage 使用同一 owner-room validator。
- **用途隔離**：只有 `message-use-v1` 的 `ordinary` message 送入摘要模型；被排除的訊息仍計入批次 watermark，但不送入 generation/evaluation。整批皆被排除時保留既有合格摘要並直接推進 watermark，不呼叫模型。舊版或未標記來源不作為遞迴摘要基底。
- **政策版本**：目前 compaction policy 為 `conversation_compaction_policy_v5`；舊版摘要不再注入或作為遞迴基底，既有原文保留，後續維護回合由原文重新選批。
- **輸出與 review 修復**：來源有可重用文字卻得到全空摘要時，以 `empty_summary` 進行有界修復；欄位超限／不安全 normalization 會拒絕並帶 field/count feedback 重試。品質 review 會以原始 prior/source 和 issue codes 進行一次 semantic repair，再重新評估；仍不合格則不更新合格摘要或 watermark。純閒聊也可能因此保守延後；不為填滿摘要而捏造內容。生成／評估失敗保留上一份現行政策的合格摘要。
- **模型契約**：generation／evaluation 讀取 `generate_chat_completion()` 的 `ChatResult.content`；測試不得再用不符合正式 contract 的純字串掩蓋介面漂移。
- **啟用閘門**：摘要 consumption 需 `CONTEXT_MODE=on`、wildcard／owner allowlist 與逐份摘要 validation。明確 operator approval 綁定 exact policy/model/provider/fingerprint 後可啟用受控 `*` global trial；未批准時 `*` 回到原 rollout readiness（至少 50 筆、pass ≥95%、review ≤5%、unavailable ≤2%、指標未過期）。approval、live health 與逐份摘要 validation 分離；review-held 摘要不注入，consumer decision 最多 cache 60 秒。

#### 2026-09-04 整合環境基線

- 私有 `social/.env` 使用 `AYUE_CONVERSATION_COMPACTION_MODE=shadow` 與 `AYUE_CONVERSATION_CONTEXT_MODE=on`；私有環境檔不得提交。
- `AYUE_MEMORY_OUTBOX_WORKER_ENABLED`／`AYUE_MEMORY_OUTBOX_POLL_SECONDS` 未覆寫，因此沿用 worker 啟用、30 秒輪詢的程式預設。
- `shadow` 會產生與評估 room-scoped summary，但不等於無條件注入 prompt；continuity 仍須通過 owner/room、source hash、watermark、evaluation 與 rollout gate。
- UI 可用重新載入／跨阿月房間後仍能回想 owner 明確保存內容，驗證 durable memory；是否真的完成 compaction 則應查 `conversation_compactions` 與 watermark，不能只憑聊天回覆猜測。

### 2.4 雙人關係語意

`semantic_plan_service.py` 只處理 accepted pair room 中雙方真正傳送的訊息：

- `semantic_plans`：room-scoped summary、theme、strategy、role 與處理進度。
- room-scoped KG triples：共同聊天中可驗證的 entity relation。
- 不等於 owner durable memory，也不應自動回寫任一方的 `PREFERS`、`AVOIDS` 或 `CURRENTLY_WANTS`。
- 對 Public Ayue 或 Private Ayue 只能輸出 consented／shared projection，不能輸出 raw chat 或完整 strategy。
- Updater 只計算尚未處理的 pair-room 訊息，並以內容長度作保守 budget proxy；累積至少 600 字元單位才更新。短訊息數量本身不觸發更新，避免只因訊息筆數達門檻就呼叫模型。
- Private V2 由 `PrivateAgentTurnContextV2` 獨立 adapter 讀取 current accepted pair room。Planner projection 只允許 role、macro summary、theme、action plan、dynamic bounds 與最多 20 筆 triples；final composer 再縮小為 role、macro summary 與 triples。Raw plan/Graph 欄位與其他 room 的資料不得進 prompt。
- Neo4j relationship read 失敗時可退回同一 semantic plan 已保存的 bounded triples；兩者都沒有時回空集合，不得改抓 owner durable memory 或跨 room Graph。

### 2.5 Public Context

常駐 8 筆以外的記憶：`memory.search_my_profile(query="主題 同義詞")` 經 Profile agent 與中央 Guard，
由 owning memory service 唯讀查 9001。Graph 先限制本人 durable relations，再依 query 字詞（中文 bigram／英文詞）
匹配 label/key 與排序，最後限制回傳 8 筆，另讀一筆判定 truncated。空 query 是一般偏好列表，不宣稱全部。
這是字詞檢索加模型提出的同義詞，不是新的語意 embedding 搜尋；未命中不能證明本人從未提過。
結果只含帶方向的安全文字、available/unavailable、graph/cache 與 truncated，不含 owner/key/id；
查詢不覆寫一般 preview。服務不可用沿用 bounded cache 並明確標 unavailable/truncated。
Planner 對偏好回想與個人化建議安排 Profile 補查。Planner／Synthesizer 明定未確認的 assistant 推測不是 owner 事實。

2026-09-08：Public HTTP adapter 與 init 在進入 Context 前呼叫共用 `refresh_owner_memory_profile`。
Mongo preview 非空也會在 300 秒後刷新；Graph failure 保留快取並於聊天路徑退避 30 秒，設定頁可強制刷新。
Graph read 使用 1 秒 connect／2 秒 read timeout 與 `durable_only=true`，僅取 PREFERS／AVOIDS；
CURRENTLY_WANTS 留在近期狀態，預設 Graph API 也不回傳已過期短期意圖。
所有新 cache refresh 以 profile_memory_revision 做 CAS，成功偏好寫入／action 會失效該 revision；
disable／correct 先移除對應 cache key，較晚的旧讀取不得恢復它。Graph read failure 不以新學的單批資料覆蓋全部 cache。

`owner_memory_projection.preference_wording` 將 typed stance 投影為「喜歡／不喜歡／避免／需要：標籤」，
最多 8 筆；排除異 owner、want、disabled、未帶 stance 的舊文字及不安全標籤。
Public agent 的 direct-chat 路徑只接收這個帶方向的封閉文字格式；Profile／Relationship slice 與 Pi presentation
沿用同一份 relevant_memories，防止普通聊天漏掉偏好。Context Builder 自身仍只讀、不刷新或寫 DB。
Concept.kind（interest／activity／partner_trait 等）與 User relation 的 PREFERS／AVOIDS 是不同欄位。DatingApp 記憶頁現行只呈現 prefer／avoid：like/require → prefer，dislike/avoid → avoid；不把 kind 當偏好方向。

`services/ayue_agent/context.py` 是 Public runtime 唯一 Context Builder。HTTP adapter 先抓最多 32 筆（含一筆 sentinel 用來標記是否超出來源），最後 projection 的 budget 是：

- 最近 12 則訊息，合計最多 6,000 字元。
- 本人近期情境一份。
- 本人長期記憶最多 8 筆。
- 配對搜尋與牽線收件匣的安全狀態；單卡相容欄位不表示帳號只能有一張卡。
- 經 server 驗證的公開 mention。
- Asia/Taipei turn clock 與 capability version。

Context Builder 只組合安全 projection，不負責重新萃取、修正或寫入記憶。原文視窗和 compaction watermark 使用相同的 `(timestamp, _id)` 排序；超出 12 則／6,000 字元時回 bounded recent-only projection，並以 adapter 的 sentinel 標示來源是否超過 32 筆，不宣稱完整覆蓋。

### 2.6 已知技術債（不要沿用成新架構）

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

### 4.1 現行最小 schema

```text
(User {id})
  -[:PREFERS]->
(Concept {key, label, kind})

(User {id})
  -[:AVOIDS]->
(Concept {key, label, kind})

(User {id})
  -[:CURRENTLY_WANTS {expires_at}]->
(Concept {key, label, kind})

(MemoryObservation {
  message_id,
  owner_user_id,
  created_at
})
```

正式 Graph 不再建立 `Trait` 節點或 `HAS_PREFERENCE` 關係；兩者只能存在於 migration／compatibility 邊界。
Evidence、confidence、active state、provenance 與 lifecycle metadata 保留在 Mongo canonical records，
Neo4j 只保留配對與 Event traversal 需要的最小 relation projection。

可以新增 evidence reference、conflict、superseded 或 decay metadata，但：

- 不保存完整 owner message；最多保存不可逆 hash、message ID 或 bounded evidence span。
- 所有 relation 必須 owner-scoped，禁止只靠 Concept key 反查後把 A 的偏好給 B。
- Protected／敏感屬性不可成為交友篩選記憶。
- 使用者修正或停用記憶時，先更新 Mongo canonical state，再刪除或重建該 owner 的 Concept relation projection；不要因單一使用者操作刪除共用 Concept 節點。
- Schema migration 預設 dry-run，顯示數量與去識別化範例；review 後才 `--apply`。

### 4.2 Matchmaker 使用方式

- `target_user.graph_memory` 只能代表發起者本人。
- 每個 candidate 的 `graph_memory` 只能代表該 candidate。
- Matchmaker 可以把記憶當 ranking signal，不能把低信心推論寫回 Graph。
- Hard conflict 必須在 LLM ranking 前做 deterministic qualification。
- Generic／activity search 沒有 Graph evidence 時可維持既有 bounded ranking；explicit preference search 則以 exact canonical Graph evidence 為 retrieval 前提，Graph unavailable 明確失敗，Graph miss 回沒有候選，不得改用 recent context 猜偏好。
- Explicit preference flow 是 `canonical Concept → bounded PREFERS user IDs → block/history/profile filters → deterministic qualification → existing small Matchmaker batches`。Graph 分支不得 full scan，也不得繞過 quota、confirmation、mutual consent 或 proposal lifecycle。
- P0 explicit preference 只支援正向 `PREFERS` exact evidence；「找不喜歡／避免 X 的人」沒有 typed search stance，必須 fail closed 而不是改走正向 Graph 或 activity branch。
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
- 主動追問先由 owner-scoped `proactive_followups` 保存 typed topic、question goal、source 與 expiry，再由 server scheduler 做 consent、48 小時／七日上限、未回答後七日冷卻、quiet/busy 與 lease gate；active slot 與固定 message event key 分別防止候選競態超額與重試重複訊息。`proactive_care.py` 只接收 bounded candidate grounding，以及保留 like/dislike/avoid/require 方向的安全 memory wording，不把 frequency selector 或 Calendar 詳細內容送進模型。

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

1. 先寫現況 fixture：owner memories、矛盾、disable、Neo4j timeout、兩位使用者相同 Concept。
2. 定義 versioned memory／context contracts 與 privacy projection。
3. 將現有 Graph read/write 包在 repository 或 domain service，保留 API 相容。
4. 建立 retrieval／ranking／budgeting 的 deterministic tests。
5. 用 shadow mode 比較舊 `PublicAgentTurnContext` 與新 bundle 的選取結果；shadow 只記 metadata，不記內容。
6. Public runtime adapter 驗證後才切換；V3 失敗仍 fail closed，不回 legacy。
7. 最後才讓 matchmaker 或 long-term advice 消費新 projection。

不要同時改 Graph schema、Context Builder、match ranking 與 UI。每階段都應能單獨 rollback。

## 8. 最低測試清單

- A、B 有相同 Concept key 時，讀取仍嚴格依 owner 隔離。
- 對方私人 memory 永不進 Public context、Private final composer 或 trace。
- Private relationship projection 僅限 current accepted pair room、allowlisted semantic-plan 欄位與最多 20 筆 triples；raw Mongo／Neo4j 欄位不進 Planner 或 composer。
- 未滿 600 字元單位的未處理短訊息不觸發 semantic update；達門檻後的 chat-log projection 必須保留實際 sender/content，而不是未展開的格式字串。
- 同一 message ID 重試不增加 evidence count。
- disable／restore／correct 會同步 Graph source 與 Mongo preview。
- 矛盾 evidence 不被後到的低信心訊息靜默覆蓋。
- 一次性活動只進近期情境，不進 durable memory。
- 長期偏好只進 durable memory，不污染近期情境。
- 複合與分開輸入的明確偏好在 canonical level 等價；Kpop／K-pop alias 共用 Concept。
- Explicit preference search 不因 candidate 的 unrelated recent activity 被排除；activity search 仍由 active recent context/vector 主導。
- 過期 recent context 不進 vector qualification、Matchmaker payload、公開 context 或 proposal snapshot。
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

Graph reads used by generic/activity matching remain optional bounded signals.
Explicit preference search requires exact durable Graph evidence: an empty
result is a normal bounded miss, while Graph unavailable is an explicit typed
pipeline failure and must not silently become vector/fuzzy inference. Neither
case is a successful write or permission to read raw data. The local Demo Graph reset is
explicitly guarded and returns no raw Graph data. Full Demo reset clears Graph,
Mongo app collections, and process-local fallback state in a fixed order; it
does not promise cross-store rollback.
