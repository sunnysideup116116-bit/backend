#!/usr/bin/env python3
"""
建立向量搜尋索引 (vector_index) 於本地 MongoDB。
$vectorSearch 需要 Atlas/Community 8.2+ 的 Search Index，不能只用 createIndex。

執行方式：
  pip install pymongo
  python create_vector_index.py
"""
from pymongo import MongoClient
import sys
import os
from pathlib import Path
from dotenv import load_dotenv

# 優先使用 .env 的 MONGO_URI（若已切換到本地）
_top_env = Path(__file__).resolve().parent.parent.parent / ".env"
if _top_env.exists():
    load_dotenv(dotenv_path=_top_env)

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://mongo_admin:localmongo123@localhost:27017/?authSource=admin&directConnection=true"
DB_NAME = "profiling_db"
COLLECTION = "profiles"
INDEX_NAME = "vector_index"
EMBEDDING_DIM = 3072  # gemini-embedding-2 輸出維度

def main():
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    # 連線測試
    try:
        client.admin.command("ping")
    except Exception as e:
        print(f"❌ 無法連線到 MongoDB：{e}")
        sys.exit(1)

    db = client[DB_NAME]
    coll = db[COLLECTION]

    # 檢查既有的 search index
    existing = list(coll.list_search_indexes())
    if any(idx.get("name") == INDEX_NAME for idx in existing):
        print(f"⚠️  索引 '{INDEX_NAME}' 已存在，跳過建立。")
        for idx in existing:
            print(f"   - {idx.get('name')}: status={idx.get('status')}")
        return

    # 建立向量搜尋索引（對應 match.py 的 $vectorSearch 設定）
    index_def = {
        "fields": [
            {
                "type": "vector",
                "path": "context_embedding",
                "numDimensions": EMBEDDING_DIM,
                "similarity": "cosine"
            }
        ]
    }

    print(f"📤 正在建立向量索引 '{INDEX_NAME}' ...")
    coll.create_search_index({"name": INDEX_NAME, "type": "vectorSearch", "definition": index_def})
    print("✅ 已送出建立請求。索引狀態會在背景同步完成。")

    # 等待索引就緒（最多 60 秒）
    import time
    for _ in range(30):
        time.sleep(2)
        idxs = list(coll.list_search_indexes())
        target = next((i for i in idxs if i.get("name") == INDEX_NAME), None)
        if target:
            status = target.get("status", "UNKNOWN")
            print(f"   狀態：{status}")
            if status == "READY":
                print(f"🎉 索引 '{INDEX_NAME}' 已就緒！")
                return
    print("⏳ 索引仍在建立中，稍後可用 list_search_indexes() 確認。")

if __name__ == "__main__":
    main()