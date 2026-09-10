# 已提交修正整合（2026-09-10）

## 範圍

- 使用者授權將 9/9 晚間及 9/10 上午已完成提交整合到遠端 main。
- Backend：legacy-origin/20260909_NEW_BUG_FIXES，398f3c8、dbe397d；基底 backend/main=c39a1a1。
- DatingApp：origin/專案教室電腦，02b9d85、82681d3；基底 origin/main=ae161d1。
- 兩者皆可快轉；在獨立 worktree 驗證，不切換、不 stash、不提交組員原工作目錄正在修改的語音註冊、平台設定及約會卡。
- 不更改私有 .env、不新增模型呼叫、不重啟正在開發的 Server、不手動 dispatch APK 建置。main push 仍可能觸發既有 workflow。

## 驗證時發現的問題

- 舊驗證 venv 缺少新提交已宣告的 jsonschema，造成 31 個 provider/settings 測試 import failure。只安裝到臨時測試目錄，再透過 PYTHONPATH 執行離線 runner，沒有修改正式 venv。
- 補齊後只剩舊串流 UI 測試要求 gmp-place-details-compact。新版程式已統一使用 renderCustomPlaceCard；只更新該斷言為禁止舊元件、確認自訂 renderer。其餘提及、Markdown、安全網址與卡片檢查保留，產品程式不另改動。
- 前端六檔回歸 94 passed、四個修改來源 Dart analyze 無問題；後端 contracts 69 passed；Social 最終完整 1843 passed、4 skipped、133 subtests。
- 原有 Risk google.genai 依賴缺漏及 DatingAppBuild 下載 iOS artifact 的 403，不屬這兩批已提交修正；本次不宣稱已處理。Android 編譯成功不等於 Release 已更新。

## 整合方式

後端在 dbe397d 上追加上述測試／紀錄提交，再以非強制 push 快轉 backend/main；前端快轉到 82681d3。推送前執行 GitNexus detect_changes（目前變更及相對正確遠端 main），確認無 partial/truncated。不要使用後端另一 remote 所屬的本機舊 main 作比較基底。

安裝包仍需完成既有發布流程；本次 Git main 更新不保證使用者已下載或安裝新版 APK。
