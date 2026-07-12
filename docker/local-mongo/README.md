# 本地 MongoDB（取代 Atlas 雲端）

本目錄用於在本機跑 MongoDB + Vector Search，取代 MongoDB Atlas 雲端依賴。

## 架構

使用官方 `mongodb/mongodb-atlas-local:preview` Docker 映像檔，**同一個 container 內含**：
- `mongod`：MongoDB 資料庫引擎（Community 8.2+）
- `mongot`：搜尋引擎（基於 Apache Lucene，提供 `$vectorSearch`）

`$vectorSearch`、`$search` 等 aggregate pipeline 與 Atlas 功能對等，**`match.py` 程式碼零修改**。

## 前置需求

- Docker + Docker Compose

## 快速啟動

```bash
# 1. 啟動 MongoDB（背景執行）
cd Server/docker/local-mongo
docker compose up -d

# 2. 確認容器健康
docker ps
# 預期看到 local-mongo 狀態為 healthy

# 3. 從 Atlas 遷移現有資料（可選）
pip install pymongo python-dotenv
python migrate_from_atlas.py

# 4. 建立向量搜尋索引（$vectorSearch 必要）
python create_vector_index.py
```

## 切換 .env

把 `Server/.env` 的 MongoDB 連線字串改成本地：

```env
# --- MongoDB (本地自架，取代 Atlas) ---
MONGO_URI="mongodb://mongo_admin:localmongo123@localhost:27017/?authSource=admin"
AI_CHAT_MONGO_URI="mongodb://mongo_admin:localmongo123@localhost:27017/?authSource=admin"
```

改完後 `main_app` 與 `ai_gen` 都會自動連本地。

> `ai_gen/app.py` 的 `mongoDB_string.txt` fallback 機制仍可用，但建議直接靠 env，未來可刪除該檔。

## 管理指令

| 操作 | 指令 |
|---|---|
| 啟動 | `docker compose up -d` |
| 停止 | `docker compose down` |
| 停止並清空資料 | `docker compose down -v` |
| 查看日誌 | `docker logs -f local-mongo` |
| 進 mongosh | `docker exec -it local-mongo mongosh -u mongo_admin -p localmongo123 --authenticationDatabase admin` |

## 連線資訊

| 項目 | 值 |
|---|---|
| Host | `localhost` |
| Port | `27017` |
| 帳號 | `mongo_admin` |
| 密碼 | `localmongo123` |
| Auth DB | `admin` |
| 連線字串 | `mongodb://mongo_admin:localmongo123@localhost:27017/?authSource=admin` |

## 注意事項

- **Public Preview**：此映像檔的 Vector Search 仍為 preview 階段，適合開發/測試，production 需評估。
- **單節點**：`atlas-local` 是單節點 replica set，無高可用性，不建議直接上 production。
- **資料卷**：資料存在 Docker volume（`db`、`configdb`、`mongot`），`docker compose down` 不會清空，加 `-v` 才會。
- **embedding 維度**：`gemini-embedding-2` 輸出 3072 維，`create_vector_index.py` 已設定好。