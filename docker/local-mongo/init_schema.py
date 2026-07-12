#!/usr/bin/env python3
"""
初始化本地 MongoDB 的完整 schema：
  - profiling_db: 6 個 collection + 欄位驗證 + 普通索引 + 向量索引
  - ai_chat_db:   2 個 collection + 欄位驗證 + 索引

所有 collection 都加上 JSON Schema validator，防止寫入缺漏欄位的髒資料。
一般查詢用的欄位（user_id, room_id, session_id...）加上普通索引加速。
vector_index 單獨用 create_search_index 建立（$vectorSearch 專用）。

執行方式：
  pip install pymongo python-dotenv
  python init_schema.py
"""
from pymongo import MongoClient, ASCENDING, DESCENDING
import os
import sys
import time
from pathlib import Path
from dotenv import load_dotenv

_top_env = Path(__file__).resolve().parent.parent.parent / ".env"
if _top_env.exists():
    load_dotenv(dotenv_path=_top_env)

MONGO_URI = os.getenv("MONGO_URI") or "mongodb://mongo_admin:localmongo123@localhost:27017/?authSource=admin&directConnection=true"
EMBEDDING_DIM = 3072


# ------------------------------------------------------------------
# profiling_db — main_app 用
# ------------------------------------------------------------------

PROFILES_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["user_id"],
        "properties": {
            "user_id":             {"bsonType": "string", "description": "使用者唯一 ID"},
            "big_five":            {"bsonType": "object", "description": "大五人格 {O,C,E,A,N,summary}"},
            "deep_profile":        {"bsonType": "object", "description": "深層價值觀 {values,life_goals,relationship_needs,...}"},
            "current_context":     {"bsonType": "string", "description": "近期情境摘要 10-15 字"},
            "context_embedding":   {"bsonType": "array", "description": "向量化 embedding (3072 維)", "items": {"bsonType": "double"}},
            "ai_chat_locked":      {"bsonType": "bool", "description": "AI 聊天是否鎖定中"},
            "ai_chat_interaction_count": {"bsonType": "int", "description": "AI 聊天互動次數"},
            "last_proactive_time": {"bsonType": "double", "description": "上次主動訊息 timestamp"}
        }
    }
}

MATCHES_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["from_user", "to_user", "status", "created_at"],
        "properties": {
            "from_user":        {"bsonType": "string"},
            "to_user":          {"bsonType": "string"},
            "reason":           {"bsonType": "string", "description": "配對原因（給發起者看）"},
            "receiver_reason":  {"bsonType": "string", "description": "配對原因（給接收者看）"},
            "contrast_label":   {"bsonType": "string", "description": "對比標籤"},
            "distinctive_tags": {"bsonType": "array", "items": {"bsonType": "string"}, "description": "差異化標籤"},
            "status":           {"enum": ["draft", "pending", "accepted", "declined"], "description": "配對狀態機"},
            "created_at":       {"bsonType": "double", "description": "建立時間 (unix timestamp)"}
        }
    }
}

MESSAGES_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["room_id", "sender_id", "content", "timestamp"],
        "properties": {
            "room_id":       {"bsonType": "string", "description": "聊天室 ID = sorted([u1,u2]).join('_')"},
            "sender_id":     {"bsonType": "string"},
            "content":        {"bsonType": "string"},
            "timestamp":     {"bsonType": "double", "description": "unix timestamp"},
            "is_system_idle": {"bsonType": "bool", "description": "系統閒置標記"}
        }
    }
}

SEMANTIC_PLANS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["room_id", "last_updated"],
        "properties": {
            "room_id":              {"bsonType": "string"},
            "current_role":         {"bsonType": "string", "description": "AI 角色 FRIEND/COACH/..."},
            "previous_role":        {"bsonType": "string"},
            "used_ai_next_msg":     {"bsonType": "bool"},
            "dynamic_threshold":    {"bsonType": "double"},
            "buffered_messages":    {"bsonType": "array"},
            "agent1_queued":        {"bsonType": "bool"},
            "agent1_running":       {"bsonType": "bool"},
            "signal_state":         {"bsonType": "object", "description": "{s_lat_window,s_par_window,h_lat_ema,...}"},
            "context":              {"bsonType": "object", "description": "{macro_summary,user_A_id,user_B_id}"},
            "strategy":             {"bsonType": "object", "description": "{strategic_intent,theme,action_plan,dynamic_content_bounds}"},
            "last_updated":         {"bsonType": "string", "description": "ISO 8601 timestamp"}
        }
    }
}

KNOWLEDGE_GRAPH_EDGES_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["room_id", "subject", "predicate", "object", "created_at"],
        "properties": {
            "room_id":            {"bsonType": "string"},
            "subject":             {"bsonType": "string", "description": "三元組主詞"},
            "predicate":           {"bsonType": "string", "description": "三元組謂詞"},
            "object":              {"bsonType": "string", "description": "三元組賓詞"},
            "significance_score":  {"bsonType": "double"},
            "reasoning":           {"bsonType": "string"},
            "source_message_ids":  {"bsonType": "array", "items": {"bsonType": "string"}},
            "created_at":         {"bsonType": "double"},
            "updated_at":         {"bsonType": "double"}
        }
    }
}

AUDIT_LOGS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["suggestion_id", "room_id", "generatedAtTime"],
        "properties": {
            "suggestion_id":   {"bsonType": "string", "description": "sug_xxxxxxxx"},
            "room_id":         {"bsonType": "string"},
            "@context":        {"bsonType": "string", "description": "W3C PROV context"},
            "type":            {"bsonType": "string"},
            "generatedAtTime": {"bsonType": "double", "description": "unix timestamp"},
            "wasGeneratedBy":  {"bsonType": "object", "description": "W3C PROV Activity"},
            "output":          {"bsonType": "object", "description": "{nudge_type,nudge_text}"}
        }
    }
}

# ------------------------------------------------------------------
# ai_chat_db — ai_gen 用
# ------------------------------------------------------------------

CHAT_LOGS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["session_id", "messages", "last_updated"],
        "properties": {
            "session_id":   {"bsonType": "string"},
            "messages":     {"bsonType": "array", "description": "聊天訊息陣列"},
            "last_updated": {"bsonType": "string", "description": "ISO 8601"}
        }
    }
}

AI_SEMANTIC_PLANS_VALIDATOR = {
    "$jsonSchema": {
        "bsonType": "object",
        "required": ["session_id"],
        "properties": {
            "session_id":    {"bsonType": "string"},
            "current_role":  {"bsonType": "string"},
            "signal_state":  {"bsonType": "object"},
            "context":       {"bsonType": "object"},
            "strategy":      {"bsonType": "object"},
            "last_updated":  {"bsonType": "string"}
        }
    }
}


def create_collection_with_validator(db, name, validator):
    """建立 collection 並套用 JSON Schema validator。已存在則更新 validator。"""
    if name in db.list_collection_names():
        db.command("collMod", name, validator=validator)
        print(f"   ✓ {name} validator 已更新")
    else:
        db.create_collection(name, validator=validator)
        print(f"   ✓ {name} 已建立（含 validator）")


def create_normal_indexes(db, name, indexes):
    """建立普通索引 [(field, direction), ...]"""
    coll = db[name]
    for field, direction in indexes:
        coll.create_index([(field, direction)], background=True)


def main():
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    try:
        client.admin.command("ping")
    except Exception as e:
        print(f"❌ 無法連線 MongoDB：{e}")
        sys.exit(1)
    print("✅ 連線成功\n")

    # ===================== profiling_db =====================
    db = client["profiling_db"]
    print("📦 profiling_db")

    # 1) 建立 collection + validator
    create_collection_with_validator(db, "profiles", PROFILES_VALIDATOR)
    create_collection_with_validator(db, "matches", MATCHES_VALIDATOR)
    create_collection_with_validator(db, "messages", MESSAGES_VALIDATOR)
    create_collection_with_validator(db, "semantic_plans", SEMANTIC_PLANS_VALIDATOR)
    create_collection_with_validator(db, "knowledge_graph_edges", KNOWLEDGE_GRAPH_EDGES_VALIDATOR)
    create_collection_with_validator(db, "audit_logs", AUDIT_LOGS_VALIDATOR)

    # 2) 普通索引
    create_normal_indexes(db, "profiles", [
        ("user_id", ASCENDING),
    ])
    create_normal_indexes(db, "matches", [
        ("from_user", ASCENDING),
        ("to_user", ASCENDING),
        ("status", ASCENDING),
        ("created_at", DESCENDING),
    ])
    create_normal_indexes(db, "messages", [
        ("room_id", ASCENDING),
        ("timestamp", ASCENDING),
    ])
    create_normal_indexes(db, "semantic_plans", [
        ("room_id", ASCENDING),
    ])
    create_normal_indexes(db, "knowledge_graph_edges", [
        ("room_id", ASCENDING),
        ("subject", ASCENDING),
        ("predicate", ASCENDING),
        ("object", ASCENDING),
    ])
    create_normal_indexes(db, "audit_logs", [
        ("suggestion_id", ASCENDING),
        ("room_id", ASCENDING),
    ])
    print("   ✓ 普通索引建立完成")

    # 3) 向量索引（$vectorSearch 專用）
    coll = db["profiles"]
    existing = list(coll.list_search_indexes())
    if any(i.get("name") == "vector_index" for i in existing):
        print("   ⚠️  vector_index 已存在，跳過")
    else:
        coll.create_search_index({
            "name": "vector_index",
            "type": "vectorSearch",
            "definition": {
                "fields": [{
                    "type": "vector",
                    "path": "context_embedding",
                    "numDimensions": EMBEDDING_DIM,
                    "similarity": "cosine"
                }]
            }
        })
        print("   ✓ vector_index 已建立")
        # 等待就緒
        for _ in range(30):
            time.sleep(2)
            idx = next((i for i in coll.list_search_indexes() if i.get("name") == "vector_index"), None)
            if idx and idx.get("status") == "READY":
                print("   🎉 vector_index READY")
                break

    # ===================== ai_chat_db =====================
    db2 = client["ai_chat_db"]
    print("\n📦 ai_chat_db")

    create_collection_with_validator(db2, "chat_logs", CHAT_LOGS_VALIDATOR)
    create_collection_with_validator(db2, "semantic_plans", AI_SEMANTIC_PLANS_VALIDATOR)

    create_normal_indexes(db2, "chat_logs", [
        ("session_id", ASCENDING),
    ])
    create_normal_indexes(db2, "semantic_plans", [
        ("session_id", ASCENDING),
    ])
    print("   ✓ 普通索引建立完成")

    # ===================== 驗證 =====================
    print("\n" + "=" * 50)
    print("📊 最終狀態")
    print("=" * 50)
    for db_name in ["profiling_db", "ai_chat_db"]:
        d = client[db_name]
        print(f"\n{db_name}:")
        for coll_name in d.list_collection_names():
            count = d[coll_name].count_documents({})
            idxs = sorted(d[coll_name].index_information().keys())
            search_idxs = list(d[coll_name].list_search_indexes())
            search_names = [i.get("name") for i in search_idxs]
            print(f"  {coll_name}: {count} 筆, 索引={idxs}, search={search_names}")

    print("\n✅ Schema 初始化完成！")


if __name__ == "__main__":
    main()