import os
from pathlib import Path
from dotenv import load_dotenv

project_env = Path(__file__).resolve().parents[1] / ".env"
local_env = Path(__file__).resolve().parent / ".env"
if project_env.exists():
    load_dotenv(dotenv_path=project_env)
if local_env.exists():
    load_dotenv(dotenv_path=local_env, override=True)
if not project_env.exists() and not local_env.exists():
    load_dotenv(override=True)

MONGO_URI = os.getenv("MONGO_URI")
GOOGLE_API_KEY = os.getenv("GOOGLE_AI_STUDIO_API_KEY") or os.getenv("GOOGLE_API_KEY")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "gemini-3-flash-preview:cloud")
OLLAMA_FAST_CHAT_MODEL = os.getenv("OLLAMA_FAST_CHAT_MODEL")
if not OLLAMA_FAST_CHAT_MODEL:
    OLLAMA_FAST_CHAT_MODEL = "glm-4.7:cloud" if OLLAMA_CHAT_MODEL == "glm-5.2:cloud" else OLLAMA_CHAT_MODEL
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "https://ollama.com")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")
GOOGLE_EMBEDDING_MODEL = os.getenv("GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2")
