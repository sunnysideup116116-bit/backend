# Voice Assistant 能力與限制

## 2026-09-15：Gemini Live template routing

- App Voice protocol v4 現在可使用 `VOICE_APP_TOOL_ROUTING_MODE=template`。它沿用 Gemini Live 原生雙向 WebSocket、Server VAD、PCM16 與 barge-in，但把模型可見工具收斂為 15 個 domain-level tools，不再要求每個簡單請求先執行 capability search。
- 聊天室、配對、日曆、地點與明確的 App 讀取請求若 Gemini 沒有發出 function call，Server 會以最終使用者逐字稿做確定性 fallback；仍會通過既有 permission、scope、revision、target binding 與 Flutter action result 邊界，不會直接寫入 App。
- 自然月份會由台灣日期展開為包含起訖日的區間；例如「查這個月行事曆」會查本月第一日至最後一日，而不是只查 `upcoming`。
- 長任務與 delegated Ayue 會先回覆「稍等一下」；語音工具結果回傳後才允許模型說完成。模板模式的 synthetic fallback 不會送不存在的 Gemini tool-call ID，避免 Live API protocol error。
- `legacy` 與 `proxy` 仍保留作 fallback；實際部署的 `Server/.env` 已切換至 template，所有已驗證使用者仍使用同一個 Appwrite JWT 與 owner-scoped task service。
- 本次驗證：App Voice Server 188 項通過；template direct dispatcher、deterministic fallback 與 real Gemini Live WSS smoke 覆蓋聊天室、找新配對、整月日曆及高雄地點，均產生正確 action 且沒有 error event。Flutter 語音相關 Dart analyzer 無問題。

## 2026-09-13：短期記憶、Google 即時氣象與任意行事曆區間

- 記憶收尾日誌曾出現 `voice_memory_summary_failed`；現在 DeepSeek 仍優先嘗試兩次，也會接受被 Markdown 包住或帶額外欄位的有效 JSON。兩次都失敗時改用同樣受遮蔽、本人姓名移除與 200 字限制保護的本機滾動摘要，仍遞增 Appwrite revision，不再遺失整段 session。
- 「結束／關閉語音模式」等控制句與阿月的「好，語音模式已關閉／我先休息」會在摘要前移除。只有關閉控制句的 session 不呼叫 DeepSeek、不更新 revision；前面有實質對話時仍保存對話內容，但不保存關閉套話。
- 語音啟動在 capability、同意、暱稱、WebSocket 與麥克風各個非同步邊界都核對同一個 session generation。啟動中再次點吉祥物會立刻收合並取消；晚完成的連線會再次關閉，不會在背景自行啟動麥克風。
- Appwrite 實際文件已確認為 `revision=2`，較早摘要 39 字、近期摘要 67 字；合成 Gemini Live 測試在不呼叫工具的情況下，直接依注入摘要回答「星期日下午四點」，因此 Appwrite→ticket→Live system instruction 路徑有效。
- 「上一段語音／剛才說到哪／上次語音約幾點」會直接使用短期摘要；「阿月記住的事／長期偏好」仍讀 canonical `read_memories`，回答時可補充近期語音脈絡並清楚區分兩種來源，不會因長期清單為空而忽略短期摘要。
- 新增 `read_weather`／`weather.query`，只查目前狀況。使用者說出城市或區域時優先使用該地點；未說地點時，才讀取設定頁「所在地（僅用於附近資訊查詢）」保存的 Agent 專用 `profile_location`。不讀 Appwrite 個人資料的 `region`，Agent 地區不存在時才追問。這個讀取只在缺少氣象地點時執行，不增加其他語音功能的連線或回覆時間。地名先由 Google Places Text Search 解析為座標，API key 全部留在 Server。
- 每個有效氣象查詢都會並行且各呼叫一次 Google Weather `currentConditions:lookup` 與 Air Quality `currentConditions:lookup`，不使用快取替代任何一個來源。回傳目前天氣、溫度、體感、降雨機率、濕度、風速、UV、台灣 AQI、主要污染物與一般族群健康建議；單一來源失敗時會回傳另一來源並明示資料不完整。
- `GOOGLE_WEATHER_API_KEY`、`GOOGLE_AIR_QUALITY_API_KEY` 與地名解析 key 可獨立設定；留空時沿用既有受限的 `GOOGLE_PLACES_SERVER_API_KEY`。目前 Server key 已實測 Weather 與 Air Quality 均回 200。
- `read_calendar`／`calendar.query` 除原本今天、明天、本週等預設值外，新增包含起訖日的 `start_date`、`end_date`。可查任意過去、現在或未來區間，不設固定回溯、展望或跨度上限；回覆仍只口述前五筆，避免超長語音。
- 當時 action catalog 升為 v3；目前已由 protocol v4 catalog 取代，capability 仍回報 `voice_weather=true`、`voice_weather_sources=[google_weather, google_air_quality]` 與 `current_weather_and_air_quality`。
- 本次驗證：App Voice Server 121 項通過；Flutter 五組語音與 action 契約 83 項通過；相關 Dart 靜態分析與 capability 產物同步檢查通過。內網 Appwrite storage smoke 驗證 DeepSeek 強制失敗仍寫入 revision 1、關閉-only session 不增加 revision、結束套話未保存，測試文件隨後以 204 刪除；既有摘要與 DeepSeek 輸出中的舊結束套話也有清理回歸。真實 Gemini 文字備援把「去年三月一日到五月三十一日」解析為 `2025-03-01` 至 `2025-05-31`；Gemini Live 對「今天天氣如何」選出 `read_weather(args={})`，確認未說地點時會交由 Server 套用 Agent 地區。先前含明確地點的 Live smoke 與 Google Weather／Air Quality 雙來源也均成功。
- 官方介面：[Weather current conditions](https://developers.google.com/maps/documentation/weather/current-conditions)、[Air Quality current conditions](https://developers.google.com/maps/documentation/air-quality/current-conditions)。

## 2026-09-12：語音顯示與共同約會確認補強

- Gemini Live 逐段轉錄若在中文字之間插入單一空白，Server 顯示邊界會只移除相鄰漢字／中文標點間的空白；`嗨 Candy`、英文單字與 `下午 4 點` 等有意義的空格保留。
- 全螢幕在尚無對話時顯示的「正在準備語音助理／正在開啟麥克風」等狀態文字，改放在 94% 不透明的 theme surface、圓角與輕微陰影上，半透明背景不再降低可讀性。
- 「確認安排／確認共同約會／確認和某人的安排」明確對應 `date.confirm`。Live 會先 `read_shared_dates` 讀取最新對象與表單，再建立只屬於登入者這一方的口頭確認；`確認安排` 也可核准前一步待確認的共同約會修改，但裸「安排」不會執行寫入。
- `confirmation_phrase` 依 `date.confirm`、`date.update`、接受邀請與拒絕邀請分開，不再共用模糊的「確認共同約會操作」。其他設定、傳訊、發布與配對確認口令未放寬。
- 本次回歸：App Voice Server 104 項通過；Flutter 語音介面、共同約會、畫面 context 與 action executor 五組共 80 項通過；相關 Dart 靜態分析無問題。合成內容的真實 Gemini Live smoke 依序選出 `read_shared_dates(contact_name=小安)` 與 `confirm_shared_date(contact_name=小安)`，沒有執行 App 寫入。

## 2026-09-12：兩層短期對話記憶

- 每次建立語音 session 時，Flutter 會取得短效 Appwrite JWT；Server 以內網 `/account` 驗證 JWT 內的 `$id` 必須等於 Client 宣告的 `userId`，再以該 ID 讀取個人資料。Client 傳入的姓名不作為可信來源。
- 每個帳號在獨立 Appwrite database `voice_memory`、collection `voice_session_memories` 只有一份文件；文件 ID 與 `user_id` 都使用已驗證的 Appwrite userId。`username` 若與同一 userId 的 profile `name` 不同，建立下一次 session 時會同步更新，但只作身分同步與本人姓名移除依據，不作為摘要內容。
- Server 只透過 `APPWRITE_INTERNAL_ENDPOINT` 連線 Appwrite。設定必須是 loopback、私有 IP 或單段內網服務名稱且以 `/v1` 結尾；公開網域與 HTTP redirect 都會被拒絕。目前 schema 已透過 `https://127.0.0.1/v1` 建立及驗證。
- 對話結束、Client 正常關閉或 WebSocket 中斷後，Server 在背景把上一份摘要與本次文字轉錄交給 Ollama DeepSeek，更新成 `older_summary` 最多 70 字及 `recent_summary` 最多 120 字，兩者合計最多 200 字。問候或沒有實質內容的 session 不呼叫模型。
- Gemini Live 的 `input_transcription.finished` 不保證成為 `True`；Server 會在 finished、model turn complete 與 WebSocket 關閉三個邊界保存最後使用者逐字稿並去重，避免畫面看得到對話但收尾誤判成空 session。日誌只記錄 `saved/skipped`、revision 與回合數，不包含姓名或逐字稿。
- 下一次語音會把這份摘要加入 Gemini Live 與文字備援的輸入背景。摘要明確標為可能過期且不可信，不能授權操作，也不能取代行事曆、配對、權限或其他工具與 API 的即時結果。
- 摘要中的登入者姓名會在讀取舊摘要、username 改名同步、送入 DeepSeek 前及模型輸出後四個邊界改成「使用者」；其他對話人物姓名仍可保留，因此改名不會留下兩個像是不同人物的本人名稱。
- 原始錄音與完整逐字稿不寫入 Appwrite；Email、台灣手機、口述密碼／驗證碼及憑證形式字串會先遮蔽。Ollama 或 Appwrite 寫回失敗時保留上一版記憶，不影響已完成的語音回覆。
- 本次驗證已併入上方 104 項 App Voice Server 回歸；真實 DeepSeek 合成 smoke 會把 `Candy` 改成「使用者」並保留另一人物「小安」。完整 storage smoke 已把隨機測試文件寫成 revision 1、驗證摘要與 session ID 後立即刪除。Appwrite 的獨立 database、collection、六個欄位與唯一索引已由內網遷移程式實際建立並驗證。

## 2026-09-12：畫面上下文與結構化動作結果

第二批基礎版新增真人聊天室、行事曆、牽線頁的項目參照與選取狀態，支援「他／這個／第二個」接到本頁實際資料；新增相容的結構化結果與共用能力清單。原本的確認和業務 API 流程保留。參照範圍、權限、同步產生指令與驗證方式見 [CONTEXT_CONTRACT.md](CONTEXT_CONTRACT.md)。

這份文件記錄 Folks App 全域吉祥物語音助理目前已完成的能力、操作安全規則、啟用條件，以及目前尚未支援的範圍。

## 2026-09-12：暱稱與中英辨識偏好

- 開場姓名改讀 Appwrite 個人資料文件的 `name`（沿用 profile cache），不再拿登入帳號的 `AppSession.user.name` 當暱稱。例如登入名稱 `a`、個資暱稱 `candy`，語音 session 會帶入 `candy`。最多等待兩秒；讀不到就用不帶姓名的招呼，不猜測、不退回錯誤帳號名稱。帳號變更、停止或重開 session 會撤銷舊姓名讀取。
- 設定「辨識語言偏好」提供 `中文＋English`（預設）、`中文優先（繁體顯示）`、`English 優先`，與「回覆語言」分開保存。更改後重新開啟語音套用。
- 目前 SDK 使用 `AudioTranscriptionConfig.language_hints.language_codes` 傳送 `zh-TW`／`en-US`。在本專案使用的 Gemini Live 模型上，不傳音訊的真實握手已成功接受中英雙語提示；這證明設定可被接受，不等於已驗證實際辨識準確率。
- Google 把語言碼定義為辨識提示，並非嚴格的語言封鎖。不能承諾完全不辨識其他語言；不清楚時要求重說。參考：[AudioTranscriptionConfig](https://ai.google.dev/api/live#AudioTranscriptionConfig)、[Live 語言說明](https://ai.google.dev/gemini-api/docs/live-api/capabilities#change-voice-and-language)。
- 輸入的中文逐字稿統一使用既有繁體轉換服務顯示；英文保留原文與單字空白。繁體轉換只處理完整累積的顯示文字，不修改原始音訊、工具 ID 或逐段原文串接。Android 裝置端 STT 備援在英文模式使用 `en-US`，其他模式使用 `zh-TW`；雙語自動提示屬 Gemini Live 功能。
- 本次驗證：44 項 Flutter 語音測試、64 項 App Voice Server 測試通過；包含 `a`／`candy` 不一致、姓名讀取失敗、停止後遲到結果、設定保存與繁體／英文空白。

## 2026-09-12：共同約會、邀請分類與雙模式介面

最新決定：全域語音阿月只限原生 Android App。Windows、Linux、Web（包含 Android 瀏覽器）均隱藏水獺與設定入口，coordinator 也阻止啟動、重連與喊醒流程；直接進入語音設定頁只顯示 Android 限定提示。註冊頁的獨立語音功能不在本次修改範圍。

全螢幕只保留聊天室標題列的小頭像，隱藏原本可拖曳的浮動水獺；使用縮小按鈕返回簡易模式。訊息泡泡使用完全不透明底色，文字明確使用主題前景色與中等字重。準備中／連線中等中央提示為 13sp 中性色小字，並有自己的圓角 surface 背景與輕微陰影。全域浮層有自己的 Overlay 與 Material，確保位於 Navigator 上方時文字與按鈕提示正常。

本次平台與全螢幕修正：41 項語音測試通過（包含瀏覽器／桌面停用、實際 MaterialApp.builder 結構下的中性色小字及模式切換）；相關 Dart 靜態分析無問題。

歷史瀏覽器驗證（最新 Android 限定決定前）：IAB 曾驗證右上角水獺正常呈現；這不代表目前 Web 仍可使用語音。最新 Web 建置應隱藏語音阿月。

- 說「有沒有約會邀請」會直接讀 `/api/relationship/date/pending`，包含等待本人接受／拒絕的邀請，以及尚待本人確認的共同安排。它與阿月牽線的配對邀請不同，不再混用。此清單不包含已成立或等待對方回覆的約會；指定對象可讀 `/api/relationship/date/state` 查看完整最新狀態。
- 支援 `read_shared_dates`、`respond_date_invitation`、`update_shared_date`、`confirm_shared_date`。可以說「查看和小安的共同約會」「接受小安的約會邀請」「改成週日晚上七點到九點看電影」「確認和小安的安排」，由語音直接操作既有共享表單與 API。
- 修改只覆蓋指定的日期、起訖時間、活動、地點、備註或預算，保留其他欄位。時間未填齊時會要求補充；已成立的共同約會改走原本改期流程，原行程保留到雙方重新確認。
- 寫入前要先讀取對象的安排，Client 保存本人／對象、coordination ID、revision、狀態與表單快照；口頭確認後重新讀取比對，過期、換帳號、版本變更時不執行。模型只提供自然稱呼與允許欄位，不提供 user ID 或 coordination ID。成功後開啟原本雙人聊天室呈現共同卡片，並更新行事曆 revision。
- 接受邀請只表示願意開始協調；確認安排只代表登入使用者這一方。不能代表對方接受、確認，也不會將「等待對方」朗讀成約會已成立。
- 使用既有 `calendar_read`／`calendar_write` 權限，設定中改標示「行事曆與約會邀請」與「修改行程與共同約會」。提出雙方空檔建議仍依既有私人阿月授權與 busy/free 工具；目前沒有背景全自動排程，也不會在缺乏資料時猜測對方空檔。
- 阿月牽線按 canonical status 分組朗讀：目前待你回覆、目前等待對方、歷史已接受、歷史已拒絕、歷史撤回／過期／失效，以及狀態不明。歷史不計為新邀請。
- 記憶泛問一律讀最新記憶清單；不再拿整句「你記得我什麼」做字面過濾。有記憶但主題不吻合時會說明並提供現有主題，不回報成完全沒有記憶。Live 誤選公開阿月工具時，明確的記憶問題會改回 direct memory query。
- Live 工具回覆原本只保留 200 字，現調整為最多 12,000 字，保留邀請分類與記憶資料供模型回答。逐段 transcription 保留英文單字間空白；完整回覆顯示上限為 2,000 字。
- **簡易模式**：可拖曳水獺與回覆泡泡，僅顯示阿月回覆，講完後保留內容，可捲動閱讀、展開或關閉。
- **全螢幕模式**：半透明漸層覆蓋原 App，顯示使用者與阿月的本次對話，自動跟隨最新內容（手動往上閱讀時暫停跟隨），可縮回簡易模式。原 Navigator 保持掛載，語音導航與資料更新仍正常執行。
- 對話最多保留本次 100 則於記憶體；結束語音／登出後完整逐字內容會清除，只留下上述最多 200 字的跨 session 短期摘要。長期偏好仍使用「阿月記住的事」。

本次驗證：Flutter 全套 602 項通過；最後的共同約會／API 追加回歸 27 項通過。App Voice Server 62 項通過，Social 約會領域與改期 HTTP 14 項通過。`dart analyze lib test` 無錯誤，僅兩個既有 style info。Android debug APK 建置成功；正式 capability 會回報 `direct_shared_dates`、`date_invitation_response`、`shared_date_form_update` 與 `shared_date_form_confirm`。本次沒有連接 Android 裝置，未宣稱實際麥克風或雙真人帳號端到端驗證。

GitNexus 檢查目前整個未提交工作區（含本次之前已有的聊天、配對、個資同步修改）：DatingApp 49 個 symbols／16 條流程、Server 67 個 symbols／17 條流程，均為 CRITICAL。這是累積跨頁／契約影響，不代表每項修改皆為 CRITICAL；新建的約會 controller 與測試檔另由上述回歸覆蓋。

## 產品定位

目前版本是原生 Android 專用、畢業專題展示用途的全域語音助理。Android 使用者完成登入與個人資料後，畫面會顯示 `NavBar 愛心.webp` 水獺吉祥物；點擊吉祥物後開啟小型浮動氣泡並進入前景對話模式。目前工作區暫時關閉測試帳號 allowlist，但仍只應使用專題測試或非敏感資料。

語音助理只會提出白名單內的操作。Server 不會直接寫入個人資料、設定或貼文，真正的操作仍由 Flutter 使用目前登入中的 `AppSession` 與既有 service 執行。

## 目前可以做什麼

### 全域吉祥物與語音介面

- 僅原生 Android 在登入且 `profileReady` 後顯示水獺；全螢幕時隱藏浮動水獺。
- 吉祥物可自由拖曳到 SafeArea 內的位置；位置會以比例保存，旋轉或改變視窗尺寸後仍會留在畫面內。
- 設定頁提供「阿月語音助理」入口；在專屬頁關閉整體功能後，會立即停止語音工作階段並隱藏吉祥物，仍可從設定頁重新開啟。
- 點擊吉祥物一次即可展開小型浮動氣泡並開始前景對話，不必每一輪都重新按開始。
- 語音 session 就緒後阿月會主動用一個很短的句子打招呼；使用者已開口時可直接打斷招呼。
- 設定可開啟「嘿！阿月！」前景喊醒；它改用獨立 Android 原生 MethodChannel 與系統 `SpeechRecognizer`，Android 12 以上優先 `createOnDeviceSpeechRecognizer`，其他裝置要求 `EXTRA_PREFER_OFFLINE`。待命時不會建立 Gemini session 或把環境音送到 App Voice API。它只在 App 前景與已完成個資時循環監聽，不是背景 wake word service。
- 喊醒使用標準 `zh-TW` 語系，並接受語音引擎常見的「阿岳／阿悅／阿玥」同音轉寫；單獨在一般句子提到「阿月」不會觸發。
- Android on-device 引擎存在但缺少 `zh-TW` 模型時，下一輪會改用系統 recognizer；設定頁會顯示 starting、listening、permission required、recognizer unavailable、mic busy 或 recovering 真實狀態，不再只有開關假狀態。
- 設定頁提供「喊醒測試」與「重新啟動」，並在記憶體中顯示最近一次手機辨識候選；結果不持久化、不送到 App Voice Server。
- Android 全雙工模式在氣泡開啟期間會持續收音，不用每一輪按開始或停止；Gemini Server VAD 判斷說話開始與結束。
- 說「關閉語音模式」之外，「你休息一下／休息吧／先不要聽／不要聽了／停止聆聽／安靜一下」等明確表示不再聆聽的語句也會結束模式；裝置端文字模式立即關閉，Gemini 全雙工模式由 `close_voice_mode` 收尾。
- 簡易氣泡保留展開、關閉與麥克風中斷後重新啟動按鈕。
- 顯示待命、連線、聆聽、處理、朗讀與失敗狀態。
- 全螢幕模式顯示即時輸入／輸出轉錄，兩種模式皆提供麥克風狀態；簡易泡泡不顯示使用者逐字稿。
- 浮動氣泡不再顯示「Gemini 自然聲音／本機聲音」標籤；聲音選擇集中在專屬設定頁。
- App 切到背景、登出、帳號 session 改變或關閉助理時，會停止 STT、TTS 與 WebSocket。
- Android 在 AI 朗讀期間仍持續將麥克風 PCM 送入同一個 Gemini Live session；Server 收到 `interrupted` 後立即要求 Client pause、flush 舊 AudioTrack 緩衝。
- 麥克風使用 `VOICE_COMMUNICATION`、`AcousticEchoCanceler`、noise suppressor 與 auto gain；PCM16 以 16 kHz、40 ms 區塊持續傳送。
- Android 播放與錄音統一使用通話路徑：`MODE_IN_COMMUNICATION` + `USAGE_VOICE_COMMUNICATION`，並以 `volumeControlStream = STREAM_VOICE_CALL` 讓實體音量鍵調整通話音量。擴音開啟時，Android 12+ 以 `setCommunicationDevice(TYPE_BUILTIN_SPEAKER)` 選取手機擴音喇叭，舊版使用 `isSpeakerphoneOn` 相容；回覆結束後恢復原路由。Client 還會把實際 Gemini PCM 降採樣成 far-end reference，對麥克風 frame 做延遲視窗相關性比對；吻合的自身播放改送等長靜音，持續的 near-end 人聲仍可打斷。
- 為減少沒有說話卻在句中被中斷，本機插話判定改為連續 6 個 40 ms 高於回音基線 2.2 倍的 frame，Gemini Server VAD 的 start sensitivity 同時改為 LOW；仍可插話，但不會因短噪音或漏音立即停播。
- 每兩秒檢查是否真的收到麥克風 frame；連續四秒沒有音訊就顯示明確中斷狀態並重連，不再只依賴 UI phase 假裝正在聽。

### 語音輸入

- Android protocol v3 預設使用持續 PCM 輸入與 Gemini Live input transcription；裝置端 `speech_to_text` 只作為舊 Server／非全雙工備援。
- Web 的歷史 SpeechRecognition adapter 保留在程式中，但全域語音平台閘道已阻止使用。
- Gemini Live 提供 interim/final input transcription，Server VAD 靜音門檻為 450 ms，比原本裝置 STT 的兩秒 pause 更快結束回合。
- 裝置端的 `no_match`、語音逾時與暫時忙碌只會重新聆聽，不會再被誤判成缺少離線模型。
- 裝置沒有可用的離線中文辨識時，可改用 Gemini PCM16 音訊回退。
- Gemini 回退使用本機 PCM16 語音活動偵測；偵測到說話後約 0.9 秒持續靜音即自動送出，最長仍有 15 秒保護期限。
- Gemini 回退每次最多收取 30 秒 PCM16、16 kHz、單聲道音訊。
- 加入 Ollama 與 Appwrite 短期摘要後，同意版本升為 `demo-free-gemini-live-ollama-memory-v1`，舊同意不會被沿用。

### AI 語音輸出

- Android 預設使用單一持久 `gemini-3.1-flash-live-preview` audio-to-audio session，同時承載輸入音訊、VAD、轉錄、function call 與 24 kHz PCM16 回覆。
- Android 原生層使用 `AudioTrack.MODE_STREAM` 即時排入 PCM；「打斷」會 pause、flush 並丟棄舊 response ID 音訊。
- AudioTrack 輸出使用 `USAGE_VOICE_COMMUNICATION`，手機音量鍵調整「通話音量」；「使用手機擴音」只改變通訊輸出裝置，不改回媒體模式。錄音端保留 `VOICE_COMMUNICATION` 與 AEC。
- Android 的非 PCM 備援使用完整 Gemini TTS WAV；不會回退本機聲音。Web／桌面不開放語音工作階段。
- 預設聲音為 Gemini `Achird`，提示要求自然、溫暖的台灣華語、正常稍快對話速度、短停頓且避免播報腔。
- 聲線選單已對齊 Gemini Live 官方 30 種 prebuilt voices，包含 `Zephyr`、`Puck`、`Charon`、`Kore`、`Aoede`、`Achird`、`Sulafat` 等；語速可選較慢／正常／稍快，會以 Live system instruction 控制自然 pace，不是後製固定倍速。
- 回覆語言可設定台灣繁體中文、簡體中文或英文；輸入語言另有中英混合／中文優先／英文優先的辨識提示。
- Server 要求 AI 以一至兩個自然短句回答；非確認型 action 只朗讀執行後的最終結果，不再先念提案、完成後又念一次。
- Gemini Live session 會保留 resumption handle 並處理 GoAway；重連後會以上一輪第一個 PCM chunk 指紋丟棄 Gemini 重播的舊回合。
- 每次回覆與音訊都綁定唯一 `response_id`；Client 只播放目前回覆一次，會忽略重複及過期音訊。
- Client 會解析 Gemini WAV header 的實際播放長度，即使平台過早回報 `onPlayerComplete`，也不會在音訊只播第一個字時切回聆聽。
- Live PCM 的失敗 watchdog 改為 20 秒「無新音訊」才觸發，每個 `audio_chunk` 都會續期；長句或網路短暫抖動不再從文字出現時固定計時 10 秒後切斷。
- Android 完整 WAV 備援使用 `audioplayers`、Live PCM 使用原生 `AudioTrack`；Web 使用瀏覽器原生 `HTMLAudioElement`，避免 Flutter MethodChannel plugin 註冊時序造成 `audioplayers.global/events` MissingPlugin。
- Gemini TTS 無法產生音訊時只顯示文字並恢復聆聽，不會改用本機聲音。
- AI 回覆中的常見 Email、台灣手機號碼與口述密碼會先遮蔽。

### 阿月語音助理設定

- 原本「顯示語音助理」Switch 改為「阿月語音助理」入口，副標是「喊醒詞、功能權限、聲音與語言」。
- 專屬頁可開關整體語音功能與「嘿！阿月！」前景喊醒。
- 專屬頁的標題、說明、狀態與選項字級跟隨既有設定頁的 15／12 sp 規格；聲音、語速及語言改用全 App 共用的 `OtterDropdownField` 下拉樣式。
- 功能權限分為：動畫導航、畫面狀態、個資、設定、貼文／相簿／發布、配對讀取／操作、公開阿月、阿月悄悄話、聊天室清單／內容／傳送、行事曆讀寫、Web、地點、精確位置、記憶讀取與功能狀態。
- 設定頁不刪除任何權限，改以「基本操作」、「貼文與相簿」、「配對與阿月」、「聊天與傳送」、「行事曆、搜尋與位置」、「記憶與功能狀態」六個可展開分類呈現，並顯示每類已開啟數量。
- 新增的私人悄悄話、聊天內容、傳送訊息、配對操作、行事曆寫入與精確位置預設關閉；既有低風險能力保留原設定。
- 權限同時由 Flutter executor 與 Server action gateway 驗證；關閉後模型不能只靠 function call 繞過。
- 狀態頁顯示定位、訊息通知、AI 主動關心、流體玻璃與深色模式。使用者問「你有什麼權限」時，Gemini 會呼叫 `get_voice_capabilities`，只取得 allowlist 內的布林狀態。
- 聲音、語速、回覆語言與辨識語言偏好在下一個語音 session 建立時套用。
- 「使用手機擴音」預設開啟並儲存於本機；切換後從下一次 Gemini Live 回覆套用，關閉時仍是通話模式，但交由 Android 系統選擇聽筒或外接路由。
- 聲音與語言下拉選單擴寬向左取得空間，當前選項與選單項目均固定單行，較長名稱不會再換行擠高設定列。

### 主分頁效能與快取

- 主分頁使用 `AutomaticKeepAliveClientMixin` 保留已造訪頁面的 State 與滾動位置，切換「聊天／配對／個人／設定」時不重建頁面。
- 個人與設定共用帳號 + session epoch 分區的成功畫面快照；頁面重建時先顯示快照，個資、貼文、配對分析、推播與定位再於背景更新。登出後 epoch 立即失效，從設定頁登出也會明確清除該帳號的記憶體快照。
- 配對頁保留本次登入 session 的分析、邀請、未讀與待確認約會快照；快照只用來先顯示，背景 API 仍是最終權威狀態。
- 聊天清單沿用既有帳號分區磁碟快取與 stale-while-revalidate，並將底部分頁切回的自動網路更新限制為 15 秒內不重複請求。
- 快取命中時不用全頁 Loading 蓋住舊內容；手動重試、動作提交與 Server 回應仍保留原本的進度與錯誤狀態。

### 動畫導航與畫面理解

- 支援聊天、配對、個人、設定四個主分頁，以及編輯個資、語音設定、行事曆、配對阿月、阿月牽線、阿月記憶與發文頁。
- 子頁導航優先使用已驗證的 `AppSession.userId`，不再每次依賴可能短暫失敗的 AuthService session read；root navigator 最多等待 480 ms，主分頁 controller 也必須已掛載才執行，避免動畫／重建期間誤報無法進入。
- 每個子頁 push 前都先檢查主分頁切換結果；聊天分頁即使已經是目前分頁，也會要求聊天清單立即 refresh。
- 主分頁由 `jumpToPage` 改為 280 ms `easeOutCubic` 動畫；子頁沿用 220 ms `AppPageRoute`，語音結果會等動畫完成後回報。
- 切換主分頁前會逐層以原生 reverse transition 離開既有子頁，不直接 `popUntil` 閃跳。
- Root Navigator 會追蹤實際 route，返回主頁時等待 `TransitionRoute.completed` 與畫面 frame 真正結束後才開新頁；不再以固定 220 ms 猜測舊頁已關閉。
- 如果公開阿月、私人阿月或其他語音 page scope 尚有網路 action 進行中，使用者離開該頁時會立即解除對 Gemini function call 的等待並回報畫面已關閉，避免一次未完成的配對回覆將後續導航與其他功能一起卡住。
- Gemini 只能提交固定 destination key，不能提供任意 route 或 Widget 名稱。
- `describe_current_screen` 只讀取 allowlisted scope、圖片數與能否發布等安全狀態，不擷取任意畫面文字。

### 編輯個人資料

語音可以開啟個人資料編輯頁並填入下列草稿欄位：

- 暱稱 `name`
- 電話 `phone`
- 年齡 `age`
- 縣市 `region`
- 手動城市 `city`
- 手動行政區 `district`
- 自介 `userinfo`

安全行為：

- 電話必須符合台灣手機格式 `09` 加 8 位數字。
- 年齡必須介於 18–120 歲。
- 縣市必須是系統支援的台灣縣市。
- AI 填入的欄位會以品牌色標示。
- 語音 patch 帶有 draft revision；使用者手動修改後，舊 patch 與舊確認會失效。
- 使用者說「幫我儲存」時只會建立確認，不會直接儲存。
- 必須在 30 秒內口頭說「確認」、「確定」或 `confirm`，且 Server 仍持有同一個 pending ID、頁面與 revision 均未改變，才會呼叫原本的儲存流程。

### 調整設定

目前可以語音調整五項真正有作用的設定：

- 全域訊息通知 `notifications.global`
- 定位 `location.enabled`
- AI 主動關心 `ai.proactive_care`
- 流體玻璃導覽列 `ui.liquid_glass`
- 深色模式 `ui.dark_mode`

安全行為：

- 通知、定位、AI 主動關心及其他寫入動作仍需要二次確認。
- 當且僅當 Server 有未過期、scope/revision 相符的 pending 動作時，可口頭說「確認／確定／同意／好／好的」、`confirm` 或原完整口令；沒有 pending 時，這些短詞不會自行觸發寫入。
- protocol v3 的 pending confirmation 只存在 Server；Gemini 只傳回剛聽到的口令，Server 比對 scope、revision、30 秒期限與允許變體後才執行，同一 action ID 只能成功一次。
- protocol v2 與舊 Server 備援仍由 Client 保留 confirmation ID，並以 `confirmation_response` 或正規化完整口令傳送。
- 舊 Server 仍會收到正規化的完整口令作為相容備援；不含確認前綴的裸指令不會被補字放行。
- 聊天泡泡的 pending 選項改由 `AppVoiceVisibleChoiceRegistry` 註冊目前 route 上唯一、未過期且實際可操作的按鈕。語音確認直接呼叫該按鈕原本的 `onSelected(choiceId, action)` callback，不掃描／送出聊天文字，也不模擬螢幕座標；多個按鈕時必須先指定，成功觸發後立即移除以避免重複。
- 開啟定位仍必須通過 Android 系統權限流程；權限被拒絕時不會回報成功。
- 開啟通知仍必須通過通知權限與裝置註冊；失敗時不會假裝完成。
- 深色模式與流體玻璃是可逆設定，可立即套用，並顯示「復原」操作。
- 設定完成後會切回設定分頁，並通知現有設定頁重新讀取最新狀態。

### 撰寫貼文草稿

支援下列語句類型：

- 「幫我 po 一個關於我去海邊的故事」
- 「幫我寫一篇關於今天打籃球的貼文」
- 「幫我撰寫貼文」

目前行為：

1. AI 依使用者明確說出的資訊產生最多 2,000 字貼文文字。
2. 自動開啟既有發文頁並填入 caption。
3. 使用者可以繼續手動修改文字。
4. 使用者必須自行選擇 1–5 張圖片。
5. caption 與已選圖片會保存到帳號分區的 App 私有本機草稿。
6. 離開發文頁或 App 被關閉後，可以恢復尚未發布的本機草稿。

AI 指示不得自行虛構同行者、精確地點或沒有被使用者說出的事件；資訊不足時應先要求補充。

### 選取相簿最近照片

- 支援「幫我選相簿最近三張照片」、「選最新兩張」等明確的時間順序與張數指令，限制 1–5 張。
- 若目前不在發文頁，會先開啟既有發文草稿頁、恢復帳號本機草稿，再選取照片。
- Android 直接透過 `photo_manager` 讀取使用者已授權範圍內的「所有照片」最近項目；第一次仍須由使用者處理系統相簿權限畫面。
- 圖片路徑、縮圖與內容不會傳到語音 Server 或 Gemini；Server 只知道使用者要求的張數及選取成功與否。
- 相簿權限是獨立 `gallery` 功能權限，可在「阿月語音助理」設定關閉。
- 不支援「選夕陽照片」、「選海邊照片」或「選有某個人的照片」等視覺／語意搜尋；阿月必須明確說目前沒有視覺能力。

### 發布貼文

- 沒有圖片時，語音助理會要求使用者先選擇圖片。
- 說「圖片選好了，幫我發布」只會建立發布確認。
- AI 會說明目前圖片數量；待確認存在時可口頭說「確認」，Client 會以原 pending ID 送回完整正規化口令。
- 確認綁定目前發文頁、單次確認 ID、草稿 revision 與 30 秒期限。
- 在確認後修改文字、增減／排序圖片、離開頁面或超過 30 秒，都會使舊確認失效。
- 發布只會呼叫原本的 `_publish`／`PostService` 流程，保留既有圖片壓縮、上傳進度、失敗復原與孤兒檔案清理。
- 成功後清除本機草稿並朗讀「發布完成了」。
- 失敗時保留草稿，且不會回報發布成功。

### 配對狀態與配對阿月

- Gemini Live 提供獨立 `read_match_status` 與 `read_match_hub` function，分別轉成 `match.query(view=status|hub)`；這兩種唯讀操作直接等待 Flutter 回傳 canonical 結果，不建立背景阿月委派，也不先播放「我找一下」。只有配對建議、開始／取消搜尋等需要對話推理的需求才進入公開阿月聊天室。
- 語音阿月先說「我找一下，稍等一下」，Flutter 會以既有動畫切到配對分頁，再開啟固定的 `MatchChatPage`；使用者可以在同一個聊天室看到自己的問題、處理狀態與逐段串流回覆。
- 中間提示不再說「配對阿月／阿月悄悄話」，只說「我找一下，稍等一下」；簡體與英文模式分別使用對應語言，不會在 English 模式突然播中文提示。
- 語音委派不傳 `ai_room_id`、不呼叫建立聊天室，固定沿用原本的永久公開阿月對話；連續追問不會每次新增聊天室。
- 固定聊天室開啟後會註冊語音 page scope；後續問題與口頭確認直接交給畫面上的同一個串流 controller，不會退回首頁、另開聊天室或等輪詢後才顯示。帳號分區 refresh signal 僅保留給背景／相容流程補同步。
- 串流完成後才將 canonical reply 回傳給語音 session 朗讀；逾時、離線、未登入或權限關閉都不會假裝取得結果。
- Live 語音阿月會先理解 canonical reply，再用第一人稱與自然對話方式回答；不得逐字照念、提到轉送流程，或加入配對阿月原答案沒有的事實與承諾。

### 公開阿月、行事曆、Web 與地點

- `calendar.query` 由 App Voice 直接讀取本人行事曆，支援今天、明天、未來七天、週末、下週與未來一個月等預設值，也支援包含起訖日的任意過去、現在或未來日期區間；只需 `calendar_read`，不需 `public_ayue`、`match_ayue` 或切換到配對頁。
- 行事曆寫入改為 `calendar.create`、`calendar.update`、`calendar.cancel` 三個 App Voice 直接 function；`ask_public_ayue` schema 已移除 calendar domain，Server 會拒絕舊的公開阿月行事曆委派。
- 新增行程只接受 title、YYYY-MM-DD、HH:mm 與可選地點／備註；未說結束時間時預設一小時。修改／取消只讓模型提供自然名稱 `target`，Flutter 讀取本人行事曆後解析唯一事件，模型不能提供 event ID。
- 三種寫入都需要 `calendar_write` 與 30 秒 Server confirmation；修改／取消因需要先解析事件，另需 `calendar_read`。個人行程使用 canonical PATCH／cancel API；雙人約會改期沿用既有 reschedule API，不繞過對方確認狀態。
- 「配對進度／狀態／結果」使用 `read_match_status` 直接呼叫 `/api/match/status`，朗讀搜尋狀態、百分比、目前處理階段、待本人回覆數與等待對方數；需要 `match_read`，不需要先問公開阿月，也不依賴 `public_ayue`／`match_ayue` 權限。唯讀狀態不強迫切頁，需要看邀請時可接著說「打開阿月牽線」。
- 「我配對到誰、是否有要確認」同一個 direct read 會讀 `/api/match/status` 與 `/api/contacts`：朗讀已接受且可聊天的對象名稱、待本人確認與等待對方的數量。
- `ask_public_ayue` 僅用於 matching、web、places、memory 或 profile；會切到永久公開阿月聊天室並使用畫面本身的 `streamPublicMessage`，讓問題與回覆即時可見，但不會為每次查詢新增聊天室。
- 可查看配對進度、牽線卡與已接受對象；可使用公開 Web 搜尋、頁面摘要、附近餐廳／咖啡廳／景點、營業資訊、評分、價位與距離；可讀取本人阿月記憶及自我摘要。
- 配對對話內的確認仍由既有 choice 協定負責；語音以目前可見按鈕的原 `choice_id` 執行 callback，避免把「確認」當成新問題而重複產生確認卡。
- 行事曆新增、修改或取消只有 direct API 回傳成功事件後才算完成；失敗不會照念模型的成功句。
- 行事曆真的變更後會遞增 `AppSession.calendarRevision`，並自動以動畫開啟 `AyueCalendarPage`；頁面註冊獨立 calendar scope，進入後立即從 Server 重載，避免仍把操作送給被蓋住的聊天室。
- 精確裝置位置只有 `location_precise` 權限開啟時才會隨語音發起的公開阿月查詢傳送；否則公開阿月只能使用已儲存地區或明確地名。
- 查詢 places 時若定位未開啟且個人資料沒有手動所在地，語音阿月會要求城市／縣市與行政區；取得後以 `profile.patch` 開啟編輯個人資料並填入 `city`/`district`/`region`，經「確認儲存個人資料」成功後，在同一語音 session 重試原地點問題。

### 阿月悄悄話與聊天

- `list_contacts`／`contacts.query` 會直接讀取本人 `/api/contacts`，只列出已接受、未封鎖且目前可聊天的配對對象；再沿用聊天頁的 Appwrite profile 快取補全真實顯示名稱（例如 API fallback「對方」可補成畫面上的 `Sideup Sunny`）。最多朗讀八個顯示名稱及三位未讀數，不把最新訊息內容送進一般名單回覆。
- 可直接問「我的好友有誰／聊天裡有誰／聯絡人名單／我可以傳訊息給誰」；阿月必須先呼叫真實名單工具，不能再回答「我看不到聊天裡有誰」或猜測名字。此功能只需 `chat_list` 權限。
- `ask_private_ayue` 先以目前登入者的已接受聯絡人清單解析自然名稱；唯一符合時以動畫切到聊天分頁並開啟該對象既有的 `MediatorPrivateChatPage`，多位或找不到時要求補充，不接受模型提供 user ID。
- 「我想和／跟 XXX 安排約會或見面」固定解析為 `ayue.private_query(contact_name=XXX)`，保留完整原句，自動開啟 XXX 既有的阿月悄悄話並在同一頁進入安排流程；不會誤送公開阿月或一般配對問答。首次讀取私人聊天仍受原本一次性隱私確認保護。
- 私人頁面註冊對象綁定的語音 scope；問題、處理提示、token 與最終訊息直接顯示在目前聊天室。後續詢問沿用同一頁與同一私人房間，原有 3 秒 polling 和 refresh signal 只作為斷線／背景同步備援。
- 私人確認同樣呼叫目前畫面按鈕的原 callback；若沒有唯一可操作按鈕，單說「確認」會明確失敗，不會以普通文字送出並進入重複確認。
- 公開阿月／阿月悄悄話只要有唯一可操作卡片，Client 會把 `visible_choice_pending=true` 即時送入 Live context。使用者說「確認／確定／同意／好」會呼叫 `activate_visible_choice(confirm)`，說「取消／不要／不同意」會呼叫 `activate_visible_choice(cancel)`，再以卡片原本的 `choice_id` 執行同一個 Flutter 按鈕 callback。
- 即使 Gemini 誤把確認詞分類成 `send_chat_message`、`ask_public_ayue` 或 `ask_private_ayue`，Server gateway 也會在有 visible choice 且沒有另一個 Server confirmation 時，確定性改寫為 `ui.choice.activate`；確認詞不再進入輸入框，也不會觸發「新訊息使舊選擇自動取消」。
- Client 另在 final transcript 做最前置攔截：只要目前沒有另一個 Server confirmation 且畫面有唯一可操作卡片，就在後續 Gemini tool call 抵達前先執行原按鈕。接下來三秒內若仍收到把同一句誤分成新聊天的 action，Client 只回報「按鈕已處理」，不再執行第二次。
- 可分析一位對象的共同脈絡、訊息含義、回覆建議及雙方 busy/free；不能讀取對方私下對阿月說的內容，也不能揭露對方行程名稱。
- 新增 `private.relationship.search_shared_history`，只在該對共同真人聊天室內，以 escaped keyword 搜尋完整歷史，最多二十則；原文只進入既有 Private Ayue 安全 context。
- 每個 App Voice session 第一次使用私人聊天內容都會要求確認；待確認存在時口頭說「確認」即可，背景化、斷線或重建 session 後授權失效。
- `chat.open` 可動畫開啟指定已接受對象的真人聊天室。
- 「傳訊息給／告訴／回覆／幫我問某人某內容」都會轉成 `chat.request_send`；只接受聯絡人自然名稱與最多 500 字文字，名稱由 App 對真實已接受名單做唯一解析，找不到或重名時會朗讀候選人要求補充。
- 待確認存在時可口頭說「確認」，之後仍通過既有聊天風險檢查。Server 已接受訊息後即以傳送成功為準；Client 會重試主分頁切換與 `ChatRoomPage` push，成功回覆會明確區分「聊天室已開啟」；若兩次導航都失敗，仍不會把已送達的訊息誤報成傳送失敗而誘發重送。

### 本人身分與阿月記憶

- 建立語音 session 前會讀本人個人資料的暱稱；不使用登入帳號名稱。Email 形式的值仍會在 Server allowlist 邊界移除，不當成名字。
- 「我是誰／我叫什麼」使用 `read_self_profile(detail=name) → self.query`；「你對我了解多少／描述我」使用 `detail=summary`，由 Flutter 直接讀本人 Appwrite profile 與既有 matching analysis，只回傳暱稱、年齡、地區、自介、個性摘要與近期狀態，不交給配對阿月。
- 「你記得我什麼／阿月記住的事／我的偏好」使用 `read_memories → memory.query`，直接讀現有 `/api/profile/memories`；可依短 query 過濾，最多朗讀八則，不進公開阿月聊天室。
- 「記住我喜歡／不喜歡／需要／避免⋯」使用 `add_memory → memory.add`。寫入需要新增的 `memory_write` 權限與「確認新增阿月記憶」；成功後呼叫 `/api/profile/memories/add`，再沿用既有 Matchmaker Graph 驗證、敏感偏好拒絕、idempotency 與 profile projection 更新流程，並開啟「阿月記住的事」頁。
- `memory_write` 預設關閉；`memory_read` 維持既有預設。模型不能提供 user ID、Graph key 或 message ID，Flutter 與 Social Server 分別以目前 session 與 server-generated key 綁定。

### 阿月牽線直接讀取與操作

- 說「打開／查看阿月牽線」或「朗讀所有邀請」固定使用 `read_match_hub`：先直接讀 `/api/match/status` 的 live cards 與 Hub history，再動畫開啟既有 `MatchHubPage`；歷史最多取最近 20 個引用，並逐一以 `/api/match/state` 驗證後才朗讀暱稱／主題／活動、狀態與本人可見理由。切換主分頁、push 或 page scope 未完成時不再假稱「已開啟」；歷史失敗也會明確說明，不會誤報成沒有邀請。
- 說「接受第一個／婉拒小美／撤回第二則」時，只以序號或本人可見暱稱解析目前可操作卡片；模型不能提供 `match_id`。
- 寫入前保存 30 秒待確認，綁定 canonical `match_id`、`expected_status`、`expected_revision` 與 namespace。使用者口頭說「確認」後才呼叫既有 `/api/match/decision`，重複確認不會再送第二次。
- 接受、婉拒或撤回完成後，已開啟的阿月牽線頁會收到帳號分區 refresh signal 並立即更新；3 秒 polling 保留作斷線備援。
- 公開聊天中的一般 `choice_prompt` 會直接觸發同一個確認／取消按鈕 callback。若卡片為既有 `hub_only` 類型，不註冊聊天按鈕；會改開阿月牽線並要求說明接受、婉拒或撤回哪一則。
- 直接讀取只需要 `match_read`；接受、婉拒與撤回另需 `match_actions`。開始新的配對搜尋與一般配對建議仍需公開阿月權限。

### 語音個性探索

- 說「開始個性探索」會呼叫 `personality_exploration_turn`，透過 `personality.explore` 將開始訊息送入原本永久公開阿月房間。
- 阿月會用語音提出下一題；使用者可直接說出自然回答，Gemini Live 會在同一 session 將每一輪繼續送回同一工具，不切換成打字模式或新建聊天室。
- 只有使用者明確說停止探索、探索完成或語音 session 結束才離開這個互動模式。需要 `public_ayue` 與 `memory_read` 權限。

## Server 提供的介面

### `GET /api/app-voice/capability`

提供 protocol v4、full-duplex、持久 Live session、Server VAD、session resumption、function calling、demo-only 狀態、session 時限與同意版本。v4 另回報 capability proxy v2、task protocol v1、guide protocol v1、task interaction 與 personal routine 支援、7 個最大模型工具與每次最多 8 項操作。

### `POST /api/app-voice/session`

使用安裝 ID、Appwrite userId、同意版本、輸入模式、輸出模式與 Client protocol 申請一次性 ticket。Protocol v4 一律必須帶 `Authorization: Bearer <Appwrite JWT>`，Server 驗證 JWT 與 userId 相同後才綁定 proxy 與 task owner；v3 legacy 在過渡期保留舊行為。

### `GET /api/app-voice/tasks` / `POST /api/app-voice/tasks/{task_ref}/{cancel|retry|input|dismiss|undo}`

這些 owner-scoped API 一律驗證 Appwrite JWT。查詢 API 回傳 active、recent 或 all 的最近 20 項；所有修改 API 都以 `expected_revision` 防止舊畫面覆寫新狀態。

### `WSS /api/app-voice`

Client 可傳送：

- `hello`
- `utterance`
- `interrupt`
- `confirmation_response`
- `context_changed`
- PCM16 binary audio
- `audio_end`
- `action_result`
- `task_cancel`
- `task_retry`
- `task_input`
- `task_dismiss`
- `task_undo`
- `stop`

Server 可回傳：

- `ready`
- `assistant_reply`
- `assistant_transcript`
- `user_transcript`
- `microphone_state`
- `state`
- `action_proposal`
- `confirmation_required`
- `confirmation_expired`
- `task_snapshot`
- `task_update`
- `guide_update`
- `permission_repair`
- `audio`
- `audio_chunk`
- `audio_complete`
- `audio_interrupted`
- `audio_unavailable`
- `error`
- `closed`

### Gemini Live function call 安全閘道

- Proxy 模式只暴露 `find_app_capabilities`、`run_app_capabilities`、`describe_current_screen`、`read_tasks`、`cancel_task`、`resolve_pending_interaction`、`close_voice_mode` 七個固定工具。
- 執行前必須先搜尋能力；`capability_ref` 綁定 owner、session、catalog、權限、scope/revision 並於 120 秒過期。
- `capabilities.json` v5 同時提供結構化的操作步驟、前置條件、限制與固定 workflow；說明模式不會發出可執行 ref。
- action 標題、別名、完整 argument JSON Schema、風險、執行位置、可取消性與 Flutter metadata 都來自同一份 catalog；新增 action 不會改變模型看到的 7 個工具。
- perform 搜尋只在用戶意圖夠明確且功能目前可用時發出 ref；模糊領域詞只回傳說明並要求追問。

### Durable task 行為

- 多項操作與 background capability 才建立 Mongo task batch；單一快速操作仍沿用 `action_proposal → action_result`。
- 唯讀任務每帳號最多並行 3 項；寫入與 UI control 依原始順序串行，前置失敗後的寫入停在 `waiting_input`。
- Server background worker 預設上限 4，以 lease 與 attempt 回收中斷的唯讀工作；不確定是否已送出的寫入不自動重做。
- 等待 App 超過 10 分鐘轉 `expired`；確認 30 秒逾時轉 `waiting_input`；terminal task 7 天後由 TTL 清除。
- 插話只停止播音，不取消 task。queued 可立即取消；running read 採 cooperative cancellation；已送出的寫入不可取消或自動復原。
- 語音關閉後 task 仍留在 Server，不播音也不發 push；下次開啟透過 owner-scoped snapshot 恢復 UI。

- 一般問候、「你是誰」與不需要操作 App 的問題直接回答，不呼叫工具。
- 操作工具包含 `navigate_app`、`describe_current_screen`、`read_calendar`、`read_match_status`、`read_match_hub`、`read_self_profile`、`read_memories`、`add_memory`、個資／設定／貼文工具、`ask_public_ayue`、`ask_private_ayue`、`activate_visible_choice`、`list_contacts`、`open_chat`、`send_chat_message` 與相容用 `ask_matching_ayue`；所有 arguments 都由 schema 限定。
- 所有 function arguments 仍必須通過現有 `validate_proposal`、scope、revision、圖片與發布狀態驗證；Gemini 不能指定 route、API、user ID 或檔案路徑。
- 確認時 Gemini 只能呼叫 `confirm_pending_action`，Server 以自己保留的 pending action 比對口頭「確認／確定／confirm」；若模型誤重送原設定工具，Server 也不會產生第二個確認。委派型確認會送回原 domain 的 pending 狀態機。
- function call ID、tool response 與 Flutter action ID 會去重；重連重放相同 call 時回傳已快取結果，不重複執行。

## Catalog v5 體驗層

- Gemini 仍只看到 protocol v4 的 7 個固定工具；新增能力是 catalog action、固定 workflow 或 task interaction，不增加模型 function 數。
- `find_app_capabilities(mode=explain)` 可回傳導覽教練步驟，Flutter 會顯示可切換步驟的教學卡；有缺少權限時另顯示權限修復卡，只會帶使用者前往原本設定頁，不會自動授權。
- `app.digest.query` 可並行整理已授權的配對、共同約會、今日行程與聯絡人狀態。`app.search` 只查詢請求中列出且當下已授權的 contacts、calendar、shared_dates、memory 或 matching 領域。
- 固定 workflow 包含每日狀態檢查、貼文準備與發布、共同約會安排、個資更新。Server 先展開成既有 action，每個寫入仍經過原本權限、scope、revision、confirmation 與 idempotency 邊界。
- `waiting_input`、`failed` 與 `expired` 任務可帶結構化 interaction card。使用者可重試、修改已允許的參數後重試，或結束任務；斷線後結果不明的寫入不允許重放。
- 任務復原目前只開放 30 秒內的深色模式與流體玻璃設定，且只能使用一次；已傳送訊息、已發布貼文、行事曆與約會寫入不會自動復原。
- 每台裝置最多儲存 8 個個人語音捷徑。只允許每日摘要、開啟配對中心、開啟行事曆、已授權 App 搜尋與記憶搜尋模板；不允許將傳訊息或發布等高風險寫入存成捷徑。
- WebSocket 新增 `guide_update`、`permission_repair`、`task_retry`、`task_input`、`task_dismiss` 與 `task_undo`；REST 同步提供 owner-scoped retry/input/dismiss/undo endpoint。所有 task mutation 都支援 expected revision 檢查。

## 安全與額度限制

- 只接受固定 intent、個資欄位與設定 key；模型不能指定任意 API、route、函式、user ID 或檔案路徑。
- ticket 一次使用、綁定帳號匿名指紋與來源 IP 指紋，且有短效期限。
- 原始音訊、個人資料、caption 與 transcript 不寫入 Server 日誌。完整 transcript 只在記憶體中暫存並交給摘要模型，Appwrite 只保存最多 200 字的摘要。
- Android 麥克風只在 App 前景且浮動氣泡開啟時持續串流；背景化、登出、隱藏功能或明確關閉會停止 AudioRecord。
- Server 只回傳操作提案，不持有 Flutter 的檔案路徑或圖片內容。
- 預設每帳號 10 分鐘最多 6 個 session、每天 30 個 session、全站同時 4 個 session；單次前景 session 最長 600 秒，屆時 Client 會自動申請新 ticket 重連。
- 所有回覆改用 Gemini 後，Gemini TTS 限制調整為每帳號每天最多 60 次；達上限時保留文字回覆但不播放語音。
- 多把 Google API Key 不會增加同一 Google project 的總額度。
- `VOICE_APP_DEMO_ONLY=on` 時，只允許設定於 `VOICE_APP_TEST_USER_IDS` 的帳號；名單為空時會拒絕所有 session。
- `VOICE_APP_DEMO_ONLY=off` 時，測試帳號 allowlist 不生效；短期記憶啟用時仍須以有效 Appwrite JWT 證明 userId，且每帳號與全站額度限制仍然有效。
- v4 operational metric 只記錄 routing mode、capability ID，搜尋排名、延遲、結果碼與 task stage；不記錄 query、arguments、owner ID 或逐字稿。
- 目前本機 `Server/.env` 設為 `VOICE_APP_DEMO_ONLY=off`，因此暫時不限制測試帳號。

## 畢業專題測試帳號

語音 allowlist 使用的是 Appwrite `userId`，不是 Email。專案目前可辨識的測試帳號 ID 為：

- `seed_user_01`
- `seed_user_04`
- `match_test_01` 至 `match_test_30`（由 `Server/scripts/seed_match_test_accounts.py` 建立）

`match_test_01` 至 `match_test_30` 原本對應 `matchtest01@gmail.com` 至 `matchtest30@gmail.com`。依 `TEST_ACCOUNT_CLEANUP_2026-09-10.md`，上述 match 帳號與 `seed_user_01`～`seed_user_10` 已於 2026-09-10 永久刪除，目前不能用來登入做 Web E2E；程式中保留的 ID 只代表測試識別規則。

程式測試中另有 `test-user-id`，它是自動化測試用的假 ID，不代表正式 Appwrite 環境一定有這個帳號。

## 目前還不能做什麼

### 平台限制

- 僅原生 Android 可用；Web、Windows、Linux、iOS、macOS 都不提供可用的全域語音入口。
- 不支援語音的平台仍可使用原本的手動操作，但不顯示吉祥物語音入口。
- 尚未完成 Android 實機內建喇叭器、有線耳機、藍牙 SCO 與不同廠牌 AEC 測試；目前已通過 Android APK 原生編譯，不等於實機回音效果已驗證。

### 互動限制

- 不支援 App 在背景或被關閉時持續聆聽、永遠開啟的麥克風或系統級 wake word；「嘿！阿月！」僅是 App 前景的裝置端喊醒功能。
- Android 12 以上只有在系統回報 on-device recognition 可用時才使用原生離線引擎；較舊系統只能向預設 `SpeechRecognizer` 要求 `EXTRA_PREFER_OFFLINE`，Android 官方說明指出辨識服務可能忽略此旗標。無論哪一種都不會消耗本專案 Gemini API 額度，但「完全離線」仍取決於手機與已下載語言模型。
- 持續收音只在 App 位於前景且語音浮動氣泡保持開啟時有效；背景化、登出或明確關閉會停止。
- 不支援在 App 關閉或背景時自動念出個人結果。
- 不支援聲紋驗證；旁人若能操作已解鎖手機，系統不能判斷說話者是不是帳號本人。
- 不支援任意控制整個 App，只能執行白名單內的個資、設定與貼文操作。
- 不支援以模型產生的任意 API 指令直接操作 Server。
- 打斷目前是 Android 前景實驗功能；已加入 HAL AEC、音量校準與 far-end PCM 相關性過濾，但尚未內建完整 WebRTC AEC3 native library。喇叭器效果仍會受手機廠牌、音量、藍牙裝置與現場噪音影響，不能宣稱等同 Gemini 官方 App。
- protocol v3 已是「Gemini Live audio-to-audio + function call 安全閘道」，不再是另外做完 STT 才呼叫 TTS；但所有 App 操作仍多一層 schema、scope、revision 與確認驗證，因此不承諾與 Google 自家 App 完全相同。
- Gemini Developer API 的 session resumption 不提供 Enterprise `transparent` 模式的最後已消費音訊索引；Server 會丟棄指紋相同的重播回合，但斷線邊界仍需要實機壓力測試。

### 個人資料限制

- 不能用語音修改 Email。
- 不能用語音輸入或修改密碼。
- 不能用語音選擇、拍攝或上傳個人照片。
- 不能繞過電話、年齡、縣市與 revision 驗證。

### 設定限制

- 「AI 戀愛顧問」目前的畫面開關沒有真實持久化功能，因此語音只會回覆「目前尚未支援」，不會假裝已修改。
- 不支援安全與隱私頁中的封鎖、解除封鎖、刪除帳號或登出等高風險操作。
- 不支援未列入白名單的新設定；新增設定需要同步擴充 Server schema、Flutter executor 與測試。

### 貼文限制

- AI 只能依「最近／最新／前幾張」自動選圖；不會讀取或判斷影像內容，也不能選出「最適合」的照片。
- 沒有至少一張圖片時不能發布，沿用現有貼文規則。
- 不支援不經二次口令確認就直接發布。
- 不支援跨裝置同步貼文草稿；草稿只存在目前裝置的 App 私有目錄。
- 登出、成功發布或清除帳號草稿時，本機草稿會被移除。
- 不支援排程發布、標記其他使用者、自動新增位置或自動產生圖片。

### 尚未接入的 App 功能

- 已支援確認後傳送一般文字訊息；仍不支援語音直接傳送圖片、GIF、語音訊息或繞過聊天風險攔截。
- 配對接受／婉拒／撤回與行事曆新增／修改／取消已改為確認後呼叫既有 canonical API；仍不支援跳過二次確認、模型指定 match/event ID 或繞過 status/revision 檢查。
- 不支援封鎖、檢舉、刪除聊天室或其他安全操作。
- 已支援全語音個性探索；仍不支援不經公開阿月安全流程直接改寫記憶、跳過探索確認或任意指定評估分數。

### Gemini 免費層限制

- 本功能的 Gemini 全量回退只適合畢業專題展示與合成／非敏感測試資料。
- 免費 Gemini 內容可能被 Google 用於改善產品，不應當成真人正式服務的個資處理方案。
- 免費額度、Preview 模型、RPM／TPM／RPD 與可用容量可能調整，不能保證固定人數或 SLA。
- 若要給真人封測或正式公開上線，必須切換適合生產個資的付費資料方案，或停用 Gemini 全量回退。

## 啟用方式

1. 實際設定位置是 `Server/.env`；可參考 `Server/app_voice_assistant/.env.example`。
2. 暫時關閉測試帳號限制：設定 `VOICE_APP_DEMO_ONLY=off`。
3. 要恢復限制：設定 `VOICE_APP_DEMO_ONLY=on`，並在 `VOICE_APP_TEST_USER_IDS` 填入以逗號分隔的實際 Appwrite `userId`。
4. 設定 `VOICE_MEMORY_ENABLED=on`、`APPWRITE_INTERNAL_ENDPOINT`、Appwrite project/key、profile 與 voice memory database／collection ID；內網端點不能使用公開網域。
5. 確認既有 `OLLAMA_HOST`、`OLLAMA_API_KEY` 與 `VOICE_MEMORY_OLLAMA_MODEL` 指向要使用的 DeepSeek 模型。
6. 首次部署以 `Server/venv/bin/python Server/app_voice_assistant/setup_memory_appwrite.py --apply` 建立可重複套用的獨立 schema。
7. 確認 Google API Key 變數已由既有 key pool 讀取。
8. 設定修改後必須透過 `Server/start_all.sh` 重新啟動完整 Server 才會生效。
9. Flutter 必須連線固定正式網址 `https://service.misproject.us.ci/`。

若未完成 Server 部署／重啟，執行中的 Server 仍會沿用舊設定。若重新開啟 demo-only 卻沒有設定 allowlist，Android App 雖會顯示吉祥物，但建立語音 session 時會被 Server 拒絕。

關閉 allowlist 不會關閉 Gemini 免費層的用量限制，也不會把免費服務變成適合真實敏感個資的生產環境。短期記憶啟用後 session API 會驗證 Appwrite JWT，但公開展示仍應保留合理額度與測試帳號限制。

## 驗證紀錄

- 2026-09-13 短期記憶收尾、啟動取消、即時氣象與任意行事曆區間：App Voice Server 121 項、Flutter 五組相關回歸 83 項通過；相關 Dart 靜態分析、capability 同步、內網 Appwrite fallback storage、真實 Gemini memory injection、自然日期區間解析、無地點與明確地點兩種 `read_weather` smoke，以及 Google Weather／Air Quality 雙來源呼叫均成功。

- 2026-09-12 顯示、共同約會確認、本人姓名排除與 Live 記憶收尾：App Voice Server 104 項通過；Flutter 五組語音／約會回歸 80 項通過；Dart 相關檔案靜態分析無問題；真實 Gemini Live、DeepSeek 與 Appwrite storage smoke 均通過。Appwrite schema 仍使用已透過 `https://127.0.0.1/v1` 建立及驗證的獨立 database。

- 2026-09-12 短期對話記憶：Appwrite schema 已透過 `https://127.0.0.1/v1` 實際建立及驗證；完整 Server 曾通過四個固定服務與正式 capability readiness。

- 2026-09-12 語音配對補修：Flutter 全量 504 項通過；App Voice Server 全量 56 項通過；Social 離線回歸 1852 項通過、4 項跳過、133 組 subtests 通過。另以真實 `MainPageController`／`MatchHubPage` fixture 驗證 direct `match.query`、牽線卡內容、導航完成與失敗不假稱成功；瀏覽器自動化當下沒有可用 Chrome／IAB session，因此未將裝置麥克風或真人資料頁冒稱 E2E 成功。

- Flutter 完整測試：589 項通過；新增覆蓋 final transcript 在 Gemini 工具之前直接觸發公開／私人阿月可見卡片的原 `choice_id` callback、錯誤聊天 action 的短暫抑制、本人顯示名稱進入 Live context，以及直接讀本人資料、讀取與新增阿月記憶；並包含真實聯絡人列舉、口述收件人解析、route 生命週期、Live PCM 與既有完整 UI 回歸。
- App Voice Server 測試：53 項通過；涵蓋 visible choice 確認／取消固定解析、Gemini 錯叫聊天工具時強制改走按鈕 action、本人資料與記憶 intent、登入顯示名稱的安全 context 過濾，以及既有直接行事曆、配對總覽、牽線決策、口頭確認與全雙工契約。
- Social 本人記憶 HTTP 測試：5 項通過；包含語音新增記憶成功、受保護／敏感內容拒絕與既有記憶讀取回歸。
- Private Ayue 與聊天路由相關回歸：36 項通過、1 項依環境跳過，另有 6 組 subtests 通過。
- Android debug APK：建置成功。
- Web release 與版本化離線 shell：建置成功（63 個資源）。
- Gemini `Achird` 真實 TTS smoke：成功產生 97,964 bytes WAV；本環境首次未快取短句約 3.37 秒。
- Gemini Live `Achird` 真實串流 smoke：連線約 0.35 秒、第一個音訊區塊約 0.91 秒、短句完成約 2.34 秒，共 10 chunks／67,202 bytes PCM。
- Gemini Live 真實全雙工 smoke：簡短「你是誰」首音兩次約 0.51／0.71 秒；語音輸入回傳阿月身分且 function call 為空。
- Gemini Live 真實工具 smoke：「關閉通知」產生 `set_app_setting(key=notifications.global, enabled=false)`，工具回覆後只朗讀一次「請說確認關閉訊息通知」，下一輪產生 `confirm_pending_action`。
- Gemini Live 真實打斷 smoke：產生中送入新回合後約 0.038 秒收到 `interrupted`，新回合回答「好」；實際麥克風端到端延遲仍取決於 Android AEC 與現場環境。
- Gemini Live 真實啟動問候 smoke：同一個持久 session 收到啟動事件後主動回答「嗨，我是阿月，今天想請我幫什麼？」，首個音訊區塊約 0.55 秒，且沒有誤呼叫 App 工具。
- 舊 Gemini Live 配對 smoke 曾驗證 matching tool 能保留原問題；目前配對進度／對象／待確認已由 Flutter 截流後直接讀 canonical API，不再使用該 smoke 的聊天委派結果作為狀態答案。
- Gemini Live 相簿複合指令 smoke：本次嘗試兩次皆卡在 Live 握手，45 秒後逾時，未取得可宣稱成功的線上模型結果；本機 function schema、action 順序及發布確認已有離線測試覆蓋。
- Gemini session resumption 真實握手成功並取得 resumable handle；同時觀察到斷線後會重送前一輪，已加入 PCM 指紋丟棄與單元測試。
- `Server/start_all.sh`：Shell 語法檢查通過。
- 本次新增／修改 Dart 範圍：`dart analyze lib test` 無錯誤，只有 2 個既有 style info（`drift_widgets.dart` 與 `relationship_date_coordination_card.dart`）。
- Web 瀏覽器：專用測試帳號成功登入且 `profileReady`；吉祥物拖曳後重載保留相近比例位置，「顯示語音助理」關閉／重新開啟皆成功，測試後已恢復設定並登出。
- Web 語音入口：可開啟並接受 Gemini 資料同意，但透過 4173 origin 連正式服務時顯示無法連線，未能取得可驗證的 SpeechRecognition 或通知語音 E2E；訊息通知全程維持原狀。
- Web 音訊 console：4173 舊 service-worker origin 曾保留 `audioplayers.global/events` MissingPlugin；改用全新 4174 origin 載入最新 `HTMLAudioElement` build 後，登入頁完整載入且 error/warn console 為空。
- 正式網址已於 2026-09-11 透過 `Server/start_all.sh` 重新載入 protocol v3；capability 實測包含 `full_duplex_live`、持久 Gemini session、Server VAD、function calling、600 秒 session、`contact_list`、`chat_send`、`visible_choice_action`、`direct_self_profile`、`direct_memory_read` 與 `direct_memory_add`。
- Android Pixel 9a／Android 16 模擬器 smoke：最新版 debug APK 覆蓋安裝、專用帳號登入、配對／聊天／阿月文字頁與 `Sideup Sunny` 聯絡人顯示成功，沒有紅屏。自動化環境無法把中文可靠注入虛擬麥克風，因此沒有把文字路徑冒充成語音 E2E；可重複步驟與限制記錄於根目錄 `模擬器使用說明.md`。
- GitNexus 最終影響檢查：DatingApp 累積差異涵蓋 237 個 symbols、51 條流程，整體風險為 CRITICAL（這是整批尚未提交的全域 App shell、原生音訊、個資、貼文、相簿、動畫主分頁、route 生命週期、穩定導航、聯絡人／傳訊息、可見按鈕 bridge、直接本人資料與記憶、行事曆、配對總覽／牽線與三種聊天頁累積結果）；Server 已追蹤差異涵蓋 20 個 symbols、5 條流程、MEDIUM，集中於 Private Ayue 搜尋、聊天路由與 Social 模型／系統路由。尚未追蹤的 `app_voice_assistant/` 目錄無法由 GitNexus 完整列入，因此另以 53 項 App Voice、5 項 Social 記憶、589 項 Flutter 回歸、Android 模擬器 smoke、APK build、正式 capability 與 `start_all.sh` shell 語法檢查補強。
