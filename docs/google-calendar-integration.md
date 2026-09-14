# Google Calendar 唯讀整合

這個整合只讀取登入使用者的 **Google 主要日曆**，並在 DatingApp 的行事曆頁合併顯示。它不會建立、修改或刪除 Google 行程。

## 權限與資料界線

- OAuth scope 固定為 `https://www.googleapis.com/auth/calendar.events.owned.readonly`。
- Google Client Secret、access token 與 refresh token 只由 Server 處理。
- token 用 Fernet 加密後儲存於 MongoDB。
- Google 行程內容不寫入 MongoDB 或 Flutter 離線快取；只在使用者打開頁面或手動同步時讀取。
- 前端只接收事件 ID 的不透明雜湊、標題、時間、地點、全日標記與狀態。
- 只有原本就具備行事曆讀取能力的公開阿月 Calendar agent、私聊媒人的 busy/free 查詢、App Voice 行事曆查詢與主動關心的 busy gate 會納入 Google 行程；其他 agent 不會獲得新權限。
- Agent 只會在「允許阿月讀取行事曆」開啟、請求攜帶且驗證通過 Appwrite JWT，並且 Google 連線有效時讀取。
- Google 行程不會成為可修改的 agent reference、語音畫面 target 或行事曆編輯動作，因此 agent 不能修改或刪除它。

## Google Cloud 設定

1. 啟用 Google Calendar API。
2. OAuth 用戶端類型使用「網頁應用程式」。
3. 在「已授權的重新導向 URI」加入下列完整網址：

   `https://service.misproject.us.ci/api/integrations/google-calendar/callback`

4. OAuth 同意畫面只保留上述唯讀 scope，並完成 Google 要求的驗證。

## Server 設定

在 `Server/social/.env` 加入：

```dotenv
AYUE_GOOGLE_CALENDAR_ENABLED=on
# 建議剛上線時先填測試者的 Appwrite user ID，多個用逗號分隔。
AYUE_GOOGLE_CALENDAR_ALLOWED_USER_IDS=
GOOGLE_CALENDAR_CLIENT_ID=
GOOGLE_CALENDAR_CLIENT_SECRET=
GOOGLE_CALENDAR_TOKEN_KEY=
GOOGLE_CALENDAR_REDIRECT_URI=https://service.misproject.us.ci/api/integrations/google-calendar/callback
```

產生一把 Fernet key：

```bash
Server/.local-venv/social/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

把輸出放到 `GOOGLE_CALENDAR_TOKEN_KEY`，不要提交 `.env`，也不要把 Client Secret 放進 Flutter 或公開儲存庫。完成後依正式啟動契約重啟：

```bash
Server/start_all.sh
```

## 使用者流程

1. 在設定頁開啟「行事曆設定」，選擇是否允許阿月讀取，並點「連接 Google 日曆」。
2. APP 取得短效 Appwrite JWT，Server 用它驗證真實帳號。
3. Server 建立十分鐘、單次使用的 OAuth state 與 PKCE 驗證碼，APP 再開啟 Google 授權頁。
4. Google 導回 Server callback 後，Server 交換並加密 token。
5. 使用者回到 APP 時會重新檢查狀態；也可以點「檢查連線」或「同步」。
6. 點「斷開」會先刪除 Server 儲存的憑證，再嘗試向 Google 撤銷 token。

## HTTP 端點

- `GET /api/integrations/google-calendar/status`
- `POST /api/integrations/google-calendar/authorize`
- `GET /api/integrations/google-calendar/callback`
- `GET /api/integrations/google-calendar/events?from=YYYY-MM-DD&to=YYYY-MM-DD`
- `DELETE /api/integrations/google-calendar/connection`

除 Google callback 外，所有端點都需要 `Authorization: Bearer <Appwrite JWT>`。查詢範圍上限為 92 天，單次最多讀取 1,000 筆事件。
