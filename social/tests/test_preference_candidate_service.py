from unittest.mock import Mock

from services import preference_candidate_service as service


def test_internal_preference_adapter_revalidates_and_filters_ids(monkeypatch):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "success", "canonical_key": "k_pop",
        "candidate_ids": ["owner", "blocked", "candidate", "candidate"],
        "candidate_count_before_filter": 4,
        "candidate_count_after_filter": 3,
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(service.requests, "post", post)

    result = service.retrieve_preference_candidate_ids(
        "owner", "Kpop", excluded_user_ids={"blocked"}, limit=999,
    )

    assert result["candidate_ids"] == ["candidate"]
    assert result["canonical_key"] == "k_pop"
    assert result["query_provenance"] == "deterministic_alias"
    assert result["candidate_count_before_filter"] == 4
    assert result["candidate_count_after_filter"] == 3
    sent = post.call_args.kwargs["json"]
    assert sent["topic"] == "K-pop"
    assert sent["limit"] == 100
    assert set(sent["excluded_user_ids"]) == {"blocked"}


def test_internal_preference_adapter_rejects_a_mismatched_canonical_key(monkeypatch):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "success", "canonical_key": "wrong",
        "candidate_ids": ["candidate"],
    }
    monkeypatch.setattr(service.requests, "post", Mock(return_value=response))

    try:
        service.retrieve_preference_candidate_ids(
            "owner", "K-pop", excluded_user_ids=set(), limit=20,
        )
    except service.PreferenceCandidateLookupError:
        pass
    else:
        raise AssertionError("mismatched canonical identity must fail closed")
