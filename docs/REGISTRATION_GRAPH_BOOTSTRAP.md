# 註冊興趣與 Neo4j 初始偏好

## 責任與流程

註冊表單的 Appwrite `dating_db.user_profiles.interest` 是本人明確填寫的興趣，
不是聊天推論、近期計畫或對方資料。Mongo `initial_interest` 保持既有相容用途。

1. Big Five 初始化／收到註冊興趣，以及 `PATCH /api/profile` 成功後，排入
   `profile_memory_outbox`；request 不執行模型或 Graph HTTP。
2. 既有 memory outbox worker 以 lease 處理 `registration_bootstrap`／`registration_identity`。
   每次都重新確認 Appwrite profile 存在，讀取公開暱稱與興趣；讀取失敗不拿舊 Mongo 猜測。
3. 9001 `POST /api/users/registration-projection` 建立 `User {id}` 並同步 `name`。
   `id` 仍是 Appwrite ID；暱稱可重複、可改名，不改關係端點。較舊讀取不得覆蓋較新名稱。
   首次使用建立 User.id 唯一約束；既有重複 ID 導致約束失敗時停止並保留 retry，不自動合併資料。
4. 只有完全沒有 `PREFERS / AVOIDS / CURRENTLY_WANTS / MEMORY_DISABLED` 且未完成初始 seed
   的帳號才補偏好。已有記憶的帳號只同步 User／暱稱，不逐項補舊興趣，以免復活已修正的內容。
5. 興趣由既有 `profile_skills.analyze_profile_message` 做 typed extraction；只接收原興趣內的
   evidence span、`like`、confidence ≥ 0.9、activity/habit/lifestyle，最多三筆。
   不寫近期情境、不推論人格／partner trait、不把「不喜歡」轉成 PREFERS。
6. 使用既有 `memory_service.apply_profile_memory_proposals` → `/api/memory/apply`，
   `surface=registration_interest` 走 insert-only transaction：`PREFERS -> Concept(kind=interest)`，
   記錄 source、bounded evidence、confidence、時間；marker 與關係同時提交。
   同一帳號固定 observation ID，User node lock 防止重複 seed；一般 memory write 保持優先覆寫能力。
7. Mongo memory preview 沿用既有刷新流程；Concept embedding 與 Event relevance 沿用既有 worker。
   **建好偏好不等於立刻有活動提案**，仍需 embedding、有效活動、相關性及配對資格。

## 重試與邊界

- Worker 沿用最多八次指數退避；模型／Appwrite／Graph 失敗不影響已完成的註冊。
- Outbox 不保存 raw chat；registration job 保存穩定 identity、interest hash 及通過驗證的 bounded proposals。
- 抽取結果第一次成功後保存 prepared memories，重試不重新產生不同 concepts。
- 原興趣在重試期間改變時拒絕套用舊 prepared memories，以 `registration_source_changed` 留待人工審查。
- 無興趣或無有效正向偏好只建立 User。完成的 bootstrap 不因反覆開啟測驗而重新執行；日後新增偏好走既有聊天流程。
- 這是明確註冊表單來源的例外入口，不放寬聊天 memory 的 saved owner message／message-use 要求。

## 既有帳號補資料

從 Server 執行，使用具 Social／Neo4j 依賴的 Python 環境：

```bash
venv/bin/python scripts/bootstrap_registration_graph.py
# 審閱 dry-run，透過 start_all.sh 部署新版 Social 與 Matchmaker 後才執行：
venv/bin/python scripts/bootstrap_registration_graph.py --apply
```

預設純唯讀，不呼叫模型、不建立節點、不發邀請。apply 只排工作，先檢查新版 Social worker
能力及 Matchmaker endpoint，避免舊 worker 將未知 job 誤當空記憶成功。不要在舊服務仍运行時排新 job。
若 outbox 為 failed，先解決 last_error_code，再以精確 job ID 人工審核重試；不批次重設全部記憶。

2026-09-15 初輪盤點誤把 profile 筆數當帳號數；實際 46 份 Mongo profile 對應 36 個唯一帳號，
有 10 份重複 profile。33 個唯一帳號有 Appwrite profile，另 3 個缺 Appwrite profile，補資料時排除。
CLI 已改為先依 user_id 去重，並分別回報文件數、唯一帳號數與重複文件數；沒有刪除重複或缺來源資料。

## 圖上顯示暱稱

Neo4j Browser 的 caption 是各自 Browser 的樣式設定，不是資料庫節點 ID。
用 `:style` 匯入 [neo4j-browser.grass](neo4j-browser.grass)，或點 User 節點類型將 caption 選成 `name`。
Graph API 不會替換 ID 成暱稱，也不會修改使用者電腦上的 Browser 設定。

## 驗證

離線測試包含 enqueue 去重、改名、來源驗證、缺 profile 重試、版本防護、正負偏好／證據驗證、
prepared source 變更保護、Graph insert-only 與停用記憶保護。Cypher 另以 EXPLAIN 驗證現行 Neo4j
可解析；EXPLAIN 不執行寫入，不能當成正式帳號補資料完成的證明。

部署後以新註冊真人帳號驗證：未聊天即出現 User.name 與有效興趣 PREFERS，重開 profiling 不重複，
改暱稱後 id 不變；停用偏好後重送 bootstrap 不復活；再觀察 embedding／EVENT_RELEVANCE。
本次不處理過期活動、活動候選數或強制重跑配對。

2026-09-15 本輪驗證：Social 1,369 passed、Matchmaker 86 passed、contracts 72 passed；
Python compile、`bash -n start_all.sh`、`git diff --check` 通過。三個新 Cypher EXPLAIN 通過，
這是部署前的驗證基線。既有測試第一次因新增獨立 lock query 改變 query 次數而失敗，
已把 lock 合併至原 transaction queries，保留原測試並全部通過。

GitNexus 綁定 `/home/sunny/桌面/Graduate_Project/Server` 的實際 checkout。
修改前 assessment facade（chat endpoint 呼叫）、memory worker（worker loop 呼叫）、
memory apply（feedback 呼叫）的 upstream impact 為 LOW；profile HTTP handler 為 UNKNOWN，
另以 route／呼叫端與測試確認，而非解讀成無影響。
收尾全工作樹掃描含組員未完成的 App voice／Pi 變更，整體為 Critical，非可整包部署保證；
初輪未提交、推送或重啟。後續部署經使用者明確批准，結果如下；仍未提交或推送。

## 2026-09-15 正式部署與補入驗收

- 正式 checkout：`Server/`，當時 HEAD `291d73b` 加目前工作樹，保留組員 Voice／Pi 修改。
- 透過 `start_all.sh ollama` 啟動固定四個服務，最終四個 health 與正式公開 API 均回 200。
  `GET /api/registration-graph/status` 確認版本 `registration-bootstrap-v1`、worker 存活。
- 第一次 nohup 背景啟動在啟動 shell 結束後未持續存活；改用持續終端承載正式腳本，後續修正以其 `r`
  指令完整重啟。日誌位於 `.runtime-logs/registration-20260915/`，不納入版本庫。
- 33 個唯一帳號全部具備一個 User 節點，沒有重複 ID；32 個有非空安全暱稱，1 個未取得安全暱稱，
  不用 ID、聯絡資訊或「對方」假冒姓名。Graph caption 仍須在 Browser 選 `name`。
- 新增 23 個原先缺少的 User；10 個原有記憶帳號只同步身份／暱稱。19 個帳號寫入 22 筆初始 PREFERS，
  4 個帳號無有效正向興趣，只建立 User，不捏造記憶。
- 22 筆偏好對應 20 個 Concept，20 個均有 768 維 embedding；驗證時 28 個帳號具有
  User→EVENT_RELEVANCE→Event 關聯，共 84 條、涵蓋 19 個未達 expires_at 的 active 活動。
  活動關聯是候選訊號，不代表已產生或送出提案。
- 所有 76 個已排入工作完成（33 bootstrap、43 identity）；identity 多出的 10 次來自初版報表尚未去重，
  只重複投影同一份公開名字，沒有重複 User／偏好。後續 CLI 已去重。

### 部署驗收發現與處理

1. **證據包含表單前綴造成漏失**：模型可回傳「平常興趣是:吃飯」，但初版要求整段 evidence 直接存在原欄位，
   因此前綴／全半形標點造成誤拒。新增 deterministic 原文對照，只保留 evidence 與原興趣區間的交集，
   不保存表單前綴，不放寬 like／category／confidence 規則。既有 extractor 的 previous_context 已是相容參數、
   不参与 prompt，移除這個無效參數的使用；沒有新增平行 extractor。
2. 只對 13 個初輪空結果排入一次 `registration_empty_recheck_v1` 重驗，保留 source hash，
   重新驗證 Appwrite 與 Graph；不重跑已有偏好或覆蓋已修改記憶。最終剩 4 個無有效興趣。
3. 新 seed label 移除喜歡／偏好／興趣這類顯示前綴，使用純 Concept label；不批次重寫既有共用 Concept。
4. 發現 1 個活動已達 ends_at，但尚未達 expires_at，仍在 active inventory；這是已知的活動有效期差異，
   本輪只記錄，沒有修改、清除或重跑活動。正式三件套重跑前需檢查這個 gate，不能以關聯數代表全部活動都有效。

共享部署工作樹最新測試：Social 1,386 passed、Matchmaker 89 passed、contracts 74 passed；
部署前 Voice 145 passed。另將本次 16 個檔案套入獨立 worktree 驗證：Social 1,380 passed、
Matchmaker 89 passed、contracts 74 passed；第一次隔離測試因 worktree 缺少 Git 忽略的 Pi
`node_modules` 而有 3 項假失敗，連結現行鎖定依賴後全數通過。
初次補資料驗收時，使用者選擇先回報、暫不重跑活動；後續另行明確授權的第 19 輪
完整活動流程已完成，最終活動與發卡結果見 [2026-09-15 活動週期紀錄](EVENT_CYCLE_2026-09-15.md)。
