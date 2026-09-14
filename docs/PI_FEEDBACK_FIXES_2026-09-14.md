> **歷史文件（2026-09-14 以前）**：本文記錄已退役的公開 DAG 架構，不可作為現行操作指引。公開阿月目前固定使用 Pi；現行規格見 Server/AYUE_V3_ARCHITECTURE.md。\n\n# Candy Pi 使用回饋修正（2026-09-14）

## 問題與修正

| 問題 | 確認原因 | 修正 |
| --- | --- | --- |
| 黃框 `TimeoutException after 0:00:15` | Flutter 初始 HTTP 等待只有 15 秒且未轉成應用例外；Pi 尚未有工具／回覆時沒有首段 stream body | Pi 先發 `run_started`，空行保活；Flutter 初始等待 30 秒並分類逾時，不自動重送 |
| 把人稱為「物件」 | Pi finalizer 的 OpenCC `s2twp` 在用語修正之後再次轉換「對象」 | 最後一輪繁體轉換後再套用 person terminology |
| 無法按衝浪主題配對 | Pi 沿用空參數的 `match.start_search` schema，沒有提供主題欄位 | Pi 專用 `kind/topic/source_text`，驗證可見使用者原句，接既有配對準備與確認後 executor |
| 未按確認就失去卡片文案 | Match 歷史查詢漏選 `preview_text`，刷新投影覆蓋原 `display` | 查回後端原 preview，保留同卡的標題、摘要、結果說明及狀態 |

本輪維持 Candy 限定 Pi、共用資料、DAG 可切換。主題搜尋不保證人選具有指定技能，也不自動代送邀請。沒有改寫配對演算法、變更帳號權限或重送 Candy 正式操作。

## 確定性驗證

- 修改前相關基線：97 passed、6 個原有 TestClient 案例未納入。
- 新增四項回歸先取得失敗：缺 display、對象變物件、首段串流等待模型、Match schema 無 topic；修正後通過。
- 配對測試另驗證前文「衝浪」後補「對」、改為近期情境會清除主題、拒絕無來源主題及模型偽造權限，以及確認後只提交一次。
- Flutter 驗證 20 秒初始回應成功、30 秒逾時轉中文且零重送、空行保活不能延長 150 秒總等待期限；既有 public/private/voice stream 與按鈕協定通過。
- 本輪 Pi／stream／history 相關集合：81 passed、6 個原有 TestClient 案例未納入。擴大的 DAG／Match／Calendar 集合：214 passed、16 skipped、1 failed；失敗是 `test_chat_decision_redirects_to_the_hub_without_changing_the_proposal`。以修改前的歷史投影在獨立測試程序中重跑，仍同樣失敗；未刪除或放寬斷言。
- Flutter 全套：588 passed（新增 3 個 timeout／keepalive 回歸）。
- 最後新增的 provider-stream 隔離檢查通過；本檔回饋測試共 9 passed，模型原始片段／卡片占位符不直接轉送使用者。

## 交付狀態

- Linux release 已於 2026-09-14 20:08 重新建置，使用固定 `https://service.misproject.us.ci/`；產物為 `DatingApp/build/linux/x64/release/bundle/dating_app`。已開啟的 App 必須關閉再開啟才能載入新執行檔。
- `start_all.sh` shell syntax 與 Python compile 通過；透過既有 `start_all.sh` 的 r 重啟套用。四個固定服務 ready，公開 `/api/health` 回 `status=ok`。
- Server 與 DatingApp 已重新索引。整個既有 dirty Server worktree 分析為 CRITICAL（35 files／81 symbols），DatingApp 為 MEDIUM（4 files／6 symbols）；這些數字包含本輪前修改，不是本輪 diff 的獨立風險統計。圖譜仍有未追蹤的入口、跨語言及 dynamic dispatch 限制，並非完整依賴證明。
- 沒有刪資料、提交 Git、操作 Candy 正式配對確認，或替使用者關閉正在使用的桌面視窗。

## 真實模型終點驗證

使用 `scripts/run_pi_feedback_acceptance.py`，真實 Pi loop、schema、preparation、confirmation manager、message fixture 保存、啟用、刷新三次及確認 executor。所有帳號、collection 與配對 side effect 都是隔離 fixture。

| 案例 | Run ID | 耗時 | 模型呼叫 | 結果 |
| --- | --- | ---: | ---: | --- |
| 找一起衝浪的人 | 472926c7a7794b4ba9a0a78cf98bfa29 | 10.3 s | 2 | 通過 |
| 找會衝浪的人，先看人選 | bc3b178aa8b2466c838448f535a8f290 | 8.4 s | 2 | 通過 |
| 前文衝浪，補「對，幫我找」 | a6996196a82c420495071266325fa462 | 8.2 s | 2 | 通過 |
| 使用近期情境 | c585223cc89a4111a073913a633194fa | 11.9 s | 2 | 通過 |
| 不要衝浪，改用近期情境 | 0d0d4d4361cb4134b91ad714da46c0ca | 5.8 s | 2 | 通過 |

首輪 5/5 通過。每例確認前零提交，刷新三次仍有相同 display，重複按確認仍只提交一次。

這五例不是完整 180 場景 benchmark；Flutter 自動測試與 Linux build 也不能代表 Candy Linux 實機已驗收。Appwrite mirror 的 HTTP/HTTPS 500 與標題背景重試是另外已識別的問題，本輪沒有更動。
