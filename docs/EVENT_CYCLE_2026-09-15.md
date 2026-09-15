# 2026-09-15 註冊偏好補入後重跑活動週期

## 操作範圍

使用者已明確批准重跑完整活動三件套：清理、找活動、建立關聯並發卡。
透過既有 singleton durable queue，以 `job_kind=weekly_cycle` 執行；沒有使用會清空活動／歷史提案的 demo reset。

- Run number：19。
- Run ID：`b5b5f6ab0d1b468cabac25cc7b5b2290`。
- Source：`operator_registration_backfill`。
- 使用現行環境的地區、時間窗、類別，沒有改動星期一排程或模型設定。
- 正式流程先更新活動，再清理過期資料、等待 embedding／relevance 就緒，最後逐一掃描並發卡。
  每批三張是節流，不是整輪最多三張；是否發出仍受偏好、風險、既有提案與對象可用性限制。

## 已完成的前置清理

`web_a92918d83d4dc096`（全聯中秋烤肉祭／前鎮瑞南店）已達儲存的結束時間，
但 `expires_at` 比 `ends_at` 晚一天，仍被列為 active。

- 原 `ends_at=1789401599`，原 `expires_at=1789487999`。
- 修改前完整 Event properties 保存在 Mongo `event_maintenance_audit`，
  紀錄 ID：`ends-at-correction:web_a92918d83d4dc096:1789401599`。
- 以精確 ID、原始時間與 active 狀態比對後，將該筆 expires_at 校正為 ends_at。
- 交由既有 `run_event_lifecycle_once` 處理：成功清除 1 個 Event、0 個 proposal 過期、0 個 stale。
- 沒有刪除 User、偏好、accepted 關係、歷史提案或聊天室。Event-owned 衍生關聯隨清理移除；
  必要時可由上述備份還原 Event，再重建衍生關聯。

這是本次已確認資料的一次性校正，不代表通用 ends_at／expires_at 邊界已在程式上永久修正。

## 最終執行結果

第 19 輪已完成，worker state／stage 均為 `completed`。最終 outcome 是 `partial`，
原因是三個搜尋來源網址失效，不是 relevance 或發卡失敗：

- `市集:dead_source_url`
- `節慶:dead_source_url`
- `美食:supplemental_dead_source_url`

本輪搜尋 63 筆、寫入 10 個活動。完成 reconcile 後共有 29 個有效 active 活動：
市集 6、音樂 6、運動 5、節慶 6、美食 6；最低覆蓋門檻全部達標，只有運動比目標 6 少 1。
最終沒有 `ends_at <= now` 卻仍 active 的 Event。

Embedding／relevance readiness 為 ready、pending 0，共有 84 條 EVENT_RELEVANCE。
36 個帳號全部完成掃描、failed 0：12 個建立新提案、14 個已有活動提案、10 個沒有符合人選。
新建 12 筆目前均為 draft，第一位使用者的卡已保存；pending delivery 為 0。
這不代表雙方同時收到卡：依正常流程，第一位先接受後才會送給第二位。

使用者在執行中看到 27 個活動；最終 canonical Graph 查詢為 29 個。若 UI 仍顯示 27，
需另查 UI 的刷新時間與顯示篩選，不能用執行中畫面覆蓋最終 Graph 計數。

查看 `GET /api/match/events/discover/status`，應同時確認 run_number=19、最終 outcome、
`weekly_progress.created_proposal_count`、`saved_card_count`、`pending_delivery_count` 與 failed user 數。
完整 checkpoints 位於 Mongo `event_weekly_runs`／`event_weekly_users`，用上述 run ID 定位。
若恢復或重試，沿用這筆工作；不要重複 enqueue 新一輪或刪除既有 checkpoint。

## 簡報對照

本輪結果對應簡報中的：

```text
Weekly Event Discovery
→ Event Validation + Event/Concept Graph
→ Event Relevance and Avoidance Analysis
→ Fair, Batched Opportunity Scan
→ Canonical Mongo Proposal
→ User 1 Consent → User 2 Pending → User 2 Consent
→ Create or Reuse Pair Chat
```

英文 speaker notes 見 [Context／Memory／Event progress notes](PROGRESS_REPORT_CONTEXT_MEMORY_EVENT_2026-09-15.md)。報告時建議說 `partial because of three unavailable source URLs`，不要說成配對狀態或雙方 consent 失敗；也不要把 12 張 draft 卡說成 12 組已完成配對。
