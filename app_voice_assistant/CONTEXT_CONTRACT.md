# 語音阿月：第二批基礎版

此版本沿用 protocol v3、現有確認與 canonical API，只新增可相容的畫面上下文、結構化動作結果與共用能力定義。

## 可操作的畫面

| 畫面 | 提供的項目 | 可使用的指代 |
| --- | --- | --- |
| 真人聊天室 | 目前聯絡人，自動選取；不帶聊天訊息內容 | 「回覆他說我可以」「查看和他的共同約會」 |
| 行事曆 | 本頁行程的順序、名稱、日期時間、狀態 | 「選第二個」「把這個改到明天」「取消第二筆行程」 |
| 阿月牽線 | 目前畫面已展開的邀請；保留真實 namespace 和 revision 在本機 | 「接受第二個」「婉拒這張」 |

單獨選取項目使用 `select_screen_target → ui.target.select`，不修改業務資料。點擊卡片空白處與語音選取共用狀態，以不占版面空間的外框標記。牽線的收合卡片不列入清單；每頁最多提供 20 項並標示 `truncated`，序號只對應這份清單。這不是全螢幕截圖辨識或任意元件控制。

## 畫面參照

Flutter `AppVoiceSurface` 保存 route、帳號 epoch、已載入資料及選取狀態。新增的 `context.screen` 包含：

- `surface_id`、`ready`、`selected_ref`、`available_actions`、`truncated`。
- `items`：`ref`、`kind`、`label`、允許的日期／時間／狀態及動作。
- `context.revision` 在清單內容、順序、資料版本或可用狀態改變時更新；相同資料的 polling 保留參照。

真正的 user ID、event ID、match ID、namespace 和 canonical revision 只留在 Flutter 本機的 `AppVoiceTarget`。模型只傳 `target_ref`。Server 在建立既有確認前，把「他／這個／第二個」綁成目前參照；Flutter 執行時再次核對頁面、帳號、revision、項目種類與權限。行程及牽線另外重新讀 canonical 資料比對版本；聯絡人重新確認仍在可聊天名單內。同名不會退回部分名稱比對。換頁、清單更新或對象失效時回報 `stale_target`，不猜測新目標。

`screen_read` 加上該資料種類的權限才會傳送項目。Server 再做白名單過濾與大小限制；畫面標籤視為資料而非指令。未準備好或離線畫面不接受參照操作。既有完整名稱指令仍可使用原流程。

## 動作結果

Flutter 保留舊版 `success`、`message`，新增 `result_version: 1`、`status`、可選 `error_code` 與 `data`。

- `status`：`success`、`failed`、`needs_input`、`awaiting_confirmation`。牽線僅準備待確認時使用 `awaiting_confirmation`。
- 錯誤碼包含 `permission_denied`、`stale_target`、`ambiguous_target`、`not_found`、`unauthenticated`、`network_error`、`validation_failed`、`unsupported`、`operation_failed`。
- 行事曆、聯絡人查詢提供結構化的可見資料；選取結果提供目前參照。未遷移的舊 handler 仍能回傳舊訊息，未知錯誤保留通用失敗。
- Live direct tool response 及背景阿月回覆均保留新版結果；舊 Client 的兩欄結果維持原有行為。

此版沒有新增任務持久化、跨 session 恢復、完整執行中取消或可復原交易；確認和副作用去重仍沿用既有流程。

## 共用能力清單

`capabilities.json` 是基本動作權限、參數名稱、固定確認需求、目標種類與目標權限的來源。條件式設定確認、領域權限及參數值驗證仍由原有 validator 處理。Python 直接讀這份清單；Flutter 使用獨立可部署的產生檔 `app_voice_capabilities.dart`，不需要執行時存取 Server 原始檔。

在 Server 根目錄產生／檢查 Flutter 清單：

```bash
venv/bin/python app_voice_assistant/generate_catalog.py --output ../DatingApp/lib/services/app_voice/app_voice_capabilities.dart
venv/bin/python app_voice_assistant/generate_catalog.py --check --output ../DatingApp/lib/services/app_voice/app_voice_capabilities.dart
```

發布時同步交付 JSON 清單與 Flutter 產生檔。新增欄位有版本標記，Server capability 也回報 `action_catalog_version`、`screen_context_version` 與 `structured_action_results`。

## 驗證範圍

新增測試涵蓋：同名聯絡人與行程綁定、畫面換頁／帳號切換／版本更新、權限過濾、清單上限、手動與語音共用選取、真實三頁上下文、Live 確認前畫面變更、舊結果相容及新結果傳輸。

上述測試使用可控的語音工具事件與 API 回應，不代表 Android 實際麥克風、Gemini 自然語句辨識或真實網路延遲已驗證。畫面上下文使用本機既有頁面資料；沒有新增模型服務或每輪全量資料查詢。

完整 Server 仍透過 `start_all.sh` 啟動；本版不改 ports 或公開 API 網址。

## 2026-09-12 驗證結果

- App Voice Server：82 項通過。
- Flutter 語音與三頁回歸：135 項通過；最後追加牽線逾時處理後，10 項上下文測試重新通過（含新增案例）。
- 本次修改範圍的 Dart 靜態分析：沒有問題。全工作區分析只有其他檔案的兩個既有 style info。
- 完整 Flutter 測試：532 項通過、1 項既有失敗。`ui_consistency_test.dart` 禁止固定數字字級，但 HEAD 版本的 `app_voice_assistant_overlay.dart` 已有 13／12 字級；本次沒有修改該浮層或放寬測試。
- 共用能力產生檔 `--check`、`git diff --check`、`bash -n start_all.sh` 通過。
- GitNexus 最後以兩個實際 repository 路徑執行 `detect-changes --scope all --limit 10000`，沒有 partial／truncated 分析警告。Server 為 72 個 symbols、44 條流程、CRITICAL；DatingApp 為 72 個 symbols、9 條流程、HIGH。這是整個未提交工作區（包含既有聊天、個資、配對修改）的影響，不是本次獨有的差異。新檔已納入索引；Git diff 分析不列出尚未追蹤的新檔，另由新增測試與靜態分析覆蓋。
- 全部頁面測試使用 mock API；沒有以測試資料寫入正式帳號，也沒有進行實機麥克風或 Live 模型端到端／延遲量測。
