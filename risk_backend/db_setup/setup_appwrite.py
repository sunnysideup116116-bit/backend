import json
import time
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

SERVER_ENV = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(dotenv_path=SERVER_ENV, override=False)

ENDPOINT = "http://127.0.0.1/v1"
PROJECT_ID = "6a44de590010fa46afbd"
API_KEY = (os.getenv("APPWRITE_API_KEY") or "").strip()
DB_ID = "chat_logs"

if not API_KEY:
    raise RuntimeError("APPWRITE_API_KEY is missing from Server/.env")

headers = {
    "X-Appwrite-Project": PROJECT_ID,
    "X-Appwrite-Key": API_KEY,
    "Content-Type": "application/json"
}

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
schema_path = os.path.join(BASE_DIR, "db_setup", "appwrite_schema_dump.json")
print(f"[*] Reading schema dump from: {schema_path}")

with open(schema_path, "r", encoding="utf-8") as f:
    schema = json.load(f)

# 1. Create Database
db_url = f"{ENDPOINT}/databases"
db_payload = {"databaseId": DB_ID, "name": "Chat Logs Database"}
res = requests.post(db_url, json=db_payload, headers=headers)
if res.status_code in [201, 200]:
    print(f"[+] Database {DB_ID} created successfully.")
elif res.status_code == 409:
    print(f"[!] Database {DB_ID} already exists.")
else:
    print(f"[!] Error creating database: {res.status_code} - {res.text}")

# Permissions for dev environment
dev_permissions = [
    "read(\"any\")",
    "create(\"any\")",
    "update(\"any\")",
    "delete(\"any\")"
]

# 2. Process Collections
for col in schema["collections"]:
    col_id = col["id"]
    col_name = col["name"]
    print(f"\n[*] Processing collection: {col_name} ({col_id})...")
    
    col_url = f"{ENDPOINT}/databases/{DB_ID}/collections"
    col_payload = {
        "collectionId": col_id,
        "name": col_name,
        "permissions": dev_permissions,
        "documentSecurity": False
    }
    
    res = requests.post(col_url, json=col_payload, headers=headers)
    if res.status_code in [201, 200]:
        print(f"[+] Collection {col_id} created successfully.")
    elif res.status_code == 409:
        print(f"[!] Collection {col_id} already exists.")
    else:
        print(f"[!] Error creating collection: {res.status_code} - {res.text}")
        continue

    # Get existing attributes to avoid duplicates
    existing_attrs = []
    attr_list_url = f"{ENDPOINT}/databases/{DB_ID}/collections/{col_id}/attributes"
    res = requests.get(attr_list_url, headers=headers)
    if res.status_code == 200:
        existing_attrs = [a["key"] for a in res.json().get("attributes", [])]

    # Create attributes
    for attr in col["attributes"]:
        key = attr["key"]
        if key in existing_attrs:
            print(f"  [-] Attribute '{key}' already exists. Skipping.")
            continue
            
        attr_type = attr["type"]
        required = attr.get("required", False)
        default = attr.get("default", None)
        array = attr.get("array", False)
        
        if default == "" or default == "null" or default is None:
            default = None

        print(f"  [*] Creating attribute '{key}' ({attr_type})...")
        
        attr_url = f"{ENDPOINT}/databases/{DB_ID}/collections/{col_id}/attributes/"
        payload = {
            "key": key,
            "required": required,
            "array": array
        }
        
        if attr_type == "string":
            size = attr.get("size", 255)
            if size is None or size <= 0:
                size = 255
            payload["size"] = size
            payload["default"] = default
            url = attr_url + "string"
        elif attr_type == "integer":
            payload["min"] = attr.get("min", -9223372036854775808)
            payload["max"] = attr.get("max", 9223372036854775807)
            payload["default"] = default
            url = attr_url + "integer"
        elif attr_type == "double":
            payload["min"] = attr.get("min", -1.7976931348623157e+308)
            payload["max"] = attr.get("max", 1.7976931348623157e+308)
            payload["default"] = default
            url = attr_url + "float"
        elif attr_type == "boolean":
            bool_default = None
            if default is not None:
                bool_default = bool(default)
            payload["default"] = bool_default
            url = attr_url + "boolean"
        elif attr_type == "datetime":
            url = attr_url + "datetime"
            
        res = requests.post(url, json=payload, headers=headers)
        if res.status_code in [201, 200]:
            print(f"    [+] Created '{key}' attribute.")
        else:
            print(f"    [!] Error creating attribute {key}: {res.status_code} - {res.text}")

    # Wait for attributes to be active/available
    print("  [*] Waiting for attributes to be available...")
    start_time = time.time()
    while True:
        res = requests.get(attr_list_url, headers=headers)
        if res.status_code == 200:
            attrs = res.json().get("attributes", [])
            pending = [a["key"] for a in attrs if a["status"] != "available"]
            if not pending:
                print("    [+] All attributes are now available.")
                break
            if time.time() - start_time > 120:
                print(f"    [!] Timeout waiting for attributes. Still pending: {pending}")
                break
            print(f"    [-] Still pending attributes: {pending}. Waiting 2 seconds...")
            time.sleep(2)
        else:
            print(f"    [!] Error checking attribute status: {res.status_code}")
            time.sleep(2)

    # Get existing indexes
    existing_indexes = []
    index_list_url = f"{ENDPOINT}/databases/{DB_ID}/collections/{col_id}/indexes"
    res = requests.get(index_list_url, headers=headers)
    if res.status_code == 200:
        existing_indexes = [i["key"] for i in res.json().get("indexes", [])]

    # Create indexes
    for idx in col.get("indexes", []):
        key = idx["key"]
        if key in existing_indexes:
            print(f"  [-] Index '{key}' already exists. Skipping.")
            continue
            
        idx_type = idx["type"]
        attributes = idx["attributes"]
        orders = idx.get("orders", ["ASC"] * len(attributes))
        
        print(f"  [*] Creating index '{key}' ({idx_type}) on {attributes}...")
        idx_url = f"{ENDPOINT}/databases/{DB_ID}/collections/{col_id}/indexes"
        idx_payload = {
            "key": key,
            "type": idx_type,
            "attributes": attributes,
            "orders": orders
        }
        res = requests.post(idx_url, json=idx_payload, headers=headers)
        if res.status_code in [201, 200]:
            print(f"    [+] Created index '{key}'.")
        else:
            print(f"    [!] Error creating index {key}: {res.status_code} - {res.text}")

print("\n[+] Appwrite Schema Setup Completed successfully!")
