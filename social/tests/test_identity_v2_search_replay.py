"""Synthetic persisted-request fidelity tests; no Graph/provider/service I/O."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

import mongomock
import pytest

from matchmaker_agent.concept_identity import PreferenceTextError, canonicalize_concept_v1
from services import match_search_job_service as jobs
from services.match_search_context import safe_search_context, validate_persisted_search_context
from services.ayue_agent.shared import write_executors as writes
from services.ayue_agent.shared.confirmation import ConfirmationManager, INTERACTION_LEGACY, SURFACE_PUBLIC


TOPIC = "Quiet castle visits with independent exploration and no guided tours"
ERROR = "preference_search_reconfirmation_required"


@pytest.fixture
def full_context():
    return safe_search_context({"search_intent": "preference", "normalized_topic": TOPIC,
                                "query_text": "Find someone who prefers " + TOPIC})


@pytest.fixture(params=["legacy_prefix", "missing_version", "display_only", "forged_hash", "wrong_key", "topic_prefix", "wrong_version"])
def bad_context(request, full_context):
    value = deepcopy(full_context)
    if request.param == "legacy_prefix":
        legacy = canonicalize_concept_v1(TOPIC)
        return {"search_intent": "preference", "normalized_topic": legacy.label,
                "canonical_preference_key": legacy.key}
    if request.param == "missing_version":
        value.pop("canonicalization_version")
    elif request.param == "display_only":
        value.pop("semantic_text")
    elif request.param == "forged_hash":
        value["semantic_input_hash"] = "incorrect"
    elif request.param == "wrong_key":
        value["canonical_preference_key"] = "wrong"
    elif request.param == "topic_prefix":
        value["normalized_topic"] = value["display_label"]
    elif request.param == "wrong_version":
        value["canonicalization_version"] = "v1"
    return value


def test_persisted_preference_source_cannot_be_upgraded_or_repaired(bad_context):
    before = deepcopy(bad_context)
    with pytest.raises(PreferenceTextError) as raised:
        validate_persisted_search_context(bad_context)
    assert raised.value.code == ERROR
    assert bad_context == before


def test_legacy_preference_job_fails_before_pipeline_or_quota(bad_context):
    job = {"_id": "job", "job_id": "synthetic-job", "user_id": "owner",
           "source": "agent_pi", "quota_billable": True, "search_context": bad_context}
    with patch.object(jobs, "_claim_next_job", return_value=job), \
            patch.object(jobs, "_report_progress", return_value=True), \
            patch.object(jobs, "_pipeline") as pipeline, \
            patch.object(jobs, "task_scope") as quota_scope, \
            patch.object(jobs, "_finish_job", return_value=True) as finish, \
            patch.object(jobs, "queue_mediator_event") as event:
        assert jobs.run_one_match_search_job()
    pipeline.assert_not_called()
    quota_scope.assert_not_called()
    finish.assert_called_once_with(job, "failed", error_code=ERROR, failure_stage="preference_input")
    assert event.call_args.args[2] == "match_search_failed"
    assert "重新提供" in event.call_args.args[1]


def test_full_v2_job_replays_without_changing_semantic_source(full_context):
    job = {"_id": "job", "job_id": "synthetic-job", "user_id": "owner",
           "source": "agent_pi", "search_context": full_context}
    original = deepcopy(job)
    with patch.object(jobs, "_claim_next_job", return_value=job), \
            patch.object(jobs, "_report_progress", return_value=True), \
            patch.object(jobs, "_pipeline", side_effect=[{"status": "stale"}, {"matches": []}]) as pipeline, \
            patch.object(jobs, "_settle_stale_job") as retry, \
            patch.object(jobs, "_job_has_ownership", return_value=True), \
            patch.object(jobs, "_finish_job", return_value=True), \
            patch.object(jobs, "queue_mediator_event"):
        assert jobs.run_one_match_search_job()
        assert jobs.run_one_match_search_job()
    retry.assert_called_once_with(job)
    assert pipeline.call_count == 2
    for call in pipeline.call_args_list:
        assert call.kwargs["search_context"] == full_context
        assert call.kwargs["search_context"]["semantic_text"] == TOPIC
    assert job == original


@pytest.mark.parametrize("context", [None, {}, {"search_intent": "generic"},
                                      {"search_intent": "recent_context"},
                                      {"search_intent": "activity", "invitation_topic": "看展", "query_text": "一起看展"}])
def test_non_preference_replay_retains_existing_contract(context):
    assert validate_persisted_search_context(context) == safe_search_context(context)


def test_confirmation_payload_guard_runs_before_domain_idempotency_claim(bad_context):
    collection = mongomock.MongoClient().db.confirmations
    manager = ConfirmationManager(collection)
    choice = manager.create_confirmation(
        user_id="owner", agent_name="synthetic", tool_name="match.start_search", arguments={},
        payload={"search_context": bad_context}, origin_run_id="run", preview="要開始搜尋嗎？",
        room_id="room", surface=SURFACE_PUBLIC, interaction_mode=INTERACTION_LEGACY,
    )
    context = SimpleNamespace(user_id="owner", room_id="room")
    with patch.object(writes, "_claim_once") as claim, patch.object(writes, "start_match_search") as start:
        result = manager.execute_confirmed(
            user_id="owner", choice_id=choice, surface=SURFACE_PUBLIC,
            executor=lambda tool, args, _uid, payload: writes.execute_write(
                tool, args, context, SimpleNamespace(), "run", 0, payload=payload,
            ),
        )
    assert result[0]["ok"] is False
    assert result[0]["error_code"] == ERROR
    assert collection.find_one()["status"] == "failed"
    assert collection.find_one()["payload"]["search_context"] == bad_context
    claim.assert_not_called()
    start.assert_not_called()


def test_full_v2_confirmation_stays_bound_and_executes_once(full_context):
    collection = mongomock.MongoClient().db.confirmations
    manager = ConfirmationManager(collection)
    choice = manager.create_confirmation(
        user_id="owner", agent_name="synthetic", tool_name="match.start_search", arguments={},
        payload={"search_context": full_context}, origin_run_id="run", preview="要開始搜尋嗎？",
        room_id="room", surface=SURFACE_PUBLIC, interaction_mode=INTERACTION_LEGACY,
    )
    context = SimpleNamespace(user_id="owner", room_id="room")
    executor = lambda tool, args, _uid, payload: writes.execute_write(
        tool, args, context, SimpleNamespace(), "run", 0, payload=payload,
    )
    with patch.object(writes, "_claim_once", return_value=True) as claim, \
            patch.object(writes, "_finish"), \
            patch.object(writes, "start_match_search", return_value={"status": "queued"}) as start:
        first = manager.execute_confirmed(user_id="owner", choice_id=choice, surface=SURFACE_PUBLIC, executor=executor)
        second = manager.execute_confirmed(user_id="owner", choice_id=choice, surface=SURFACE_PUBLIC, executor=executor)
    assert first[0]["ok"] is True
    assert second == []
    claim.assert_called_once()
    start.assert_called_once()
    assert start.call_args.kwargs["search_context"] == full_context
    assert collection.find_one()["status"] == "completed"
    assert collection.find_one()["payload"]["search_context"] == full_context


def test_completed_proposal_checkpoint_is_reconciled_before_legacy_guard():
    job = {"_id": "job", "job_id": "synthetic-job", "user_id": "owner", "step": "proposal_write",
           "search_context": {"search_intent": "preference", "normalized_topic": "legacy prefix"}}
    proposal = {"_id": "already-created", "from_user": "owner", "to_user": "candidate", "status": "draft"}
    with patch.object(jobs, "_claim_next_job", return_value=job), \
            patch.object(jobs, "_report_progress", return_value=True), \
            patch.object(jobs, "_pipeline") as pipeline, \
            patch.object(jobs, "validate_persisted_search_context") as validate, \
            patch.object(jobs, "_live_proposal_for_job", return_value=proposal), \
            patch.object(jobs, "_deliver_invitation_on_match") as delivery, \
            patch.object(jobs, "_finish_job", return_value=True) as finish, \
            patch.object(jobs, "_queue_completed_match_event"):
        assert jobs.run_one_match_search_job()
    pipeline.assert_not_called()
    validate.assert_not_called()
    delivery.assert_called_once()
    finish.assert_called_once_with(job, "completed")
