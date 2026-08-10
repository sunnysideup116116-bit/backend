"""Offline-safe defaults for the deterministic unittest suite."""

import os


os.environ.setdefault("AYUE_TEST_MODE", "1")
os.environ.setdefault("AYUE_SKIP_DOTENV", "1")
os.environ.setdefault(
    "MONGO_URI",
    "mongodb://127.0.0.1:27017/ayue_test?serverSelectionTimeoutMS=50&connectTimeoutMS=50",
)
os.environ.setdefault("MONGO_DB_NAME", "ayue_test")
os.environ.setdefault("OLLAMA_HOST", "http://127.0.0.1:9")
os.environ.setdefault("OLLAMA_API_KEY", "test")
os.environ.setdefault("GOOGLE_AI_STUDIO_API_KEY", "test")
