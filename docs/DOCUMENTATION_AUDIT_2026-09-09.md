> **架構更新註記**：本文保留其 domain／歷史內容；其中公開 V3 Planner、Scheduler、subagent 或 DAG 的描述已被 Pi 正式架構取代。\n\n# Event／Memory／Match 文件核對紀錄

## 範圍與基線

- 本輪只更新 Markdown，不改功能、私有環境設定或執行中的服務；不合併 main。
- 功能基線：Server `e06f9a1`、DatingApp `7b86365`，已在「專案教室電腦」遠端分支。文件提交以此實作和既有驗證結果核對，不代表 main 已包含。
- 核對現況入口、架構、Tool／Planner／UI 契約、Neo4j Schema、Event／Memory domain 指南及這幾輪修正／驗收紀錄。風險偵測、貼文等其他領域不重新定義；具日期的舊整合／計畫文件是歷史，不作現行部署規格。

## 修正的過時描述

1. Match 不再是帳號唯一一張提案、也不從聊天新建接受／婉拒確認；現行多卡片 Hub、搜尋確認、卡片 HTTP CAS 分工已同步。
2. general/activity 由 Planner 語意欄位傳遞；活動名稱不等於代送授權。檢索窗 100、最終候選池 20；擴大檢索不等於降低配對門檻。
3. Hub「先不用」先詢問是否記錄原因；只記 opt-in 勾選項目，沒有選擇不新增 AVOIDS。前端程式更新需 hot restart／重建 APK。
4. Memory 工具可選 query，先比對後限額；Graph durable、Mongo preview 12 與 Context 8 的來源／上限分開。Context 為 32 則／8,000 字元；停用／恢復使用 owner MEMORY_DISABLED，不改共享 Concept。
5. 根目錄 Memory 文件改成正式指南入口，完整內容統一在 docs，避免兩份指南漂移。PrivateAgentTurnContextV2 是仍存在的 Private 契約，不因 Public V3 更新而改名。
6. weekly Event 為增量探索→過期清理→readiness→持久分批；不是整庫 reset，也不是全週三張。離線 delivery、重試與 run 17 最終結果已補充；未定位的排程延遲不冒稱已修復。
7. 測試指南改用 pytest offline runner，避免 unittest discover 漏測；早期測試數量標為歷史。Social 最近 1773 passed／4 skipped，Flutter 兩組重疊的 63／65 結果不相加。
8. 保留 Matchmaker 跨日 fixture 與模型偶發格式失敗等限制；人工回報 OK 不等於未來排程或所有故障注入均已驗證。
9. 修正 docs 搬移後錯誤的 Markdown 相對連結。

## 本輪檢查

- Git diff 僅限 Markdown；程式測試結果引用已執行的功能基線，本輪不宣稱重新跑完整功能測試。
- 檢查修改文件的本地 Markdown 連結、過時關鍵敘述與 diff whitespace。
- 提交前更新 GitNexus 並執行 detect_changes，檢查 partial/truncated；圖譜的 execution-flow coverage 不代表完整程式證明。
- 不提交 .env、log、cache、venv 或使用者原始聊天內容；不執行額外配對、記憶寫入或活動排程。
