# 0726-new 阿月前後端整合紀錄

> [!NOTE]
> **歷史紀錄說明 (Historical Archive Notice)**
> 本文件記錄 2026-07-26 完成之階段性整合紀錄。請注意：後續提交（`fd8d1ca`）已將原本的 `main_app` 與 `ai_gen` 重構平鋪為 **`social`**（主服務目錄為 `Server/social/`）；現行開發與系統架構請以 [`AYUE_V3_ARCHITECTURE.md`](./AYUE_V3_ARCHITECTURE.md) 與 [`README.md`](../README.md) 為主。

## 1. 文件目的

本文件記錄 2026-07-26 完成的 Revision 阿月後端與 Flutter 前端整合，包括：

- 前後端 Git 分支與提交。
- Revision 後端核心的正式位置。
- 本次功能調整與 API 變更。
- 本機開發、測試及啟動方式。
- 如何確認 Flutter 連到本機新版後端。
- 舊 worktree 與 `Revision_grad` 的清理及封存方式。
- 常見問題與安全回復方式。

## 2. 正式專案位置

前端與後端是兩個獨立的 Git 儲存庫，不應互相複製到對方目錄。

| 項目 | 正式位置 | 分支 | 整合提交 |
| --- | --- | --- | --- |
| Python/FastAPI 後端 | `/home/sunny/桌面/Graduate_Project/Server` | `0726-new` | `88578b9` |
| Flutter 前端 | `/home/sunny/桌面/Graduate_Project/DatingApp` | `0726-new` | `d4a71df` |

後端原本的 `server-revision-ayue` 只是一個 Git worktree。整合完成後，正式 `Server` 已切換到 `0726-new`，該 worktree 已安全移除。

## 3. 系統架構

```text
Flutter DatingApp
    |
    | HTTP: http://127.0.0.1:8000
    v
Main FastAPI Server :8000 (social API, 舊稱 main_app / ai_gen)
    |-- social API
    |
    |-- Risk Backend :8001
    |
    `-- Matchmaker Agent :9001

資料服務：
- Appwrite：登入、使用者基本資料、照片與風險知識庫
- MongoDB：Big Five、Deep Profile、配對、聊天室與媒人事件
- Neo4j：偏好記憶、候選人特質與全域配對規則
```

### Port 對照

| Port | 服務 | 驗證位置 |
| --- | --- | --- |
| `8000` | Main FastAPI、main_app、ai_gen | `http://127.0.0.1:8000/health` |
| `8001` | Risk Backend | `http://127.0.0.1:8001/health` |
| `9001` | Matchmaker Agent | `http://127.0.0.1:9001/openapi.json` |

Matchmaker Agent 目前沒有 `/health` 路由，因此應使用 `/openapi.json` 確認服務是否正常載入。

## 4. 本次後端整合內容

### 4.1 Revision 阿月核心

- 阿月私聊後端以 Revision 實作為主。
- 完全移除 probe／探口風流程、設定、事件與相容端點。
- 清除舊版可能殘留的 probe、feedback request 等私人事件。
- 保留已接受配對、聊天、媒人卡片與通知等既有必要流程。

### 4.2 語意計畫與約會協調

- 新增 relationship semantic plan 服務。
- 新增約會資料整理、更新與雙方確認流程。
- 約會表單使用 revision 防止舊畫面覆蓋較新的資料。
- 雙方確認後，結果會寫入共用聊天室。

主要檔案：

- `main_app/services/semantic_plan_service.py`
- `main_app/services/date_coordination_service.py`
- `main_app/routers/chat.py`
- `matchmaker_agent/agent_api.py`

### 4.3 配對婉拒與通知

- 拒絕原因改用該候選人的 `distinctive_tags`。
- 沒有候選人特質時，才使用候選人的近期情境作為備選。
- Flutter 不再提供自訂拒絕原因欄位。
- 保留「先不拒絕」，關閉視窗時不會送出婉拒。
- 對方拒絕已送出的邀請時，發起者會收到委婉通知。

### 4.4 默契小測驗與今日話題

- 使用者送出默契測驗後，測驗卡會收起。
- 點擊「再看今天話題」時，即使今天已抽過，也會重新把同一個話題寫入雙方共用聊天室。
- 後端投遞成功後，Flutter 會關閉話題卡並顯示成功提示。

### 4.5 註冊與 Big Five

- 一般註冊及 OAuth 補資料完成時，會呼叫：

```text
POST /api/profile/big-five/initialize
```

- 初始化採冪等方式，不會覆蓋既有 Big Five 分析。
- Big Five AI 每次尚未完成分析時，會回傳兩個符合當前問題的常見回答。
- Flutter 把兩個回答顯示在輸入框上方。
- 使用者可點擊模板直接送出，也可繼續輸入自己的回答。

### 4.6 Flutter 新使用者引導

- 新註冊使用者會看到分段操作引導。
- 引導涵蓋個性分析、配對邀請決定權與私下詢問阿月。
- 引導完成狀態由後端保存，避免每次進入都重複顯示。

## 5. 環境需求

### 5.1 後端

正式後端目錄應具備：

```text
/home/sunny/桌面/Graduate_Project/Server/.env
/home/sunny/桌面/Graduate_Project/Server/venv
```

`.env` 至少需要依部署環境配置 MongoDB、Appwrite、Neo4j 與 LLM 相關設定。不得將 `.env`、密碼、API key 或資料庫連線字串提交到 Git。

### 5.2 Flutter

需要：

- Flutter SDK。
- Linux desktop build 相關套件。
- Appwrite 設定可連到目前使用的 Appwrite 專案。
- 本機測試時，Flutter API URL 必須是 `http://127.0.0.1:8000`。

## 6. 啟動後端

開啟第一個終端機：

```bash
cd /home/sunny/桌面/Graduate_Project/Server
git switch 0726-new
./start_all.sh
```

`start_all.sh` 會依序啟動：

1. Risk Backend，port `8001`。
2. Matchmaker Agent，port `9001`。
3. Main FastAPI Server，port `8000`。

主服務會留在前景執行。停止服務時，在該終端機按 `Ctrl+C`。

### 6.1 健康檢查

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8001/health
curl -fsS http://127.0.0.1:9001/openapi.json
```

Main Server 預期回傳：

```json
{
  "status": "ok",
  "main_app": "mounted",
  "matchmaker_agent": "external:http://127.0.0.1:9001",
  "risk_backend": "external:http://127.0.0.1:8001"
}
```

## 7. 啟動 Flutter

開啟第二個終端機：

```bash
cd /home/sunny/桌面/Graduate_Project/DatingApp
git switch 0726-new
python3 run_test.py
```

`run_test.py` 提供：

1. 同時啟動兩個測試視窗。
2. 只啟動第二個獨立測試視窗。
3. 重新編譯並啟動第一個視窗。

若選擇重新編譯，工具實際執行的核心指令為：

```bash
flutter build linux \
  --dart-define=MATCHMAKER_API_URL=http://127.0.0.1:8000
```

因此透過 `run_test.py` 新建置的 Linux 版本會連到本機 `Server/0726-new`，而不是預設遠端服務。

### 7.1 手動啟動與 Hot Reload

需要 Hot Reload 時可直接執行：

```bash
cd /home/sunny/桌面/Graduate_Project/DatingApp
flutter run -d linux \
  --dart-define=MATCHMAKER_API_URL=http://127.0.0.1:8000
```

### 7.2 手動建立 Release

```bash
cd /home/sunny/桌面/Graduate_Project/DatingApp
flutter build linux --release \
  --dart-define=MATCHMAKER_API_URL=http://127.0.0.1:8000
```

輸出位置：

```text
build/linux/x64/release/bundle/dating_app
```

## 8. 測試方式

### 8.1 後端主測試

```bash
cd /home/sunny/桌面/Graduate_Project/Server
MONGO_URI=mongodb://127.0.0.1:27017 \
  ./venv/bin/python -m pytest tests -q
```

目前結果：

```text
25 passed
```

測試使用 fake collections，指定本機 `MONGO_URI` 是為了避免測試載入正式 Atlas SRV 設定，不代表正式服務一定使用本機 MongoDB。

### 8.2 Risk Backend

```bash
cd /home/sunny/桌面/Graduate_Project/Server
MONGO_URI=mongodb://127.0.0.1:27017 \
  ./venv/bin/python -m pytest risk_backend/tests -q
```

目前結果：

```text
75 passed
```

### 8.3 Flutter

```bash
cd /home/sunny/桌面/Graduate_Project/DatingApp
flutter test
```

目前結果：

```text
3 passed
```

測試涵蓋：

- Big Five 回答模板只保留兩個非空答案。
- 候選人專屬拒絕原因與「先不拒絕」。
- 三階段新使用者引導。

## 9. 確認目前執行的是新版

### 9.1 確認分支

```bash
git -C /home/sunny/桌面/Graduate_Project/Server branch --show-current
git -C /home/sunny/桌面/Graduate_Project/DatingApp branch --show-current
```

兩者都應顯示：

```text
0726-new
```

### 9.2 確認後端程序來源

先找出監聽程序：

```bash
lsof -nP -iTCP:8000 -iTCP:8001 -iTCP:9001 -sTCP:LISTEN
```

再檢查 PID 的工作目錄：

```bash
readlink /proc/<PID>/cwd
```

應顯示：

```text
/home/sunny/桌面/Graduate_Project/Server
```

若仍顯示 `server-revision-ayue` 或其他舊目錄，請停止舊程序，再由正式 `Server` 執行 `./start_all.sh`。

### 9.3 確認 Flutter API URL

`MATCHMAKER_API_URL` 是 Dart compile-time define，單純在啟動執行檔前設定 shell 環境變數不會改變已編譯版本。

若懷疑仍連到舊後端，必須重新建置：

```bash
flutter build linux --release \
  --dart-define=MATCHMAKER_API_URL=http://127.0.0.1:8000
```

或透過 `python3 run_test.py` 選擇重新編譯。

## 10. Git 狀態與提交範圍

後端整合提交：

```text
88578b9 feat: integrate revised Ayue backend flows
```

Flutter 整合提交：

```text
d4a71df feat: integrate revised Ayue Flutter flows
```

Flutter 工作目錄原本已有以下未提交內容，本次刻意沒有混入整合提交：

```text
pubspec.lock
lib-backup/
```

除非先確認來源與用途，後續不應使用 `git add -A` 把它們一起提交。

## 11. Revision_grad 與 worktree 清理

原始目錄：

```text
/home/sunny/桌面/Graduate_Project/Revision_grad
```

已在整合、測試及安全封存完成後刪除。

原 worktree：

```text
/home/sunny/Sunny/PPEYAN/server-revision-ayue
```

已使用 `git worktree remove` 移除。

非敏感封存：

```text
/home/sunny/桌面/Graduate_Project/archive/Revision_grad-source-20260726.tar.gz
```

封存資訊：

```text
檔案數：94
大小：583 KB
SHA-256：e9d53f40bf7b334b03557d2295db6e1cf47fc16d51c3aa6ac90e86e9356a47d9
```

封存已排除：

- `.env`
- `mongoDB_string.txt`
- `*.log`
- `.git`
- `venv` 與 `.venv`
- Python cache 與 mypy cache

## 12. 安全回復方式

若後端需要暫時回到整合前版本：

```bash
cd /home/sunny/桌面/Graduate_Project/Server
git switch 0721-risk_backend-change
```

若 Flutter 需要回到整合前版本：

```bash
cd /home/sunny/桌面/Graduate_Project/DatingApp
git switch main
```

切換前先執行：

```bash
git status --short
```

若有未提交修改，應先確認來源；不要使用 `git reset --hard` 或直接刪除使用者變更。

回到新版：

```bash
git -C /home/sunny/桌面/Graduate_Project/Server switch 0726-new
git -C /home/sunny/桌面/Graduate_Project/DatingApp switch 0726-new
```

切換後需重新啟動後端；若 Flutter 的 API URL 或程式碼版本不同，也應重新建置。