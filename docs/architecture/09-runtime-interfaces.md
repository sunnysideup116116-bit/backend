# Public Pi runtime interfaces

> 本文件是現行介面。舊 Planner／Scheduler／subagent 文件只供歷史查閱。

## Public HTTP boundary

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

## Background and voice boundaries

Registration Graph `registration-bootstrap-v1`：Social profiling 初始化／profile 更新只 enqueue，
既有 memory outbox worker 重新驗證 Appwrite，透過 9001 `POST /api/users/registration-projection`
同步 User.id/name。`POST /api/memory/apply` 的 `surface=registration_interest` 採 insert-only 初始
偏好，不覆蓋既有／停用記憶。`GET /api/registration-graph/status` 只提供版本與 worker 活性，
不回帳號資料；供補資料 CLI 防止對舊 worker 投遞新 job。詳見 [bootstrap contract](../REGISTRATION_GRAPH_BOOTSTRAP.md)。

註冊興趣與一般 owner message 共用 atomic/canonical memory boundary：明確列舉拆成獨立 Concept，K-pop aliases 收斂到 `k_pop`，單訊息數量受同一 configurable limit 約束。

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
