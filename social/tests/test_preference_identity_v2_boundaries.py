"""Hermetic ingress/confirmation regressions for complete preference input."""
from types import SimpleNamespace
from unittest.mock import patch

import mongomock
import pytest
from pydantic import ValidationError

from matchmaker_agent.concept_identity import (
    MAX_PREFERENCE_TEXT_CHARS, PreferenceTextError, canonicalize_concept,
)
from models import MatchRequest, ProfileMemoryAddRequest
from services.match_search_context import safe_search_context
from services.ayue_agent.pi.match_tools import MatchSearchInput, _prepare_search
from services.ayue_agent.shared import write_executors
from services.ayue_agent.contracts import AgentTurnContext


LONG_TOPIC = "Visiting quiet historical castles with no guided tours and step-free wheelchair access"


@pytest.mark.parametrize("topic", [LONG_TOPIC, "Kpop", "K-pop", "Board Games"])
def test_preference_context_roundtrip_retains_full_semantic_text(topic):
    identity = canonicalize_concept(topic)
    result = safe_search_context({
        "search_intent": "preference", "normalized_topic": topic,
        "canonical_preference_key": "forged", "display_label": "forged",
        "canonicalization_version": "v1", "raw_private_memory": "not allowed",
        "candidate_ids": ["not allowed"],
    })
    assert result["semantic_text"] == identity.semantic_text
    assert result["normalized_topic"] == identity.semantic_text
    assert result["canonical_preference_key"] == identity.key
    assert result["canonicalization_version"] == "v2"
    assert result["display_label"] == identity.display_label
    assert "raw_private_memory" not in result and "candidate_ids" not in result
    assert safe_search_context(result) == result


def test_conflicting_semantic_and_normalized_fields_fail_closed():
    with pytest.raises(PreferenceTextError):
        safe_search_context({"search_intent": "preference", "semantic_text": LONG_TOPIC,
                             "normalized_topic": "Noisy parties"})


@pytest.mark.parametrize("value", [
    {"search_intent": "preference"},
    {"search_intent": "preference", "normalized_topic": "x" * 501},
    {"search_intent": "preference", "normalized_topic": "K-pop", "query_text": "x" * 601},
])
def test_invalid_explicit_preference_is_not_generic(value):
    with pytest.raises(ValueError):
        MatchRequest(user_id="synthetic-owner", search_context=value)
    ctx = AgentTurnContext(user_id="synthetic-owner", room_id="room", message="確認")
    with patch.object(write_executors, "_claim_once") as claim, \
         patch.object(write_executors, "start_match_search") as start:
        ok, _, error = write_executors._start_search(
            ctx, "run", 0, confirmation_id="choice", payload={"search_context": value},
        )
    assert not ok and error
    claim.assert_not_called()
    start.assert_not_called()


def test_other_intents_do_not_accept_preference_metadata_or_changed_limits():
    result = safe_search_context({
        "search_intent": "activity", "invitation_topic": "x" * 100,
        "query_text": "y" * 650, "semantic_text": LONG_TOPIC,
        "canonicalization_version": "v2", "semantic_input_hash": "forged",
    })
    assert len(result["invitation_topic"]) == 80
    assert len(result["query_text"]) == 600
    assert not {"semantic_text", "canonicalization_version", "semantic_input_hash"} & result.keys()
    with pytest.raises(ValidationError):
        MatchSearchInput(kind="activity", topic=LONG_TOPIC)


def test_preference_confirmation_then_executor_preserves_complete_topic():
    ctx = AgentTurnContext(user_id="synthetic-owner", room_id="room",
                          message=f"幫我找喜歡 {LONG_TOPIC} 的人")
    turn = SimpleNamespace(active_proposal=None)
    with patch.object(write_executors, "assess_match_opportunity", return_value=SimpleNamespace(state="ready")):
        payload, _ = write_executors.prepare_write_confirmation(
            "match.start_search", {"search_request": {"kind": "preference", "topic": LONG_TOPIC}}, ctx, turn,
        )
    assert payload["data"]["search_context"]["semantic_text"] == LONG_TOPIC
    assert "delivery_mode" not in payload["data"]
    with patch.object(write_executors, "_claim_once", return_value=True), \
         patch.object(write_executors, "_finish"), \
         patch.object(write_executors, "start_match_search", return_value={"status": "queued"}) as start:
        ok, _, _ = write_executors._start_search(ctx, "run", 0, confirmation_id="choice", payload=payload["data"])
    assert ok
    assert start.call_args.kwargs["search_context"]["semantic_text"] == LONG_TOPIC


def test_overlimit_source_never_prepares_confirmation():
    message = "幫我找喜歡 K-pop 的人 " + "x" * 600
    ctx = AgentTurnContext(user_id="synthetic-owner", room_id="room", message=message)
    runtime = SimpleNamespace(turn=SimpleNamespace(_raw_ctx=ctx, message=message, recent_messages=[]))
    with patch("services.ayue_agent.pi.match_tools.prepare_write_confirmation") as prepare:
        payload, error = _prepare_search(runtime, MatchSearchInput(kind="preference", topic="K-pop"))
    assert payload is None and error
    prepare.assert_not_called()


def test_manual_memory_preserves_full_semantics_and_rejects_normalization_expansion():
    from routers import system
    request = ProfileMemoryAddRequest(user_id="synthetic-owner", label=LONG_TOPIC,
                                      request_id="synthetic-request")
    identity = canonicalize_concept(LONG_TOPIC)
    with patch("services.memory_service.apply_profile_memory_proposals", return_value=[identity.as_dict()]) as apply:
        system.add_profile_memory(request)
    proposal = apply.call_args.args[1][0]
    assert proposal["semantic_text"] == LONG_TOPIC
    assert proposal["key"] == identity.key
    for label in ("x" * (MAX_PREFERENCE_TEXT_CHARS + 1), "ﬃ" * 200):
        with pytest.raises(ValidationError):
            ProfileMemoryAddRequest(user_id="synthetic-owner", label=label, request_id="synthetic-request")


@pytest.mark.parametrize("topic", [LONG_TOPIC, "a" * MAX_PREFERENCE_TEXT_CHARS])
def test_long_preference_pi_persisted_confirmation_runs_once(monkeypatch, topic):
    from services.ayue_agent.pi import tool_runtime
    from services.ayue_agent.shared.confirmation import ConfirmationManager
    from tests.test_pi_feedback_regressions import make_turn

    store = mongomock.MongoClient().test
    monkeypatch.setattr(write_executors, "assess_match_opportunity", lambda *_a, **_k: SimpleNamespace(state="ready"))
    monkeypatch.setattr(write_executors, "TOOL_CALLS", store.calls)
    request, turn = make_turn(f"幫我找喜歡 {topic} 的人")
    runtime = tool_runtime.PiToolRuntime(
        turn, run_id="v2-long", trace={}, confirmation_collection=store.c,
        contact_selection_collection=store.s, operation_batch_collection=store.b,
    )
    with patch.object(write_executors, "start_match_search", return_value={"status": "queued"}) as start:
        runtime.dispatch("match.start_search", {"kind": "preference", "topic": topic})
        start.assert_not_called()
        record = store.c.find_one({})
        assert record["payload"]["search_context"]["semantic_text"] == topic
        manager = ConfirmationManager(store.c)
        manager.bind_final_preview(user_id=request.user_id, origin_run_id="v2-long", final_content=record["preview_text"])
        assert manager.mark_presented(user_id=request.user_id, origin_run_id="v2-long", message_id="saved", persisted_content=record["preview_text"])
        for _ in range(2):
            manager.execute_confirmed(
                user_id=request.user_id, room_id=request.room_id, surface="public_ayue", choice_id=record["_id"],
                executor=lambda name, args, uid, payload: write_executors.execute_write(name, args, request, turn, "confirm-v2", 0, payload=payload),
            )
        assert start.call_count == 1
        assert start.call_args.kwargs["search_context"]["semantic_text"] == topic
        assert "delivery_mode" not in start.call_args.kwargs


def test_correction_rejects_oversize_before_http_and_returns_permanent_validation_error():
    from fastapi import HTTPException
    from routers import system

    request = system.ProfileMemoryActionRequest(
        user_id="synthetic-owner", key="legacy_key", action="correct", value="x" * 501,
    )
    with patch("services.memory_service.requests.post") as post:
        with pytest.raises(HTTPException) as raised:
            system.profile_memory_action(request)
    assert raised.value.status_code == 422
    post.assert_not_called()


def test_manual_short_chinese_s2t_parity_and_post_conversion_limit():
    from fastapi import HTTPException
    from routers import system
    request = system.ProfileMemoryAddRequest(user_id="synthetic-owner", label="喜欢安静的咖啡厅", request_id="synthetic-request")
    with patch("services.memory_service.apply_profile_memory_proposals", return_value=[{"label": "安靜的咖啡廳"}]) as apply:
        system.add_profile_memory(request)
    assert apply.call_args.args[1][0]["semantic_text"] == "安靜的咖啡廳"
    with patch("matchmaker_agent.concept_identity._fresh_preference_converter",
               return_value=SimpleNamespace(convert=lambda _text: "x" * 501)), \
         patch("services.memory_service.apply_profile_memory_proposals") as apply:
        with pytest.raises(HTTPException) as raised:
            system.add_profile_memory(request)
    assert raised.value.status_code == 422
    apply.assert_not_called()
