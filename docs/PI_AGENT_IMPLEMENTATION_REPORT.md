> **歷史文件（2026-09-14 以前）**：本文記錄已退役的公開 DAG 架構，不可作為現行操作指引。公開阿月目前固定使用 Pi；現行規格見 Server/AYUE_V3_ARCHITECTURE.md。\n\n# Candy 公開阿月 Pi 完整遷移實作報告

日期：2026-09-14

後續 Candy 實測的 15 秒逾時、稱呼「物件」、衝浪主題配對與卡片文案消失，已另列[修正與驗收紀錄](PI_FEEDBACK_FIXES_2026-09-14.md)。以下計畫驗收數據保留原執行範圍，不與後續結果混算。

## 交付狀態

- Candy 保留 Pi／DAG 手動切換；其他帳號仍是 DAG。
- DAG、Scheduler、共用資料與既有確認資料均未刪除。
- Pi 公開能力已按 Calendar、Relationship、Product、Profile/Memory、Places、Web、Match、Assessment、Workflow 與 System 分檔。
- 公開語音問題沿用相同 public chat stream；獨立 App Voice agent、Private、Risk／Guardrail 與配對演算法未遷移。

## 主要安全與架構結果

- 中立 `public_runtime` 在載入 DAG 前先分流；Candy Pi 一般 turn 進 `pi/public_turn.py`，不載入 Scheduler、Planner 或 Synthesizer。DAG 才 lazy import Scheduler。
- `pi/registry.py` 固定列出 31 個核心工具；`tools.enable_domain` 與關鍵字曝光已移除。Node/Python 共同核對封閉 schema 與後端權限。
- Node 使用 `Agent`、awaited `subscribe`、`beforeToolCall`、`afterToolCall`、`prepareNextTurnWithContext`、`shouldStopAfterTurn`、`transformContext`、`followUp`、`abort` 與 `waitForIdle`。
- 一般任務預算為 6 model／8 tool／90 秒；研究或複合任務為 10／16／120 秒，presentation 與一次 protocol repair 都包含在內。
- 歷史互動改以 Pi 專用無權限摘要提供；歷史仿卡文字不再進 assistant prose。無後端紀錄的 marker、仿卡標題、卡片已準備宣稱及未驗證成功宣稱會被拒絕。
- Pi 確認卡與選人卡都有 publication handshake；新紀錄含 `source_engine=pi` 與 protocol version。Flutter 仍只從 `choice_prompt`／`interaction_blocks_v1` 建立控制項。
- Pi 不再建立或讀取 Places snapshot/reference；「第二間／那個活動」由同房間可見前後文理解，工具只接收具體名稱與條件。DAG 的舊 reference 資料與模組未刪除。
- 回覆驗證保存具體 public reason 與階段；純文案最多修復一次且修復階段沒有工具。查詢成功但文案失敗時改用已驗證 observation 摘要，不再錯誤提示確認卡。
- 卡片仍可由 LLM 安排前後文；保存或啟用失敗時 API 不發布 live-looking 控件。
- 多項請求保存 2–4 個自然語言目標，一次只準備一個互動；Pi/DAG 切換時佇列不跨引擎推理。

## 驗證結果（本輪修改後）

- Pi、Calendar、provider/time：93 passed、6 個 TestClient 案例未納入。
- Confirmation/contact/workflow/public-reply/calendar/write：147 passed、20 subtests。
- Scheduler/Product：106 passed、16 skipped、5 subtests；曾發現 1 個 Product topics 相容回歸，修正後全檔通過。
- Public stream/delivery：34 passed。
- Node bridge：固定工具、repair 無工具、最後預算回合接受與既有 loop 案例通過。
- 架構載入檢查：import Pi public lifecycle 後，Scheduler／Planner／Synthesizer 均未載入。
- HTTP TestClient 在目前 workspace 混用兩套 venv 的 AnyIO portal，進入 context 時會掛住；此項尚未驗收，不能用局部單元測試替代。
- Flutter：585 passed；Linux release 重新建置成功，產物為 `DatingApp/build/linux/x64/release/bundle/dating_app`。
- `start_all.sh` shell syntax 通過，並由此入口重新啟動最新程式；Social 8000、Risk 8001、Matchmaker 9001、Guardrail 8081 均回 healthy。
- 真實模型 Calendar 首輪 19/21；一例 ReadTimeout、一例修改地點未進 prepare。兩例保留後分別重跑通過，駁二 16:00／17:00 均通過。
- 跨領域終點 smoke 首輪 9/11；Web 回合耗盡與 Workflow schema 失敗均保留，修正／有界 fallback 後各自重跑通過。這仍不是 180 案 benchmark。
- GitNexus 重新索引：19,218 nodes／44,616 edges／458 flows；全 Server dirty worktree 分析為 CRITICAL（35 files、158 symbols、50 flows），包含大量本輪前既有修改，不能作為 DAG 可清理訊號。DatingApp 為 MEDIUM（3 files、5 symbols、1 flow）。圖譜另回報 2,271 個入口候選未納入流程排名，不能把未顯示流程視為無引用。

## 已知基線與尚待 Candy 試用

- Social 全量離線測試：2121 passed、20 skipped、19 failed。19 個既有失敗集中於尚未提交的 DAG Match、Planner timeout contract 與舊 Places subagent prompt 斷言；本次相關回歸集合沒有新增失敗，也未修改這些斷言來掩蓋結果。
- 先前 Calendar 21 個與 11 個跨領域 live smoke 不是本輪完整 180 案驗收；本輪尚未重跑 180 個真實模型終點案例。不得把工具選擇 smoke 當成完整通過。
- 沒有執行正式 Calendar／邀約／Match 寫入自動測試；正式變更仍必須由 Candy 的真實確認卡觸發。
- 自動 Flutter、Linux build 與服務健康已完成；尚未由使用者在 Candy Linux UI 實際走完登入、真實訊息保存及卡片點擊，因此 native UAT 仍明列未完成。

## 保留範圍

本次沒有 DAG 退役、資料清除、全帳號 rollout 或預設退役日期。後續是否擴大或清理，需依 Candy 試用結果另立計畫並重新執行 impact、引用與完整回歸分析。
