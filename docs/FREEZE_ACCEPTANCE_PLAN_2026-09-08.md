# Memory／Context／Event freeze 驗收計畫

目的：驗證已存、可讀、正確使用、可撤銷與可靠投遞。回覆流暢不等於資料正確；每項保留實際結果，不提前打勾。

## 最新狀態（2026-09-09 文件核對）

- 使用者已確認 Memory／Context 基本體驗、一般／活動配對及最後 Hub 拒絕原因正常；後端 e06f9a1、前端 7b86365 已推到「專案教室電腦」。尚未合併 main。
- 最新完整 Social：1773 passed、4 skipped、133 subtests。Flutter 最近 Hub／拒絕原因組 63 passed；暱稱／即時狀態組此前 65 passed，兩組有重疊。
- 下方 A–E 是可重跑的驗收清單，不能因使用者整體回報 OK 就推定每個故障注入／同義詞／未來排程測項均逐一完成。
- 已知限制：Matchmaker 晚間跨日 fixture、模型偶發格式失敗安全退出、缺少向量的候選，以及活動覆蓋不足；詳見配對修正紀錄。合併前仍需檢查 main 差異與組員更新。

## 第一階段實作驗證基線（歷史快照）

memory.search_my_profile 已由讀固定快取改成 owner-scoped Graph 主題查詢，並保留 unavailable/truncated。
Planner／Synthesizer 已加入未確認推測不得當本人事實的規則。提示長度回歸曾發現超過 6000 字元，
已縮短重複敘述，保留既有預算與不使用 keyword router 的規則。

- Social 完整 offline：1756 passed、4 skipped（133 subtests）。
- Matchmaker offline：76 passed（15 subtests）。
- 新測試涵蓋快取以外記憶查回、owner 引數拒絕、輸出不帶 ID、失敗不當沒有、truncated 與推測提示。
- 真實唯讀 9001 查 seed_user_04 的「書店」：HTTP 200、success、命中 1 筆、truncated=false。
- start_all.sh 已正常重啟，四個服務與固定公開 health 正常，保持運行；沒有新增偏好／執行配對。
- Python compile、shell syntax、diff whitespace 通過。GitNexus 累積 tracked diff 含前輪 Event／Memory，
  回報 critical；不等於全部屬本輪，且新增未暫存檔案另由原始碼及測試核對。尚未 commit／push。

下列是驗收規格，不是逐項完成證明；模型是否正確處理假設與同義詞，不能只由提示文字測試代替。已回報結果以上方最新狀態及修正紀錄為準。

## 準備

- 使用最新 Server（唯一 start_all.sh）與對應最新前端；舊 APK 不含 prefer/avoid 標籤更新。
- 主測試帳號可用 seed_user_04；雙帳號測試另一個用 seed_user_01。使用不同瀏覽器（Chrome／IAB）隔離登入。
- 先保存兩帳號現有偏好、提案數量與版本的測試基線；不清正式 Graph、不批量刪資料。seed 的既有性格與舊偏好可能影響結果，不能當成新測試產物。
- 每項記錄時間、帳號、room、輸入、實際回覆及通過／失敗。內部查驗只回傳 count/status，不公開私人記憶或 credentials。

## A. 長期記憶（逐步執行）

| 編號 | 操作／輸入 | 通過條件 |
| --- | --- | --- |
| A1 | 「我平常很喜歡逛書店，但不喜歡吵雜的酒吧，這是我的長期偏好。」 | 記憶頁內容與方向正確，無重複反向項目；Graph PREFERS/AVOIDS 與快取一致。 |
| A2 | 新開 room：「你記得我喜歡什麼、不喜歡什麼嗎？」 | 可讀既有偏好，方向不翻轉；結果有截斷時不能宣稱全部。 |
| A3 | 「照我的偏好，想一個休閒活動，不用找店家。」 | 建議尊重偏好與避免；不可聲稱已查店家／替我配對。 |
| A4 | 記憶頁停用酒吧項目，再開新 room 問偏好 | 不再把停用項目當已知偏好，也不反過來說喜歡酒吧。原 room 歷史仍可能提及，與 durable truth 分開判讀。 |
| A5 | 已存在 owner correction／restore 入口時測修正／恢復 | 保留原 stance，舊查詢晚回不能恢復已停用項目，不改另一帳號共用 Concept。若 UI 無入口標未測，勿捏造。 |
| A6 | 「我今天想去潛水。」 | 進近期情境，不因一次計畫變成長期喜歡潛水。 |
| A7 | 「這次先不要配對。」 | 只處理本次操作；不新增永久不喜歡配對。現有舊錯誤資料另稽核來源，不擅自刪除。 |

## B. 超過八筆的檢索

- 離線 fixture：至少 16 筆已知偏好，目標置於常駐 preview 外，問具體主題；工具必須從 Graph 查回，不能只讀前 8 筆。
- 真機用已確認存在且不在常駐集合的偏好詢問：「你記得我對○○的喜好嗎？」記錄是否呼叫 memory.search_my_profile，以及回傳是否命中。
- 測同義詞（如游泳／游水）：Profile query 可展開同義詞；字詞索引可能未命中，未命中應說目前沒查到，而不是斷言你沒說過。
- 「列出所有偏好」若 truncated=true，只能說這次取得部分；不得宣稱完整清單。
- Graph outage 用離線 timeout fixture 驗證：回 unavailable/cache，不清空已知快取、不宣稱 Graph 沒記憶。

## C. 推測與事實

1. 在測試 room 說「我想探索新活動，但今天只想輕鬆一點」。
2. 若阿月提出「是不是想探索但不想冒險」等推測，先不確認，直接要求活動建議。
3. 不得把該推測當已確認個性；應保留可能性或只使用已知偏好。
4. 明確回「不是，我只是今天累」，再問對我的理解；不可繼續沿用錯誤推測。
5. 新 room 與記憶頁確認 assistant 自己的推測沒有成為 durable preference。

Prompt 規則與輸入管線的離線驗證不代表模型永不犯錯；C 組必須人工讀回覆，失敗就先不 freeze。

## D. 多聊天室與 Compaction

- A room 談書籍、B room 談運動，兩邊交替：當前 room 最近歷史／摘要不可串房；帳號長期偏好可共用。
- A room 的未確認推測不得在 B room 被當成事實。
- 近期情境與部分操作暫存是帳號共用：跨 room 說「繼續那個」時，模糊目標需澄清，不能替另一房的行事曆／提案執行操作。
- 長對話跨壓縮門檻後，查 summary 的 owner/room/watermark/evaluation；回覆記得一個詞，不等於 compaction 已啟用。
- 開新 room 不應讀 A room summary。停用 durable memory 不代表從 A room 已保存原文消失。

## E. Event 與配對

| 編號 | 操作 | 通過條件 |
| --- | --- | --- |
| E1 | 查看排程結果 | 區分 discovery coverage、人口掃描、created 與 delivered。超過 30 人仍全部有處理結果；三張只是每批上限。 |
| E2 | 第一方離線時等已授權排程投遞，登入 Hub | 有一張活動卡、正確暱稱／方向理由／活動資訊，不需先聊天才能保存。 |
| E3 | 第一方接受 | 第一方顯示等待；第二方此時才收到邀請。 |
| E4 | 第二方接受 | 可進共同聊天室；舊 pair 沿用，活動開場只一張。 |
| E5 | 拒絕與選擇不記原因 | 終態即時更新，不重現接受按鈕，不新增 AVOIDS。 |
| E6 | 勾原因並同意記錄 | 僅勾選內容進本人 AVOIDS，不改對方偏好。 |
| E7 | 一般配對搜尋完成 | 原 room 同頁出現入口；反覆刷新不重複卡片，Event 名額不佔一般名額。 |
| E8 | 重啟／逾時／租約接手 | 先離線 fixture 驗證 checkpoint、重試與防重複；真實週期不為測試任意重跑清理。 |

## Freeze gate

- 本輪配對收尾與新增真機步驟見 [搜尋／邀請修正紀錄](MATCH_SEARCH_CONSENT_FIX_2026-09-08.md)。一般認識、活動伴預覽、明確代送、重進 room 標籤需補驗；舊提案不當新流程測試樣本。

- A、B、C、D 的 owner／stance／停用與推測檢查無重大錯誤；E 的雙方同意及投遞成立。
- 已知模型限制、活動覆蓋不足等可接受項目明列；不以測試通過宣稱無 bug。
- 合併前檢查前後端分支、相依提交、GitNexus 變更範圍與必要測試；不可把尚未提交 WIP 當成 main 已有。
- 部署透過 start_all.sh，固定公開網址；手機使用對應版本 APK。
- 凍結新功能，只收驗收阻斷 Bug。保留版本號、測試日期、結果與 rollback commit。
