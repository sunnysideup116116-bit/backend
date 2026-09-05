from copy import deepcopy
import time

from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from services.ayue_agent import context as ayue_context, match_opportunity
from services.ayue_agent.contracts import AgentTurnContext
from services import match_state_service as state, match_search_job_service as jobs
from routers import match as router
from tests.test_match_restart_flow import flow


def test_status_api_and_real_context_builder_are_read_only(flow, monkeypatch):
    monkeypatch.setattr(router, "matches_coll", flow.matches)
    monkeypatch.setattr(router, "profiles_coll", flow.profiles)
    monkeypatch.setattr(router, "public_display_name", lambda _uid: "對方")
    monkeypatch.setattr(router, "reason_for_viewer", lambda *_args: "既有提案")
    monkeypatch.setattr(ayue_context, "matches_coll", flow.matches)
    monkeypatch.setattr(ayue_context, "profiles_coll", flow.profiles)
    monkeypatch.setattr(ayue_context, "load_validated_conversation_continuity", lambda *_args: None)
    monkeypatch.setattr(ayue_context, "validated_mentioned_contact_ids", lambda *_args: ([], False))
    monkeypatch.setattr(ayue_context, "mentioned_contact_refs", lambda *_args: [])
    flow.profiles.update_one({"user_id": "owner"}, {"$set": {
        "recent_context_draft": {"created_at": 1},
        "matchmaking_in_progress": True, "matchmaking_started_at": 1,
    }})
    before = deepcopy((flow.matches.rows, flow.profiles.rows))
    counts = (flow.matches.writes, flow.profiles.writes)
    app = FastAPI()
    app.include_router(router.router)
    client = TestClient(app)
    for room in ("legacy", "room", "another-room"):
        response = client.get("/api/match/status", params={"user_id": "owner"})
        assert response.status_code == 200
        assert response.json()["status_snapshot"]["state"] == "waiting_other"
        assert response.json()["search"]["status"] == "idle"
        ctx = AgentTurnContext(user_id="owner", room_id=room, message="我有配對中嗎？",
                               user_profile=flow.profiles.find_one({"user_id": "owner"}))
        turn = ayue_context.build_public_agent_turn_context(ctx)
        assert turn.active_proposal["stage"] == "waiting_other"
        assert turn.match_search["status"] == "idle"
        assert turn.recent_context_draft is None
    assert counts == (flow.matches.writes, flow.profiles.writes)
    assert before == (flow.matches.rows, flow.profiles.rows)


def test_only_event_invitation_does_not_block_search(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "declined"}})
    flow.matches.insert_one({"from_user": "owner", "to_user": "event-person", "status": "pending",
                            "proposal_namespace": "event_invitation", "created_at": 300})
    assert not state.load_match_state("owner")["search_blocked"]
    assert match_opportunity.assess_match_opportunity(flow.profiles.rows[0], "owner", explicit_search=True).state == "ready"


def test_ambiguous_live_proposals_block_all_search_entrypoints(flow):
    flow.matches.insert_one({"from_user": "owner", "to_user": "third", "status": "draft", "created_at": 300})
    assert state.load_match_state("owner")["ambiguous"]
    assert state.get_match_status_snapshot("owner")["reason_code"] == "ambiguous_live_match"
    assert jobs.enqueue_match_search("owner", source="test", idempotency_key="blocked") == {"status": "already_active"}
    assert not flow.jobs.rows


def test_old_locks_do_not_block_reads_and_cleanup_is_worker_only(flow):
    flow.matches.update_one({"_id": flow.old_id}, {"$set": {"status": "declined"}})
    flow.profiles.update_one({"user_id": "owner"}, {"$set": {
        "matchmaking_in_progress": True, "matchmaking_started_at": 1,
        "match_search": {"status": "running", "updated_at": 1},
    }})
    count = flow.profiles.writes
    assert state.load_match_state("owner")["search"]["status"] == "idle"
    assert flow.profiles.writes == count
    jobs.cleanup_legacy_search_locks()
    assert flow.profiles.rows[0]["matchmaking_in_progress"] is False


def test_active_job_wins_over_old_terminal_profile_and_prevents_lock_cleanup(flow):
    flow.jobs.insert_one({"user_id": "owner", "active_user_id": "owner", "job_id": "active",
                         "status": "running", "created_at": 2, "step": "vector_search"})
    flow.jobs.insert_one({"user_id": "owner", "job_id": "other", "status": "no_candidates", "created_at": 3})
    flow.profiles.update_one({"user_id": "owner"}, {"$set": {"matchmaking_in_progress": True, "matchmaking_started_at": 1}})
    before = flow.profiles.writes
    assert jobs.public_match_search_status("owner")["status"] == "running"
    jobs.cleanup_legacy_search_locks()
    assert flow.profiles.writes == before
