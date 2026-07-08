from pymongo import MongoClient
from config import MONGO_URI

# ----------------- DB Initialization -----------------
mongo_client = MongoClient(MONGO_URI)
db = mongo_client["profiling_db"]
profiles_coll = db["profiles"]
matches_coll = db["matches"]
messages_coll = db["messages"]
semantic_plans_coll = db["semantic_plans"]
knowledge_graph_edges_coll = db["knowledge_graph_edges"]
audit_logs_coll = db["audit_logs"]

# ----------------- Neo4j Initialization -----------------
from neo4j import GraphDatabase
from config import NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD, NEO4J_DATABASE

neo4j_driver = None
if NEO4J_URI and NEO4J_USERNAME and NEO4J_PASSWORD:
    neo4j_driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

