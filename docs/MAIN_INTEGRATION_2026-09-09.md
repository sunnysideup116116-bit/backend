# Main 整合驗證紀錄（2026-09-09）

## 來源與邊界

- 使用者明確授權：先補推文件，再合併 backend／DatingApp 的 main，最後透過原 start_all.sh 重啟 Server。
- 後端來源工作分支 a9a8812，目標 legacy-origin/main 基線 519b9af；前端來源 6f7c93a，目標 origin/main 基線 44b0cc2。
- merge-tree 及實際 --no-ff --no-commit 合併均無文字衝突。新增本紀錄前，兩邊合併後 tree 分別等於各自已驗收的工作分支 tree，沒有新增程式差異。
- 後端本機舊 main（b53ff5a）屬另一 remote 歷史，不能快轉為 backend/main。保留不改名、不重設；改從 legacy-origin/main 建立 codex/main-integration-20260909，合併後推 HEAD 到 backend/main。該整合分支是實際 Server checkout，並追蹤 backend/main。
- 前端本機 main 可安全快轉至 origin/main，再合併工作分支。未 force push、未刪除工作分支／worktree、未更動私有 .env 或服務 ports。

## 合併結果的實際驗證

- Social 完整 offline：1773 passed、4 skipped、133 subtests。
- Matchmaker offline：76 passed、15 subtests。本次非 23 點跨日 fixture 時段；先前已記錄的時間依賴測試限制仍保留，不代表有改過該測試。
- Flutter 配對／拒絕／暱稱／狀態五檔：78 passed。
- shell syntax、staged diff whitespace 與 unresolved paths 檢查通過。
- 提交前依規範刷新 GitNexus 並檢查 detect_changes 完整性；不得把圖譜 coverage 缺邊當成安全證明。
- 圖譜命令曾誤在 workspace root 啟動，已中止且退出 130；改在兩個明確 repo 目錄分別重跑，不使用根目錄那次分析作為簽核，也不更動 workspace 既有未提交檔案。

## 發布與服務

本紀錄隨 merge commit 保存。遠端是否發布成功以 backend/main、DatingApp/main ref 為準，不能只因本地有 merge commit 就稱已推送。
Server 使用同一 checkout 的 start_all.sh，公開 API 固定 https://service.misproject.us.ci/；發布後核對四服務健康。前端 main push 可能觸發既有 CI，並不代表 APK 已建置或安裝完成。

此前日期文件中的「未合併 main」是整合前快照；本紀錄定義此次整合來源及驗證，不改寫歷史測試結果。
