"""Add the 2026-08-31 safety-action schema without deleting existing data.

The default mode only prints the plan. Pass ``--apply`` for an explicitly
authorized environment. Running the command repeatedly is safe because the
shared schema helper skips existing collections, attributes, and indexes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RISK_ROOT = Path(__file__).resolve().parents[1]
if str(RISK_ROOT) not in sys.path:
    sys.path.insert(0, str(RISK_ROOT))

from appwrite.client import Client
from appwrite.services.databases import Databases

from app.core.appwrite_config import configure_appwrite_client
from db_setup.migrate_appwrite_schema import ensure_schema, load_schema, verify_schema


TARGET_COLLECTIONS = {"intervention_logs", "user_blocks", "user_reports"}


def migration_schema() -> dict:
    schema = load_schema()
    return {
        "collections": [
            collection
            for collection in schema["collections"]
            if collection["id"] in TARGET_COLLECTIONS
        ]
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-id", default=None)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    client = Client()
    config = configure_appwrite_client(client)
    database_id = args.database_id or config.db_id
    schema = migration_schema()
    print(f"database: {database_id}")
    print("changes: receiver_report_text, user_blocks, user_reports")
    print("policy: additive only; no collection, attribute, index, or document is deleted")
    if not args.apply:
        print("plan only; pass --apply after taking a staging/production backup")
        return 0

    databases = Databases(client)
    ensure_schema(databases, database_id, schema)
    errors = verify_schema(databases, database_id, schema)
    if errors:
        for error in errors:
            print(f"verification error: {error}")
        return 1
    print("schema verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
