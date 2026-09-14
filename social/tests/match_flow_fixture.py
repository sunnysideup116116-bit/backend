"""Shared in-memory match-service fixture with no public-agent runtime dependency."""

from types import SimpleNamespace

from bson import ObjectId
import pytest

from services import match_action_service as actions
from services import match_decision_service as decisions
from services import match_search_job_service as jobs
from services import match_state_service as state
from services.ayue_agent import match_opportunity
from tests.match_flow_store import Collection


@pytest.fixture
def flow(monkeypatch):
    old_id, expired_id = ObjectId(), ObjectId()
    matches = Collection([
        {"_id": old_id, "from_user": "owner", "to_user": "first", "status": "pending",
         "proposal_revision": 2, "created_at": 100, "proposal_namespace": "relationship_match"},
        {"_id": expired_id, "from_user": "owner", "to_user": "historical", "status": "expired",
         "expired_reason": "draft_timeout", "proposal_revision": 1, "created_at": 200,
         "proposal_namespace": "relationship_match"},
    ])
    profiles = Collection([{
        "user_id": "owner", "current_context": "週末想看展覽",
        "big_five": {"summary": "喜歡探索"}, "current_context_revision": 1,
    }])
    searches, choices, messages = Collection(), Collection(), Collection()
    for module in (state, actions, decisions, match_opportunity):
        if hasattr(module, "matches_coll"):
            monkeypatch.setattr(module, "matches_coll", matches)
    for module in (state, actions, jobs, match_opportunity):
        if hasattr(module, "profiles_coll"):
            monkeypatch.setattr(module, "profiles_coll", profiles)
    monkeypatch.setattr(jobs, "MATCH_SEARCH_JOBS", searches)
    monkeypatch.setattr(actions, "apply_transition_effects", lambda *_args, **_kwargs: None)

    def send(_message="", intent="start_search", **_kwargs):
        if intent == "dismiss_proposal":
            return SimpleNamespace(
                choice_prompt=None,
                reply="請到阿月牽線處理；我沒有替你改變邀請狀態。",
                match_state_changed=False,
            )
        return SimpleNamespace(choice_prompt=None, reply="", match_state_changed=False)

    return SimpleNamespace(
        send=send, matches=matches, profiles=profiles, jobs=searches,
        choices=choices, messages=messages, old_id=old_id, expired_id=expired_id,
    )
