"""Back up and apply the 2026-08-31 safety contract to live Appwrite.

The migration is additive for schema. It creates the chat-log block/report
collections and attributes, adds ``action_options`` to the Appwrite KB, and
updates exactly three intervention templates. It never deletes documents.
Without ``--apply`` it only prints the resolved plan.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

from appwrite.client import Client
from appwrite.exception import AppwriteException
from appwrite.query import Query
from appwrite.services.databases import Databases


RISK_ROOT = Path(__file__).resolve().parents[1]
if str(RISK_ROOT) not in sys.path:
    sys.path.insert(0, str(RISK_ROOT))

from app.core.appwrite_config import configure_appwrite_client  # noqa: E402
from db_setup.add_block_report_collections import migration_schema  # noqa: E402
from db_setup.migrate_appwrite_schema import (  # noqa: E402
    ensure_schema,
    existing_collection_ids,
    verify_schema,
)
from db_setup.safety_action_contract import (  # noqa: E402
    ACTION_OPTIONS_ATTRIBUTE,
    SAFETY_ACTION_UPDATES,
)


KB_ACTION_SCHEMA = {
    "collections": [
        {
            "id": "kb_interventions",
            "name": "KB Interventions",
            "attributes": [ACTION_OPTIONS_ATTRIBUTE],
            "indexes": [],
        }
    ]
}


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    data = getattr(value, "data", None)
    converted = value.to_dict() if hasattr(value, "to_dict") else {}
    if isinstance(data, dict):
        metadata = {
            key: item for key, item in converted.items() if str(key).startswith("$")
        }
        return {**data, **metadata}
    return converted if isinstance(converted, dict) else {}


def _document_id(value: Any) -> str:
    data = _as_dict(value)
    return str(getattr(value, "id", None) or data.get("$id") or "")


def _find_kb_document(
    databases: Databases,
    database_id: str,
    template_id: str,
) -> Any | None:
    try:
        return databases.get_document(database_id, "kb_interventions", template_id)
    except AppwriteException as error:
        if getattr(error, "code", None) != 404:
            raise
    result = databases.list_documents(
        database_id,
        "kb_interventions",
        queries=[Query.equal("template_id", template_id), Query.limit(1)],
    )
    return result.documents[0] if result.documents else None


def apply_kb_action_updates(databases: Databases, database_id: str) -> dict[str, int]:
    counts = {"updated": 0, "unchanged": 0, "missing": 0}
    for template_id, expected in SAFETY_ACTION_UPDATES.items():
        document = _find_kb_document(databases, database_id, template_id)
        if document is None:
            counts["missing"] += 1
            continue
        data = _as_dict(document)
        patch = {
            key: value
            for key, value in expected.items()
            if data.get(key) != value
        }
        if not patch:
            counts["unchanged"] += 1
            continue
        databases.update_document(
            database_id=database_id,
            collection_id="kb_interventions",
            document_id=_document_id(document),
            data=patch,
        )
        counts["updated"] += 1
    if counts["missing"]:
        raise RuntimeError(
            f"missing {counts['missing']} required kb_interventions templates"
        )
    return counts


def verify_kb_action_updates(databases: Databases, database_id: str) -> list[str]:
    errors = verify_schema(databases, database_id, KB_ACTION_SCHEMA)
    for template_id, expected in SAFETY_ACTION_UPDATES.items():
        document = _find_kb_document(databases, database_id, template_id)
        if document is None:
            errors.append(f"kb_interventions: missing document {template_id}")
            continue
        data = _as_dict(document)
        for key, value in expected.items():
            if data.get(key) != value:
                errors.append(f"kb_interventions/{template_id}: unexpected {key}")
    return errors


def _schema_snapshot(
    databases: Databases,
    database_id: str,
    collection_ids: set[str],
) -> dict[str, Any]:
    available = existing_collection_ids(databases, database_id)
    result: dict[str, Any] = {}
    for collection_id in sorted(collection_ids & available):
        attributes = databases.list_attributes(
            database_id,
            collection_id,
            queries=[Query.limit(100)],
        ).attributes
        indexes = databases.list_indexes(
            database_id,
            collection_id,
            queries=[Query.limit(100)],
        ).indexes
        result[collection_id] = {
            "attributes": [_as_dict(item) for item in attributes],
            "indexes": [_as_dict(item) for item in indexes],
        }
    return result


def backup_current_state(
    databases: Databases,
    chat_database_id: str,
    kb_database_id: str,
    backup_root: Path,
) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_dir = backup_root / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)
    payload: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "chat_database_id": chat_database_id,
        "kb_database_id": kb_database_id,
        "partial": False,
        "warnings": [],
    }
    try:
        payload["chat_schema"] = _schema_snapshot(
            databases,
            chat_database_id,
            {"intervention_logs", "user_blocks", "user_reports"},
        )
    except Exception as error:
        payload["partial"] = True
        payload["warnings"].append(f"chat schema backup: {type(error).__name__}")
    try:
        payload["kb_schema"] = _schema_snapshot(
            databases,
            kb_database_id,
            {"kb_interventions"},
        )
        payload["kb_documents"] = {}
        for template_id in SAFETY_ACTION_UPDATES:
            document = _find_kb_document(databases, kb_database_id, template_id)
            payload["kb_documents"][template_id] = (
                _as_dict(document) if document is not None else None
            )
    except Exception as error:
        payload["partial"] = True
        payload["warnings"].append(f"KB backup: {type(error).__name__}")
    target = backup_dir / "appwrite_safety_backup.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--database-id", default=None)
    parser.add_argument("--kb-database-id", default=None)
    parser.add_argument(
        "--backup-root",
        type=Path,
        default=RISK_ROOT / "db_setup" / "backups",
    )
    args = parser.parse_args()

    client = Client()
    config = configure_appwrite_client(client)
    chat_database_id = args.database_id or config.db_id
    kb_database_id = args.kb_database_id or config.kb_db_id
    if not chat_database_id or not kb_database_id:
        parser.error("both chat and KB database ids are required")
    print(f"chat database: {chat_database_id}")
    print(f"KB database: {kb_database_id}")
    print("schema: receiver_report_text, user_blocks, user_reports, action_options")
    print("documents: update exactly three kb_interventions templates")
    if not args.apply:
        print("plan only; pass --apply to back up and migrate the current Appwrite")
        return 0

    databases = Databases(client)
    backup_path = backup_current_state(
        databases,
        chat_database_id,
        kb_database_id,
        args.backup_root,
    )
    print(f"backup: {backup_path}")

    ensure_schema(databases, chat_database_id, migration_schema())
    if "kb_interventions" not in existing_collection_ids(databases, kb_database_id):
        raise RuntimeError("kb_interventions collection is unavailable")
    ensure_schema(databases, kb_database_id, KB_ACTION_SCHEMA)
    counts = apply_kb_action_updates(databases, kb_database_id)

    errors = verify_schema(databases, chat_database_id, migration_schema())
    errors.extend(verify_kb_action_updates(databases, kb_database_id))
    if errors:
        for error in errors:
            print(f"verification error: {error}")
        return 1
    print(f"KB documents: {counts}")
    print("Appwrite safety migration verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
