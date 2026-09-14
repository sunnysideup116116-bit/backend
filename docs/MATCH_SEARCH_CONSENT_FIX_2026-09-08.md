> **架構更新註記**：本文保留其 domain／歷史內容；其中公開 V3 Planner、Scheduler、subagent 或 DAG 的描述已被 Pi 正式架構取代。\n\n# 搜尋分類、邀請授權與推薦說明收尾

## 人工驗收與交付

- 使用者已回報一般搜尋、活動伴、Hub 拒絕原因均正常，並授權先推送工作分支；本次不合併 main。
- 後端追蹤 `legacy-origin/專案教室電腦`（backend），前端追蹤 `origin/專案教室電腦`（DatingApp）。正式 `.env`、執行紀錄、cache、venv 不提交。
- 下文「未 commit／push」等描述為各階段驗證當時的狀態；後續推送不代表已完成 main 整合。

## 問題與 owner

- `match_search_context.extract_invitation_topic` 的廣義找人句型將「適合認識」當活動；縮窄舊解析器，正式 Public V3 由 Planner 傳遞 general/activity 語意，不累積否定詞黑名單。
- `write_executors.prepare_write_confirmation` 原先有 topic 就設定 invite_on_match。現在預設 preview，activity 的本句代送 evidence 與可見確認共同綁定授權，仍經既有 executor/job/CAS。
- `match_reason_service` 的 topic fallback 及 router 改寫只說邀請狀態，現在保留公開互動特質或「缺少共同活動依據」說明。未增加 Graph 權限，不把使用者要求當對方興趣。
- `confirmation.project_match_choice_history` 查詢漏掉 delivery_mode，已修復；重新進聊天室的按鈕不再由搜尋並邀請變成單純搜尋，內部 payload 不公開。

## 範圍與相容

- 不修改一般候選排序、Event weekly cycle、名額、Memory、Neo4j 或前端 API 網址。
- general 預設使用既有一般配對近況流程，不使用新詞生成假活動。
- 新邀請使用新流程。已存在提案、已送邀請、已決定歷史不刪除、不倒退，也不批量重寫原始搜尋分類；人工驗收請用新搜尋。
- GitNexus：實際 Server checkout，HEAD 3efae74。文案 fallback CRITICAL（提案、通知、讀取相容）；SubTask HIGH（共用 Planner 契約），已事先告知並保留完整回歸。runtime 動態註冊圖譜 UNKNOWN，以 scheduler registration 原始碼補查。

## 驗證與遇到的問題

- Social 完整 offline：1772 passed、4 skipped、133 subtests。
- DatingApp 卡片／暱稱／即時狀態：65 passed；本輪未修改前端。
- 合成文字真實 Planner：一般認識 general；看展 activity 且無代送；明確找到就邀請 activity 且 evidence 綁定；取消看展改找合得來的人 general。
- 新增 runtime 語意傳遞、預設不代送、原文 grounding、按鈕歷史投影、雙方文案、owner 隔離、長 topic 字數測試。既有重複確認／job／CAS 測試持續執行。
- 曾遇到 Planner 提示超過原 6000 字元限制，已精簡配對重複說明並保留測試上限；新 schema 欄位已更新契約測試。
- Matchmaker 全套在 23 點後：75 passed、1 failed。未修改的 `test_late_model_result_is_discarded_before_graph_write` 將明日起始加一小時，跨日卻只附第一天 evidence，被正確拒為 date_evidence_mismatch，未到預期期限檢查。固定白天時間只重跑該測試通過（1 passed）。未擅改該測試或活動程式，非本輪 regression；不可將此輪宣稱全套全綠。
- 真實否定句「先不要送邀請」一輪 Planner invalid_arguments，安全停下未寫入；原句重測為 activity／看展／空 invitation_evidence，正確維持 preview。保留模型偶發格式失敗紀錄，不宣稱模型永不失敗。
- 無正式測試邀請或記憶寫入；未 commit、push、merge。
- 已透過原本 `start_all.sh` 重啟並保持服務運行；四服務健康，正式公開 `/api/health`、Social `/`、Matchmaker `/health` 均 HTTP 200。8 個修改來源模組 compile、shell syntax、diff whitespace 通過。
- GitNexus 已重新索引（9798 nodes／23884 edges）；process extraction 有預算截斷，不以圖譜缺邊作為未受影響證據。累積 diff 還包含前輪 Memory/Event WIP，合併前需另做完整範圍核對。

## 手機最後驗收

### 追加：Hub 婉拒與 user6 候選檢索

- 新 Hub `_decide` 漏接既有 decline dialog，已接回三條路徑：取消不送 request、只婉拒傳空 reasons、勾選後傳 exact explicit_reasons，保留 revision/CAS；不選擇不會新增避免偏好。此修正需更新 DatingApp，舊 APK 不會因 Server 重啟而更新。
- user6 的看展 job 已正確保存 activity／preview，結果在 vector_search 階段為 no_candidates，尚未進媒人評估。候選檢索原本取前 20 才做排除，現在擴為有界 100 筆、numCandidates=500，篩選與最終 20 人池不变，不改封鎖／歷史／測試群組與配對門檻。
- 唯讀資料：符合基本非測試群組條件 36 人，10 人有 3072 維向量，26 人未有向量。以 user6 已保存近況向量（不是看展原查詢）驗證前 20 筆剩 1、前 100 剩 10，證實檢索窗會截掉可用候選；未宣稱已重新完成看展配對。
- 不重送 user6 原始搜尋文字至外部 embedding；該診斷曾被安全審查拒絕，改做純 Mongo 唯讀檢查。無資料或安全門檻不足仍允許正常沒有結果，不為測試硬配。
- 本輪後端 match/decline 回歸 406 passed、1 skipped；前端 Hub／原因對話框 63 passed；Dart analyze 無問題。新增 widget test 驗證三種操作與 exact reasons，busy 指示器持續轉動時使用固定動畫等待。
- 完整 Social 最終回歸：1773 passed、4 skipped、133 subtests。原 start_all.sh 已重啟，公開 health HTTP 200。既有前端為 flutter run -d chrome --web-port 4173，需在其終端按 R 熱重啟以載入新 Hub 程式；未停止使用者前端程序。

1. 「幫我找一位適合認識的人」：一般人選卡，不是活動伴；先看人選理由再送邀請。
2. 「幫我找人一起看展」：活動伴卡含推薦說明；確認搜尋後仍待本人決定，不直接等待對方。
3. 「找人一起看展，找到就幫我邀請」：按鈕及確認文字明說搜尋並送出；確認後才可等待對方。
4. 上述每種重進 room／Hub：標籤、理由、狀態一致，重複點擊不重送。

這些真機步驟不以 offline 或單獨 Planner 測試代替，確認後再進合併。
