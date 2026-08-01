import os
from dotenv import load_dotenv

if os.getenv("AYUE_SKIP_DOTENV", "").strip().lower() not in {"1", "true", "on"}:
    load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI")
GOOGLE_API_KEY = os.getenv("GOOGLE_AI_STUDIO_API_KEY") or os.getenv("GOOGLE_API_KEY")
OLLAMA_CHAT_MODEL = os.getenv("OLLAMA_CHAT_MODEL", "deepseek-v4-flash:cloud")
OLLAMA_FAST_CHAT_MODEL = os.getenv("OLLAMA_FAST_CHAT_MODEL")
if not OLLAMA_FAST_CHAT_MODEL:
    OLLAMA_FAST_CHAT_MODEL = "glm-4.7:cloud" if OLLAMA_CHAT_MODEL == "glm-5.2:cloud" else OLLAMA_CHAT_MODEL
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "https://ollama.com")
OLLAMA_API_KEY = os.getenv("OLLAMA_API_KEY")
GOOGLE_EMBEDDING_MODEL = os.getenv("GOOGLE_EMBEDDING_MODEL", "models/gemini-embedding-2")
TAVILY_API_KEY = os.getenv("TAVILY_API_KEY")
TAVILY_PROJECT = os.getenv("TAVILY_PROJECT")
GIPHY_API_KEY = os.getenv("GIPHY_API_KEY")
GIPHY_GIF_ENABLED = os.getenv("GIPHY_GIF_ENABLED", "on").strip().lower() == "on"
AYUE_MAPS_ENABLED = os.getenv("AYUE_MAPS_ENABLED", "on").strip().lower() == "on"
OSM_NOMINATIM_URL = os.getenv("OSM_NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OSM_OVERPASS_URL = os.getenv("OSM_OVERPASS_URL", "https://overpass-api.de/api/interpreter")
OSM_OVERPASS_FALLBACK_URL = os.getenv("OSM_OVERPASS_FALLBACK_URL", "https://overpass.kumi.systems/api/interpreter")
OSM_USER_AGENT = os.getenv("OSM_USER_AGENT", "AyueDatingDemo/1.0 (educational demo)")
# MongoDB Atlas can be unavailable during local demos. Map results remain safe
# with the process-local TTL cache; enable this only when persistent cache
# writes are wanted and Atlas is healthy.
AYUE_MAPS_MONGO_CACHE = os.getenv("AYUE_MAPS_MONGO_CACHE", "off").strip().lower() == "on"
GOOGLE_PLACES_SERVER_API_KEY = os.getenv("GOOGLE_PLACES_SERVER_API_KEY", "").strip()
GOOGLE_MAPS_BROWSER_API_KEY = os.getenv("GOOGLE_MAPS_BROWSER_API_KEY", "").strip()
AYUE_GOOGLE_PLACE_CARDS_ENABLED = os.getenv("AYUE_GOOGLE_PLACE_CARDS_ENABLED", "off").strip().lower() == "on"
