"""Unfinished public DAG state retires without touching Pi or private work."""

import importlib.util
from pathlib import Path

import mongomock


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "retire_public_dag_state.py"
SPEC = importlib.util.spec_from_file_location("retire_public_dag_state", SCRIPT)
retirement = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(retirement)


def _seed(database):
    database.v3_pending_confirmations.insert_many([
        {"_id": "dag", "surface": "public_ayue", "source_engine": "dag", "status": "pending"},
        {"_id": "pi", "surface": "public_ayue", "source_engine": "pi", "status": "pending"},
        {"_id": "private", "surface": "private_ayue", "source_engine": "dag", "status": "pending"},
    ])
    database.v3_contact_selections.insert_one(
        {"_id": "selection", "surface": "public_ayue", "source_engine": "dag", "status": "prepared"},
    )
    database.v3_operation_batches.insert_one(
        {"_id": "batch", "surface": "public_ayue", "source_engine": "dag", "status": "active"},
    )


def test_apply_is_idempotent_and_preserves_pi_and_private(tmp_path, monkeypatch):
    database = mongomock.MongoClient().test
    _seed(database)
    monkeypatch.setattr(retirement, "db", database)

    first = retirement.apply_retirement(tmp_path)
    second = retirement.apply_retirement(tmp_path)

    assert first["changed"] == {
        "v3_pending_confirmations": 1,
        "v3_contact_selections": 1,
        "v3_operation_batches": 1,
    }
    assert all(value == 0 for value in second["changed"].values())
    assert database.v3_pending_confirmations.find_one({"_id": "dag"})["status"] == "expired"
    assert database.v3_pending_confirmations.find_one({"_id": "pi"})["status"] == "pending"
    assert database.v3_pending_confirmations.find_one({"_id": "private"})["status"] == "pending"
    assert retirement.verify_state()["ok"] is True
    assert list(tmp_path.glob("retire-public-dag-v1-*.json"))


def test_apply_refuses_ambiguous_or_executing_records(tmp_path, monkeypatch):
    database = mongomock.MongoClient().test
    database.v3_pending_confirmations.insert_many([
        {"_id": "ambiguous", "status": "pending"},
        {"_id": "running", "surface": "public_ayue", "source_engine": "dag", "status": "executing"},
    ])
    monkeypatch.setattr(retirement, "db", database)

    try:
        retirement.apply_retirement(tmp_path)
    except RuntimeError as exc:
        assert str(exc) == "unresolved_or_executing_records"
    else:
        raise AssertionError("retirement must fail closed")
