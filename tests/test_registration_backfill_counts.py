from collections import Counter

from scripts.bootstrap_registration_graph import unique_profile_ids


def test_duplicate_mongo_profiles_are_not_counted_as_extra_accounts():
    counts = Counter()
    ids = unique_profile_ids([
        {"user_id": "first"}, {"user_id": "second"}, {"user_id": "first"},
        {"user_id": "second"}, {"user_id": "third"},
    ], counts)
    assert ids == ["first", "second", "third"]
    assert counts == {"mongo_profiles": 5, "duplicate_profile_rows": 2, "unique_account_ids": 3}


def test_missing_identity_is_reported_without_a_ghost_account():
    counts = Counter()
    assert unique_profile_ids([{}, {"user_id": " "}], counts) == []
    assert counts["missing_user_id"] == 2
