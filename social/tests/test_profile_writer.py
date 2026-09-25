from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import MagicMock

import mongomock
import pytest
from pymongo import timeout, _csot
from pymongo.errors import AutoReconnect, DuplicateKeyError

from services.profile_writer import ensure_profile, update_profile, ProfileWriteConflict


def duplicate(owner="owner", pattern=None, value=None, code=11000):
    return DuplicateKeyError("synthetic private detail must not be logged", code, {
        "keyPattern": {"user_id": 1} if pattern is None else pattern,
        "keyValue": {"user_id": owner} if value is None else value,
    })


def db():
    coll = mongomock.MongoClient().test.profiles
    coll.create_index("user_id", unique=True)
    return coll


def test_insert_only_defaults_never_replace_rich_profile():
    coll = db()
    rich = {"user_id": "owner", "name": "Keep", "initial_interest": "Bookshops",
            "big_five": {"O": 8}, "profile_memory_revision": 9,
            "preference_bootstrap_pending": "operation", "onboarding_completed": True}
    coll.insert_one(deepcopy(rich)); before = coll.find_one()
    ensure_profile(coll, "owner", {"name": "", "initial_interest": "", "big_five": {}, "onboarding_completed": False})
    assert coll.find_one() == before and coll.count_documents({"user_id": "owner"}) == 1


def test_fresh_creation_and_normal_operator_update_keep_one_document():
    coll = db()
    first = ensure_profile(coll, "owner", {"name": "Initial"})
    assert first.upserted_id
    update_profile(coll, {"user_id": "owner"}, {"$set": {"last_presence_at": 3}}, upsert=True)
    assert coll.find_one()["name"] == "Initial" and coll.find_one()["last_presence_at"] == 3
    assert coll.count_documents({}) == 1


def test_confirmed_duplicate_recovers_existing_winner_only_once():
    coll = db(); coll.insert_one({"_id": "canonical", "user_id": "owner", "rich": {"value": 7}})
    proxy = MagicMock(wraps=coll)
    proxy.update_one.side_effect = [duplicate(), SimpleNamespace(matched_count=1, modified_count=1)]
    query, update = {"user_id": "owner"}, {"$push": {"notices": "one"}}
    result = update_profile(proxy, query, update, upsert=True)
    assert result.modified_count == 1 and proxy.update_one.call_count == 2
    assert proxy.update_one.call_args_list[1].args == ({"user_id": "owner", "_id": "canonical"}, update)
    assert proxy.update_one.call_args_list[1].kwargs == {"upsert": False}
    assert proxy.find.call_args.args == ({"user_id": "owner"}, {"_id": 1, "user_id": 1})
    assert query == {"user_id": "owner"} and update == {"$push": {"notices": "one"}}


def test_ensure_race_loser_does_not_overwrite_winner():
    coll = db(); proxy = MagicMock(wraps=coll)
    def win_then_raise(*args, **kwargs):
        coll.insert_one({"user_id": "owner", "name": "Rich winner", "assessment": {"revision": 4}})
        proxy.update_one.side_effect = coll.update_one
        raise duplicate()
    proxy.update_one.side_effect = win_then_raise
    ensure_profile(proxy, "owner", {"name": "", "assessment": {}})
    result = coll.find_one()
    assert result["name"] == "Rich winner" and result["assessment"] == {"revision": 4}
    assert coll.count_documents({}) == 1


@pytest.mark.parametrize("error", [duplicate(pattern={"_id": 1}), duplicate(pattern={"user_id": 1, "extra": 1}),
    duplicate(value={"user_id": "other"}), duplicate(code=11001), DuplicateKeyError("no details"), AutoReconnect("unknown outcome")])
def test_unproven_or_other_error_never_retries(error):
    coll = MagicMock(); coll.update_one.side_effect = error
    with pytest.raises(type(error)) as caught:
        update_profile(coll, {"user_id": "owner"}, {"$inc": {"count": 1}}, upsert=True)
    assert caught.value is error and coll.update_one.call_count == 1
    coll.find.assert_not_called()


def test_second_duplicate_is_not_retried_again():
    coll = db(); coll.insert_one({"user_id": "owner"}); proxy = MagicMock(wraps=coll)
    proxy.update_one.side_effect = [duplicate(), duplicate()]
    with pytest.raises(DuplicateKeyError):
        update_profile(proxy, {"user_id": "owner"}, {"$set": {"x": 1}}, upsert=True)
    assert proxy.update_one.call_count == 2


@pytest.mark.parametrize("winner_count", [0, 2])
def test_missing_or_ambiguous_winner_fails_closed(winner_count):
    coll = mongomock.MongoClient().test.profiles  # Deliberately no unique index in this invalid fixture.
    for _ in range(winner_count): coll.insert_one({"user_id": "owner"})
    proxy = MagicMock(wraps=coll); proxy.update_one.side_effect = duplicate()
    with pytest.raises(ProfileWriteConflict, match="profile_race_winner_unverified"):
        update_profile(proxy, {"user_id": "owner"}, {"$set": {"x": 1}}, upsert=True)
    assert proxy.update_one.call_count == 1


def test_winner_deleted_after_read_never_recreates_stub():
    coll = db(); coll.insert_one({"user_id": "owner"}); proxy = MagicMock(wraps=coll)
    proxy.update_one.side_effect = [duplicate(), SimpleNamespace(matched_count=0)]
    with pytest.raises(ProfileWriteConflict, match="profile_race_winner_changed"):
        update_profile(proxy, {"user_id": "owner"}, {"$set": {"x": 1}}, upsert=True)
    assert proxy.update_one.call_count == 2 and proxy.update_one.call_args.kwargs["upsert"] is False


def test_conditional_miss_stays_update_only():
    coll = db(); coll.insert_one({"user_id": "owner", "initial_interest": "Retain", "revision": 4})
    before = coll.find_one()
    result = update_profile(coll, {"user_id": "owner", "revision": 3}, {"$set": {"initial_interest": ""}})
    assert result.matched_count == 0 and coll.find_one() == before
    assert coll.count_documents({"user_id": "owner"}) == 1


def test_historical_conditional_upsert_is_rejected_before_db():
    coll = MagicMock()
    with pytest.raises(ValueError, match="conditional_profile_upsert_forbidden"):
        update_profile(coll, {"user_id": "owner", "$or": [{"initial_interest": ""}]}, {"$set": {"initial_interest": "new"}}, upsert=True)
    coll.update_one.assert_not_called()


def test_conditional_duplicate_error_does_not_relax_predicate():
    coll = MagicMock(); coll.update_one.side_effect = duplicate()
    with pytest.raises(DuplicateKeyError):
        update_profile(coll, {"user_id": "owner", "revision": 3}, {"$set": {"value": 5}})
    coll.find.assert_not_called(); assert coll.update_one.call_count == 1


def test_aborted_transaction_is_left_to_owning_coordinator():
    session = SimpleNamespace(in_transaction=True)
    coll = MagicMock(); coll.update_one.side_effect = duplicate()
    with pytest.raises(DuplicateKeyError): ensure_profile(coll, "owner", session=session)
    assert coll.update_one.call_args.kwargs == {"upsert": True, "session": session}
    coll.find.assert_not_called(); assert coll.update_one.call_count == 1


def test_transaction_ownership_is_recorded_before_driver_error():
    session = SimpleNamespace(in_transaction=True)
    coll = MagicMock()
    def abort(*args, **kwargs):
        session.in_transaction = False
        raise duplicate()
    coll.update_one.side_effect = abort
    with pytest.raises(DuplicateKeyError): ensure_profile(coll, "owner", session=session)
    coll.find.assert_not_called()


def test_recovery_preserves_outer_deadline_and_has_no_logging(capsys):
    coll = db(); coll.insert_one({"user_id": "owner"}); proxy = MagicMock(wraps=coll)
    proxy.update_one.side_effect = [duplicate(), SimpleNamespace(matched_count=1)]
    with timeout(.5):
        outer = _csot.get_deadline()
        def read(*args, **kwargs):
            assert _csot.get_deadline() <= outer
            return coll.find(*args, **kwargs)
        proxy.find.side_effect = read
        ensure_profile(proxy, "owner")
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("owner", [None, "", " ", " owner", {"$ne": ""}, 1])
def test_invalid_owner_never_writes(owner):
    coll = MagicMock()
    with pytest.raises(ValueError): ensure_profile(coll, owner)
    coll.update_one.assert_not_called()


@pytest.mark.parametrize("update", [{"name": "replacement"}, [{"$set": {"name": "pipeline"}}],
    {"$set": {"user_id": "other"}}, {"$setOnInsert": {"user_id": "other"}}, {"$unset": {"user_id": ""}},
    {"$rename": {"x": "user_id"}}, {"$set": {"_id": "alternate"}}, {"$set": {"user_id.nested": "x"}}])
def test_replacement_identity_rewrite_and_unsupported_operator_rejected(update):
    coll = MagicMock()
    with pytest.raises(ValueError): update_profile(coll, {"user_id": "owner"}, update, upsert=True)
    coll.update_one.assert_not_called()
