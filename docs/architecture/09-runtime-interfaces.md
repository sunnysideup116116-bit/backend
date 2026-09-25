# Public Pi runtime interfaces

> 本文件是現行介面。舊 Planner／Scheduler／subagent 文件只供歷史查閱。

## Public HTTP boundary

Preference bootstrap adds owner-authenticated `/api/profile/preferences/bootstrap`
preview/commit/status/reconcile/rollback, default OFF. The private 9001 counterpart
uses a distinct path/body/time-bound HMAC scope, not a model/user-supplied owner.
See [transaction/fence/recovery contract](../PREFERENCE_BOOTSTRAP_RUNTIME.md).
No new Pi tool or matching confirmation bypass is introduced.

`GET /api/conversation/summary-status?room_id=...` 與 `POST /api/conversation/summary-rebuild` 使用Appwrite Bearer JWT解析owner。GET只回有界狀態／數量，POST body僅接受room_id；不回摘要原文、其他房間或rollout authority。全域試行批准只由本機operator CLI寫入獨立rollout record；Public Context gate保持read-only。正常turn的compaction改為持久job，由Social lifecycle worker執行既有profile coverage與摘要管線。詳細契約見 [Summary operations](../SUMMARY_ROLLOUT_OPERATIONS.md)。

`routers/public_chat.py` 驗證 owner 後建立 `PublicAgentRequestContext`，呼叫 `run_public_agent_turn()`。公開聊天的 JSON 與 NDJSON URL、choice fields、interaction blocks、sources 與主要 response shape 保持相容；新回覆的 `agent_mode`、`agent_version` 固定為 `pi`。

未驗證 owner 回 `401`；已驗證 owner 的資源權限錯誤回 `403`；Pi bridge 不可用回 `503`。這些失敗發生在保存 owner message 及任何工具執行之前。

## Pi loop

```python
run_pi_public_turn(
    ctx: PublicAgentRequestContext,
    *,
    on_progress=None,
    on_token=None,
    debug_enabled=False,
) -> AgentResult
```

Pi 每回合只看 `pi/registry.py` 的明確 allowlist。Python 保有 provider credentials、context、budgets、guards、confirmations 與 writes；Node bridge 只協調模型與工具呼叫訊息。

## Shared confirmation and writes

`shared/confirmation.py` 擁有 public/private surface、preview fingerprint、opaque choice、TTL、CAS 與 idempotent execution。新 public record 明確標記 `source_engine=pi`；private record 標記 `private_v2`。

Calendar command/preflight、contact selection、operation batch、write executor、public reply validation、debug trace 與短期狀態皆位於 `services/ayue_agent/shared/`。Pi 與 Private 不得匯入已退役的 runtime package。

`match.start_search` 的正式 intent 為 `activity`、`recent_context` 或 `preference`。明確「找喜歡／偏好 X 的人」由 server 再驗證 owner 可見原句並建立正向 `preference` confirmation；確認後走 canonical Graph exact retrieval。負向 preference query 在 P0 fail closed。活動與近期情境維持 bounded vector retrieval，generic request 暫時沿用既有流程。模型不能用 recent context 推測 durable preference，也不能提供 canonical key 或候選 ID。

[Identity v2](../PREFERENCE_IDENTITY_V2.md) 的 preference topic 是完整 bounded semantic text（500），另有 display label；key/version/hash 由 server 產生並驗證。已持久化 confirmation/job 缺可信 v2 metadata 時要求重新確認，不從舊縮短 topic 推回原意。Matchmaker provider projection 仍只接受既有允許欄位，不公開 internal proof。

Fresh preference 輸入一律經固定 OpenCC 1.4.1／`s2twp` boundary，再進既有 deterministic alias／identity stage；缺套件或版本不符 fail closed。Stored v2 不做 fresh conversion。Social → 9001 exact／semantic request 攜帶完整 `canonical_key`／`canonicalization_version`／`semantic_text`／`semantic_input_hash` packet，驗證而不重轉；raw topic-only API 才作 fresh conversion，partial packet 不降級為 raw。

Durable memory 的 500 上限以 raw Unicode codepoints 計算，正規化後也需符合上限；Dart voice executor 使用 `runes`、保留完整原文，並由 generated catalog 取得上限。Supplementary emoji 算 1，combining marks 分別計數；不是 UTF-16 `String.length`。Registration form 維持獨立 120 上限。

## Background and voice boundaries

Registration Graph `registration-bootstrap-v1`：Social profiling 初始化／profile 更新只 enqueue，
既有 memory outbox worker 重新驗證 Appwrite，透過 9001 `POST /api/users/registration-projection`
同步 User.id/name。`POST /api/v2/memory/apply` 的 `surface=registration_interest` 採 insert-only 初始
偏好，不覆蓋既有／停用記憶。`GET /api/registration-graph/status` 只提供版本與 worker 活性，
不回帳號資料；供補資料 CLI 防止對舊 worker 投遞新 job。詳見 [bootstrap contract](../REGISTRATION_GRAPH_BOOTSTRAP.md)。

註冊興趣與一般 owner message 共用 atomic/canonical memory boundary：明確列舉拆成獨立 Concept，K-pop deterministic aliases 收斂到相同 v2 digest identity；`k_pop` 僅為歷史 v1 key，舊節點不重寫。單訊息數量受同一 configurable limit 約束。

新 Social 的 memory apply/action、feedback、Concept vector projection 使用 `/api/v2/` mutation 路徑，避免對舊 9001 寫入被截短的資料。新 9001 的舊路徑 alias 也執行相同嚴格驗證。部署需一致升級，不能把 404 fallback 成舊 writer。

- Profile extraction 與 proactive care 是 owner-message 背景流程，不由 Pi 工具啟動。
- App voice 的 `ayue.public_query` 使用公開 Pi HTTP API；private query 繼續使用 Private V2。
- Registration voice 與 App voice 的 Gemini session 不改為 Pi。
- Domain writes 始終由既有 Calendar、Match、Relationship、Profile services 擁有。

## P1-A preference semantic internal APIs

- `POST :9001/api/preferences/candidates`：P0 indexed exact canonical lookup，唯讀 `Concept.key <-[:PREFERS]- User`。
- `POST :9001/api/preferences/semantic-candidates`：只在 Social 已確認 qualified exact 不足時呼叫；先查既有 `concept_embedding_index`，再依有限 Concept keys 讀取有界 `PREFERS` 關係。Body 的 `embedding_model` 為 Social configured provider model；query vector 與 ANN hit 的 model/task 必須相容。只回 internal IDs、keys、rounded scores，不回 raw memory。向量相容性無法確認時回 typed transient failure。
- `GET :9001/api/preferences/semantic-readiness`：獨立 read-only operational audit；回報 index schema/state/dimension、PREFERS-connected Concept coverage，以及 historical fingerprint=`unknown`。不建立 index、不投影 embedding；unknown 不可用 env confirmation 覆寫為 ready。
- Social 預設 off；`shadow` 使用獨立 CLI，不進 live request。只有 `active` 加上確認且相容的 embedding evidence 才允許同步 fallback。`0.82` 是 provisional synthetic-fixture threshold。
- 9001 Matchmaker 僅對確實帶有 validated semantic evidence 的 batch 使用 semantic prompt；exact-only prompt 與 P0 相同。Internal evidence 是獨立欄位，公開 `match_basis`、state、history、delivery、opening 不回 matched Concept key／label。

## Related-interest v1 pilot (default OFF, not deployed)

本節是新的 [internal app-wide policy](../RELATED_INTEREST_PILOT_V1.md)，不改寫舊研究結論。
Social active fallback 必須另有 `MATCH_RELATED_INTEREST_ENABLED=on`，且只在 qualified exact=0 呼叫
`POST :9001/api/preferences/related-interest-candidates`。新 endpoint 只查 `embedding_v2` 專用 index，
驗證完整 source/fingerprint 後先判 relation，再展開有界 PREFERS owners；拒絕／ERROR 不展開。
`GET :9001/api/preferences/related-interest-readiness` 唯讀 index/provenance/coverage metadata，不寫入或啟用。

新 internal packet 保留 basis_type、query_preference、candidate_preference、relation、semantic_score、
validator_status 與 policy；Matchmaker sanitizer 不降級不完整的新 packet 為舊 semantic evidence。
V1 Ayue理由是 owner-role-bound evidence rendering：經安全檢查的兩項興趣可以分開描述，
但不公開 keys、validator diagnostics 或 raw memory，也不能把 query intent 說成 requester PREFERS。
長／敏感片語不截成較弱條件，而改成中性介紹；exact/public existing projection 保持原行為。
本節對 related-interest 理由的受控 label 描述，是新產品明確允許的界面，並非公開 raw Graph evidence。
Search jobs 只新增 count-only pilot telemetry，invitation outcomes 沿用 canonical state history，
不新增 confirmation/consent bypass，也不將 pilot rating 送往拒絕原因的 AVOIDS pipeline。
