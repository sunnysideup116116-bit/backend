"""
KB Appwrite Schema 與種子資料建置腳本。

將原本存放於 MySQL/SQLite 的知識庫 (kb_configs / kb_features / kb_hard_blocks
/ kb_interventions / kb_prompts / kb_rules / kb_scenario_rules) 遷移至 Appwrite，
統一後端儲存層。執行方式：

    python db_setup/setup_kb_appwrite.py

會讀取 .env 的 Appwrite 設定，並從同目錄的 dating_safety.sql 解析 INSERT 語句，
將資料寫入新建的 KB database 與 collections。重複執行為 idempotent：已存在的
database / collection / document 會被跳過或更新。
"""
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

load_dotenv()

ENDPOINT = os.getenv("APPWRITE_ENDPOINT")
PROJECT_ID = os.getenv("APPWRITE_PROJECT_ID")
API_KEY = os.getenv("APPWRITE_API_KEY")
KB_DB_ID = os.getenv("APPWRITE_KB_DB_ID", "kb")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SQL_PATH = os.path.join(BASE_DIR, "db_setup", "dating_safety.sql")

HEADERS = {
    "X-Appwrite-Project": PROJECT_ID,
    "X-Appwrite-Key": API_KEY,
    "Content-Type": "application/json",
}

DEV_PERMISSIONS = [
    "read(\"any\")",
    "create(\"any\")",
    "update(\"any\")",
    "delete(\"any\")",
]

# ---------------------------------------------------------------------------
# Schema 定義：每個 collection 的 attributes 與 indexes。
# 盡量對應原 MySQL 表結構；JSON 欄位以 string(100000) 儲存原始 JSON 字串。
# ---------------------------------------------------------------------------
SCHEMA = {
    "kb_configs": {
        "name": "KB Configs",
        "attributes": [
            {"key": "config_id", "type": "string", "size": 50, "required": True},
            {"key": "config_name", "type": "string", "size": 100, "required": False},
            {"key": "thresholds", "type": "string", "size": 100000, "required": False},
            {"key": "weights", "type": "string", "size": 100000, "required": False},
            {"key": "decay_factor", "type": "double", "required": False, "default": 0.9},
            {"key": "context_window_size", "type": "integer", "required": False, "default": 5},
            {"key": "enabled", "type": "boolean", "required": False, "default": True},
            {"key": "applied_from", "type": "datetime", "required": False},
        ],
        "indexes": [],
        "pk": "config_id",
    },
    "kb_features": {
        "name": "KB Features",
        "attributes": [
            {"key": "feature_id", "type": "integer", "required": True},
            {"key": "feature_type", "type": "string", "size": 20, "required": False, "default": "REGEX"},
            {"key": "category", "type": "string", "size": 50, "required": True},
            {"key": "feature_name", "type": "string", "size": 100, "required": True},
            {"key": "description", "type": "string", "size": 100000, "required": False},
            {"key": "risk_delta", "type": "double", "required": False, "default": 0},
            {"key": "regex_pattern", "type": "string", "size": 100000, "required": False},
            {"key": "logic_config", "type": "string", "size": 100000, "required": False},
            {"key": "enabled", "type": "boolean", "required": False, "default": True},
        ],
        "indexes": [
            {"key": "idx_category_enabled", "type": "key", "attributes": ["category", "enabled"]},
            {"key": "idx_feature_id", "type": "key", "attributes": ["feature_id"]},
        ],
        "pk": "feature_id",
    },
    "kb_hard_blocks": {
        "name": "KB Hard Blocks",
        "attributes": [
            {"key": "id", "type": "integer", "required": True},
            {"key": "keyword", "type": "string", "size": 100, "required": True},
            {"key": "reason_label", "type": "string", "size": 50, "required": False, "default": "general_safety"},
            {"key": "trigger_mode", "type": "string", "size": 20, "required": False, "default": "flag"},
            {"key": "enabled", "type": "boolean", "required": False, "default": True},
            {"key": "created_at", "type": "datetime", "required": False},
        ],
        "indexes": [
            {"key": "idx_keyword_enabled", "type": "key", "attributes": ["keyword", "enabled"]},
        ],
        "pk": "id",
    },
    "kb_interventions": {
        "name": "KB Interventions",
        "attributes": [
            {"key": "template_id", "type": "string", "size": 50, "required": True},
            {"key": "risk_level", "type": "string", "size": 20, "required": False},
            {"key": "primary_risk_type", "type": "string", "size": 50, "required": False},
            {"key": "action_type", "type": "string", "size": 100, "required": False},
            {"key": "message_template", "type": "string", "size": 100000, "required": False},
            {"key": "ui_behavior", "type": "string", "size": 100000, "required": False},
            {"key": "created_at", "type": "datetime", "required": False},
        ],
        "indexes": [
            {"key": "idx_risk_level", "type": "key", "attributes": ["risk_level"]},
        ],
        "pk": "template_id",
    },
    "kb_prompts": {
        "name": "KB Prompts",
        "attributes": [
            {"key": "prompt_id", "type": "string", "size": 50, "required": True},
            {"key": "prompt_type", "type": "string", "size": 50, "required": False},
            {"key": "template", "type": "string", "size": 1000000, "required": False},
            {"key": "version", "type": "string", "size": 10, "required": False},
            {"key": "model", "type": "string", "size": 50, "required": False},
            {"key": "enabled", "type": "boolean", "required": False, "default": True},
            {"key": "created_at", "type": "datetime", "required": False},
        ],
        "indexes": [
            {"key": "idx_prompt_type", "type": "key", "attributes": ["prompt_type"]},
        ],
        "pk": "prompt_id",
    },
    "kb_rules": {
        "name": "KB Rules",
        "attributes": [
            {"key": "rule_id", "type": "string", "size": 50, "required": True},
            {"key": "rule_name", "type": "string", "size": 100, "required": False},
            {"key": "description", "type": "string", "size": 100000, "required": False},
            {"key": "conditions", "type": "string", "size": 100000, "required": False},
            {"key": "actions", "type": "string", "size": 100000, "required": False},
            {"key": "priority", "type": "integer", "required": False, "default": 1},
            {"key": "enabled", "type": "boolean", "required": False, "default": True},
            {"key": "created_at", "type": "datetime", "required": False},
            {"key": "updated_at", "type": "datetime", "required": False},
        ],
        "indexes": [
            {"key": "idx_priority", "type": "key", "attributes": ["priority"]},
            {"key": "idx_enabled", "type": "key", "attributes": ["enabled"]},
        ],
        "pk": "rule_id",
    },
    "kb_scenario_rules": {
        "name": "KB Scenario Rules",
        "attributes": [
            {"key": "scenario_id", "type": "integer", "required": True},
            {"key": "rule_name", "type": "string", "size": 100, "required": True},
            {"key": "description", "type": "string", "size": 100000, "required": False},
            {"key": "condition_logic", "type": "string", "size": 100000, "required": False},
            {"key": "bonus_actions", "type": "string", "size": 100000, "required": False},
            {"key": "enabled", "type": "boolean", "required": False, "default": True},
        ],
        "indexes": [
            {"key": "idx_scenario_enabled", "type": "key", "attributes": ["enabled"]},
        ],
        "pk": "scenario_id",
    },
}

# 對應 init_sqlite.py 的高擬真 regex patterns，seed 後補寫入 kb_features.regex_pattern
REGEX_MAP = {
    "身體部位詞彙": "(胸部|大腿|屁股|身材|胸圍|臀圍|下體|敏感帶|私密部位)",
    "性暗示語句": "(開房|打炮|做愛|約炮|一夜情|親親|抱抱|摸摸)",
    "要求裸照": "(傳照片|裸照|私密照|露點|不穿衣服|性感照|視訊照)",
    "邀請到私密空間": "(去我家|來我家|旅館|飯店|套房|私人房間|過夜|開房)",
    "直接詢問性經驗": "(處女|處男|第一次|性經驗|做過幾次)",
    "限時語句": "(快點|立刻|現在馬上|不要等|幾分鐘內|限時|只等你)",
    "二選一框架": "(要嘛...要嘛|不...就...|選一個|行不行|要不要)",
    "威脅式語氣": "(試試看|不准|敢拒絕|別後悔|後果自負|否則|看我怎麼收拾)",
    "條件交換": "(只要你...我就|答應我...才|聽話就|不聽話就不)",
    "外流威脅": "(公開|外流|散佈|傳給別人|放上網|公布對話)",
    "引導離開平台": "(加LINE|加賴|Line ID|IG|微信|WeChat|私聊)",
    "典型詐騙特徵": "(投資|理財|虛擬貨幣|保證獲利|匯款|借錢|資金週轉)",
    "重複傳訊": "(在嗎|哈囉|為什麼不回|人呢|哈囉|哈囉)",
    "連續追問": "(在幹嘛|回我|怎麼了|人去哪了|說話呀)",
    "辱罵詞彙": "(智障|白癡|賤人|廢物|垃圾|滾蛋|死三八|去死|幹你娘)",
    "人身攻擊": "(醜八怪|死肥豬|矮冬瓜|死窮鬼)",
    "強依附語句": "(沒有你我會死|只有你能救我|一定要陪我|不能離開我)",
    "自責語句": "(都是我的錯|我很爛|我不配|我真沒用|我是垃圾)",
    "孤單暗示": "(好孤單|寂寞|沒人陪|好冷清)",
    "被拋棄焦慮暗示": "(不要丟下我|別不理我|你會拋棄我嗎|別走)",
}


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------
def _post(url, payload, ok=(200, 201, 409)):
    r = requests.post(url, json=payload, headers=HEADERS)
    if r.status_code in ok:
        return r
    print(f"    [!] {r.status_code} {url} -> {r.text[:200]}")
    return r


def _get(url):
    return requests.get(url, headers=HEADERS)


def _create_database():
    print(f"[*] Creating KB database '{KB_DB_ID}' ...")
    _post(f"{ENDPOINT}/databases", {"databaseId": KB_DB_ID, "name": "Knowledge Base"})


def _wait_attributes(col_id):
    url = f"{ENDPOINT}/databases/{KB_DB_ID}/collections/{col_id}/attributes"
    start = time.time()
    while time.time() - start < 120:
        r = _get(url)
        if r.status_code == 200:
            pending = [a["key"] for a in r.json().get("attributes", []) if a.get("status") != "available"]
            if not pending:
                return
            print(f"    [-] pending attrs {pending}, waiting 2s ...")
        time.sleep(2)


def _create_collection(col_id, col_def):
    print(f"\n[*] Collection {col_id}")
    _post(
        f"{ENDPOINT}/databases/{KB_DB_ID}/collections",
        {"collectionId": col_id, "name": col_def["name"], "permissions": DEV_PERMISSIONS, "documentSecurity": False},
    )
    # existing attributes
    existing = set()
    r = _get(f"{ENDPOINT}/databases/{KB_DB_ID}/collections/{col_id}/attributes")
    if r.status_code == 200:
        existing = {a["key"] for a in r.json().get("attributes", [])}

    for attr in col_def["attributes"]:
        if attr["key"] in existing:
            continue
        payload = {"key": attr["key"], "required": bool(attr.get("required", False)), "array": False}
        default = attr.get("default", None)
        t = attr["type"]
        attr_url = f"{ENDPOINT}/databases/{KB_DB_ID}/collections/{col_id}/attributes/"
        if t == "string":
            size = attr.get("size", 255)
            payload["size"] = size
            payload["default"] = default
            _post(attr_url + "string", payload)
        elif t == "integer":
            payload["min"] = attr.get("min", -9223372036854775808)
            payload["max"] = attr.get("max", 9223372036854775807)
            payload["default"] = default
            _post(attr_url + "integer", payload)
        elif t == "double":
            payload["min"] = -1.7976931348623157e308
            payload["max"] = 1.7976931348623157e308
            payload["default"] = default
            _post(attr_url + "float", payload)
        elif t == "boolean":
            payload["default"] = bool(default) if default is not None else None
            _post(attr_url + "boolean", payload)
        elif t == "datetime":
            _post(attr_url + "datetime", payload)

    _wait_attributes(col_id)

    # indexes
    existing_idx = set()
    r = _get(f"{ENDPOINT}/databases/{KB_DB_ID}/collections/{col_id}/indexes")
    if r.status_code == 200:
        existing_idx = {i["key"] for i in r.json().get("indexes", [])}
    for idx in col_def.get("indexes", []):
        if idx["key"] in existing_idx:
            continue
        _post(
            f"{ENDPOINT}/databases/{KB_DB_ID}/collections/{col_id}/indexes",
            {"key": idx["key"], "type": idx["type"], "attributes": idx["attributes"], "orders": ["ASC"] * len(idx["attributes"])},
        )


# ---------------------------------------------------------------------------
# SQL INSERT parser
# ---------------------------------------------------------------------------
def _decode_mysql_string(s):
    """Decode MySQL single-quoted string body (without surrounding quotes),
    handling \\' \\\\ \\n \\t \\r \\0 etc."""
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "n":
                out.append("\n")
            elif n == "t":
                out.append("\t")
            elif n == "r":
                out.append("\r")
            elif n == "0":
                out.append("\0")
            elif n == "\\":
                out.append("\\")
            elif n == "'":
                out.append("'")
            elif n == '"':
                out.append('"')
            else:
                out.append(n)
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _parse_values_tuple(text, start):
    """Parse a single (...) tuple starting at index `start` (text[start]=='(').
    Returns (list_of_values, next_index)."""
    assert text[start] == "("
    i = start + 1
    n = len(text)
    values = []
    while i < n:
        # skip whitespace & commas
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break
        if text[i] == ")":
            i += 1
            break
        if text[i] == "'":
            # string literal
            i += 1
            buf = []
            while i < n:
                c = text[i]
                if c == "\\":
                    buf.append(c)
                    if i + 1 < n:
                        buf.append(text[i + 1])
                        i += 2
                        continue
                if c == "'":
                    # check for escaped '' or \'
                    if i + 1 < n and text[i + 1] == "'":
                        buf.append("'")
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(c)
                i += 1
            values.append(_decode_mysql_string("".join(buf)))
        else:
            # numeric / NULL / bareword until comma or )
            j = i
            while j < n and text[j] not in ",)":
                j += 1
            tok = text[i:j].strip()
            i = j
            if tok.upper() == "NULL":
                values.append(None)
            else:
                try:
                    if "." in tok:
                        values.append(float(tok))
                    else:
                        values.append(int(tok))
                except ValueError:
                    values.append(tok)
    return values, i


def parse_inserts(sql_text):
    """Yield (table, columns, [row_values...]) for each INSERT statement."""
    # find INSERT INTO `tbl` (`c1`,...) VALUES ...;
    pattern = re.compile(
        r"INSERT\s+INTO\s+`(\w+)`\s*\(([^)]*)\)\s*VALUES\s*",
        re.IGNORECASE,
    )
    for m in pattern.finditer(sql_text):
        table = m.group(1)
        cols = [c.strip().strip("`") for c in m.group(2).split(",")]
        # parse value tuples until ';'
        body_start = m.end()
        # find statement end (top-level ';'); respect strings
        end = _find_statement_end(sql_text, body_start)
        body = sql_text[body_start:end]
        rows = []
        i = 0
        n = len(body)
        while i < n:
            while i < n and body[i] in " \t\r\n":
                i += 1
            if i >= n or body[i] == ";":
                break
            if body[i] == "(":
                vals, i = _parse_values_tuple(body, i)
                rows.append(vals)
            else:
                i += 1
        yield table, cols, rows


def _find_statement_end(text, start):
    """Find the index of the ';' that ends this SQL statement, respecting
    single-quoted strings with backslash escapes."""
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\" and i + 1 < n:
            i += 2
            continue
        if c == "'":
            i += 1
            while i < n:
                if text[i] == "\\" and i + 1 < n:
                    i += 2
                    continue
                if text[i] == "'":
                    # escaped '' ?
                    if i + 1 < n and text[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == ";":
            return i
        i += 1
    return n


# ---------------------------------------------------------------------------
# Seed
# ---------------------------------------------------------------------------
def _convert_value(col, val):
    if val is None:
        return None
    if col in ("enabled",) and isinstance(val, int):
        return bool(val)
    return val


def _row_to_doc(cols, pk_col, row):
    doc = {}
    pk_val = None
    for col, val in zip(cols, row):
        v = _convert_value(col, val)
        if col == pk_col:
            pk_val = v
        # 保留所有欄位（含 PK）寫入 document data，Appwrite 的 $id 與屬性是分開的
        doc[col] = v
    return pk_val, doc


def _list_existing_documents(col_id):
    docs = {}
    limit = 100
    total = None
    offset = 0
    while True:
        r = _get(f"{ENDPOINT}/databases/{KB_DB_ID}/collections/{col_id}/documents?limit={limit}&offset={offset}")
        if r.status_code != 200:
            break
        data = r.json()
        for d in data.get("documents", []):
            docs[d["$id"]] = d
        if total is None:
            total = data.get("total", 0)
        offset += limit
        if offset >= (total or 0):
            break
    return docs


def seed_collection(col_id, col_def, table, cols, rows):
    pk_col = col_def["pk"]
    existing = _list_existing_documents(col_id)
    created = updated = 0
    for row in rows:
        pk_val, doc = _row_to_doc(cols, pk_col, row)
        if pk_val is None:
            continue
        doc_id = str(pk_val)
        if doc_id in existing:
            # update
            r = requests.patch(
                f"{ENDPOINT}/databases/{KB_DB_ID}/collections/{col_id}/documents/{doc_id}",
                json={"data": doc}, headers=HEADERS,
            )
            if r.status_code in (200, 201):
                updated += 1
            else:
                print(f"    [!] update {col_id}/{doc_id} -> {r.status_code} {r.text[:120]}")
        else:
            r = requests.post(
                f"{ENDPOINT}/databases/{KB_DB_ID}/collections/{col_id}/documents",
                json={"documentId": doc_id, "data": doc}, headers=HEADERS,
            )
            if r.status_code in (200, 201):
                created += 1
            else:
                print(f"    [!] create {col_id}/{doc_id} -> {r.status_code} {r.text[:120]}")
    print(f"    [+] {col_id}: created={created} updated={updated}")


def seed_regex_patterns():
    print("\n[*] Seeding high-fidelity regex patterns into kb_features ...")
    updated = 0
    for name, pattern in REGEX_MAP.items():
        # find the document by feature_name via list_documents query
        r = _get(
            f"{ENDPOINT}/databases/{KB_DB_ID}/collections/kb_features/documents"
            f"?queries[]={requests.utils.quote(json.dumps({'method':'equal','attribute':'feature_name','values':[name]}))}"
        )
        if r.status_code != 200:
            print(f"    [!] query {name} -> {r.status_code}")
            continue
        docs = r.json().get("documents", [])
        if not docs:
            print(f"    [-] feature {name} not found")
            continue
        d = docs[0]
        doc_id = d["$id"]
        rr = requests.patch(
            f"{ENDPOINT}/databases/{KB_DB_ID}/collections/kb_features/documents/{doc_id}",
            json={"data": {"regex_pattern": pattern}}, headers=HEADERS,
        )
        if rr.status_code in (200, 201):
            updated += 1
        else:
            print(f"    [!] update regex {name} -> {rr.status_code} {rr.text[:120]}")
    print(f"    [+] regex patterns updated: {updated}")


def main():
    if not (ENDPOINT and PROJECT_ID and API_KEY):
        print("[!] Missing Appwrite env vars (APPWRITE_ENDPOINT/PROJECT_ID/API_KEY)")
        sys.exit(1)

    print(f"[*] Appwrite endpoint: {ENDPOINT}")
    print(f"[*] KB database id: {KB_DB_ID}")
    print(f"[*] SQL source: {SQL_PATH}")

    # 1. database
    _create_database()

    # 2. collections + attributes + indexes
    for col_id, col_def in SCHEMA.items():
        _create_collection(col_id, col_def)

    # 3. seed
    with open(SQL_PATH, "r", encoding="utf-8") as f:
        sql_text = f.read()

    table_to_col_id = {
        "kb_configs": "kb_configs",
        "kb_features": "kb_features",
        "kb_hard_blocks": "kb_hard_blocks",
        "kb_interventions": "kb_interventions",
        "kb_prompts": "kb_prompts",
        "kb_rules": "kb_rules",
        "kb_scenario_rules": "kb_scenario_rules",
    }

    for table, cols, rows in parse_inserts(sql_text):
        col_id = table_to_col_id.get(table)
        if not col_id:
            continue
        print(f"\n[*] Seeding {table} ({len(rows)} rows)")
        seed_collection(col_id, SCHEMA[col_id], table, cols, rows)

    # 4. regex patterns
    seed_regex_patterns()

    print("\n[+] KB Appwrite setup completed.")


if __name__ == "__main__":
    main()