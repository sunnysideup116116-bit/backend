from pymongo import MongoClient
from config import MONGO_DB_NAME, MONGO_URI

# ----------------- DB Initialization -----------------
MONGO_INIT_ERROR = None


class _UnavailableCollection:
    def __init__(self, error):
        self._error = error

    def __getattr__(self, _name):
        def unavailable(*_args, **_kwargs):
            raise self._error
        return unavailable


class _UnavailableDatabase:
    name = MONGO_DB_NAME

    def __init__(self, error):
        self._error = error

    def __getitem__(self, _name):
        return _UnavailableCollection(self._error)

    def __getattr__(self, _name):
        def unavailable(*_args, **_kwargs):
            raise self._error
        return unavailable


try:
    mongo_client = MongoClient(MONGO_URI)
    db = mongo_client[MONGO_DB_NAME]
except Exception as exc:
    # SRV DNS failures must not prevent the HTTP service from starting. The
    # unavailable proxy fails closed on every DB operation and lets health /
    # Demo status report the dependency failure instead of touching localhost.
    MONGO_INIT_ERROR = exc
    mongo_client = None
    db = _UnavailableDatabase(exc)
profiles_coll = db["profiles"]
matches_coll = db["matches"]
messages_coll = db["messages"]
semantic_plans_coll = db["semantic_plans"]
calendar_events_coll = db["calendar_events"]
ai_rooms_coll = db["ai_rooms"]
