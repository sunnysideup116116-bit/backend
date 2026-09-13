# 公開暱稱共用契約

## 資料來源與邊界

- `public_nickname_service.contact_display_name` 是本人／已授權對象的共用名稱讀取入口：Appwrite `user_profiles.name` 優先，暫時不可用才用 Mongo 的安全公開名稱。Appwrite 明確空白或不安全的名字不復活舊 alias。
- 不同步或回填 Mongo，不改名字、不新增帳號、不改寫舊訊息。只讀公開名稱；取得對象 ID 的權限仍由原有 owner／accepted-relation 驗證負責。
- 聯絡人及名稱解析先批次查名，每次至多 50 個已授權 ID，只選 `$id`／`name`。30 秒／256 筆快取共用於單筆與批次讀取，失敗也負快取，避免 outage 變成逐人重查。
- 保留既有固定 HTTPS 入口及安全的 loopback 正規化；不依任意 Location 轉送 key，不關閉 TLS 驗證。

## 已接入範圍

| 範圍 | 入口 |
| --- | --- |
| 聯絡人／＠選單／新配對聊天室 | `chat_messages.get_contacts` |
| 阿月提及、列出、比較已接受聯絡人 | `public_relationship_projection.display_name` |
| 用名字或語音辨認既有聯絡人 | `resolve_accepted_contact_name`／`accepted_contact_ids_by_display_name` |
| 配對完成狀態、私人媒人提醒與共同開場 | `match_state_service`／`match_action_service` |
| 約會卡、關係遊戲、私人聊天室 | 原有共用 display-name 呼叫路徑 |
| 通知標題與對象名稱 | `notification_service` |
| 本人名稱及 runtime 的公開對象標籤 | Context 的 `_public_label`／Profile 的 `_self_profile` |

匿名提案文字、姓名移除、地名欄位、一般敘述中的「對方」不是人名來源，不做全文替換。Proposal 的 `counterparty_nickname` 仍是 viewer-bound UI 投影；不能因此向未授權的對象公開 ID、私人記憶或行事曆。

## 缺名不是未配對

- `/api/contacts` 的真人列增加 optional `name_available`。名稱無法取得時顯示「暱稱暫無法取得」，不能把「對方」或帳號 ID 當人名。
- 已接受清單工具增加 `names_complete`，名稱未完整取得時不代表某人不存在。
- 模糊／精確名字解析在候選名稱不完整時回 `unavailable`，不能宣稱 `not_found` 或猜同名唯一性；同名仍需消歧，陌生帳號仍不能讀取他人的聯絡關係。
- 前端新 API 名稱優先於舊快取。空白、「對方」、括號佔位及 ID 不作真實暱稱；明確 unavailable 不復活舊 alias。
- ＠選單開啟時刷新，載入結果核對當前 user ID；未知名稱不可選取，不將 `@對方` 當成輸入文字。保留已有名字的離線畫面不等於授權新的發送操作。

## 本次驗證與交付

- 最終完整回歸：Social **1963 passed、4 skipped、140 subtests passed**；Flutter **570 passed**。本次 Dart 檔案靜態分析無診斷，Python compile、diff whitespace 及 `bash -n start_all.sh` 通過。
- 新增後端共用來源／批次／權限／同名／缺名／contacts API 回歸與前端名稱快取／提及文字回歸。
- 真實 Appwrite 唯讀查驗：在不讀 Mongo 名稱的情況下，批次解析截圖中的 `kkk` 成功；沒有修改任何真人資料。
- 本次只處理暱稱；保留組員的 Planner、Google Calendar 及其他未提交修改。初次完整回歸遇到 private-router 對新增 `external_calendar_authorized` 參數的測試期待差異，本次未修改該路徑；最終重跑已不再失敗。
- 前端快取／＠選單修正需要新版 App；後端需以正式 `start_all.sh` 載入。不在本次自動 commit、push、重啟或重包 APK。
