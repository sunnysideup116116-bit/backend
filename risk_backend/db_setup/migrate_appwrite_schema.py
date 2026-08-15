"""Create and populate a non-destructive Appwrite v2 database.

The existing database is never altered or deleted. Without ``--apply`` this
command only prints the migration plan. With ``--apply`` it creates (or resumes)
the target database, applies ``appwrite_schema_dump.json``, copies compatible
documents, and verifies the resulting schema.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import warnings
from pathlib import Path
from typing import Any

warnings.simplefilter("ignore", DeprecationWarning)

from appwrite.client import Client
from appwrite.exception import AppwriteException
from appwrite.query import Query
from appwrite.services.databases import Databases


RISK_ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = RISK_ROOT.parent
if str(RISK_ROOT) not in sys.path:
    sys.path.insert(0, str(RISK_ROOT))

from app.core.appwrite_config import configure_appwrite_client  # noqa: E402


SCHEMA_PATH = Path(__file__).with_name("appwrite_schema_dump.json")
DEFAULT_TARGET_DB = "chat_logs_v2_20260815"
SOURCE_ALIASES = {
    "risk_analysis_logs_": ("risk_analysis_logs_", "risk_analysis_logs"),
}

def _data(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    model_data = getattr(value, "data", None)
    if hasattr(value, "to_dict"):
        converted = value.to_dict()
        if isinstance(model_data, dict):
            metadata = {key: item for key, item in converted.items() if key.startswith("$")}
            return {**model_data, **metadata}
        return converted
    return dict(model_data or {})


def _id(value: Any) -> str:
    data = _data(value)
    return str(getattr(value, "id", None) or data.get("$id") or "")


def _key(value: Any) -> str:
    data = _data(value)
    return str(getattr(value, "key", None) or data.get("key") or "")


def _status(value: Any) -> str:
    status = getattr(value, "status", None) or _data(value).get("status") or ""
    return str(getattr(status, "value", status)).lower()


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def existing_collection_ids(db: Databases, database_id: str) -> set[str]:
    result = db.list_collections(database_id, queries=[Query.limit(100)])
    return {_id(item) for item in result.collections}


def ensure_database(db: Databases, database_id: str) -> None:
    try:
        db.client.call(
            "post",
            "/databases",
            {"content-type": "application/json"},
            {"databaseId": database_id, "name": f"Risk Chat Logs v2 ({database_id})"},
        )
        print(f"created database: {database_id}")
    except AppwriteException as error:
        if getattr(error, "code", None) != 409:
            raise
        print(f"resume existing database: {database_id}")


def _create_attribute(db: Databases, database_id: str, collection_id: str, attr: dict[str, Any]) -> None:
    params = {
        "key": attr["key"],
        "required": bool(attr.get("required", False)),
        "array": bool(attr.get("array", False)),
    }
    default = attr.get("default")
    if attr["type"] == "string":
        endpoint_type = "string"
        params.update(
            size=int(attr.get("size") or 255),
            default=default,
            encrypt=bool(attr.get("encrypt", False)),
        )
    elif attr["type"] == "integer":
        endpoint_type = "integer"
        params.update(min=attr.get("min"), max=attr.get("max"), default=default)
    elif attr["type"] == "double":
        endpoint_type = "float"
        params.update(min=attr.get("min"), max=attr.get("max"), default=default)
    elif attr["type"] == "boolean":
        endpoint_type = "boolean"
        params["default"] = default
    elif attr["type"] == "datetime":
        endpoint_type = "datetime"
        params["default"] = default
    else:
        raise ValueError(f"unsupported attribute type: {attr['type']}")
    db.client.call(
        "post",
        f"/databases/{database_id}/collections/{collection_id}/attributes/{endpoint_type}",
        {"content-type": "application/json"},
        params,
    )


def _wait_for_attributes(db: Databases, database_id: str, collection_id: str, timeout: float = 180) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = db.list_attributes(database_id, collection_id, queries=[Query.limit(100)])
        pending = [item for item in result.attributes if _status(item) != "available"]
        if not pending:
            return
        time.sleep(1)
    raise TimeoutError(f"attributes did not become available: {collection_id}")


def ensure_schema(db: Databases, target_db: str, schema: dict[str, Any]) -> None:
    collection_ids = existing_collection_ids(db, target_db)
    for collection in schema["collections"]:
        collection_id = collection["id"]
        if collection_id not in collection_ids:
            db.client.call(
                "post",
                f"/databases/{target_db}/collections",
                {"content-type": "application/json"},
                {
                    "collectionId": collection_id,
                    "name": collection["name"],
                    "permissions": [],
                    "documentSecurity": False,
                },
            )
            print(f"created collection: {collection_id}")

        current_attrs = {
            _key(item)
            for item in db.list_attributes(target_db, collection_id, queries=[Query.limit(100)]).attributes
        }
        for attr in collection["attributes"]:
            if attr["key"] not in current_attrs:
                _create_attribute(db, target_db, collection_id, attr)
                print(f"  created attribute: {attr['key']}")
        _wait_for_attributes(db, target_db, collection_id)

        current_indexes = {
            _key(item)
            for item in db.list_indexes(target_db, collection_id, queries=[Query.limit(100)]).indexes
        }
        for index in collection.get("indexes", []):
            if index["key"] in current_indexes:
                continue
            db.client.call(
                "post",
                f"/databases/{target_db}/collections/{collection_id}/indexes",
                {"content-type": "application/json"},
                {
                    "key": index["key"],
                    "type": index["type"],
                    "attributes": index["attributes"],
                    "orders": [item.upper() for item in index.get("orders", [])],
                },
            )
            print(f"  created index: {index['key']}")


def _source_collection(db: Databases, source_db: str, target_collection: str, available: set[str]) -> str | None:
    for candidate in SOURCE_ALIASES.get(target_collection, (target_collection,)):
        if candidate in available:
            return candidate
    return None


def _project_document(collection: dict[str, Any], source: Any) -> tuple[dict[str, Any] | None, str | None]:
    raw = _data(source)
    allowed = {attr["key"] for attr in collection["attributes"]}
    projected = {key: value for key, value in raw.items() if key in allowed and value is not None}
    collection_id = collection["id"]

    if collection_id == "messages":
        blocked = bool(projected.get("is_blocked", False))
        projected.setdefault("is_blocked", blocked)
        projected.setdefault("delivery_status", "blocked" if blocked else "delivered")
        projected.setdefault("reviewed_at", projected.get("timestamp") or raw.get("$updatedAt"))
        if projected["delivery_status"] == "delivered":
            projected.setdefault("delivered_at", projected.get("timestamp") or raw.get("$updatedAt"))
    elif collection_id == "conversations":
        projected.setdefault("last_activity", raw.get("$updatedAt") or raw.get("$createdAt"))
    elif collection_id == "temporal_features" and not projected.get("user_id"):
        return None, "legacy temporal feature has no user_id"

    missing = [
        attr["key"] for attr in collection["attributes"]
        if attr.get("required") and projected.get(attr["key"]) is None
    ]
    if missing:
        return None, f"missing required fields: {', '.join(missing)}"
    return projected, None


def migrate_documents(db: Databases, source_db: str, target_db: str, schema: dict[str, Any]) -> dict[str, dict[str, int]]:
    available = existing_collection_ids(db, source_db)
    report: dict[str, dict[str, int]] = {}
    for collection in schema["collections"]:
        target_collection = collection["id"]
        source_collection = _source_collection(db, source_db, target_collection, available)
        counts = {"copied": 0, "existing": 0, "skipped": 0, "failed": 0}
        report[target_collection] = counts
        if source_collection is None:
            print(f"skip unavailable source collection: {target_collection}")
            continue

        offset = 0
        while True:
            result = db.list_documents(
                source_db,
                source_collection,
                queries=[Query.limit(100), Query.offset(offset)],
            )
            documents = list(result.documents)
            if not documents:
                break
            for document in documents:
                document_id = _id(document)
                projected, reason = _project_document(collection, document)
                if projected is None:
                    counts["skipped"] += 1
                    print(f"  skipped {source_collection}/{document_id}: {reason}")
                    continue
                try:
                    db.client.call(
                        "post",
                        f"/databases/{target_db}/collections/{target_collection}/documents",
                        {"content-type": "application/json"},
                        {"documentId": document_id, "data": projected, "permissions": []},
                    )
                    counts["copied"] += 1
                except AppwriteException as error:
                    if getattr(error, "code", None) == 409:
                        counts["existing"] += 1
                    else:
                        counts["failed"] += 1
                        print(f"  failed {source_collection}/{document_id}: {error}")
            if len(documents) < 100:
                break
            offset += len(documents)
        print(f"migrated {source_collection} -> {target_collection}: {counts}")
    return report


def verify_schema(db: Databases, target_db: str, schema: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    live_collections = existing_collection_ids(db, target_db)
    for collection in schema["collections"]:
        collection_id = collection["id"]
        if collection_id not in live_collections:
            errors.append(f"missing collection: {collection_id}")
            continue
        expected_attrs = {item["key"] for item in collection["attributes"]}
        live_attrs = {
            _key(item)
            for item in db.list_attributes(target_db, collection_id, queries=[Query.limit(100)]).attributes
        }
        expected_indexes = {item["key"] for item in collection.get("indexes", [])}
        live_indexes = {
            _key(item)
            for item in db.list_indexes(target_db, collection_id, queries=[Query.limit(100)]).indexes
        }
        for key in sorted(expected_attrs - live_attrs):
            errors.append(f"{collection_id}: missing attribute {key}")
        for key in sorted(expected_indexes - live_indexes):
            errors.append(f"{collection_id}: missing index {key}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", default=None)
    parser.add_argument("--target-db", default=DEFAULT_TARGET_DB)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    client = Client()
    config = configure_appwrite_client(client)
    source_db = args.source_db or config.db_id
    if not source_db:
        parser.error("source database is required")
    if source_db == args.target_db:
        parser.error("target database must differ from source database")

    schema = load_schema()
    print(f"source database: {source_db}")
    print(f"target database: {args.target_db}")
    print("policy: source is read-only; no delete or update operations are used")
    if not args.apply:
        print("plan only; pass --apply to create and migrate the target database")
        return 0

    db = Databases(client)
    ensure_database(db, args.target_db)
    ensure_schema(db, args.target_db, schema)
    report = migrate_documents(db, source_db, args.target_db, schema)
    errors = verify_schema(db, args.target_db, schema)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        print("schema verification failed:")
        for error in errors:
            print(f"- {error}")
        return 2
    print("schema verification passed; source database was not modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
