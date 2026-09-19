# Agent 共用額度

配對阿月、阿月悄悄話、語音阿月共用額度，免費預設上限及初始額度皆為 **150K**。每次登入完成驗證都向後端查詢，後端讀取 Appwrite 並結算每日補額；既有帳號登入、查詢或首次使用時補建。初始化失敗不阻擋登入，但新 AI 任務會在無法確認額度時拒絕開始。

## Appwrite 管理

沿用現有 Appwrite project，database 預設 `dating_db`。所有額度集合不開放 client 直接讀寫，API 以 Appwrite JWT 綁定帳號。

| 集合 | 用途／可調欄位 |
| --- | --- |
| `agent_quota_settings` | 文件 `default` 的 `initial_tokens` 預設 150000，控制之後初始化的帳號；`daily_refill_tokens` 預設 40000，控制每日補充量。 |
| `agent_quotas` | 文件 ID 為帳號 ID。`max_tokens` 是百分比分母；`remaining_tokens` 是可用額度，兩者分開修改。`infinity=true` 開啟無限額度。`last_refill_at` 是後端維護的補額時間（Appwrite datetime）；`refill_policy_version` 用於一次性升級去重。 |
| `agent_quota_usage` | 每次模型呼叫的輸入、輸出、實際扣額與任務識別；不存對話內容。 |
| `agent_quota_codes` | `Sunnyfan116` 開啟無限，只有此碼可重複使用；`enabled=false` 停止所有後續兌換，不影響已啟用者。 |
| `agent_quota_redemptions` | 保留首次兌換紀錄；一般代碼每帳號一次，僅精確的 `Sunnyfan116` 例外。重複使用 Sunnyfan116 不新增重複文件。 |

每日以 **Asia/Taipei（台灣時間）午夜 00:00** 為界。後端比較目前日期與 `last_refill_at` 的台灣日期，跨一天補 40K、跨兩天補 80K；補後餘額不超過該帳號的 `max_tokens`（預設150K）。不是從上次登入起等待滿24小時。

補額與時間戳在同一交易更新，同日重登或多裝置同時登入不會重領。登入、額度頁查詢及新AI任務檢查都會觸發結算，不需要午夜背景排程；長時間不登入的帳號下次會一次補足跨日額度。滿額的日子也會推進時間戳，不會把日數囤起來。無限帳號更新時間但不補有限餘額，關閉無限後不追補無限期間已結算的天數。

`max_tokens` 與 `remaining_tokens` 仍可分開在 Appwrite 管理。例如剩20K，把上限由150K改200K，仍剩20K，只改變百分比分母及後續補額上限；立即加值請直接修改 `remaining_tokens`。不要以 `max_tokens - used_tokens` 計算剩餘額度。

`used_tokens`、`matching_tokens`、`private_tokens`、`voice_tokens` 是歷史累計，正常情況由後端維護，補額不清除。`revision` 是內部交易欄位。無限帳號持續累計用量，但不扣原有有限餘額；關閉 infinity 後使用保留的餘額。手動關閉 infinity 後，可再次輸入仍啟用的 `Sunnyfan116` 恢復無限；其他已使用代碼仍不可重複兌換。大小寫不同的字串不享有重用例外。

前端只顯示剩餘百分比或「無限」，不顯示剩餘token數；累計用量以K／M顯示。畫面僅保留「每日午夜 00:00 更新」，不顯示補充量、上限或詳細補額制度；後端每日補額規則不變。

## 啟動與遷移

沿用 `APPWRITE_INTERNAL_ENDPOINT`／`APPWRITE_ENDPOINT`、`APPWRITE_PROJECT_ID`、`APPWRITE_API_KEY`；API key 需要 collections、attributes、documents、transactions 的對應讀寫權限。可選 `AGENT_QUOTA_DATABASE_ID`，預設 `dating_db`。

第一次安裝先從 Server 目錄執行一次：

```bash
PYTHONPATH="$PWD:$PWD/social" .local-venv/social/bin/python -m agent_quota.setup
```

腳本只新增缺少的集合、欄位及初始設定，不重設既有餘額、預設額度或已停用代碼。

從舊100K、不定期恢復的版本升級時執行：

```bash
PYTHONPATH="$PWD:$PWD/social" .local-venv/social/bin/python -m agent_quota.migrate_daily_refill
```

升級會建立缺少的欄位、將共用預設改150K／40K，並對原100K帳號補上50K差額。既有用量、無限設定及非100K的手動上限均保留；沒有補額時間的舊帳號以升級時間開始計算，不追溯上線前日期。版本欄位確保重跑不會再次補50K。

正式啟動仍使用 `./start_all.sh`，port 和公開網址不變。Social 與 Matchmaker 隨既有服務啟動待結算重試工作，不需新增服務或 port。前端需重建後才會顯示「Agent額度」。

## 結算與故障恢復

模型回報的輸入加輸出計入所屬功能；risk_backend、embedding 與系統背景任務不建立計費 scope。使用者提出的配對工作即使排入工作佇列，仍保存計費身分。語音衍生的公開／私聊操作經原本 HTTP 入口重新檢查，錯誤訊息會透過 action result 告知語音阿月。

文字串流工作在用戶斷線後繼續結算；語音回覆隨 provider usage 更新，不等整通結束。模型沒有回報 usage 的失敗片段無法精確計算，因此不使用猜測 token 替代。已回報的 usage 在解析失敗／取消時仍入帳。

本機 `.runtime/agent_quota.sqlite3` 是 SQLite durable outbox，僅保存待送的用量。Appwrite 是餘額及紀錄的權威來源。每個模型呼叫有固定識別，累計快照只結算增量，重送不重扣。後端每 5 秒重試；新任務開始檢查前也會先結清該帳號的待送用量。無法結清時暫停新任務，已開始的任務繼續。

Appwrite 交易同時提交餘額和用量紀錄；跨程序帳號鎖序列化同帳號結算，防止 Appwrite 1.9 並行 staged read 導致計數遺失。**目前部署為同一台 Server**：Social、Matchmaker 必須共用 outbox 及其旁的 `agent-quota-locks` 目錄。可用 `AGENT_QUOTA_OUTBOX_PATH` 指定共用持久路徑。擴展至多主機前必須改用跨主機協調機制；不要各自使用獨立 outbox／鎖目錄。

## API

- `GET /api/agent-quota`：本人上限、餘額、infinity、總用量、三功能用量、剩餘百分比，以及 `last_refill_at`、`daily_refill_tokens`、`refill_timezone`、`refill_time`、`next_refill_at`。
- `POST /api/agent-quota/redeem`，JSON `{"code":"Sunnyfan116"}`：精確輸入 `Sunnyfan116` 可重複回傳 `redeemed`；其他代碼在重複使用時回傳 `already_redeemed`，無效或停用回傳 `invalid_code`，並附上最新額度。
- 所有操作需要 `Authorization: Bearer <Appwrite JWT>`；無法透過 request body 指定別人的額度。
- 403：`detail.code=agent_quota_exhausted`；503：`detail.code=agent_quota_unavailable`。`detail.message` 為畫面與語音可用的中文訊息。

## 驗證

```bash
PYTHONPATH="$PWD:$PWD/social" venv/bin/python -m pytest agent_quota/tests/test_quota.py -q
# 明確執行真實 Appwrite 整合測試；只建立隔離的臨時文件，最後清除。
PYTHONPATH="$PWD:$PWD/social" venv/bin/python agent_quota/tests/appwrite_smoke.py
PYTHONPATH="$PWD:$PWD/social" venv/bin/python agent_quota/tests/appwrite_refill_smoke.py
```

Flutter：`flutter test --no-pub test/agent_quota_test.dart`。
