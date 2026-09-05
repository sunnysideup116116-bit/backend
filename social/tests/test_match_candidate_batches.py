"""Bounded widening without real MongoDB, graph, model calls, or user proposals."""
import json
import time
from unittest.mock import Mock

import pytest
import requests

from routers import match as router
from services import match_search_job_service as jobs
from services import match_state_service as state
from tests.test_match_restart_flow import flow


def reply(selected=None):
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({
        "outcome": "selected" if selected else "no_suitable_candidate",
        "matches": [{"matched_user_id": selected}] if selected else [],
    }).encode()
    return response


@pytest.fixture
def batch_flow(monkeypatch):
    target = {"user_id": "owner", "current_context": "晚上想看電影", "context_embedding": [0.1]}
    candidates = [{"user_id": f"candidate-{i}", "current_context": "想去看展覽", "score": 0.8}
                  for i in range(12)]
    profiles, matches = Mock(), Mock()
    profiles.find_one.side_effect = lambda query, *_args: dict(target) if query.get("user_id") == "owner" else next(
        (dict(c) for c in candidates if c["user_id"] == query.get("user_id")), {})
    profiles.aggregate.return_value = candidates
    matches.find.return_value = []
    matches.find_one.return_value = None
    matches.insert_one.return_value.inserted_id = "isolated-proposal"
    monkeypatch.setattr(router, "profiles_coll", profiles)
    monkeypatch.setattr(router, "matches_coll", matches)
    monkeypatch.setattr(router, "agent_candidate_limit", lambda: 3)
    monkeypatch.setattr(router, "_trait_stances", lambda *_args: {})
    monkeypatch.setattr(router, "reconcile_match_state", lambda *_args: None)
    monkeypatch.setattr(router, "build_validated_match_explanation", lambda *_args: ({}, [], [], "資料支持的介紹"))
    monkeypatch.setattr(router, "build_friend_intro_v4", lambda *_args, **_kwargs: {})
    post = Mock(return_value=reply())
    monkeypatch.setattr(router.requests, "post", post)
    return profiles, matches, post


def run(**kwargs):
    return router.generate_matches_for_user(
        "owner", report_progress=kwargs.get("report_progress", lambda _step: True),
        can_commit=kwargs.get("can_commit", lambda: True), search_job_id="isolated-job",
    )


def test_first_batch_empty_second_batch_creates_one_job_bound_proposal(batch_flow):
    profiles, matches, post = batch_flow
    post.side_effect = [reply(), reply("candidate-3")]
    result = run()
    assert result["status"] == "success"
    assert result["matches"][0]["matched_user_id"] == "candidate-3"
    assert [[c["user_id"] for c in call.kwargs["json"]["candidates"]] for call in post.call_args_list] == [
        ["candidate-0", "candidate-1", "candidate-2"], ["candidate-3", "candidate-4", "candidate-5"],
    ]
    matches.insert_one.assert_called_once()
    assert matches.insert_one.call_args.args[0]["search_job_id"] == "isolated-job"
    assert not any("$limit" in stage for stage in profiles.aggregate.call_args.args[0])
    assert post.call_args_list[1].kwargs["timeout"] <= post.call_args_list[0].kwargs["timeout"] <= 120


def test_first_success_stops_without_more_batches(batch_flow):
    _, matches, post = batch_flow
    post.return_value = reply("candidate-0")
    assert run()["status"] == "success"
    post.assert_called_once()
    matches.insert_one.assert_called_once()


def test_all_empty_is_bounded_to_three_nonoverlapping_batches(batch_flow):
    _, matches, post = batch_flow
    assert run()["status"] == "no_suitable_candidate"
    assert post.call_count == 3
    ids = [c["user_id"] for call in post.call_args_list for c in call.kwargs["json"]["candidates"]]
    assert len(ids) == len(set(ids)) == 9
    matches.insert_one.assert_not_called()


def test_ineligible_first_three_do_not_hide_later_candidates(batch_flow, monkeypatch):
    _, _, post = batch_flow
    monkeypatch.setattr(router, "candidate_qualification", lambda _target, candidate, **_kwargs: {
        "eligible": int(candidate["user_id"].split("-")[-1]) >= 3,
    })
    post.return_value = reply("candidate-3")
    assert run()["status"] == "success"
    assert [c["user_id"] for c in post.call_args.kwargs["json"]["candidates"]] == [
        "candidate-3", "candidate-4", "candidate-5",
    ]


def test_excluded_and_busy_candidates_never_reenter_later_batches(batch_flow, monkeypatch):
    profiles, matches, post = batch_flow
    matches.find.return_value = [{"from_user": "owner", "to_user": "candidate-0", "status": "declined", "created_at": time.time()}]
    monkeypatch.setattr(router.risk_block_service, "excluded_user_ids", lambda _: {"candidate-1"})
    monkeypatch.setattr(router, "reconcile_match_state", lambda user: {"status": "pending"} if user == "candidate-2" else None)
    assert run()["status"] == "no_suitable_candidate"
    ids = [c["user_id"] for call in post.call_args_list for c in call.kwargs["json"]["candidates"]]
    assert not {"candidate-0", "candidate-1", "candidate-2"} & set(ids)
    assert "candidate-3" in ids
    exclusions = profiles.aggregate.call_args.args[0][1]["$match"]["user_id"]["$nin"]
    assert {"owner", "candidate-0", "candidate-1"} <= set(exclusions)


def test_cancel_between_batches_stops_without_second_request(batch_flow):
    _, matches, post = batch_flow
    count = 0
    def progress(step):
        nonlocal count
        if step == "matchmaker_request":
            count += 1
        return count < 2
    assert run(report_progress=progress)["status"] == "stale"
    post.assert_called_once()
    matches.insert_one.assert_not_called()


def test_stale_before_commit_never_creates_proposal(batch_flow):
    _, matches, post = batch_flow
    post.return_value = reply("candidate-0")
    assert run(can_commit=lambda: False)["status"] == "stale"
    matches.insert_one.assert_not_called()


@pytest.mark.parametrize("outsider", ["candidate-4", "not-a-candidate", "owner"])
def test_model_cannot_select_outside_current_batch(batch_flow, outsider):
    _, matches, post = batch_flow
    post.return_value = reply(outsider)
    with pytest.raises(jobs.MatchSearchPipelineError) as exc:
        run()
    assert exc.value.code == "matchmaker_invalid_response"
    post.assert_called_once()
    matches.insert_one.assert_not_called()


def test_provider_failure_is_not_retried_as_another_batch(batch_flow):
    _, matches, post = batch_flow
    post.side_effect = requests.Timeout()
    with pytest.raises(jobs.MatchSearchPipelineError) as exc:
        run()
    assert exc.value.code == "matchmaker_timeout"
    post.assert_called_once()
    matches.insert_one.assert_not_called()


def test_shared_selection_budget_exhaustion_is_failed_not_no_candidates(batch_flow, monkeypatch):
    _, matches, post = batch_flow
    clock = Mock(side_effect=[0, 1, 121])
    monkeypatch.setattr(router.time, "monotonic", clock)
    with pytest.raises(jobs.MatchSearchPipelineError) as exc:
        run()
    assert exc.value.code == "matchmaker_timeout"
    post.assert_called_once()
    matches.insert_one.assert_not_called()


@pytest.mark.parametrize("status,percent", [("failed", 0), ("no_candidates", 0), ("cancelled", 0), ("stale", 0), ("completed", 100), ("running", 65)])
def test_terminal_projection_does_not_expose_old_checkpoint(monkeypatch, status, percent):
    collection = Mock()
    collection.find_one.return_value = {"status": status, "step": "matchmaker_request", "progress_percent": 65,
                                        "error_code": "matchmaker_output_truncated"}
    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", collection)
    result = jobs.public_match_search_status("owner")
    assert result["progress_percent"] == percent
    assert result["cancellable"] is (status == "running")
    collection.update_one.assert_not_called()


def test_canonical_status_preserves_verified_failure_code(monkeypatch):
    monkeypatch.setattr(state, "load_match_state", lambda _: {
        "active_proposal": None, "ambiguous": False,
        "search": {"status": "failed", "reason_code": "matchmaker_output_truncated", "completed_at": 50},
    })
    collection = Mock()
    collection.find_one.return_value = None
    monkeypatch.setattr(state, "matches_coll", collection)
    assert state.get_match_status_snapshot("owner")["reason_code"] == "matchmaker_output_truncated"


def test_worker_delivers_second_batch_result_once_to_origin_room(flow, batch_flow, monkeypatch):
    _, matches, post = batch_flow
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "declined"}})
    post.side_effect = [reply(), reply("candidate-3")]
    queue = Mock()
    monkeypatch.setattr(jobs, "queue_mediator_event", queue)
    monkeypatch.setattr(jobs, "_pipeline", router.generate_matches_for_user)
    assert jobs.enqueue_match_search("owner", source="test", idempotency_key="batch-result", origin_room_id="room") == {"status": "queued"}
    assert jobs.run_one_match_search_job()
    assert flow.jobs.rows[0]["status"] == "completed"
    assert matches.insert_one.call_args.args[0]["search_job_id"] == flow.jobs.rows[0]["job_id"]
    assert queue.call_args.kwargs["origin_room_id"] == "room"
    assert queue.call_args.kwargs["event_key"] == f"match-search-job:{flow.jobs.rows[0]['job_id']}:proposal"
    assert not jobs.run_one_match_search_job()
    matches.insert_one.assert_called_once()
    queue.assert_called_once()
