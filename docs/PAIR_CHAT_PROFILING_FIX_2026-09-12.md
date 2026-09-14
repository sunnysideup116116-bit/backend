> **架構更新註記**：本文保留其 domain／歷史內容；其中公開 V3 Planner、Scheduler、subagent 或 DAG 的描述已被 Pi 正式架構取代。\n\n# 雙人聊天室開場與 Profiling 錯誤回覆修正

> 本篇是 09-12 的修正紀錄。共同開場已於 09-13 改為 Appwrite-first 暱稱及模型客製化，現況見 [後續修正](PAIR_OPENING_PERSONALIZATION_2026-09-13.md)；profiling 不變。

## 原因與修正

1. `public_chat.direct_chat` 原本在共同聊天室第一則真人文字後呼叫 LLM，並用 **收件者的 sender_id** 保存生成文字。它不是僅限測試帳號的分支。現在第一句起只保存真人送出的內容；一般配對在 accepted transition 後，以阿月身份保存一則 match-scoped、冪等的共同開場。公開依據由既有提案投影取得；私人 directional opening 仍留在各自媒人房間，Event 保留自己的活動開場而不重複新增。
2. Profiling 的模型例外、JSON 解析失敗、空／無效 reply 原本都會被包成「系統錯誤」，再轉成「我剛剛沒有聽清楚……」。`/api/chat` 卻始終回 `status=success`，前端因此將錯誤存成正常回答。現在使用 typed error、輸出結構驗證、有界重試與清楚的服務錯誤提示；原答案保留供同 request ID 重試，失敗不計分、不寫入 assistant 對話。
3. 舊 active assessment 只傳本句及 draft，未傳上一題；前端的隱藏「請開始發問」還會占用一輪回答。現在初始化不呼叫模型、不占題數，session 保存上一題、最多三組 bounded Q&A 及本次明確提供的興趣。一般長期記憶與已完成性格資料仍不混入獨立測驗。

## 歷史事件能確認到哪裡

- 可從舊程式確認上述 fallback 的觸發入口，但不能將每次觸發直接判定為「使用者說不清楚」。
- 本次檢查現存 `social.log` 沒有保留可對應的舊 profiling 例外；`start_all.sh` 使用覆寫式重導向。不能推論過去沒有錯誤，也無法可靠計算 timeout／429／解析失敗的比例。
- 這條 assessment 路徑使用聊天 completion，不呼叫 Google embedding，不能歸因於 embedding 日額度。
- 新的 diagnostic logs 只記 error code、model、kind、attempt、耗時或 revision，不記原始回答、prompt、raw exception 或金鑰。歷史留存仍受目前啟動日誌保留方式限制。

## 驗證

- `PYTHONPATH=/tmp/ayue-merge-deps.Nvwdhw venv/bin/python scripts/run_offline_tests.py social --tb=short`：**1876 passed、4 skipped、133 subtests passed**。含 CAS、取消／過期／提交、重送答案與確認、雙方首句、Event 開場及新契約測試；禁用外網／.env，資料庫為 mock。
- `flutter test --no-pub test/personality_onboarding_test.dart`：**8 passed**。包括服務錯誤不進 cache、答案留在輸入框、重試沿用 ID、第一題只出現一次。
- `flutter test --no-pub`：**519 passed、1 failed**；失敗是 `ui_consistency_test.dart` 的三層字體檢查，定位到既有／並行修改中的 `lib/widgets/app_voice_assistant_overlay.dart` 固定字級，不屬於本次兩個 bug，未修改。
- 本次三個 Dart 檔已用 SDK analyzer protocol 檢查；無編譯錯誤。LSP 模式的工具本身發生 JSON framing crash，改用 SDK 支援的 analyzer protocol；一項 null-aware element 建議已修正。
- Python compile、兩個 repo 的 `git diff --check`、`bash -n start_all.sh` 通過。
- 用虛構爬山回答單獨驗證目前 `deepseek-v4-flash:cloud`：約 **1.6 秒**返回合法 JSON、五個分數欄位與依上一題延伸的一個問題。此測試不讀真人聊天、不寫三個正式資料庫，也不代表未來供應商永不失敗。

## 交付範圍

主要修改檔案：

- 配對：`social/routers/public_chat.py`、`social/services/match_action_service.py`、`social/services/match_reason_service.py`。
- 測驗：`social/models.py`（只新增 client message ID）、`social/routers/chat_onboarding.py`、`social/services/assessment_session_service.py`、`social/services/ai_service.py`、新增 `social/services/assessment_provider.py`。
- 後端測試：`social/tests/test_ayue_agent_stream.py`、`test_match_action_service.py`、新增 `test_assessment_reliability.py` 與 `test_pair_chat_opening.py`。
- Flutter：`DatingApp/lib/pages/personality_chat_page.dart`、`lib/services/matchmaking_api_service.dart`、`test/personality_onboarding_test.dart`。
- 文件：本篇、`README.md`、`docs/AYUE_V3_ARCHITECTURE.md`、`docs/architecture/09-runtime-interfaces.md`、`docs/api/ayue-v3-mobile-bootstrap.md`。

- 程式及對應 API／架構文件已更新；不刪除舊自動回覆紀錄，不回填歷史配對的開場。
- 本次未 commit、push、合併、重啟 Server 或重包 APK；保留工作區與 index 中其他人的變更。
- App 畫面的重試修正需要新版前端，後端需透過正式 `start_all.sh` 重啟載入。真人端到端／APK 手測仍需在部署後進行。
