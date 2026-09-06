from unittest.mock import Mock

from routers import match as router


def test_startup_accepts_existing_safe_pair_index(monkeypatch):
    collection = Mock()
    collection.index_information.return_value = {
        "one_live_proposal_per_pair": {
            "key": [("live_pair_key", 1)],
            "unique": True,
            "partialFilterExpression": {"status": {"$in": ["draft", "pending"]}},
        },
    }
    monkeypatch.setattr(router, "matches_coll", collection)
    monkeypatch.setenv("MATCH_MULTI_INDEX_READY", "on")

    router.ensure_match_indexes()

    collection.create_index.assert_not_called()


def test_startup_creates_pair_index_when_missing(monkeypatch):
    collection = Mock()
    collection.index_information.return_value = {}
    monkeypatch.setattr(router, "matches_coll", collection)
    monkeypatch.setenv("MATCH_MULTI_INDEX_READY", "on")

    router.ensure_match_indexes()

    collection.create_index.assert_called_once_with(
        [("live_pair_key", 1)],
        unique=True,
        partialFilterExpression={
            "status": {"$in": ["draft", "pending"]},
            "live_pair_key": {"$exists": True},
        },
        name="one_live_proposal_per_pair",
    )
