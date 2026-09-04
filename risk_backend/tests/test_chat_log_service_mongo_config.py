"""MongoDB fallback credentials must come from the environment."""

from types import SimpleNamespace

import pymongo

from app.services import chat_log_service as chat_log_module


def _isolate_non_mongo_dependencies(monkeypatch):
    monkeypatch.setattr(chat_log_module, "Client", lambda: object())
    monkeypatch.setattr(chat_log_module, "Databases", lambda _client: object())
    monkeypatch.setattr(
        chat_log_module,
        "configure_appwrite_client",
        lambda _client: SimpleNamespace(db_id="test-db"),
    )
    monkeypatch.setattr(chat_log_module, "RelationshipService", lambda: object())


def test_mongo_fallback_uses_environment_credentials(monkeypatch):
    _isolate_non_mongo_dependencies(monkeypatch)
    monkeypatch.setenv("MONGO_URI", "mongodb://example.invalid/test")
    monkeypatch.setenv("MONGO_DB_NAME", "test-db-name")

    collection = object()
    database = {"risk_state_history": collection}
    captured = {}

    class FakeMongoClient:
        def __init__(self, uri, **kwargs):
            captured["uri"] = uri
            captured["kwargs"] = kwargs

        def __getitem__(self, name):
            captured["db_name"] = name
            return database

    monkeypatch.setattr(pymongo, "MongoClient", FakeMongoClient)

    service = chat_log_module.ChatLogService()

    assert captured["uri"] == "mongodb://example.invalid/test"
    assert captured["db_name"] == "test-db-name"
    assert captured["kwargs"]["serverSelectionTimeoutMS"] == 2000
    assert service.mongo_state_coll is collection


def test_missing_mongo_uri_disables_fallback(monkeypatch):
    _isolate_non_mongo_dependencies(monkeypatch)
    monkeypatch.delenv("MONGO_URI", raising=False)

    captured = {"called": False}

    class FakeMongoClient:
        def __init__(self, *_args, **_kwargs):
            captured["called"] = True

        def __getitem__(self, _name):
            return {"risk_state_history": object()}

    monkeypatch.setattr(pymongo, "MongoClient", FakeMongoClient)

    service = chat_log_module.ChatLogService()

    assert captured["called"] is False
    assert service.mongo_state_coll is None
