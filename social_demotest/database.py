from pymongo import MongoClient
from config import MONGO_DB_NAME, MONGO_URI

# ----------------- DB Initialization -----------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client[MONGO_DB_NAME]
profiles_coll = db["profiles"]
matches_coll = db["matches"]
messages_coll = db["messages"]
semantic_plans_coll = db["semantic_plans"]
calendar_events_coll = db["calendar_events"]
