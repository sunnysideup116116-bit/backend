"""Instrumented in-memory Mongo stand-ins: bounded reads, durable references."""

from copy import deepcopy
from datetime import datetime, timezone
from math import ceil

from bson import ObjectId
import pytest

import database
from services.ayue_agent.v3 import place_references as places


class Cursor:
    def __init__(self, records, owner):
        self.records = records
        self.owner = owner
        self.closed = False
        self.consumed = 0
        self.order = None
        self.batch = None

    def sort(self, order):
        self.order = order
        for key, direction in reversed(order):
            self.records.sort(key=lambda row: row.get(key, 0), reverse=direction < 0)
        return self

    def batch_size(self, value):
        self.batch = value
        return self

    def __iter__(self):
        for record in self.records:
            self.consumed += 1
            yield deepcopy(record)

    def close(self):
        self.closed = True


def _matches(row, query):
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(row, part) for part in expected):
                return False
            continue
        if key == "$or":
            if not any(_matches(row, part) for part in expected):
                return False
            continue
        actual = row.get(key)
        if isinstance(expected, dict):
            if "$not" in expected and _matches(row, {key: expected["$not"]}):
                return False
            if "$type" in expected and (isinstance(actual, bool) or not isinstance(actual, (int, float))):
                return False
            if "$in" in expected and actual not in expected["$in"]:
                return False
            if "$nin" in expected and actual in expected["$nin"]:
                return False
            if "$ne" in expected and actual == expected["$ne"]:
                return False
            if "$exists" in expected and (key in row) != expected["$exists"]:
                return False
        elif actual != expected:
            return False
    return True


class SnapshotStore:
    def __init__(self, records):
        self.records = records
        self.queries = []
        self.cursors = []

    def find(self, query):
        self.queries.append(deepcopy(query))
        cursor = Cursor([row for row in self.records if _matches(row, query)], self)
        self.cursors.append(cursor)
        return cursor


class MessageStore:
    def __init__(self, records):
        self.records = records
        self.queries = []

    def find(self, query, projection):
        assert projection == {"_id": 1}
        self.queries.append(deepcopy(query))
        return [{"_id": row["_id"]} for row in self.records if _matches(row, query)]

    def find_one(self, *_args, **_kwargs):
        raise AssertionError("Snapshot source checks must be batched, not N+1")


def _snapshot(index, *, selected_at=None):
    reference = f"place_ref_{index:024x}"
    record = {
        "user_id": "owner", "room_id": "room", "origin_run_id": f"run-{index}",
        "created_at": float(index), "published": True,
        "source_message_id": f"{index:024x}",
        "candidates": [{
            "reference": reference, "ordinal": 1, "label": f"店 {index}",
            "provider": "google", "provider_place_id": f"provider-{index}",
            "category": "cafe", "address_summary": "高雄市",
        }],
    }
    if selected_at is not None:
        record.update(selected_reference=reference, selected_at=selected_at)
    return record


@pytest.fixture
def stores(monkeypatch):
    snapshots = SnapshotStore([_snapshot(index) for index in range(601)])
    messages = MessageStore([
        {"_id": ObjectId(f"{index:024x}"), "room_id": "room", "sender_id": "ai_assistant"}
        for index in range(601)
    ])
    monkeypatch.setenv("AYUE_TEST_MODE", "off")
    monkeypatch.setattr(places, "_collection", lambda: snapshots)
    monkeypatch.setattr(database, "messages_coll", messages)
    return snapshots, messages


def test_latest_snapshot_is_sorted_before_reading_and_does_not_scan_all_history(stores):
    snapshots, messages = stores

    latest = places.get_candidate_set("owner", "room")

    assert latest["origin_run_id"] == "run-600"
    assert snapshots.cursors[0].order == [("created_at", -1)]
    assert snapshots.cursors[0].batch == places._SNAPSHOT_BATCH_SIZE
    assert snapshots.cursors[0].consumed == places._SNAPSHOT_BATCH_SIZE
    assert snapshots.cursors[0].closed
    assert len(messages.queries) == 1
    assert len(messages.queries[0]["_id"]["$in"]) == places._SNAPSHOT_BATCH_SIZE


def test_blocked_or_deleted_sources_are_skipped_across_batches(stores):
    snapshots, messages = stores
    for row in messages.records[530:]:
        row["is_blocked"] = True
    messages.records.pop(529)

    latest = places.get_candidate_set("owner", "room")

    assert latest["origin_run_id"] == "run-528"
    assert len(messages.queries) == 2
    assert snapshots.cursors[0].consumed == places._SNAPSHOT_BATCH_SIZE * 2
    assert snapshots.cursors[0].closed


def test_turn_scope_reuses_latest_read_but_can_select_from_older_list(stores):
    snapshots, messages = stores
    snapshots.records[2].update(
        selected_reference=snapshots.records[2]["candidates"][0]["reference"], selected_at=1000.0,
    )

    with places.place_reference_read_scope():
        latest = places.get_candidate_set("owner", "room")
        selected = places.recent_selected_projection("owner", "room")

    assert latest["origin_run_id"] == "run-600"
    assert selected["label"] == "店 2"
    assert len(snapshots.queries) == 4  # latest/selected, each with an empty legacy-type probe
    assert len(messages.queries) == 2
    assert sum(cursor.consumed for cursor in snapshots.cursors) == places._SNAPSHOT_BATCH_SIZE + 1
    assert snapshots.cursors[2].order == [("selected_at", -1), ("created_at", -1)]
    assert all(cursor.closed for cursor in snapshots.cursors)


def test_new_recommendation_invalidates_old_bare_pronoun_until_explicit_reselection(stores):
    snapshots, _messages = stores
    old = snapshots.records[2]
    old.update(selected_reference=old["candidates"][0]["reference"], selected_at=500.0)

    with places.place_reference_read_scope():
        assert places.recent_selected_projection("owner", "room") is None
    old["selected_at"] = 1000.0
    with places.place_reference_read_scope():
        assert places.recent_selected_projection("owner", "room")["label"] == "店 2"


def test_source_cache_does_not_survive_next_turn(stores):
    _snapshots, messages = stores
    with places.place_reference_read_scope():
        assert places.get_candidate_set("owner", "room")["origin_run_id"] == "run-600"
    messages.records[-1]["is_blocked"] = True
    with places.place_reference_read_scope():
        assert places.get_candidate_set("owner", "room")["origin_run_id"] == "run-599"


def test_exact_historical_reference_beyond_old_500_threshold_remains_resolvable(stores):
    snapshots, messages = stores

    candidate = places.get_candidate("owner", "room", "place_ref_000000000000000000000000")

    assert candidate["label"] == "店 0"
    assert snapshots.cursors[0].consumed == 601
    assert len(messages.queries) == ceil(601 / places._SNAPSHOT_BATCH_SIZE)
    assert snapshots.cursors[0].closed
    assert all(len(query["_id"]["$in"]) <= places._SNAPSHOT_BATCH_SIZE for query in messages.queries)


def test_exact_old_origin_uses_scoped_query_without_replacing_history(stores):
    snapshots, messages = stores

    old = places.get_candidate_set("owner", "room", origin_run_id="run-0")

    assert old["candidates"][0]["label"] == "店 0"
    assert snapshots.queries[0]["origin_run_id"] == "run-0"
    assert snapshots.cursors[0].consumed == 1
    assert len(messages.queries) == 1


def test_batch_validation_keeps_room_sender_and_block_boundaries(stores):
    snapshots, messages = stores
    messages.records[-1]["room_id"] = "different-room"
    messages.records[-2]["sender_id"] = "owner"
    messages.records[-3]["is_blocked"] = True
    snapshots.records[-4]["source_message_id"] = "invalid-source-id"

    latest = places.get_candidate_set("owner", "room")

    assert latest["origin_run_id"] == "run-596"
    assert messages.queries[0]["room_id"] == "room"
    assert messages.queries[0]["sender_id"] == "ai_assistant"
    assert messages.queries[0]["is_blocked"] == {"$ne": True}


def test_source_store_error_closes_cursor_and_fails_closed(stores, monkeypatch):
    snapshots, messages = stores

    def unavailable(*_args):
        raise RuntimeError("mock source store outage")

    monkeypatch.setattr(messages, "find", unavailable)
    assert places.get_candidate_set("owner", "room") is None
    assert snapshots.cursors[0].closed


def test_unknown_selected_reference_does_not_hide_previous_valid_selection(stores):
    snapshots, _messages = stores
    snapshots.records[600].update(selected_reference="invalid", selected_at=2000)
    snapshots.records[2].update(
        selected_reference=snapshots.records[2]["candidates"][0]["reference"], selected_at=1000,
    )
    assert places.recent_selected_projection("owner", "room")["label"] == "店 2"


@pytest.mark.parametrize("legacy_timestamp, expected", [
    (datetime.fromtimestamp(1, timezone.utc), "run-600"),
    (datetime.fromtimestamp(700, timezone.utc), "run-0"),
    ("700", "run-0"),
    ("not-a-timestamp", "run-600"),
])
def test_mixed_legacy_timestamp_types_use_time_value_instead_of_bson_type(stores, legacy_timestamp, expected):
    snapshots, _messages = stores
    snapshots.records[0]["created_at"] = legacy_timestamp

    assert places.get_candidate_set("owner", "room")["origin_run_id"] == expected
    assert all(cursor.closed for cursor in snapshots.cursors)


def test_old_datetime_snapshot_can_be_reselected_after_new_numeric_recommendation(stores):
    snapshots, _messages = stores
    snapshots.records[2].update(
        created_at=datetime.fromtimestamp(2, timezone.utc),
        selected_at="1000",
        selected_reference=snapshots.records[2]["candidates"][0]["reference"],
    )
    with places.place_reference_read_scope():
        assert places.get_candidate_set("owner", "room")["origin_run_id"] == "run-600"
        assert places.recent_selected_projection("owner", "room")["label"] == "店 2"
