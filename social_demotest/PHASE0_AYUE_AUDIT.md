# `routers/chat.py` 瘦身 Phase 0 基線

這份文件是 `routers/chat.py` 分階段拆分前的安全基線。Phase 0 **不搬動、
不刪除 production code**；它只固定 HTTP contract、關鍵行為與目前耦合，
讓 Phase 1–5 能逐步搬移並在每一階段驗證沒有改變產品行為。

> 這不是先前 Agent regex／legacy 清理計畫的 Phase 0。該次改造已完成，
> Public V3 的架構真相以 repository root 的 `AYUE_V3_ARCHITECTURE.md`
> 與 `AGENTS.md` 為準。

## 可重播的唯讀盤點

在 `social_demotest` 執行：

```powershell
python -B scripts/audit_ayue_phase0.py
```

腳本只解析 Python AST 與原始碼並將 JSON 輸出至 stdout；不 import app、
不連 MongoDB／Neo4j、不寫檔案。結果包括：

- `chat.py` 行數、top-level symbols 與函式範圍。
- 20 條 `/api` endpoint 的 method、path、handler 與 responsibility。
- function-local imports。
- `profiles_coll`、`messages_coll`、`matches_coll` 的讀寫位置。
- production／test 的保守 lexical references。

Reference 掃描可能命中同名 symbol、註解或字串，只能協助 review，不能單獨
作為刪除依據。

## Phase 0 初始基線

- `routers/chat.py`：4,118 行。
- HTTP endpoints：20 條。
- Top-level functions：76 個。
- 三個 Mongo collection 的直接操作：約 89 處。
- Public Ayue V3 的唯一 orchestrator 已在 `services/ayue_agent/v3/scheduler.py`；拆 router
  不得把 planner、guard、tool 或 domain logic 搬回 HTTP layer。
- Private Ayue 仍是獨立 runtime，這次只搬 HTTP adapter 與既有 private
  orchestration，不能和 Public V3 合併。

## Responsibility 邊界

| Responsibility | HTTP surface | 現有大致區段 | 後續搬移原則 |
| --- | --- | --- | --- |
| onboarding | `/api/chat`, `/api/chat/reset` | 1–1,772 | 先搬 leaf router；不重寫 Big Five 流程 |
| public messaging | `/api/messages/*`, `/api/direct_chat*`, `/api/contacts` | 1,773–3,023 | 最後才拆；V3 仍委派 `ayue_agent.v3.scheduler` |
| private mediator | `/api/mediator/private*` | 3,024–3,734 | 保持 accepted-match 與 private runtime 邊界 |
| relationship | `/api/relationship/*` | 3,735–3,958 | Date 寫入仍委派 date coordination service |
| proactive delivery | `/api/proactive_check` | 3,959–4,108 | 不得把 scheduler/care generation 搬回 router |
| demo maintenance | `/api/demo/reset_db_state` | 4,109–4,118 | 僅搬 adapter；保留 demo endpoint contract |

行號只記錄 Phase 0 當下位置；搬檔後不得拿它作 runtime dependency。

## 已鎖定的 contract

`tests/test_chat_router_characterization.py` 固定：

- 20 條 method + path。
- request body model 與 query parameter surface。
- 非公開聯絡人的 stream 仍包裝既有 JSON direct-chat result。
- Private V2 關閉時回 409，未接受配對時回 403，不 fallback。
- 不存在的使用者 polling 不產生 feedback、event 或 care 副作用。
- `conversation_active=true` 時不消耗 proactive care delivery。

既有 `tests/test_ayue_agent_stream.py` 另固定 Public V3／legacy mode、owner 與
final 各保存一次、background task、progress allowlist 及錯誤隱私。

## Phase 1–5 的 deletion gate

搬移或刪除任何 symbol 前，必須同時滿足：

1. AST／`rg` 未發現 production、test、dynamic import、environment flag 或
   frontend caller。
2. 對應 endpoint 已由新的 aggregate router 掛載，method、path、body、query
   與 response shape 不變。
3. Characterization tests 與完整 deterministic suite 全部通過。
4. Public V3、private、legacy rollback、profile background task 與 stream
   ownership 邊界仍符合 `AGENTS.md`。
5. 該 Phase 的 High review 完成後才進入下一 Phase。

## Phase 0 驗收

- Production files：零修改。
- 新增／修正內容只限基線文件、唯讀 audit script 與 characterization tests。
- 完整測試必須離線執行，不得連正式 MongoDB Atlas 或 Neo4j。
- Phase 0 完成後停止；Phase 1 才開始搬 leaf routers。

## Phase 1 完成紀錄

- `chat_onboarding.py` 已承接 `/api/chat` 與 `/api/chat/reset`。
- `chat_messages.py` 已承接 `/api/messages/{contact_id}` 與 `/api/contacts`；
  `routers.chat` 保留 `get_messages`、`get_contacts` re-export，避免既有 Python
  caller 在這次純搬移中中斷。
- `relationship_dates.py` 已承接五條 `/api/relationship/date/*` adapter；它只呼叫
  `date_coordination_service`，沒有複製 domain logic。
- `demo.py` 已承接 `/api/demo/reset_db_state`。
- `routers/chat.py` 仍是唯一掛入 `main.py` 的 aggregate router，include 四個 child
  routers；20 條 method/path/request surface 不變。Public V3 direct chat、private
  mediator、relationship quiz、feedback/probe 與 proactive 尚留在 `chat.py`，由後續
  Phase 處理。
- Child routers 只宣告相對 path，不自行設定 `/api` prefix 或 `Chat` tag；prefix 與
  tag 由 aggregate router 統一提供，避免 `/api/api/*` 或重複 OpenAPI tags。
- 搬移後 `chat.py` 為 3,836 行、66 個 top-level symbols、10 個直接 endpoint、
  72 處直接 collection operations；其餘 10 條 endpoint 已由 child routers 擁有。

## Phase 2 完成紀錄

- `relationship_engagement_service.py` 已承接 automatic probe、feedback、關係摘要、
  post-chat activity 與其 CAS／event side effects；Public direct chat 與 private mediator
  暫以 `routers.chat` compatibility aliases 使用它。
- `proactive_delivery_service.py` 已承接 polling 時的 notice、mediator event 與
  proactive-care delivery；`routers/proactive.py` 僅保留 `/api/proactive_check` adapter。
- Private mediator endpoint、本身的 manual fun-fact probe 與 private runtime dynamic
  import 尚留到 Phase 3，以避免跨 Phase 改動 private runtime。
- Review 已補 automatic probe 候選快照、relationship-private event、stale proposal
  與 polling precedence 的 deterministic regression tests；polling response shape、
  unread／pending state 寫入與 CAS 行為維持原 contract。
- 搬移後 `chat.py` 為 3,140 行、50 個 top-level symbols、9 個直接 endpoint、
  45 處直接 collection operations；對外仍維持原有 20 條 chat API surface。

## Phase 3 完成紀錄

- `private_mediator.py` 已承接 `/api/mediator/private/{other_id}`、
  `/api/mediator/private` 與 `/api/mediator/private/stream`；aggregate router
  仍統一提供 `/api` prefix 與 `Chat` tag，原有 HTTP surface 不變。
- Manual fun-fact probe 與 pending probe answer 的 CAS／event state 已收斂至
  `relationship_engagement_service.py`；private runtime 與 private 不再 dynamic
  import `routers.chat`。
- `profile_task_service.py` 成為 owner-message profile extraction 的 shared
  scheduler；`routers.chat.queue_profile_skills` 保留 facade，維持既有 public
  caller 與 test seam。
- `mediator_context_service.py` 承接 public legacy 與 private mediator 共用的
  persona、語氣、shared chat 與 bounded graph-memory projection。
- Review 已確認 private handler 與搬移前 AST 等價，並補上 JSON／NDJSON
  delegation、owner-message save-once、profile task policy、manual probe 與 pending
  answer CAS 的 deterministic regression tests。
- 搬移後 `chat.py` 為 2,352 行、37 個 top-level symbols、6 個直接 endpoint、
  29 處直接 collection operations；對外仍維持原有 20 條 chat API surface。

## Phase 4 完成紀錄

- `relationship_quiz.py` 已承接 `/api/relationship/fun/{other_id}` 與三條
  `/api/relationship/quiz/*` endpoint；aggregate router 仍統一提供 `/api` prefix
  與 `Chat` tag。
- `relationship_quiz_service.py` 成為 quiz lifecycle、答案驗證、expiry projection、
  shared result card 與 mediator invite effect 的唯一 owner；router 不再直接寫 match
  state。
- Review 將 start、answer、complete、cancel 與 expiry transition 收斂為
  `round_id + revision + status` 的 atomic compare-and-set；只有成功取得完成
  transition 的 worker 才能發布 shared result card，並使用顯示名稱產生邀請，
  避免並發覆寫、重複卡片及內部 user ID 外洩。
- Deterministic tests 涵蓋 active-start idempotency、同時 start 的 CAS loser、
  第二人完成答題、completion CAS loser 不重複發布、expiry 不洩漏結果、
  terminal round 不被取消，以及取消 endpoint 的特定 403 contract。
- 搬移後 `chat.py` 為 2,086 行、32 個 top-level symbols、2 個直接 endpoint、
  22 處 direct collection operations；quiz endpoint 與其 direct collection operations
  都已移除。

## Phase 5 完成紀錄

- `public_chat.py` 已承接 `POST /api/direct_chat` 與
  `POST /api/direct_chat/stream`，包括 public V3 的 save-once／progress stream
  orchestration；Public V3 rollback 僅透過部署／commit rollback，不提供 request-level legacy fallback。
- `routers/chat.py` 現在只作 aggregate router：統一 `/api` prefix、`Chat` tag
  與八個 leaf router 的掛載；不再 re-export leaf handler，也沒有直接 HTTP
  endpoint 或 collection operation。
- Characterization tests 將兩條 direct-chat route 鎖定為
  `routers.public_chat` owner；既有 JSON、NDJSON、progress privacy、save-once、
  V3 不載入 legacy rollback 的 deterministic tests 維持通過。
- Review 另移除六個經 AST／全 repository reference 掃描確認無 caller 的 legacy
  helper，並修正 provider failure 時可能直接顯示給使用者的 mojibake fallback。
- 搬移後 `chat.py` 為 27 行、0 個 top-level functions、0 個直接 endpoint、
  0 處 direct collection operations；完整 20 條 API surface 不變。


> ARCHIVED / HISTORICAL: this audit records an earlier router-split phase. Current ownership and runtime rules are defined by the root AGENTS.md and AYUE_V3_ARCHITECTURE.md; do not use the historical rollout/fallback text as implementation instructions.
