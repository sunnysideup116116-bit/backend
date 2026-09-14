"""Focused coverage for confirmed topic searches and immediate source entries."""

from unittest.mock import MagicMock, patch

from bson import ObjectId

from services import match_action_service as actions
from services import match_search_job_service as jobs
from services import match_decision_service as decisions
from services.proactive_delivery_service import _safe_source_entry, _source_pointer_message
from tests.match_flow_store import Collection


def test_enqueue_preserves_invite_mode_only_with_a_bound_topic(monkeypatch):
    jobs_collection = Collection()
    profiles = Collection([{"user_id": "owner", "current_context_revision": 4}])
    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", jobs_collection)
    monkeypatch.setattr(jobs, "profiles_coll", profiles)
    monkeypatch.setattr(jobs, "_has_live_match", lambda _user_id: False)

    result = jobs.enqueue_match_search(
        "owner",
        source="agent_v3",
        idempotency_key="topic-search",
        search_context={"invitation_topic": "滑雪"},
        delivery_mode=jobs.INVITE_ON_MATCH,
    )

    assert result == {"status": "queued"}
    assert jobs_collection.rows[0]["delivery_mode"] == jobs.INVITE_ON_MATCH
    jobs_collection.rows[0]["active_user_id"] = ""

    result = jobs.enqueue_match_search(
        "owner",
        source="agent_v3",
        idempotency_key="generic-search",
        search_context={},
        delivery_mode=jobs.INVITE_ON_MATCH,
    )
    assert result == {"status": "queued"}
    assert jobs_collection.rows[1]["delivery_mode"] == jobs.PREVIEW_ON_MATCH


def test_worker_passes_invite_mode_and_runs_the_canonical_transition(monkeypatch):
    job = {
        "_id": "job-row",
        "job_id": "job-1",
        "user_id": "owner",
        "source": "agent_v3",
        "lease_id": "lease-1",
        "delivery_mode": jobs.INVITE_ON_MATCH,
        "search_context": {"invitation_topic": "滑雪"},
        "origin_room_id": "room",
    }
    pipeline = MagicMock(return_value={
        "status": "success",
        "matches": [{"match_id": "match-1", "matched_user_id": "candidate"}],
    })
    monkeypatch.setattr(jobs, "_claim_next_job", lambda _now: job)
    monkeypatch.setattr(jobs, "_pipeline", pipeline)
    monkeypatch.setattr(jobs, "_report_progress", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(jobs, "_job_has_lease", lambda _job: True)
    monkeypatch.setattr(jobs, "_finish_job", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(jobs, "profiles_coll", MagicMock())
    matches_collection = MagicMock()
    matches_collection.find_one.return_value = None
    monkeypatch.setattr(jobs, "matches_coll", matches_collection)
    transition = MagicMock()
    monkeypatch.setattr(jobs, "_deliver_invitation_on_match", transition)
    queue = MagicMock()
    monkeypatch.setattr(jobs, "queue_mediator_event", queue)

    assert jobs.run_one_match_search_job()
    assert pipeline.call_args.kwargs["delivery_mode"] == jobs.INVITE_ON_MATCH
    assert pipeline.call_args.kwargs["origin_room_id"] == "room"
    transition.assert_called_once_with(job, [{"match_id": "match-1", "matched_user_id": "candidate"}])
    queue.assert_called_once()
    assert queue.call_args.args[0] == "owner"
    assert queue.call_args.args[2] == "match_proposal"
    assert queue.call_args.kwargs["event_key"] == "match-search-job:job-1:proposal"
    assert queue.call_args.args[1] == "已替你送出「滑雪」邀請，正在等對方回覆。"


def test_invite_transition_uses_job_idempotency_key_and_draft_revision():
    job = {
        "job_id": "job-42",
        "user_id": "owner",
        "delivery_mode": jobs.INVITE_ON_MATCH,
    }
    with patch("services.match_action_service.decide_match", return_value={"status": "success"}) as decide:
        jobs._deliver_invitation_on_match(
            job,
            [{"match_id": "64f000000000000000000001", "proposal_revision": 0}],
        )

    assert decide.call_args.kwargs == {
        "user_id": "owner",
        "match_id": "64f000000000000000000001",
        "action": "accept",
        "expected_status": "draft",
        "expected_revision": 0,
        "expected_namespace": "relationship_match",
        "idempotency_key": "match-search-job:job-42:invite-on-match",
    }


def test_invite_pending_effects_notify_requester_and_receiver():
    match_doc = {
        "_id": "match-1",
        "from_user": "owner",
        "to_user": "candidate",
        "status": "pending",
        "delivery_mode": jobs.INVITE_ON_MATCH,
        "proposal_namespace": "relationship_match",
    }
    with patch.object(actions, "profiles_coll") as profiles, \
            patch.object(actions, "queue_mediator_event") as queue:
        profiles.find_one.return_value = {"display_name": "小葵", "mediator_tone": "friend"}
        actions.apply_transition_effects(match_doc, "accept", "draft", [])

    assert [call.args[0] for call in queue.call_args_list] == ["candidate", "candidate"]
    assert queue.call_args_list[0].args[2] == "incoming_match_intro"
    assert queue.call_args_list[1].args[2] == "incoming_match_interest"


def test_source_entry_is_a_saved_message_shaped_allowlisted_projection():
    entry = _safe_source_entry(
        {
            "_id": "system-event:source",
            "message_id": "system-event:source",
            "sender_id": "ai_assistant",
            "content": "我找到一位可以介紹給你的人，介紹放在阿月牽線。",
            "message_type": "text",
            "timestamp": 123.0,
            "metadata": {"participant_id": "must-not-leak"},
        },
        room_id="ai_room::owner::origin",
        match_id="64f000000000000000000001",
        destination_room_id="ai_room::owner::match_hub",
        proposal_namespace="relationship_match",
    )

    assert entry == {
        "room_id": "ai_room::owner::origin",
        "message_id": "system-event:source",
        "sender_id": "ai_assistant",
        "content": "我找到一位可以介紹給你的人，介紹放在阿月牽線。",
        "message_type": "text",
        "metadata": {
            "event_type": "match_proposal_ready",
            "destination_room_id": "ai_room::owner::match_hub",
            "proposal_room_id": "ai_room::owner::match_hub",
            "focus_match_id": "64f000000000000000000001",
            "match_id": "64f000000000000000000001",
            "proposal_namespace": "relationship_match",
        },
        "timestamp": 123.0,
    }


def test_source_pointer_message_uses_canonical_invitation_state():
    assert _source_pointer_message({
        "status": "pending",
        "delivery_mode": jobs.INVITE_ON_MATCH,
        "search_context": {"invitation_topic": "滑雪"},
    }) == "已替你送出「滑雪」邀請，正在等對方回覆。"
    assert _source_pointer_message({
        "status": "pending",
        "delivery_mode": jobs.INVITE_ON_MATCH,
        "search_context": {},
    }) == "已替你送出邀請，正在等對方回覆。"
    assert _source_pointer_message({
        "status": "draft",
        "delivery_mode": jobs.PREVIEW_ON_MATCH,
    }) == "我找到一位可以介紹給你的人，介紹放在阿月牽線。"


def test_reclaimed_job_recovers_draft_without_rerunning_or_spending_again(monkeypatch):
    match_id = ObjectId("64f000000000000000000011")
    jobs_collection = Collection([{
        "_id": "job-row",
        "job_id": "job-reclaimed",
        "user_id": "owner",
        "active_user_id": "owner",
        "status": "running",
        "lease_id": "expired-lease",
        "lease_until": 0,
        "step": "proposal_write",
        "progress_percent": 85,
        "delivery_mode": jobs.INVITE_ON_MATCH,
        "search_context": {"invitation_topic": "滑雪"},
        "origin_room_id": "room",
        "created_at": 1,
        "updated_at": 1,
    }])
    matches = Collection([{
        "_id": match_id,
        "search_job_id": "job-reclaimed",
        "from_user": "owner",
        "to_user": "candidate",
        "status": "draft",
        "proposal_revision": 0,
        "proposal_namespace": "relationship_match",
        "delivery_mode": jobs.INVITE_ON_MATCH,
    }])
    profiles = Collection([{
        "user_id": "owner",
        "active_match_search_job_id": "job-reclaimed",
        "matchmaking_in_progress": True,
    }])
    pipeline = MagicMock()
    queue = MagicMock()
    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", jobs_collection)
    monkeypatch.setattr(jobs, "matches_coll", matches)
    monkeypatch.setattr(jobs, "profiles_coll", profiles)
    monkeypatch.setattr(decisions, "matches_coll", matches)
    monkeypatch.setattr(actions, "matches_coll", matches)
    monkeypatch.setattr(actions, "profiles_coll", profiles)
    monkeypatch.setattr(actions, "apply_transition_effects", MagicMock())
    monkeypatch.setattr(jobs, "_pipeline", pipeline)
    monkeypatch.setattr(jobs, "_report_progress", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(jobs, "_job_has_lease", lambda _job: True)
    monkeypatch.setattr(jobs, "queue_mediator_event", queue)

    assert jobs.run_one_match_search_job()
    assert not pipeline.called
    assert matches.find_one({"_id": match_id})["status"] == "pending"
    assert jobs_collection.find_one({"_id": "job-row"})["status"] == "completed"
    queue.assert_called_once()
    assert queue.call_args.args[1] == "已替你送出「滑雪」邀請，正在等對方回覆。"


def test_reclaimed_job_accepts_already_accepted_proposal_without_rerunning(monkeypatch):
    match_id = ObjectId("64f000000000000000000012")
    jobs_collection = Collection([{
        "_id": "job-row",
        "job_id": "job-accepted",
        "user_id": "owner",
        "active_user_id": "owner",
        "status": "running",
        "lease_id": "expired-lease",
        "lease_until": 0,
        "step": "proposal_write",
        "progress_percent": 85,
        "delivery_mode": jobs.INVITE_ON_MATCH,
        "search_context": {"invitation_topic": "滑雪"},
        "origin_room_id": "room",
        "created_at": 1,
        "updated_at": 1,
    }])
    matches = Collection([{
        "_id": match_id,
        "search_job_id": "job-accepted",
        "from_user": "owner",
        "to_user": "candidate",
        "status": "accepted",
        "proposal_revision": 2,
        "proposal_namespace": "relationship_match",
        "delivery_mode": jobs.INVITE_ON_MATCH,
    }])
    profiles = Collection([{
        "user_id": "owner",
        "active_match_search_job_id": "job-accepted",
        "matchmaking_in_progress": True,
    }])
    pipeline = MagicMock()
    queue = MagicMock()
    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", jobs_collection)
    monkeypatch.setattr(jobs, "matches_coll", matches)
    monkeypatch.setattr(jobs, "profiles_coll", profiles)
    monkeypatch.setattr(decisions, "matches_coll", matches)
    monkeypatch.setattr(actions, "matches_coll", matches)
    monkeypatch.setattr(actions, "profiles_coll", profiles)
    monkeypatch.setattr(actions, "apply_transition_effects", MagicMock())
    monkeypatch.setattr(jobs, "_pipeline", pipeline)
    monkeypatch.setattr(jobs, "_report_progress", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(jobs, "_job_has_lease", lambda _job: True)
    monkeypatch.setattr(jobs, "queue_mediator_event", queue)

    assert jobs.run_one_match_search_job()
    assert not pipeline.called
    assert matches.find_one({"_id": match_id})["status"] == "accepted"
    assert jobs_collection.find_one({"_id": "job-row"})["status"] == "completed"
    queue.assert_called_once()
    assert queue.call_args.args[1] == "已替你送出「滑雪」邀請，正在等對方回覆。"
