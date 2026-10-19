from unittest.mock import Mock

from services import preference_candidate_service as service
from matchmaker_agent.concept_identity import canonicalize_concept

K_POP = canonicalize_concept("K-pop").key


def test_internal_preference_adapter_revalidates_and_filters_ids(monkeypatch):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "success", "canonical_key": K_POP,
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
    assert result["canonical_key"] == K_POP
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


def test_activity_adapter_requires_v2_only_ack_and_bounds_additional_exact_topics(monkeypatch):
    main = canonicalize_concept("羽球")
    phrase = canonicalize_concept("打羽球")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "success", "canonical_key": main.key, "v2_only": True,
        "candidate_ids": ["owner", "blocked", "candidate-exact"],
        "additional_candidates": [
            {"canonical_key": phrase.key, "candidate_ids": ["candidate-phrase", "blocked"]},
            {"canonical_key": canonicalize_concept("羽球雙打").key, "candidate_ids": []},
            {"canonical_key": canonicalize_concept("格鬥遊戲").key, "candidate_ids": []},
            {"canonical_key": canonicalize_concept("ignored-extra").key, "candidate_ids": []},
            {"canonical_key": "unexpected-key", "candidate_ids": ["ignored"]},
        ],
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(service.requests, "post", post)

    result = service.retrieve_preference_candidate_ids(
        "owner", "羽球", excluded_user_ids={"blocked"}, limit=999,
        v2_only=True,
        additional_topics=["打羽球", "羽球雙打", "格鬥遊戲", "ignored-extra", "fifth-extra"],
    )

    payload = post.call_args.kwargs["json"]
    assert payload["v2_only"] is True
    assert payload["additional_topics"] == ["打羽球", "羽球雙打", "格鬥遊戲", "ignored-extra"]
    assert post.call_args.kwargs["timeout"] == (1, 5)
    assert result["candidate_ids"] == ["candidate-exact"]
    assert result["additional_candidates"] == [
        {"canonical_key": phrase.key, "candidate_ids": ["candidate-phrase"]},
        {"canonical_key": canonicalize_concept("羽球雙打").key, "candidate_ids": []},
        {"canonical_key": canonicalize_concept("格鬥遊戲").key, "candidate_ids": []},
        {"canonical_key": canonicalize_concept("ignored-extra").key, "candidate_ids": []},
    ]


def test_activity_adapter_fails_closed_when_matchmaker_does_not_ack_v2_only(monkeypatch):
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "success", "canonical_key": K_POP, "candidate_ids": ["candidate"],
    }
    monkeypatch.setattr(service.requests, "post", Mock(return_value=response))

    try:
        service.retrieve_preference_candidate_ids(
            "owner", "K-pop", excluded_user_ids=set(), limit=20, v2_only=True,
        )
    except service.PreferenceCandidateLookupError:
        pass
    else:
        raise AssertionError("a server without the v2-only contract must fail closed")


def test_activity_avoidance_adapter_requires_a_complete_bounded_snapshot(monkeypatch):
    sf = canonicalize_concept("Street Fighter")
    phrase = canonicalize_concept("玩街頭霸王")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "success",
        "candidate_checks": [
            {"candidate_id": "candidate", "verification_status": "verified",
             "avoid_concept_keys": [sf.key]},
            {"candidate_id": "other", "verification_status": "unverified",
             "avoid_concept_keys": []},
        ],
    }
    post = Mock(return_value=response)
    monkeypatch.setattr(service.requests, "post", post)

    result = service.retrieve_activity_avoidance_snapshot(
        ["candidate", "other"],
        ["Street Fighter", "街頭霸王", "快打旋風", "玩街頭霸王", "玩快打旋風"],
    )

    sent = post.call_args.kwargs["json"]
    assert post.call_args.args[0].endswith("/api/preferences/activity-avoidances")
    assert post.call_args.kwargs["timeout"] == (1, 5)
    assert sent["candidate_ids"] == ["candidate", "other"]
    assert len(sent["topic_family"]) == 5
    assert result == {
        "candidate": {"verification_status": "verified", "avoid_concept_keys": [sf.key]},
        "other": {"verification_status": "unverified", "avoid_concept_keys": []},
    }
    assert phrase.key in {canonicalize_concept(topic).key for topic in sent["topic_family"]}


def test_activity_avoidance_adapter_rejects_missing_or_out_of_family_checks(monkeypatch):
    sf = canonicalize_concept("Street Fighter")
    response = Mock()
    response.raise_for_status.return_value = None
    response.json.return_value = {
        "status": "success",
        "candidate_checks": [{
            "candidate_id": "candidate", "verification_status": "verified",
            "avoid_concept_keys": [canonicalize_concept("格鬥遊戲").key],
        }],
    }
    monkeypatch.setattr(service.requests, "post", Mock(return_value=response))

    try:
        service.retrieve_activity_avoidance_snapshot(["candidate"], ["Street Fighter"])
    except service.ActivityAvoidanceSnapshotError:
        pass
    else:
        raise AssertionError("unexpected concept keys must not be accepted as AVOID proof")

    response.json.return_value = {"status": "success", "candidate_checks": []}
    try:
        service.retrieve_activity_avoidance_snapshot(["candidate"], ["Street Fighter"])
    except service.ActivityAvoidanceSnapshotError:
        pass
    else:
        raise AssertionError("missing owner checks must not mean no AVOID")
