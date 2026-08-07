"""Small Mongo-like store used only when AYUE_TEST_MODE is explicitly enabled."""

from __future__ import annotations

import copy
import threading
from types import SimpleNamespace
from typing import Any


class MemoryCollection:
    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def create_index(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    def insert_one(self, document: dict[str, Any]) -> SimpleNamespace:
        with self._lock:
            key = str(document.get("_id") or document.get("run_id") or len(self._docs))
            self._docs[key] = copy.deepcopy(document)
        return SimpleNamespace(inserted_id=key)

    @staticmethod
    def _matches(document: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, expected in query.items():
            actual = document.get(key)
            if isinstance(expected, dict):
                if "$gt" in expected and not (actual is not None and actual > expected["$gt"]):
                    return False
                if "$in" in expected and actual not in expected["$in"]:
                    return False
            elif actual != expected:
                return False
        return True

    def find(self, query: dict[str, Any]) -> list[dict[str, Any]]:
        with self._lock:
            return [
                copy.deepcopy(document)
                for document in self._docs.values()
                if self._matches(document, query)
            ]

    @staticmethod
    def _apply_update(document: dict[str, Any], update: dict[str, Any]) -> None:
        document.update(copy.deepcopy(update.get("$set") or {}))
        for key, value in (update.get("$push") or {}).items():
            document.setdefault(key, []).append(copy.deepcopy(value))

    def update_one(self, query: dict[str, Any], update: dict[str, Any]) -> SimpleNamespace:
        with self._lock:
            for document in self._docs.values():
                if self._matches(document, query):
                    self._apply_update(document, update)
                    return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> SimpleNamespace:
        modified = 0
        with self._lock:
            for document in self._docs.values():
                if self._matches(document, query):
                    self._apply_update(document, update)
                    modified += 1
        return SimpleNamespace(modified_count=modified)
