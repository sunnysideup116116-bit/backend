import json
from unittest.mock import Mock

import pytest
import requests

from routers import match as router
from services import match_search_job_service as jobs
from tests.test_match_restart_flow import flow


@pytest.mark.parametrize("code", [
    "matchmaker_timeout", "matchmaker_graph_timeout", "matchmaker_graph_unavailable",
    "matchmaker_empty_response", "matchmaker_output_truncated", "matchmaker_provider_error",
    "transport_timeout",
])
def test_timeout_and_invalid_model_output_finish_job_and_deliver_failure(flow, monkeypatch, code):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "declined"}})
    target = {**flow.profiles.rows[0], "context_embedding": [0.1]}
    candidate = {"user_id": "candidate", "score": 0.9, "current_context": "週末想看展覽"}
    profiles, matches = Mock(), Mock()
    profiles.find_one.side_effect = lambda query, *_args: target if query.get("user_id") == "owner" else candidate
    profiles.aggregate.return_value = [candidate]
    matches.find.return_value = []
    matches.find_one.return_value = None
    monkeypatch.setattr(router, "profiles_coll", profiles)
    monkeypatch.setattr(router, "matches_coll", matches)
    monkeypatch.setattr(router, "_trait_stances", lambda *_args: {})
    monkeypatch.setattr(router, "candidate_qualification", lambda *_args, **_kwargs: {"eligible": True})
    if code == "transport_timeout":
        post = Mock(side_effect=requests.Timeout("not public"))
        expected_code = "matchmaker_timeout"
    else:
        response = requests.Response()
        response.status_code = 504 if "timeout" in code else 502
        response._content = json.dumps({"detail": {"code": code}}).encode()
        post = Mock(return_value=response)
        expected_code = code
    monkeypatch.setattr(router.requests, "post", post)
    queue = Mock()
    monkeypatch.setattr(jobs, "queue_mediator_event", queue)
    monkeypatch.setattr(jobs, "_pipeline", router.generate_matches_for_user)
    assert jobs.enqueue_match_search("owner", source="test", idempotency_key="test-failure", origin_room_id="room") == {"status": "queued"}
    assert jobs.run_one_match_search_job()
    current = flow.jobs.rows[0]
    assert current["status"] == "failed"
    assert current["error_code"] == expected_code
    assert "active_user_id" not in current
    assert flow.profiles.rows[0]["matchmaking_in_progress"] is False
    public = jobs.public_match_search_status("owner")
    assert public["status"] == "failed" and not public["cancellable"]
    assert public["reason_code"] == expected_code
    assert queue.call_args.args[2] == "match_search_failed"
    assert queue.call_args.kwargs["origin_room_id"] == "room"
    assert "沒有合適" not in queue.call_args.args[1]
    matches.insert_one.assert_not_called()
    post.assert_called_once()
