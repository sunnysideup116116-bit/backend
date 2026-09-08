"""The final pool must not be the pre-exclusion vector window."""
from routers import match as router
from tests.test_match_candidate_batches import batch_flow


def test_vector_window_is_wider_than_final_pool(batch_flow):
    # The fixture isolates Mongo, model, Graph and all proposal effects.
    profiles = router.profiles_coll
    profiles.aggregate.return_value = []
    result = router.generate_matches_for_user("owner", report_progress=lambda _: True)
    pipeline = profiles.aggregate.call_args.args[0]
    vector = pipeline[0]["$vectorSearch"]
    assert vector["limit"] == 100
    assert vector["limit"] > router.MATCH_CANDIDATE_POOL_SIZE
    assert vector["numCandidates"] >= vector["limit"]
    assert "owner" in pipeline[1]["$match"]["user_id"]["$nin"]
    assert result["matches"] == []
