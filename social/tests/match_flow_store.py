"""Small atomic Mongo subset for cross-service match tests, never production."""
from copy import deepcopy
from threading import RLock
from types import SimpleNamespace

from bson import ObjectId
from pymongo.errors import DuplicateKeyError


def value(document, path):
    for key in path.split("."):
        if not isinstance(document, dict) or key not in document:
            return None
        document = document[key]
    return document


def matches(document, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(matches(document, item) for item in expected):
                return False
            continue
        if key == "$or":
            if not any(matches(document, item) for item in expected):
                return False
            continue
        actual = value(document, key)
        if isinstance(expected, dict) and any(k.startswith("$") for k in expected):
            for op, wanted in expected.items():
                if op == "$ne" and actual == wanted:
                    return False
                if op == "$in" and actual not in wanted:
                    return False
                if op == "$exists" and (actual is not None) != wanted:
                    return False
                if op in {"$gt", "$gte", "$lt", "$lte"}:
                    if actual is None:
                        return False
                    if not {"$gt": actual > wanted, "$gte": actual >= wanted,
                            "$lt": actual < wanted, "$lte": actual <= wanted}[op]:
                        return False
        elif isinstance(actual, list) and not isinstance(expected, list):
            if expected not in actual:
                return False
        elif actual != expected:
            return False
    return True


def assign(document, path, val, *, unset=False):
    keys = path.split(".")
    for key in keys[:-1]:
        document = document.setdefault(key, {})
    if unset:
        document.pop(keys[-1], None)
    else:
        document[keys[-1]] = deepcopy(val)


class Cursor(list):
    def sort(self, keys, direction=None):
        fields = [(keys, direction)] if isinstance(keys, str) else keys
        for key, order in reversed(fields):
            super().sort(key=lambda row: value(row, key) or 0, reverse=order < 0)
        return self

    def limit(self, count):
        return Cursor(self[:count])


class Collection:
    def __init__(self, rows=()):
        self.rows = deepcopy(list(rows))
        self.lock = RLock()
        self.writes = 0

    def find(self, query=None, projection=None):
        with self.lock:
            # Return full documents: tests assert public projections separately.
            return Cursor(deepcopy([row for row in self.rows if matches(row, query or {})]))

    def find_one(self, query=None, projection=None, sort=None):
        rows = self.find(query, projection)
        if sort:
            rows.sort(sort)
        return rows[0] if rows else None

    def count_documents(self, query, **_kwargs):
        return len(self.find(query))

    def insert_one(self, document):
        with self.lock:
            document = deepcopy(document)
            document.setdefault("_id", ObjectId())
            if any(row.get("_id") == document["_id"] for row in self.rows):
                raise DuplicateKeyError("duplicate id")
            if document.get("active_user_id") and any(row.get("active_user_id") == document["active_user_id"] for row in self.rows):
                raise DuplicateKeyError("duplicate active user")
            self.rows.append(document)
            self.writes += 1
            return SimpleNamespace(inserted_id=document["_id"])

    def _apply(self, row, update, inserted=False):
        for key, val in update.get("$set", {}).items():
            assign(row, key, val)
        if inserted:
            for key, val in update.get("$setOnInsert", {}).items():
                assign(row, key, val)
        for key in update.get("$unset", {}):
            assign(row, key, None, unset=True)
        for key, val in update.get("$inc", {}).items():
            assign(row, key, (value(row, key) or 0) + val)
        for key, val in update.get("$push", {}).items():
            assign(row, key, [*(value(row, key) or []), val])

    def update_one(self, query, update, upsert=False):
        with self.lock:
            for row in self.rows:
                if matches(row, query):
                    self._apply(row, update)
                    self.writes += 1
                    return SimpleNamespace(modified_count=1)
            if upsert:
                row = deepcopy(query)
                self._apply(row, update, inserted=True)
                self.insert_one(row)
            return SimpleNamespace(modified_count=0)

    def update_many(self, query, update):
        with self.lock:
            selected = [row for row in self.rows if matches(row, query)]
            for row in selected:
                self._apply(row, update)
            self.writes += len(selected)
            return SimpleNamespace(modified_count=len(selected))

    def find_one_and_update(self, query, update, upsert=False, return_document=False, **_kwargs):
        with self.lock:
            before = self.find_one(query)
            self.update_one(query, update, upsert=upsert)
            return self.find_one({"_id": before["_id"]}) if before and return_document else before

    def create_index(self, *_args, **_kwargs):
        return "test-index"
