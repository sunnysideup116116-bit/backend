"""Ensure the dating_db.chat_messages collection has the attributes the
social server mirror needs for Appwrite Realtime delivery.

The collection already exists (created 2026-07-01) with sender_id /
receiver_id / room_id / content / type / file_id / file_url / reaction /
reply_to / triggered_by_msg_id / is_unsent. This script adds the fields the
MongoDB -> Appwrite mirror writes, plus the room_id + timestamp index used by
the app's realtime filtering.

Usage:
    python scripts/setup_chat_messages_collection.py

Reads APPWRITE_ENDPOINT / APPWRITE_PROJECT_ID / APPWRITE_API_KEY from the
Server/.env file (same convention as risk_backend/db_setup scripts).
"""

import json
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

SERVER_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(dotenv_path=SERVER_ROOT / ".env", override=False)

ENDPOINT = (os.getenv("APPWRITE_ENDPOINT") or "http://appwrite.misproject.us.ci/v1").rstrip("/")
PROJECT_ID = os.getenv("APPWRITE_PROJECT_ID") or ""
API_KEY = os.getenv("APPWRITE_API_KEY") or ""
DB_ID = "dating_db"
COLLECTION_ID = "chat_messages"

if not PROJECT_ID or not API_KEY:
    print("[!] APPWRITE_PROJECT_ID / APPWRITE_API_KEY missing from Server/.env")
    sys.exit(1)

HEADERS = {
    "X-Appwrite-Project": PROJECT_ID,
    "X-Appwrite-Key": API_KEY,
    "Content-Type": "application/json",
}

# (key, type, required, extra) — extra is merged into the attribute payload.
TARGET_ATTRIBUTES = [
    ("timestamp", "integer", True, {"min": 0, "max": 9223372036854775807}),
    ("message_type", "string", False, {"size": 20, "default": "text"}),
    ("metadata", "string", False, {"size": 1000}),
    ("risk_level", "string", False, {"size": 20}),
    ("delivery_status", "string", False, {"size": 20}),
    ("is_blocked", "boolean", False, {"default": False}),
]

TARGET_INDEXES = [
    {
        "key": "idx_room_id_timestamp",
        "type": "key",
        "attributes": ["room_id", "timestamp"],
        "orders": ["ASC", "DESC"],
    },
]


def _attr_url(attr_type: str) -> str:
    return (
        f"{ENDPOINT}/databases/{DB_ID}/collections/{COLLECTION_ID}/attributes/{attr_type}"
    )


def _list_attributes() -> dict[str, dict]:
    res = requests.get(
        f"{ENDPOINT}/databases/{DB_ID}/collections/{COLLECTION_ID}/attributes",
        headers=HEADERS,
        timeout=15,
    )
    res.raise_for_status()
    return {item["key"]: item for item in res.json().get("attributes", [])}


def _list_indexes() -> dict[str, dict]:
    res = requests.get(
        f"{ENDPOINT}/databases/{DB_ID}/collections/{COLLECTION_ID}/indexes",
        headers=HEADERS,
        timeout=15,
    )
    res.raise_for_status()
    return {item["key"]: item for item in res.json().get("indexes", [])}


def _wait_available(keys: list[str], timeout: float = 120.0) -> None:
    start = time.time()
    while True:
        attrs = _list_attributes()
        pending = [k for k in keys if attrs.get(k, {}).get("status") != "available"]
        if not pending:
            return
        if time.time() - start > timeout:
            print(f"    [!] Timeout waiting for attributes: {pending}")
            return
        print(f"    [-] Still pending: {pending}. Waiting 2s...")
        time.sleep(2)


def main() -> int:
    print(f"[*] Ensuring {DB_ID}.{COLLECTION_ID} attributes...")
    existing = _list_attributes()

    created_keys: list[str] = []
    for key, attr_type, required, extra in TARGET_ATTRIBUTES:
        if key in existing:
            print(f"  [-] Attribute '{key}' already exists. Skipping.")
            continue
        payload = {"key": key, "required": required, "array": False, **extra}
        res = requests.post(_attr_url(attr_type), json=payload, headers=HEADERS, timeout=15)
        if res.status_code in (200, 201, 202):
            print(f"  [+] Created attribute '{key}' ({attr_type}).")
            created_keys.append(key)
        else:
            print(f"  [!] Error creating attribute {key}: {res.status_code} - {res.text}")

    if created_keys:
        _wait_available(created_keys)

    print("[*] Ensuring indexes...")
    existing_indexes = _list_indexes()
    for idx in TARGET_INDEXES:
        key = idx["key"]
        if key in existing_indexes:
            print(f"  [-] Index '{key}' already exists. Skipping.")
            continue
        res = requests.post(
            f"{ENDPOINT}/databases/{DB_ID}/collections/{COLLECTION_ID}/indexes",
            json=idx,
            headers=HEADERS,
            timeout=15,
        )
        if res.status_code in (200, 201, 202):
            print(f"  [+] Created index '{key}'.")
        else:
            print(f"  [!] Error creating index {key}: {res.status_code} - {res.text}")

    print("\n[+] chat_messages collection setup completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
