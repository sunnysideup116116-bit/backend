from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from appwrite.client import Client
from appwrite.exception import AppwriteException
from appwrite.query import Query
from appwrite.services.databases import Databases

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app_voice_assistant.memory import VoiceMemorySettings  # noqa: E402


SCHEMA = {
    "database_name": "Voice Memory",
    "collection_name": "Voice Session Memories",
    "attributes": [
        {"type": "string", "key": "user_id", "size": 128, "required": True},
        {"type": "string", "key": "username", "size": 80, "required": True},
        {"type": "string", "key": "older_summary", "size": 70, "required": False, "default": ""},
        {"type": "string", "key": "recent_summary", "size": 120, "required": False, "default": ""},
        {"type": "integer", "key": "revision", "required": False, "default": 0, "min": 0},
        {"type": "string", "key": "last_session_id", "size": 64, "required": False, "default": ""},
    ],
    "indexes": [
        {"key": "unique_user_id", "type": "unique", "attributes": ["user_id"], "orders": ["ASC"]},
    ],
}


def _data(value):
    if isinstance(value, dict):
        return value
    converted = value.to_dict() if hasattr(value, "to_dict") else {}
    return converted if isinstance(converted, dict) else {}


def _key(value) -> str:
    return str(getattr(value, "key", None) or _data(value).get("key") or "")


def _status(value) -> str:
    raw = getattr(value, "status", None) or _data(value).get("status") or ""
    return str(getattr(raw, "value", raw)).lower()


def _ensure_database(client: Client, database_id: str, name: str) -> None:
    try:
        client.call(
            "post", "/databases", {"content-type": "application/json"},
            {"databaseId": database_id, "name": name, "enabled": True},
        )
    except AppwriteException as error:
        if getattr(error, "code", None) != 409:
            raise


def _ensure_collection(db: Databases, database_id: str, collection_id: str) -> None:
    existing = {
        str(getattr(item, "id", None) or _data(item).get("$id") or "")
        for item in db.list_collections(database_id, queries=[Query.limit(100)]).collections
    }
    if collection_id in existing:
        return
    db.client.call(
        "post", f"/databases/{database_id}/collections",
        {"content-type": "application/json"},
        {
            "collectionId": collection_id,
            "name": SCHEMA["collection_name"],
            "permissions": [],
            "documentSecurity": False,
            "enabled": True,
        },
    )


def _create_attribute(db: Databases, database_id: str, collection_id: str, spec: dict) -> None:
    params = {
        "key": spec["key"],
        "required": spec["required"],
        "array": False,
        "default": spec.get("default"),
    }
    kind = spec["type"]
    if kind == "string":
        params.update(size=spec["size"], encrypt=False)
    else:
        params.update(min=spec.get("min"), max=spec.get("max"))
    db.client.call(
        "post",
        f"/databases/{database_id}/collections/{collection_id}/attributes/{kind}",
        {"content-type": "application/json"}, params,
    )


def ensure_schema(db: Databases, database_id: str, collection_id: str) -> None:
    _ensure_collection(db, database_id, collection_id)
    current = {
        _key(item)
        for item in db.list_attributes(database_id, collection_id, queries=[Query.limit(100)]).attributes
    }
    for spec in SCHEMA["attributes"]:
        if spec["key"] not in current:
            _create_attribute(db, database_id, collection_id, spec)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        attributes = db.list_attributes(
            database_id, collection_id, queries=[Query.limit(100)],
        ).attributes
        if len(attributes) >= len(SCHEMA["attributes"]) and all(
            _status(item) == "available" for item in attributes
        ):
            break
        time.sleep(1)
    else:
        raise TimeoutError("voice memory attributes did not become available")
    current_indexes = {
        _key(item)
        for item in db.list_indexes(database_id, collection_id, queries=[Query.limit(100)]).indexes
    }
    for spec in SCHEMA["indexes"]:
        if spec["key"] in current_indexes:
            continue
        db.client.call(
            "post", f"/databases/{database_id}/collections/{collection_id}/indexes",
            {"content-type": "application/json"}, spec,
        )


def verify_schema(db: Databases, database_id: str, collection_id: str) -> list[str]:
    attributes = {
        _key(item): _data(item)
        for item in db.list_attributes(database_id, collection_id, queries=[Query.limit(100)]).attributes
    }
    indexes = {
        _key(item): _data(item)
        for item in db.list_indexes(database_id, collection_id, queries=[Query.limit(100)]).indexes
    }
    errors = []
    for spec in SCHEMA["attributes"]:
        actual = attributes.get(spec["key"])
        if actual is None:
            errors.append(f"missing attribute: {spec['key']}")
        elif str(actual.get("status") or "").lower() != "available":
            errors.append(f"attribute not available: {spec['key']}")
    for spec in SCHEMA["indexes"]:
        if spec["key"] not in indexes:
            errors.append(f"missing index: {spec['key']}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or verify the private Appwrite voice-memory database.")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    settings = VoiceMemorySettings.from_env()
    print(f"endpoint: {settings.endpoint}")
    print(f"database: {settings.memory_database_id}")
    print(f"collection: {settings.memory_collection_id}")
    if not args.apply:
        print("plan only; pass --apply to create the additive schema")
        return 0
    client = Client()
    client.set_endpoint(settings.endpoint)
    if not settings.verify_tls:
        client.set_self_signed(status=True)
    client.set_project(settings.project_id)
    client.set_key(settings.api_key)
    db = Databases(client)
    _ensure_database(client, settings.memory_database_id, SCHEMA["database_name"])
    ensure_schema(db, settings.memory_database_id, settings.memory_collection_id)
    errors = verify_schema(db, settings.memory_database_id, settings.memory_collection_id)
    if errors:
        for error in errors:
            print(f"error: {error}")
        return 1
    print("voice memory Appwrite schema is ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
