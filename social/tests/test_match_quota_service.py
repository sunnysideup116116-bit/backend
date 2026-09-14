"""Deterministic checks for the separate active/background allowances."""

from datetime import datetime
from types import SimpleNamespace

from pymongo.errors import DuplicateKeyError

from services import match_quota_service as quota


class _QuotaCollection:
    def __init__(self, *, operation=False):
        self.rows = []
        self.operation = operation

    def find_one(self, query, _projection=None):
        for row in self.rows:
            if all(row.get(key) == value for key, value in query.items()):
                return dict(row)
        return None

    def find(self, query, _projection=None):
        return [
            dict(row) for row in self.rows
            if all(
                row.get(key) in value["$in"] if isinstance(value, dict) and "$in" in value
                else row.get(key) == value
                for key, value in query.items()
            )
        ]

    def insert_one(self, row):
        if not self.operation and any(
            all(row.get(key) == old.get(key) for key in ("user_id", "bucket", "local_date"))
            for old in self.rows
        ):
            raise DuplicateKeyError("quota row")
        if any(
            all(row.get(key) == old.get(key) for key in ("user_id", "bucket", "local_date", "operation_key"))
            for old in self.rows
        ):
            raise DuplicateKeyError("operation")
        self.rows.append(dict(row))
        return SimpleNamespace()

    def update_one(self, query, update, **_kwargs):
        for row in self.rows:
            matches = True
            for key, value in query.items():
                actual = row.get(key)
                if isinstance(value, dict) and "$lt" in value:
                    matches = matches and actual < value["$lt"]
                elif isinstance(value, dict) and "$gt" in value:
                    matches = matches and actual > value["$gt"]
                else:
                    matches = matches and actual == value
            if matches:
                for key, value in (update.get("$inc") or {}).items():
                    row[key] = row.get(key, 0) + value
                return SimpleNamespace(modified_count=1)
        return SimpleNamespace(modified_count=0)

    def delete_one(self, query):
        for index, row in enumerate(self.rows):
            if all(row.get(key) == value for key, value in query.items()):
                self.rows.pop(index)
                return SimpleNamespace(deleted_count=1)
        return SimpleNamespace(deleted_count=0)


def test_active_and_background_buckets_are_independent(monkeypatch):
    usage, operations = _QuotaCollection(), _QuotaCollection(operation=True)
    monkeypatch.setattr(quota, "MATCH_QUOTA_USAGE", usage)
    monkeypatch.setattr(quota, "MATCH_QUOTA_OPERATIONS", operations)
    monkeypatch.setenv("AYUE_TEST_MODE", "off")
    now = datetime(2026, 9, 6, 12, 0, tzinfo=quota.QUOTA_TIMEZONE)

    for index in range(3):
        assert quota.reserve_daily_quota("owner", operation_key=f"active-{index}", now=now)["status"] == "reserved"
    assert quota.reserve_daily_quota("owner", operation_key="active-3", now=now)["status"] == "exhausted"
    assert quota.reserve_daily_quota("owner", bucket="background", operation_key="event-1", now=now)["status"] == "reserved"
    assert quota.reserve_daily_quota("owner", bucket="background", operation_key="event-2", now=now)["status"] == "exhausted"
