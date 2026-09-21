# 語音阿月：額度、冷卻與傳送結果

此功能需要本次 Server 與 DatingApp 一起更新。Server 以
`operational_status_version=1` 協商；語音主協定維持 v4，能力清單升為 v15。
Template 保留直接操作並新增 `read_app_data(domain=quota|delivery)`；proxy
沿用簽署能力，legacy 使用 `read_agent_quota`／`read_chat_status`。

## 本人狀態查詢

- `GET /api/app-voice/status/quota`：共用剩餘百分比、無限狀態、是否耗盡、下次補額時間。
- `GET /api/app-voice/status/chat?contact_id=...&client_message_id=...`：授權對話的冷卻；傳送識別可省略，只查冷卻。
- 需要 Appwrite JWT，owner 取自 JWT；不接受外部指定 owner。聊天必須是已接受的配對。
- 查詢不建立語音 session、不呼叫模型、不重新傳訊或評分；仍會依既有額度服務結算用量與每日補額。
- 每帳號每分鐘最多 60 次 HTTP 狀態查詢。同時發生的本人額度讀取合併，語音期間每 30 秒更新；啟動、操作完成與錯誤也可更新。

結果沿用 `result_version=1`，在 `data` 下提供可選的 `quota`、`cooldown`、`delivery`。
額度用完與百分比四捨五入為零分開判斷。模型看不到剩餘 token 數、內部風險分數、收件者指令或本機聯絡人識別。

## 額度耗盡與限流

配對、悄悄話、語音共用原有額度；沒有修改扣額或補額規則。低於 10% 且非耗盡／無限時，每帳號、每裝置、每台灣日期最多主動提醒一次。

耗盡或無法確認額度時停止雲端 Live，切換狀態模式，不自動重連。尚未發出的任務停在 waiting_input；已發出的操作保留控制通道接收結果，完成後再關閉。已完成寫入不重做。

依後續使用者要求，狀態模式已移除文字輸入與查詢按鈕，只提供語音詢問，不執行寫入。Android 僅在裝置端辨識可用時使用 `createOnDeviceSpeechRecognizer`；TTS 只選擇不需要網路的 voice。不存在合適離線能力時顯示說明，不退回雲端辨識。供應商限流與個人餘額不足分開回報；說「恢復語音」會先重新查額度，只有確認恢復才建立雲端連線。

## 冷卻與送達

Risk 新增權威 `cooldown` 投影：`state=clear|active|unknown`、`server_time`、`until`、`remaining_seconds`。查詢失敗回 unknown／null，不當成解除。原有分級與豁免演算法維持不變。

訊息送達依 `delivery`，不是風險等級；已送出仍可能有冷卻。Flutter 使用伺服器截止時間與單調計時倒數，到零再次查詢後端。只有前景且語音仍開啟才提醒一次，不自動重送。

語音傳送識別採 `voice-` 前綴，Server 再次驗證 JWT owner、既有配對與封鎖規則、冷卻狀態。冷卻 active 或 unknown 不發出語音訊息。相同識別已存在持久回執時回傳原結果，不重做 Risk 評分或訊息寫入。一般手動聊天的既有 Risk 放行政策不變。

裝置在傳送前保存 owner-scoped 嘗試識別與草稿；HTTP 發出前先標記 unknown。逾時後查回執，無法確認時不另造識別重送。草稿可在重開聊天室後還原；手動重試同一份不確定的語音草稿也先查結果。成功或登出／換帳號清除相應草稿內容。

## 部署與驗證

不新增服務、port、公開網址或資料庫集合。更新完整 Server 只能使用 `Server/start_all.sh`；前端需要重新建置並安裝 Android APK。

測試覆蓋：額度 0／10%／低額度／無限、提醒去重與跨日、JWT 與配對歸屬、查詢限流、Risk 失敗與冷卻邊界、送達但冷卻、豁免、逾時去重、已發出操作的耗盡收尾、草稿保留、鍵盤與窄螢幕，以及 Android 原生通道初始化和取消。

本輪使用隔離測試資料與唯讀 Pixel 9a 模擬器驗證；未連接實體 Android 手機，不將模擬器測試當成真實收音、藍牙或離線語音品質驗證。
