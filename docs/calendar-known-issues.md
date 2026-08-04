# 行事曆功能已知問題紀錄

> 本文紀錄對「行事曆」功能（個人行程＋共同約會）的靜態程式碼審查發現。
> 狀態：**尚未修改任何 code**，多數為「可能觸發」的風險，需實際測試驗證。

審查範圍（檔案）：
- `services/calendar_service.py`（個人行程）
- `services/date_coordination_service.py`（共同約會狀態機）
- `services/ayue_agent/tools.py`、`tool_registry.py`、`runtime.py`、`router.py`
- `routers/calendar.py`（REST）
- `frontend.html`（UI）

---

## 一、共同約會（shared date）相關

### 🔴 1.（已修復）撤回改期後未清除「改期中」標記，之後改表單會污染已確認行程

**情境**：A、B 的共同約會原本是 6/1。A 提出改期到 6/8，行程暫時標成「等重新確認」。A 後悔，按「撤回改期」──行程還原為 6/1 正常狀態。**之後 B 又改了一次約會表單**（例如改地點），因為系統沒清掉「這筆行程正在改期」的標記，改地點的動作會把「待確認」狀態又掛回 6/1 那筆行程，導致 6/1 明明沒被改，卻卡在待確認。

**位置**：`routers/calendar.py` `cancel_reschedule`（L79-106）還原行程時，沒清除 coordination 的 `rescheduling_event_id`；`date_coordination_service.py` `update_form`（L183-195）仍會對該 event 寫入 `pending_change`。

### 🔴 2.（已修復）改期把「協調單版本」與「行程版本」強制綁定，可能誤判 409

**情境**：共同約會成立時，行程與協調單版本同為 1。之後 B 只改表單（協調單變 2，行程仍是 1）。A 再提出改期時，系統**同時**要求協調單版本=2 **且**行程版本=2，但行程其實只有 1 → 直接 409「行程剛有變動」，A 莫名其妙無法改期。

**位置**：`date_coordination_service.py` `request_reschedule`（L293-298）；`runtime.py` `_execute_calendar_pending`（L1102-1103、L1136-1138）共用同一個 `event_revision` 當兩邊的 expected。

### 🟠 6.（已修復）`get_calendar_context` 可能帶出對方行程 ID

**情境**：這個函式回傳對方行程時會連「行程內部編號」一起帶出。目前阿月呼叫時剛好沒帶對方帳號，所以尚未真的外洩；但萬一未來某功能帶了對方帳號呼叫，對方的行程編號就會跑出去。

**位置**：`calendar_service.py` `serialize_event` 的 non-private 分支（L143-145）仍回傳 `event_id`；`get_calendar_context`（L452）使用它。

### 🟠 9.（已修復）改共同約會表單時，未確認行程是否真的在「等確認」狀態

**情境**：與第 1 點連動。系統要寫「待確認」標記到行程時，沒先確認那筆行程目前是否真的「等確認」。正常情況安全，但一旦狀態亂了（像第 1 點），這裡會一起出錯。

**位置**：`date_coordination_service.py` `update_form`（L183-195）。

---

## 二、個人行程（personal event）相關

### 🔴 3.（已修復）網頁直接修改行程沒有版本檢查，會被覆寫

**情境**：同時用聊天室讓阿月改行程，以及網頁手動改行程。阿月改完版本變 2，網頁再改一次──網頁沒送版本號，直接覆寫，把阿月剛改的資料蓋掉，兩邊不同步。

**位置**：`frontend.html` L4845（PATCH 只送 title/date/times，無 `expected_revision`）；`calendar_service.py` `update_personal_event`（L248）在 `expected_revision=None` 時無條件更新。

### 🟠 8.（已修復）網頁取消共同約會沒有版本檢查

**情境**：同第 3 點。網頁直接按「取消」時沒送版本號，只用「還沒取消」當判斷。若同時有人也在動這筆，可能重複處理或誤判。

**位置**：`routers/calendar.py` L50-59，呼叫 `cancel_coordination_or_event(..., expected_revision=None)`。

### 🔴 新增疑點 10.（已修復）「我最近的行程」會跳過今天的行程

**情境**：使用者對阿月說「我最近的行程」，回傳結果似乎**漏掉今天**的行程。

**推測根因**：`tools.py` `_calendar_events`（L58-66）在沒有時間關鍵字（今天/明天…）時，把查詢起點設為**目前時間**（`now = clock_utc(clock)`），`list_events` 用 `end_at > start`（L153）過濾──**今天已經結束**的行程（如早上 8-9 點、現在 10 點）會被排除。若使用者預期看到「今天包含已過去的行程」，就會誤以為今天被跳過。

> ⚠️ 需實際測試確認：是要顯示今天整天，還是只顯示「尚未結束」的行程。尚未修。

### 🔴 新增疑點 11.（已修復）網頁編輯行程的日期/時間來源不一致，會存錯時間

**情境**：編輯行程時，**日期**用 `toISOString()`（轉成 UTC 標準時間），**開始/結束時間**卻用 `toTimeString()`（瀏覽器本地時區）。兩者來源不同。行程存檔時後端再把這組「日期+時間」當作台北時區解析 → 只要瀏覽器時區不是台北，存進去就偏掉，重開頁面看到的時間和原本不同。台灣使用者若瀏覽器為台北時區則不受影響。

**位置**：`frontend.html` L4839（日期用 `.toISOString()`）vs L4840-4841（時間用 `.toTimeString()`）── 時區來源不一致。

---

## 三、其他 / 跨功能

### 🟠 4.（已修復）改行程前 Guard 硬要「列出全部行程」，不接受「找單筆行程」

**情境**：對阿月說「幫我把下週二看電影那筆改成周三」。要改前必須先找到「看電影」那一筆（該用「找單筆行程」），但 Guard 規定改行程前一定要先「列出全部行程」；Planner 先用「找單筆」定位就會被擋，強迫多列一次 → 多花一輪對話，阿月可能困惑。

**位置**：`router.py` L409-415 只接受 `calendar.list_my_events` observation，未接受 `calendar.find_my_event`。

### 🟠 5.（已修復）批次取消部分失敗後，確認狀態被清空，須全部重講

**情境**：說「取消這三筆」。成功取消兩筆、第三筆剛被別人改過而失敗；系統卻把整個「確認」狀態清掉，必須重新說出全部三筆（含已取消的），而不是只補失敗那筆。

**位置**：`runtime.py` `_execute_calendar_pending`（L1147-1180）回傳 `partial`，但 `_handle_calendar_pending_confirmation`（L1219）一律 unset confirmation。

---

## 四、行程 hint 比對（resolver）相關

### 🔴 12.（已修復）`_event_matches_hint` 比對過於嚴格，帶時間/介系詞/全形標點的 hint 一律找不到

**情境**：使用者對阿月說「幫我取消今天的行程」。Planner 讀到行程後，產生類似 `"今天 8/3 09:00–10:00 在 50嵐 喝飲料"` 的 `event_hint` 傳給 resolver，但 resolver 回「找不到」——即使行程真的存在。

**三個疊加的根因**：
1. **haystack 沒索引時間**：原本只把 title/activity/location/日期放進 haystack，行程的開始/結束時間完全沒被索引 → hint 帶 `09:00-10:00` 就一定比對不到。
2. **整段子字串比對**：原本用 `hint in haystack`（整段連續子字串）。只要 hint 裡多了「今天」、或欄位順序跟行程不同（例如先活動名再日期），整段比對就失敗。
3. **標點字元不一致**：模型回 `09:00–10:00`（en dash `–` U+2013），但 haystack 產生的是 `09:00-10:00`（普通連字號 `-`）→ 字元不同即視為不匹配。全形冒號 `：`、全形數字 `０９`、全形空白 `　`、全形逗號 `，`、全形波浪號 `～` 同理。

**修復**（`services/calendar_service.py` `_event_matches_hint`）：
- 新增 `_normalize_hint_text`，比對前把 en/em dash、全形波浪號、全形空白、全形逗號、全形冒號、全形數字統一正規化成半形。
- haystack 補上行程的 `HH:MM-HH:MM` 時間。
- 改為**分段比對**：hint 拆段落，每段都要出現在 haystack（順序不拘）。
- 先剔除時間詞與動詞（今天/明天/幫我/取消/在/去/到 等），避免它們干擾比對。

**回歸測試**：`tests/test_calendar_event_matching.py`（18 條 case，涵蓋帶時間、en dash、em dash、全形、順序相反、地點、不命中、空字串等）。未來模型若回新怪字元，加一條 case 即可由 CI 擋住。

**防範建議**：
- 正規化要「寬鬆收」——模型回覆的標點/空白/數字不可假設一律半形，比對前兩邊都跑正規化。
- 任何「字串比對/模糊匹配」功能都應有回歸測試，用真實 hint 樣本鎖定行為，不要只靠手測。

### 🔴 13.（已修復）「最近的行程」跳過今天

**情境**：對阿月說「我最近的行程」，回傳結果漏掉今天**已經結束**的行程（例如早上 8-9 點、現在 10 點）。

**根因**：`tools.py` `_calendar_events` 在沒有時間關鍵字（今天/明天…）時，把查詢起點設為「現在此刻」，`list_events` 用 `end_at > start` 過濾 → 今天已結束的行程被排除。「最近」在中文語感上通常包含今天整天。

**修復**：改為從「今天 00:00 當地時間」起算，含今天整天的行程。

---

## 五、修改紀錄（changelog）

以下列出本次所有修復的實際變更，供接手者快速定位。

### #1 撤回改期未清「改期中」標記
- **檔案**：`social_demotest/routers/calendar.py` `cancel_reschedule`
- **變更**：撤回改期、還原 coordination 為 `completed` 時，用 `$unset` 清除 `rescheduling_event_id` 與 `last_action_key`；Python dict 端同步 `pop` 掉這兩個 key。

### #2 改期版本誤判 409
- **檔案**：`social_demotest/services/date_coordination_service.py`
- **變更**：
  - `request_reschedule`：放寬 front guard，只檢查 `event_revision != expected_revision`，不再強制 `coordination.revision == expected_revision`。coordination 有自己的 status CAS（下方 `match_query`）保護。
  - `cancel_coordination_or_event`：移除 `coordination.revision` 的 preguard 檢查與 `match_query` 的 `revision` CAS；只用 event revision CAS 與 status CAS 保護。避免表單編輯導致協調單與行程版本分歧時誤判 409。

### #3 網頁編輯無版本檢查
- **檔案**：`social_demotest/frontend.html`、`social_demotest/models.py`、`social_demotest/routers/calendar.py`
- **變更**：
  - `CalendarEventUpdateRequest` 新增 `expected_revision: int | None = None`。
  - `patch_event` 讀取 `expected_revision` 並傳給 `update_personal_event`，啟用 CAS。
  - 前端編輯按鈕送 `expected_revision` 與 `timezone`；409 時提示「行程剛剛已變更，請重新整理」。

### #4 Guard 不接受 find_my_event 作為 update 前讀取
- **檔案**：`social_demotest/services/ayue_agent/router.py` `guard_v2_decision`
- **變更**：`calendar_target_requires_read` 檢查從只接受 `calendar.list_my_events` 改為接受 `{list_my_events, find_my_event}`。

### #5 批次取消部分失敗清空確認狀態
- **檔案**：`social_demotest/services/ayue_agent/runtime.py`
- **變更**：
  - 新增 `_remaining_cancel_targets`：以 DB 現況判斷哪些目標已成功取消（status=cancelled），回傳剩餘未取消的。
  - `_handle_calendar_pending_confirmation`：`code == "partial"` 時不再 unset 整個 confirmation，而是把 `targets` 更新為剩餘的，回覆加「回覆確認會再取消剩下的行程」。使用者只需再確認一次。
  - 新增 `from database import calendar_events_coll`（runtime.py 頂部）。

### #6 get_calendar_context 帶出對方 event_id
- **檔案**：`social_demotest/services/calendar_service.py` `get_calendar_context`
- **變更**：`partner_busy` 的 non-private 分支回傳前 `projection.pop("event_id", None)`，剝掉對方行程內部編號。不動 `serialize_event` 共用結構（它是 CRITICAL，27 個上游），只在這個呼叫點剝。

### #8 網頁取消無版本檢查
- **檔案**：`social_demotest/models.py`、`social_demotest/routers/calendar.py`、`social_demotest/frontend.html`
- **變更**：
  - `CalendarActionRequest` 新增 `expected_revision: int | None = None`。
  - `cancel_calendar_event` 個人與共同約會都傳 `expected_revision`。
  - 前端取消按鈕送 `expected_revision`，409 時提示「行程剛剛已變更，請重新整理後再取消」。

### #9 update_form 未確認 event 是否在 pending_reconfirmation
- **檔案**：`social_demotest/services/date_coordination_service.py` `update_form`
- **變更**：檢查 `pending_result.modified_count`；若 event 已不在 `pending_reconfirmation`（例如被撤回改期還原），用 `$unset` 清掉 `rescheduling_event_id`，Python dict 端同步 `pop`，避免後續再誤寫 pending_change。

### #10 「最近的行程」跳過今天
- **檔案**：`social_demotest/services/ayue_agent/tools.py` `_calendar_events`
- **變更**：沒有時間關鍵字時，查詢起點從「現在此刻」改為「今天 00:00 當地時間」，含今天整天（含已結束）的行程。

### #11 網頁編輯日期/時間不同源
- **檔案**：`social_demotest/frontend.html`
- **變更**：編輯按鈕的日期與時間統一用 `Intl.DateTimeFormat` + `timeZone: event.timezone` 解析，不再 `toISOString()`（UTC）混 `toTimeString()`（瀏覽器本地）。

### #12 _event_matches_hint 比對過嚴
- **檔案**：`social_demotest/services/calendar_service.py`、`social_demotest/tests/test_calendar_event_matching.py`（新增）
- **變更**：
  - 新增 `_normalize_hint_text` + `_HINT_NORMALIZE_TABLE`：en/em dash、全形波浪號/空白/逗號/冒號/數字 → 半形。
  - `_event_matches_hint` 改為分段比對（順序不拘），haystack 補上 `HH:MM-HH:MM`，先剔除時間詞與介系詞。
  - 新增 18 條回歸測試。

### #13 「最近的行程」跳過今天（與 #10 同一項，編號重複，以 #10 為準）

---

## 六、驗證結果

### 單元測試（全測試套件 `tests/`）

| 階段 | 失敗 | 通過 |
|------|------|------|
| 修改前（`git stash`） | 46 | 355 |
| 修改後 | 31 | 370 |

- 本次修復讓 **15 個測試從失敗變通過**，**無新增任何回歸**。
- 剩餘 31 個失敗全部為修改前就存在，分布於 `test_profile_skills`、`test_proactive_care`、`test_private_v2`、`test_ayue_agent_stream`、`test_ayue_agent_router`、`test_ayue_agent_trajectories`、`test_ayue_agent_v2_tools` 共 7 個測試檔，與行事曆功能無關。

### 行事曆專屬測試

- `tests/test_calendar_event_matching.py`（本次新增）：**18/18 通過** ✅
- `tests/test_ayue_agent_calendar_actions.py`：通過 ✅
- `tests/test_ayue_agent_v2_policy.py`：通過 ✅
- `tests/test_ayue_agent_registry.py`：通過 ✅

### 編譯

- 全部修改檔案 `py_compile` 通過。

### 服務啟動

- `uvicorn main:app --port 9001` 成務啟動。
- `GET /` 回 **200** ✅
- 測試完成後 server 已關閉。

### Impact 分析

- 修改前對每個目標函式跑 impact 分析：
  - `_event_matches_hint`：LOW
  - `guard_v2_decision`：LOW
  - `_handle_calendar_pending_confirmation`：HIGH
  - `cancel_calendar_event`：LOW
  - `update_form`：LOW
  - `request_reschedule`：HIGH
  - `cancel_coordination_or_event`：HIGH
  - `get_calendar_context`：CRITICAL
  - `serialize_event`：CRITICAL（未修改，只在 `get_calendar_context` 呼叫點剝欄位）
- `detect_changes()` 風險評估：low。
- 實際修改只放寬版本檢查、在單一呼叫點剝欄位、或新增輔助函式，不破壞既有 CAS 語意與共用結構。

### 測試指令

```powershell
$env:PYTHONUTF8=1
cd social_demotest
python -m pytest tests/ -q                                    # 全測試
python -m pytest tests/test_calendar_event_matching.py -v     # 行事曆比對回歸測試
python -c "import py_compile; py_compile.compile('services/calendar_service.py', doraise=True)"  # 編譯檢查
```
