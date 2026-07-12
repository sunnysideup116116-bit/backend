import os

from pymongo import MongoClient
from config import MONGO_URI

# ----------------- DB Initialization -----------------
mongo_client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=int(os.getenv("MONGO_SERVER_SELECTION_TIMEOUT_MS", "5000")),
    connectTimeoutMS=int(os.getenv("MONGO_CONNECT_TIMEOUT_MS", "5000")),
    socketTimeoutMS=int(os.getenv("MONGO_SOCKET_TIMEOUT_MS", "10000")),
)
db = mongo_client["profiling_db"]
profiles_coll = db["profiles"]
matches_coll = db["matches"]
messages_coll = db["messages"]

