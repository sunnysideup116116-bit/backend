# 共同開場：暱稱與客製化修正

## 原因

上一版共同開場只讀 Mongo 的 display_name／nickname／name；Appwrite 才是帳號暱稱來源，所以前端已顯示名稱，開場仍可能變成「對方、對方」。上一版正常路徑也是固定句型，而非模型生成。

實際唯讀驗證另發現目前 Appwrite 設定為 loopback HTTP，會回 301 到 loopback HTTPS。暱稱 adapter 原本不跟隨轉址，因此即使用該 adapter 仍無法取得名字。現在只把已知 loopback HTTP 預設／80 埠的 `/v1` 入口正規化成 App 現有固定 `https://appwrite.misproject.us.ci/v1`；其他設定與開發埠不變、不依 Location 轉址、不關閉 TLS 驗證、不改 `.env`。

## 現行行為

- 接受後的共同開場改用既有 Appwrite-first `proposal_display_name`；查不到才使用安全 Mongo fallback，仍無名稱時使用「你們」，不重複稱兩人「對方」。
- `pair_opening_service.py` 讓目前的聊天模型依這次邀請主題、已公開且角色正確的情境／性格及公開 match basis，生成短引介與一個具體破冰問題，不只是插入理由。
- 不把邀請主題當成雙方共同興趣；模型只看到 bounded evidence，不傳姓名、使用者 ID、私人提醒或記憶。姓名最後由程式補上。
- 一次模型呼叫、15 秒 transport budget；使用既有 background scheduler，不延長接受配對的 HTTP 回應。缺少依據或模型／格式失敗才使用 fallback。
- 仍以阿月身份寫一則共同 system message；同 match event key 只保存一次。Event 自己的活動開場、私人媒人訊息與已修好的 profiling 不改。

## 驗收與範圍

- 完整 Social 離線回歸：**1898 passed、4 skipped、133 subtests passed**；Python compile、`git diff --check`、`bash -n start_all.sh` 通過。測試以現有 `.local-venv/social/lib/python3.12/site-packages` 補足測試環境的 jsonschema 搜尋路徑，未修改依賴。
- 開場與配對接受針對性離線測試：24 passed。包含 Appwrite 有名稱／Mongo 無名稱、生成內容、隱私隔離、缺名、timeout、格式／依據錯誤、背景執行、重試冪等。
- 現有 Ollama 聊天模型以虛構運動／書店資料實測皆為 `generated`，分別約 3.7／3.9 秒；問題分別為散步或流汗的運動方式、鎖定書區或隨興逛，確認並非固定問句。沒有寫入正式訊息或配對。
- 實際以修正後 reader 唯讀查驗截圖的公開暱稱，HTTP 200 並正確解析 `kkk`；沒有輸出帳號 ID、查閱私人對話或更動資料。
- 本次沒有修改前端、真人紀錄或已保存的舊開場。圖片中的歷史訊息不會因部署自動重寫；請用部署後新接受的配對驗證新開場。
- 使用者後續授權重啟與推送：已透過 `start_all.sh ollama` 重啟，8000／8001／9001／8081 與正式公開 API 健康檢查皆為 HTTP 200，原模式及固定網址未改。
- 初次背景啟動未持續存活，已改用持續終端承載正式啟動腳本並重新驗證健康；重啟前日誌保留於本機臨時備份，未納入版本庫。
- 提交前增量圖譜曾產生空白 symbol ID 與跨模組誤判；完整重建且不使用 parse cache 後複查，31 個變更符號／11 個檔案、2 個已識別受影響流程，整體 medium，無 partial／truncated 結果。暱稱共用 helper 仍為已知 HIGH 影響範圍，已保留安全邊界並完成完整回歸。
