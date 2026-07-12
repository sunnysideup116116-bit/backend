#!/usr/bin/env python3
"""
從 MongoDB Atlas 雲端遷移資料到本地 MongoDB。

會搬移 profiling_db 的 6 個 collection：
  profiles, matches, messages, semantic_plans, knowledge_graph_edges, audit_logs
以及 ai_chat_db 的 2 個 collection：
  chat_logs, semantic_plans

執行方式：
  pip install pymongo python-dotenv
  python migrate_from_atlas.py
"""
from pymongo import MongoClient, ASCENDING
from dotenv import load_dotenv
from pathlib import Path
import os
import sys
import time

# 載入 .env
_top_env = Path(__file__).resolve().parent.parent.parent / ".env"
if _top_env.exists():
    load_dotenv(dotenv_path=_top_env)
else:
    load_dotenv()

# --- 來源：Atlas 雲端 ---
ATLAS_URI_SOCIAL = os.getenv("MONGO_URI")          # profiling_db
ATLAS_AI_CHAT    = os.getenv("AI_CHAT_MONGO_URI")  # ai_chat_db

# --- 目標：本地 ---
LOCAL_URI = "mongodb://mongo_admin:localmongo123@localhost:27017/?authSource=admin&directConnection=true"

COLLECTIONS_SOCIAL = [
    "profiles", "matches", "messages",
    "semantic_plans", "knowledge_graph_edges", "audit_logs"
]
COLLECTIONS_AI = ["chat_logs", "semantic_plans"]

BATCH = 1000


def migrate_db(src_uri, src_db_name, dst_uri, dst_db_name, collections):
    if not src_uri:
        print(f"⚠️  來源 URI 為空，跳過 {src_db_name}")
        return 0

    print(f"\n{'='*60}")
    print(f"📦 遷移 {src_db_name} → {dst_db_name}")
    print(f"   來源：{mask_uri(src_uri)}")
    print(f"{'='*60}")

    src_client = MongoClient(src_uri, serverSelectionTimeoutMS=10000, tls=True, tlsAllowInvalidCertificates=True)
    dst_client = MongoClient(dst_uri, serverSelectionTimeoutMS=5000)

    try:
        src_client.admin.command("ping")
    except Exception as e:
        print(f"❌ 無法連線到來源 Atlas：{e}")
        return 0

    src_db = src_client[src_db_name]
    dst_db = dst_client[dst_db_name]
    total_migrated = 0

    for coll_name in collections:
        if coll_name not in src_db.list_collection_names():
            print(f"   ⏭️  {coll_name}: 來源不存在，跳過")
            continue

        src_coll = src_db[coll_name]
        dst_coll = dst_db[coll_name]

        count = src_coll.count_documents({})
        print(f"\n   📄 {coll_name}: {count} 筆")

        if count == 0:
            print(f"      （空集合，跳過）")
            continue

        # 清空目標（避免重複匯入）
        deleted = dst_coll.delete_many({})
        if deleted.deleted_count > 0:
            print(f"      🧹 已清空目標 {deleted.deleted_count} 筆舊資料")

        migrated = 0
        cursor = src_coll.find({})
        batch = []
        for doc in cursor:
            batch.append(doc)
            if len(batch) >= BATCH:
                dst_coll.insert_many(batch, ordered=False)
                migrated += len(batch)
                print(f"\r      已匯入 {migrated}/{count}", end="", flush=True)
                batch = []
        if batch:
            dst_coll.insert_many(batch, ordered=False)
            migrated += len(batch)

        print(f"\r      ✅ {coll_name}: 完成 {migrated} 筆{' ' * 20}")
        total_migrated += migrated

    return total_migrated


def mask_uri(uri):
    if not uri:
        return "(空)"
    # 遮蔽密碼
    if "@" in uri and "://" in uri:
        scheme, rest = uri.split("://", 1)
        if "@" in rest:
            creds, host = rest.split("@", 1)
            if ":" in creds:
                user, pwd = creds.split(":", 1)
                return f"{scheme}://{user}:***@{host}"
    return uri[:30] + "..."


def main():
    print("🚀 開始從 Atlas 遷移到本地 MongoDB\n")

    total = 0
    total += migrate_db(ATLAS_URI_SOCIAL, "profiling_db", LOCAL_URI, "profiling_db", COLLECTIONS_SOCIAL)
    total += migrate_db(ATLAS_AI_CHAT,    "ai_chat_db",   LOCAL_URI, "ai_chat_db",   COLLECTIONS_AI)

    print(f"\n{'='*60}")
    print(f"🏁 遷移完成！總計 {total} 筆文件。")
    print(f"   下一步：執行 python create_vector_index.py 建立 vector_index")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()