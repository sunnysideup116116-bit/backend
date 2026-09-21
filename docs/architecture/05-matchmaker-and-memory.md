# 05. 媒婆服務、圖記憶與 Context Engine

> 本篇說明 port 9001 媒婆服務（candidate 排序＋Neo4j 記憶）與主服務的記憶／profile pipeline。完整資料邊界請見 [Memory 指南](../MEMORY_CONTEXT_ENGINE_GUIDE.md)。

## 1. 媒婆服務（matchmaker_agent, port 9001）

### 角色

媒婆是候選排序與 Neo4j 記憶服務，不是聊天 runtime。主服務把「已通過資格檢查的候選集合」與發起者資料送來，媒婆用 LLM 選出 **0 或 1 位**最值得牽線的人，回傳嚴格 JSON。

### API 端點（`agent_api.py`）

| 端點 | 方法 | 用途 |
| --- | --- | --- |
| `/health` | GET | Process-level readiness（不讀 profile／Neo4j） |
| `/api/match` | POST | 候選排序：`{target_user, candidates, target_deep_profile}` → `{outcome: selected\|no_suitable_candidate, matches: [1 筆]}` |
| `/api/feedback` | POST | 只正規化使用者本次明確勾選的 `explicit_reasons`；空清單回 `skipped`。目前提案 UI 僅在 opt-in 婉拒時呼叫，並共用 memory writer 轉成 `AVOIDS` Concept relationships |
| `/api/global_reflection` | POST | 從一對（from_big_five→to_big_five）歸納全域法則 `GlobalRule`（相似度合併，weight 遞增） |
| `/api/memory/apply` | POST | 寫入已驗證的 memory proposals（不重新萃取），message_id 冪等 |
| `/api/memory/{user_id}` | GET | 讀某使用者 active 偏好（owner-scoped） |
| `/api/memory/action` | POST | disable／restore／correct 一筆偏好 |
| `/api/preferences/candidates` | POST | 內部 exact preference retrieval；以 canonical `Concept.key` 回傳最多 100 個候選 ID，不回 raw memory |
| `/api/chat_triples` | POST/GET | 雙人聊天室的三元組（session-scoped，**不**自動成為任一方 durable preference） |
| `/api/clear_graph` | POST | Demo 專用：清空 Neo4j |

### 排序決策（`matchmaker.py:MatchmakerAgent`）

權重：近期情境 context 30% + 雙方 graph memory 25% + deep_profile/價值觀 20% + Big Five 15% + 立即可聊話題 10%。硬性規則：任一方的 `AVOIDS` Concept 明確命中對方公開特質時，原則上不得推薦；沒有值得誠實推薦的人時回 `no_suitable_candidate`，不得硬選。

Preference exact search 是例外語意：候選集合已由 Social 以 canonical Graph evidence 驗證；9001 只收到 `search_intent=preference`、`normalized_topic` 與本輪 query，不收到 canonical key、來源訊息 ID 或 raw memory。Matchmaker 不得因候選人的近期活動不同而否定已驗證偏好，也不得把它擴張成發起者也喜歡或「雙方共同偏好」。

`/api/match` 的處理流程（`agent_api.py:match_endpoint`）：

1. 平行讀取發起者 graph memory、候選人 graph memories、全域法則（strict Graph read；逾時／不可用回傳明確失敗，不以占位資料繼續配對）。
2. 每個 candidate 附加自己的 `graph_memory` 欄位（**candidate 的記憶只代表 candidate**）。
3. `agent.match_async(...)` 呼叫 LLM；解析 JSON 失敗或格式不符 → HTTP 502 `Invalid matchmaker response`（呼叫端不能把 provider 失敗當成「沒有合適人選」）。
4. `matches` 最多保留 1 筆，`matched_user_id` 必須存在。

## 2. Neo4j 圖記憶模型

```
(:User {id}) -[:PREFERS]-> (:Concept {key, label, kind})
(:User {id}) -[:AVOIDS]-> (:Concept {key, label, kind})
(:User {id}) -[:CURRENTLY_WANTS {expires_at}]-> (:Concept {key, label, kind})
(:Agent {name:"System"}) -[LEARNED_RULE {weight}]-> (:GlobalRule {content, category})
(:MemoryObservation {message_id})   ← message_id 冪等（每則 owner 訊息最多套用一次）
(:ChatEntity {key}) -[IS_A|HAS|LIKES|...]-> (:ChatEntity {key})   ← 雙人聊天 triples
```

- 偏好必須 **owner-scoped**：`MemoryObservation.message_id` 唯一約束保證同一訊息不會重複套用。
- `Concept.key` 使用既有 unique constraint／RANGE index；正式環境已驗證為 ONLINE。新 writer 不信任 provider key，而由 `concept_identity.py` 在 server boundary 決定 identity。
- 已知小型 alias 表先處理高信心 variant：`Kpop`、`K-pop`、`K pop`、`k-pop` 都是 `key=k_pop, label=K-pop`。其他概念用保守、穩定的 label-derived identity；不維護大型手寫 ontology。
- `stance=like|require` 投影為 `PREFERS`；`stance=dislike|avoid` 投影為 `AVOIDS`；短期活動意圖另由 recent-context projection 產生 `CURRENTLY_WANTS`。
- `LIKES_TRAIT`／`DISLIKES_TRAIT` 目前只是 Matchmaker LLM／文字投影的 compatibility label；寫入 Neo4j 前必須分別轉成 `PREFERS`／`AVOIDS`，不得建立同名 Graph relationship。
- 敏感內容（種族、宗教、性傾向、疾病等）在寫入前被正則擋掉（`agent_api.py` 的 `protected`）。
- **系統產生的長期建議是 recommendation，不是使用者事實**：禁止寫成 `PREFERS`、`AVOIDS` 或 `CURRENTLY_WANTS`（需獨立 versioned contract + TTL + dismissed state，見 `MEMORY_CONTEXT_ENGINE_GUIDE.md`）。

## 3. 主服務的記憶 pipeline

### 3.1 Profile extraction（`profile_skills.py`）

- 只接受**已保存的 owner 原始訊息**（`public_chat.py` 在保存後先寫入 `metadata.message_use`，再背景排程）；只有 `message-use-v1` 的 `ordinary` source 可處理，calendar operation、assessment、no-memory 與未標記 source fail closed。
- 同一 `message_id` 最多成功處理一次；provider 暫時失敗保留 processing lease，最多三次依 30／120 秒退避，由 `profile-retry-worker` 重試；政策排除與 source 不可用不重試。
- 禁止使用 assistant reply、conversation history、tool result、match state 或對方資料作為寫入來源。
- LLM 只提出 typed `ProfileExtractionDecision`（`profile_contracts.py`）＋原句 `evidence_span`；evidence 必須是 owner message 的連續原文子字串（`_valid_evidence_span`），否則拒絕該欄位。
- 近期情境只保存本人現實活動；找人、配對、提案、等待回覆不得成為近期情境。長期記憶只保存明確且可持續的本人偏好（confidence ≥0.90，`subject=owner`）。
- 每個 durable candidate 只能代表一個 atomic concept。只有明確、短而獨立的列舉才由 deterministic boundary 拆開；例如 `K-pop、J-pop、西洋音樂` 拆三筆，但 `適合讀書的安靜咖啡廳` 保持一筆。9001 writer 再做相同的保守防線，不能繞過 extractor 寫入清楚的複合列舉。
- 同一 candidate／evidence 同時含明確正負 polarity 時 fail closed；只有模型提供 item-level evidence 的獨立 candidates 才分別保留 like／avoid，不能把混合句壓成單一 stance。
- Concept identity 不是 semantic ontology：`韓國流行音樂` 與 `韓流音樂` 仍是不同 identity。P1-A 只能在 retrieval 階段把高分鄰近 Concept 當成 `semantic_related` evidence；不得建立 alias、MERGE Concept 或改寫 durable edge。
- 每訊息可建立的 durable candidates 由兩個服務共用 `DURABLE_MEMORY_MAX_CANDIDATES_PER_MESSAGE`（預設 6、程式硬上限 8）；extraction、retry/outbox、registration 與 writer 都用同一界線。
- 使用者可描述近期想做的事而沒有時間單位；不得把「缺時間詞」當作拒絕理由。
- 顯示摘要由程式投影組合，不直接儲存模型自由文字摘要。

### 3.2 Durable memory facade（`memory_service.py`）

- `apply_profile_memory_proposals`：validated durable-memory write facade，只走媒婆 `/api/memory/apply`。主服務不直接連 Neo4j；9001 不可用、endpoint 缺失或回應無效時，寫入 bounded `profile_memory_outbox` 待重試並明確回 `MemoryWriteError`。
- `refresh_owner_memory_profile`：聊天／init 的共用 lazy refresh，300 秒 TTL、Graph 失敗保留 cache 並於聊天路徑退避 30 秒，設定頁可 force refresh。以 `profile_memory_revision` CAS 避免晚到讀取復活已停用記憶。
- `get_graph_memory_snapshot` 讀 durable-only 偏好；`profile_memory_preview` 最多 12 筆，是 Mongo read projection，**不是第二個 source of truth**。
- `apply_memory_action`：透過 `/api/memory/action` 執行 disable／restore／correct，再同步 read projection。
- `_sync_memory_projection`：把圖記憶壓縮成 ≤12 筆的 Mongo 投影與摘要。
- `memory_outbox_service.py`：以 Mongo lease、bounded exponential backoff 與最多八次嘗試重送已驗證 proposals；不重新讀 raw chat。9001 以單一 transaction 寫入 observation marker 與全部 edges，讓 duplicate delivery 可安全結案。
- 記憶管理以 owner-scoped `MEMORY_DISABLED` 暫存原 relation；restore 不得把 `AVOIDS` 變成 `PREFERS`，correct 不得改寫其他 owner 共用的 Concept edge。設定頁用 status-aware Graph read 刷新 projection，只有 Graph unavailable 才沿用 cache。

已移除的 `/api/memory/observe` 與自由文字 memory extractor 不得恢復。唯一 extraction owner 是 `profile_skills.py`；媒婆只接受已通過 subject、confidence 與原句 evidence 驗證的 typed proposals。

### 3.3 Context Engine 邊界（現況）

`context.py:build_public_agent_turn_context` 每回合組 bounded context：HTTP adapter 先抓最多 32 筆作 sentinel，最終 projection 最多保留 12 則／6,000 字元，帶方向 `relevant_memories` ≤8 筆。`memory.search_my_profile(query)` 可向 Graph 補查 preview 以外的本人 durable 偏好；先 query 匹配再限 8 筆，回傳 unavailable/truncated，未命中不代表從未提過。新 Context Engine 若建置，只能輸出 bounded、versioned typed bundle；Public／Private runtime 各自套用 privacy adapter。Retrieval 必須先做 owner／room／accepted-relation 硬隔離，再做相關度排序、budget、dedup；失敗時回 bounded empty projection 與 error code，不得改抓 raw data。

### 3.4 Intent-aware candidate retrieval（P0）

- `activity`／`recent_context`：維持 Mongo bounded vector retrieval；candidate pool 仍限制為既有 20 人，再依既有小批次送 Matchmaker。
- `preference`：Pi 只在 owner 可見原句明確表示「找喜歡／偏好某主題的人」時建立確認卡。確認後以 canonical key 做 `Concept <-[:PREFERS]- User` exact lookup（最多 100 ID），再套 block、pair history、test cohort、Mongo profile hydration、hard conflict、quota、proposal dedupe 與 consent lifecycle。不得先拿 query embedding 比 candidate recent-context embedding。負向 preference search 尚未有 typed stance，P0 會要求改述／澄清，不得錯送正向 `PREFERS` branch。
- `generic`：暫時沿用既有 bounded vector pipeline；本階段沒有 hybrid full scan。
- 所有分支在 qualification／LLM 前都收斂到既有 `MATCH_CANDIDATE_POOL_SIZE=20`，Matchmaker 最多處理既有三個小 batch。Graph miss 不以 fuzzy inference 宣稱某人有未保存的偏好。
- internal job diagnostics 只保存 bounded intent/topic/key、retrieval source、各階段 count、shared key、hard-conflict key 與 reason-code count；不保存 candidate IDs、raw messages 或 Graph payload。

### 3.5 Explicit preference semantic fallback（P1-A）

- Feature 預設 `MATCH_PREFERENCE_SEMANTIC_MODE=off`；canary threshold 預設 1，只有 `qualified_exact_count == 0` 才能啟動。Exact Graph hits 必須先經 block/history、test cohort、profile hydration 與 hard-conflict qualification，不能用 raw hit count 壓掉 fallback。
- `off`／live `shadow` 保留 P0 的 retrieval 順序、20 人視窗、hydration 後 qualification、空結果 reason code 與 exact-only Matchmaker prompt。提前 qualification 和 semantic evidence 強度檢查只作用於 `active`。Differential regression 直接執行 frozen P0 source 比較候選、payload、quota 與 proposal 寫入。
- `deterministic_alias` 只是 query provenance；alias canonicalize 後仍是 exact key 與 direct evidence。Semantic evidence 永遠是 `semantic_related`，不加入 `shared_persistent_preferences`，也不呈現成 exact／共同偏好。
- Semantic Graph endpoint 先以既有 `concept_embedding_index` 取 bounded ANN Concepts，再以 indexed `Concept.key` 展開 `PREFERS`。每 Concept 的 streaming fanout limit 在 exclusion、排序與 aggregation 前生效；熱門 Concept 可能因此 underfill，不會加大掃描。所有上限維持原設定；不存在全圖 cosine scan 或 LLM synonym inference。
- 同一 candidate 的多個 Concept hits 先依 similarity 降序、Concept key 升序保留 bounded best evidence，再依 exact/alias first、semantic score 降序、candidate ID 升序決定 internal order；最終仍只有既有 20 人 pool。
- 重用 query Concept 向量需要 768 維、finite/nonzero，以及 `embedding_model`／`embedding_task` 與 query space 相符；無法確認時只 embed canonical label，不保存 query vector。ANN Concepts 缺少或不符合 model/task 證據時，在 user expansion 前回 `semantic_readiness_unconfirmed`。不新增 fingerprint 寫入或補資料流程。
- Graph／provider／index failure 在 exact 合格人選為零時是 typed transient failure；只有正常 ANN／qualification 無 ground 才是 `insufficient_semantic_ground`。Provider timeout 與 SDK retry、key failover 共用 deadline；late response 不作為結果。
- `shadow` 使用獨立 observation CLI，live request 不呼叫 semantic。Readiness audit 唯讀檢查 index state/schema、768 維、coverage；historical fingerprint=`unknown` 時，即使 operator confirmation flag 為 on，也不會回 production ready。這項 technical debt 必須另外取得可驗證證據，這一版保持 `mode=off`／`embedding_space_confirmed=off`。
- `0.82` 維持 provisional；fixtures 只驗證 threshold 行為，不是實測 precision。真實 calibration 留給 staging/read-only observation。
- P1-A 不修改 generic/activity/recent-context retrieval、Event-Driven Match、Concept write path、quota、proposal dedupe、confirmation 或 mutual-consent lifecycle。

### 3.6 Recent Activity Context expiry

`recent_context_state`、`current_context`、typed `context_signals` 與 embedding 都是短期活動 projection，不是 durable preference。具有 `recent_context_expires_at` 且已過期（或格式無效）時，read-only projection 會清空 context/signals/embedding；matching、Public／Private context、proactive care、contacts、Event proposal snapshot、Graph projection 與 Event relevance queries 都不得再把它當 active evidence。只含 `recent` evidence 的衍生 Event link 必須仍能對回同一個未過期 `CURRENTLY_WANTS`。缺少 expiry 的舊資料暫時維持相容讀取；TTL 數值仍是既有設定，本階段沒有猜測或調整天數。

舊 Concept 只提供唯讀稽核：`python scripts/audit_canonical_preferences.py --limit 100`。工具沒有 apply mode，只輸出 old concept、明確可拆 atoms、alias normalization、受影響 user／edge counts；模糊自然語言只列 ambiguous，不自動拆，也不顯示 user ID。

## 4. 配對狀態真相（canonical lifecycle）

- Lifecycle：`draft → pending → accepted`；`declined`／`expired` 為終態。
- `match_decision_service.py:apply_match_decision` 是唯一 CAS 轉移：`status + proposal_revision` 條件更新（`find_one_and_update`），stale 回報最新狀態且不覆寫；`idempotency_key` 存於 `last_decision`，重放回 `idempotent: true`。
- 一般與 Event 的 namespace／名額政策分開；Hub 可同時有多張卡。本人發起的未決 draft 或 queued/running 搜尋阻擋新搜尋，等待對方及收到邀請不一律阻擋。`accepted` 是已建立聯絡關係，不是 live proposal。
- Durable search job 會綁定建立時的 `current_context_revision`。若 concurrent recent-context extraction 在搜尋中提交新 revision，worker 會以 Mongo CAS 將同一 job 最多重排一次並從最新 snapshot 重跑；queued/running 狀態持續可見。第二次仍變動才終止為 stale，並投遞 idempotent `match_search_failed` 說明，不得無聲消失或無限重跑。
- 效果（通知、開聊天室、Event 開場卡、GIF、opt-in feedback）只在 transition 成功後執行（`match_action_service.apply_transition_effects`）；effect 失敗不讓已提交 transition 被重送。Event invitation 對既有 accepted pair 沿用 canonical chat，並以 match-scoped key 冪等保存一次公開活動介紹，不建立第二個 relationship anchor。

## 5. 雙人關係 context（不可誤用）

- `semantic_plan_service.py`：accepted pair room 的 shared semantic plan 與 chat triples 屬於雙人關係 context，**不得**自動轉成任一方 durable preference。
- `relationship.get_verified_evidence`、`get_mentioned_contact_summary`：只能讀 canonical accepted relation 的公開投影。
- 媒婆的 `/api/chat_triples` 以 session_id 隔離，只服務雙人聊天室的關係脈絡。
