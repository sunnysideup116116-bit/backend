"""Offline cross-service contracts for interests and opt-in proposal feedback."""

from copy import deepcopy
from unittest.mock import MagicMock

import pytest
import requests
from bson import ObjectId
from fastapi import FastAPI
from fastapi.testclient import TestClient

from models import ChatRequest
from routers import chat_onboarding, match as routes
from services import assessment_session_service as assessment
from services import match_action_service as actions
from services import match_decision_service as decisions
from tests.match_flow_store import Collection


MATCH_ID = "64f000000000000000000001"


def proposal(status="draft", namespace="relationship_match"):
    return {
        "_id": ObjectId(MATCH_ID), "from_user": "alice", "to_user": "bob",
        "status": status, "proposal_revision": 0,
        "proposal_namespace": namespace, "reason_version": "v4_friend_intro",
        "distinctive_tags": ["小柏喜歡夜生活", "重視自由"],
        "match_context_snapshot": {
            "target": {"user_id": "alice", "public_personality": "偏安靜",
                       "current_context": "平常喜歡看電影", "context_signals": {"activity": "看電影"}},
            "candidate": {"user_id": "bob", "public_personality": "小柏比較外向",
                          "current_context": "想參加音樂祭", "context_signals": {"activity": "音樂祭"}},
        },
    }


@pytest.fixture
def http_flow(monkeypatch):
    matches = Collection([proposal()])
    for module in (routes, actions, decisions):
        monkeypatch.setattr(module, "matches_coll", matches)
    monkeypatch.setattr(routes, "public_display_name", lambda uid: "小柏" if uid == "bob" else "小安")
    queue = MagicMock()
    monkeypatch.setattr(actions, "queue_mediator_event", queue)
    post = MagicMock()
    post.return_value.json.return_value = {"status": "success", "memories": [{
        "key": "concept_nightlife", "label": "夜生活", "stance": "avoid", "confidence": 1.0,
    }]}
    monkeypatch.setattr(actions.requests, "post", post)
    save = MagicMock()
    monkeypatch.setattr(actions, "upsert_preference_facts", save)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        yield client, matches, post, save, queue


def decision_body(*, reasons=(), status="draft", action="decline", namespace="relationship_match"):
    return {
        "user_id": "bob" if status == "pending" and action != "cancel" else "alice",
        "match_id": MATCH_ID, "action": action, "expected_status": status,
        "expected_revision": 0, "proposal_namespace": namespace,
        "explicit_reasons": list(reasons),
    }


@pytest.mark.parametrize("status", ["draft", "pending"])
@pytest.mark.parametrize("namespace", ["relationship_match", "event_invitation"])
def test_selected_reasons_survive_http_cas_and_feedback_with_owner_evidence(http_flow, status, namespace):
    client, matches, post, save, _ = http_flow
    matches.rows[:] = [proposal(status, namespace)]
    reasons = ["個性：喜歡夜生活", "近期情境：想參加音樂祭", "價值觀：重視自由"]
    response = client.post("/api/match/decision", json=decision_body(reasons=reasons, status=status, namespace=namespace))
    assert response.status_code == 200
    assert response.json()["new_status"] == "declined"
    actor, other = ("alice", "bob") if status == "draft" else ("bob", "alice")
    post.assert_called_once_with("http://127.0.0.1:9001/api/feedback", json={
        "user_id": actor, "target_id": other, "action": "decline",
        "target_traits": {}, "explicit_reasons": reasons,
    }, timeout=15)
    save.assert_called_once_with(
        actor, post.return_value.json.return_value["memories"], source="match_feedback",
        message_id=f"feedback:{MATCH_ID}:{actor}", match_id=MATCH_ID,
    )
    assert matches.rows[0]["status"] == "declined"
    # A retry cannot forward a second preference observation.
    retry = client.post("/api/match/decision", json=decision_body(reasons=reasons, status=status, namespace=namespace))
    assert retry.status_code == 409
    assert post.call_count == save.call_count == 1


@pytest.mark.parametrize("reasons", [[], ["", "   "]])
def test_opt_out_declines_without_any_feedback_or_preference_write(http_flow, reasons):
    client, matches, post, save, _ = http_flow
    response = client.post("/api/match/decision", json=decision_body(reasons=reasons))
    assert response.status_code == 200
    assert matches.rows[0]["status"] == "declined"
    post.assert_not_called()
    save.assert_not_called()


def test_cancel_is_not_preference_consent_even_if_client_sends_reasons(http_flow):
    client, matches, post, save, queue = http_flow
    matches.rows[:] = [proposal("pending")]
    response = client.post("/api/match/decision", json=decision_body(reasons=["夜生活"], status="pending", action="cancel"))
    assert response.status_code == 200
    post.assert_not_called()
    save.assert_not_called()
    assert queue.call_args.args[2] == "match_cancelled"


def test_feedback_outage_does_not_undo_or_repeat_a_successful_decline(http_flow):
    client, matches, post, save, _ = http_flow
    post.side_effect = requests.Timeout("stub provider offline")
    response = client.post("/api/match/decision", json=decision_body(reasons=["夜生活"]))
    assert response.status_code == 200
    assert response.json()["new_status"] == matches.rows[0]["status"] == "declined"
    save.assert_not_called()


def test_new_card_state_has_viewer_bound_anonymized_options(http_flow):
    client, matches, _, _, _ = http_flow
    initial = deepcopy(matches.rows)
    response = client.get("/api/match/state", params={"user_id": "alice", "match_id": MATCH_ID})
    assert response.status_code == 200
    options = response.json()["decline_reason_options"]
    assert "對方喜歡夜生活" in options
    assert "音樂祭" in options
    assert not any("小柏" in option or "bob" in option for option in options)
    assert "偏安靜" not in options
    assert "other_id" not in response.json()
    assert matches.rows == initial

    matches.rows[0]["status"] = "pending"
    receiver = client.get("/api/match/state", params={"user_id": "bob", "match_id": MATCH_ID}).json()
    assert "偏安靜" in receiver["decline_reason_options"]
    assert "看電影" in receiver["decline_reason_options"]
    assert "重視自由" not in receiver["decline_reason_options"]


def test_event_options_are_from_saved_public_event_and_counterparty(http_flow):
    client, matches, _, _, _ = http_flow
    matches.rows[:] = [proposal(namespace="event_invitation")]
    matches.rows[0].update(proposal_source="event_opportunity", event_snapshot={
        "title": "音樂祭", "category": "音樂", "event_id": "private-event-id",
    })
    response = client.get("/api/match/state", params={"user_id": "alice", "match_id": MATCH_ID})
    assert response.json()["decline_reason_options"][0] == "音樂"
    assert "private-event-id" not in response.text
    assert "重視自由" not in response.json()["decline_reason_options"]


def test_foreign_and_terminal_cards_never_supply_actionable_feedback_options(http_flow):
    client, matches, _, _, _ = http_flow
    assert client.get("/api/match/state", params={"user_id": "stranger", "match_id": MATCH_ID}).status_code == 403
    matches.rows[0]["status"] = "declined"
    response = client.get("/api/match/state", params={"user_id": "alice", "match_id": MATCH_ID})
    assert "decline_reason_options" not in response.json()


@pytest.mark.parametrize("existing_interest", [None, "", "平常喜歡閱讀"])
def test_initialize_interest_is_idempotent_and_does_not_create_another_profile(monkeypatch, existing_interest):
    profile = {"user_id": "owner", "current_context": "最近想看展", "big_five": {"summary": "保留舊結果"}}
    if existing_interest is not None:
        profile["initial_interest"] = existing_interest
    profiles = Collection([profile])
    monkeypatch.setattr(assessment, "profiles_coll", profiles)
    monkeypatch.setattr(chat_onboarding, "profiles_coll", profiles)
    analyze = MagicMock()
    monkeypatch.setattr(assessment, "analyze_big_five", analyze)
    for supplied in ("平常喜歡爬山", "平常喜歡電影", None, "無特別興趣"):
        response = chat_onboarding.chat_endpoint(ChatRequest(
            user_id="owner", message="", state="big_five", initialize=True,
            **({"initial_interest": supplied} if supplied is not None else {}),
        ))
        assert response["assessment_state"] == "active"
    assert len(profiles.rows) == 1
    saved = profiles.rows[0]
    assert saved["initial_interest"] == (existing_interest or "平常喜歡爬山")
    assert saved["current_context"] == "最近想看展"
    assert saved["big_five"] == {"summary": "保留舊結果"}
    assert saved["agentic_assessment_session"]["turn_count"] == 0
    analyze.assert_not_called()


def test_new_owner_interest_and_draft_share_the_same_mongo_profile(monkeypatch):
    profiles = Collection()
    monkeypatch.setattr(assessment, "profiles_coll", profiles)
    result = assessment.handle_assessment_ui_message("new-owner", "big_five", "", initial_interest="做甜點", initialize=True)
    assert result["status"] == "started"
    assert len(profiles.rows) == 1
    assert profiles.rows[0]["initial_interest"] == "做甜點"
    assert profiles.rows[0]["agentic_assessment_session"]["user_id"] == "new-owner"
