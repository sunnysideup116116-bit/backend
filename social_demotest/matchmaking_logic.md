# 阿月 V3 配對邏輯

這份文件只說明目前的 canonical 配對流程。公開阿月完整架構、工具與 App 遷移方式請看 [`../AYUE_V3_ARCHITECTURE.md`](../AYUE_V3_ARCHITECTURE.md)（V3 架構文件）；後續 coding agent 的限制請看 [`../AGENTS.md`](../AGENTS.md)。

## 1. 配對不是隨機挑人

使用者明確要求找人後，Public Ayue Planner 只能提出 `match.start_search` confirmation。確認成功後，Scheduler 才會呼叫共用的 `match_action_service`：

```text
Owner request
→ Planner confirmation
→ Scheduler confirmation guard
→ match_action_service
→ Mongo candidate pre-filter
→ port 9001 matchmaker ranking
→ directional proposal
```

第一層使用本人近期情境、偏好與既有關係排除不適合的人，並以 Mongo vector search 縮小候選集合。第二層 matchmaker 綜合雙方各自的近期情境、偏好、個性與已驗證記憶，選出零或一位人選。沒有通過 qualification 的候選人時，結果是沒有合適人選，不會隨機補一位。

所在地目前只供公開資訊與附近地點查詢，不進入 matchmaker 排序。

## 2. 角色與理由不可互換

新 proposal 保存 `directional_reason_v3`：

- 每個方向都明確綁定 `viewer_id` 與 `counterparty_id`。
- Viewer 看到的理由以「你」描述 viewer，以「對方」描述 counterparty。
- 近期情境、個性、偏好與 snapshot 都依實際 owner 綁定，不用 `target`／`candidate` 名稱猜角色。
- 沒有 verified shared evidence 時，只能說兩項活動可能形成互補約會情境，不能假裝是共同興趣。
- 對外 projection 不得包含 seed ID、Mongo ID、revision、raw profile 或對方私人記憶。

舊 live proposal 可用 `scripts/repair_directional_match_reasons.py` 做 dry-run；人工 review 前不可使用 `--apply`。

## 3. Canonical lifecycle

```text
draft
├─ interested → pending
├─ declined   → declined
└─ timeout    → expired

pending
├─ accepted   → accepted
├─ declined   → declined
├─ cancelled  → declined
└─ timeout    → expired
```

- `draft`、`pending` 是 live proposal。
- `accepted`、`declined`、`expired` 是終態。
- Accepted 代表已建立聯絡關係，不得再顯示為「配對仍在進行」。
- Accepted 不阻擋使用者之後明確發起新的搜尋；只有 live proposal 會阻擋重複建立。
- 歷史邀請必須保留，但不可重新變成可操作卡片。

## 4. 所有決策共用同一條寫入路徑

```text
UI /api/match/decision ─┐
                        ├→ match_action_service
Agent decision tool ────┘   → match_decision_service
                              → atomic status + revision CAS
                              → committed effects
```

Planner 不得提供 user、match、proposal ID 或 revision。Executor 根據登入者與 canonical state 注入 identifier、expected status、revision，以及由 `agent_run_id + tool_call_index` 組成的 idempotency key。

Stale request 只回傳最新狀態，不反向覆寫終態。通知、聊天室建立與 feedback 只在 transition 成功後執行；effect failure 不得讓已提交 transition 被重送。

## 5. App 串接重點

- 搜尋與自然語言接受／婉拒由 Public Ayue typed tools 處理。
- 卡片決策呼叫 `POST /api/match/decision`，帶入畫面取得的 `expected_status` 與 `expected_revision`。
- HTTP 409 時重新讀 canonical match state，不重送舊 decision。
- App 不從阿月回覆文字猜狀態，也不自行建立聯絡人。
- 「他接受了嗎／他是誰」應由阿月讀 `match.get_status` 或 `match.get_counterparty_summary`，不能從舊聊天紀錄推測。

## 6. 必測案例

- 雙方並發、重複 request、stale revision 都不覆寫終態。
- 對方先婉拒後，不能自動開下一次搜尋。
- Accepted 後狀態與對象摘要一致，且不洩漏內部 ID。
- Viewer 兩側看到的 proposal reason 角色、近期情境與個性都正確。
- 沒有合適候選人時回 `no_suitable_candidate`。
- 舊歷史卡片保留，但永遠不可重新操作。
