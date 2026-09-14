"""All person-label entry points share Appwrite names without crossing owners."""
import json
from collections import OrderedDict
from unittest.mock import MagicMock

import mongomock
import pytest

from services import public_nickname_service as names
from services.ayue_agent import public_relationship_projection as relationships
from services import match_action_service, match_state_service, notification_service
from tests.test_chat_read_efficiency import mongo as contact_mongo, accepted


@pytest.fixture
def store(monkeypatch):
    db = mongomock.MongoClient().test
    monkeypatch.setattr(relationships, "profiles_coll", db.profiles)
    monkeypatch.setattr(relationships, "matches_coll", db.matches)
    monkeypatch.setattr(names, "_cache", OrderedDict())
    monkeypatch.setattr(names, "_PROJECT_ID", "test-project")
    monkeypatch.setattr(names, "_API_KEY", "test-key")
    monkeypatch.setattr(names, "_ENDPOINT", "https://appwrite.example.test/v1")
    return db


def accept(db, other):
    db.matches.insert_one({"from_user": "owner", "to_user": other, "status": "accepted",
        "last_decision": {"from": "pending", "to": "accepted", "action": "accept"}})


def test_batch_reads_only_requested_ids_and_populates_shared_name_cache(store, monkeypatch):
    get = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"documents": [
        {"$id": "a", "name": "kkk"}, {"$id": "b", "name": "Candy"}, {"$id": "stranger", "name": "無權讀取的名字"},
    ]}))
    monkeypatch.setattr(names.requests, "get", get)
    names.warm_public_nicknames(["a", "b", "a"])
    assert names.contact_display_name("a", {}) == "kkk"
    assert relationships.display_name("b", profile={}) == "Candy"
    assert "stranger" not in names._cache
    assert get.call_count == 1
    query = [json.loads(q) for q in get.call_args.kwargs["params"]["queries[]"]]
    assert query[0]["values"] == ["a", "b"]
    assert query[1]["values"] == ["$id", "name"]
    assert get.call_args.kwargs["allow_redirects"] is False


def test_name_resolution_works_with_no_mongo_name_but_remains_owner_scoped(store, monkeypatch):
    accept(store, "a")
    lookup = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"documents": [{"$id": "a", "name": "kkk"}]}))
    monkeypatch.setattr(names.requests, "get", lookup)
    resolved = relationships.resolve_accepted_contact_name("owner", "kkk")
    assert resolved.status == "resolved_exact"
    assert resolved.other_id == "a"
    assert relationships.accepted_contact_ids_by_display_name("owner", "kkk") == ["a"]
    assert relationships.resolve_accepted_contact_name("stranger", "kkk").status == "not_found"
    assert lookup.call_count == 1


def test_duplicate_real_names_stay_ambiguous(store, monkeypatch):
    for uid in ("a", "b"):
        accept(store, uid)
    monkeypatch.setattr(names.requests, "get", MagicMock(return_value=MagicMock(status_code=200, json=lambda: {
        "documents": [{"$id": uid, "name": "kkk"} for uid in ("a", "b")],
    })))
    assert relationships.resolve_accepted_contact_name("owner", "kkk").status == "ambiguous"


def test_missing_names_are_unavailable_not_proof_contact_does_not_exist(store, monkeypatch):
    accept(store, "a")
    get = MagicMock(return_value=MagicMock(status_code=503))
    monkeypatch.setattr(names.requests, "get", get)
    assert relationships.resolve_accepted_contact_name("owner", "kkk").status == "unavailable"
    assert names.contact_display_name("a", {}) == ""
    assert get.call_count == 1


def test_empty_authoritative_name_never_revives_old_mongo_alias(store, monkeypatch):
    monkeypatch.setattr(names.requests, "get", MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"name": ""})))
    assert names.contact_display_name("a", {"name": "Old", "display_name": "Also old"}) == ""


@pytest.mark.parametrize("placeholder", ["對方", "(對方)", "（對方）"])
def test_parenthesized_placeholders_cannot_become_resolvable_person_names(store, monkeypatch, placeholder):
    monkeypatch.setattr(names.requests, "get", MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"name": placeholder})))
    assert names.contact_display_name("a", {"name": "Old"}) == ""


def test_match_success_state_and_notifications_share_canonical_name(store, monkeypatch):
    get = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"name": "kkk"}))
    monkeypatch.setattr(names.requests, "get", get)
    monkeypatch.setattr(match_state_service, "profiles_coll", store.profiles)
    monkeypatch.setattr(notification_service, "profiles_coll", store.profiles)
    assert match_action_service._safe_profile_label({}, "a") == "kkk"
    assert match_state_service._display_name("a") == "kkk"
    assert notification_service._display_name("a") == "kkk"
    assert relationships.display_name("a", profile={}) == "kkk"
    assert get.call_count == 1


def test_contacts_http_exposes_appwrite_names_without_mongo_profiles(contact_mongo, monkeypatch):
    from routers import chat_messages
    for uid in ("a", "b"):
        accepted(contact_mongo, uid)
    monkeypatch.setattr(names, "_PROJECT_ID", "test-project")
    monkeypatch.setattr(names, "_API_KEY", "test-key")
    monkeypatch.setattr(names, "_ENDPOINT", "https://appwrite.example.test/v1")
    monkeypatch.setattr(names, "_cache", OrderedDict())
    get = MagicMock(return_value=MagicMock(status_code=200, json=lambda: {"documents": [
        {"$id": "a", "name": "kkk"}, {"$id": "b", "name": "Candy"},
    ]}))
    monkeypatch.setattr(names.requests, "get", get)
    contacts = chat_messages.get_contacts("owner")["contacts"][1:]
    assert {row["name"] for row in contacts} == {"kkk", "Candy"}
    assert all(row["name_available"] for row in contacts)
    get.assert_called_once()
