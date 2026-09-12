"""Offline contracts for typed failures, bounded retries and session dialogue."""

import json
from unittest.mock import MagicMock

import httpx
import mongomock
import pytest

from models import ChatRequest
from routers import chat_onboarding
from services import ai_service, assessment_session_service as assessment
from services.assessment_provider import (
    AssessmentProviderError, request_assessment_json, validate_assessment_response,
)


def valid_response(reply="是自己安排，還是找朋友一起呢？"):
    return {"reply": reply, "big_five": {"O": 7}, "is_complete": False}


@pytest.fixture
def profiles(monkeypatch):
    collection = mongomock.MongoClient().test.profiles
    monkeypatch.setattr(assessment, "profiles_coll", collection)
    monkeypatch.setattr(chat_onboarding, "profiles_coll", collection)
    return collection


@pytest.mark.parametrize("bad", [None, [], True, "text", {},
    {"reply": [], "big_five": {}, "is_complete": False},
    {"reply": "下一題？", "big_five": [], "is_complete": False},
    {"reply": "下一題？", "big_five": {}, "is_complete": "false"},
    {"reply": "下一題？", "big_five": {"O": float("nan")}, "is_complete": False},
])
def test_invalid_provider_shapes_never_advance_the_session(profiles, monkeypatch, bad):
    started = assessment.start_assessment_session("owner", "big_five", idempotency_key="start")
    before = profiles.find_one({"user_id": "owner"})
    monkeypatch.setattr(assessment, "analyze_big_five", MagicMock(return_value=bad))
    result = assessment.advance_assessment_session("owner", started["session"]["session_id"], "我會先規劃")
    assert result["status"] == "provider_error"
    assert "沒聽清楚" not in result["reply"]
    assert profiles.find_one({"user_id": "owner"}) == before


def test_malformed_json_retries_once_with_the_same_deadline(caplog):
    call = MagicMock(side_effect=["private raw invalid JSON", json.dumps(valid_response())])
    assert request_assessment_json(call, kind="big_five", model="test") == valid_response()
    assert call.call_count == 2
    assert call.call_args_list[0] == call.call_args_list[1]
    assert "invalid_json" in caplog.text
    assert "private raw" not in caplog.text


@pytest.mark.parametrize("error,code", [
    (httpx.ReadTimeout("private answer"), "timeout"),
    (httpx.HTTPStatusError("secret key", request=httpx.Request("POST", "https://example.test"),
        response=httpx.Response(429)), "rate_limited"),
    (httpx.HTTPStatusError("secret key", request=httpx.Request("POST", "https://example.test"),
        response=httpx.Response(401)), "provider_auth"),
])
def test_quota_auth_and_slow_timeout_do_not_hot_retry(error, code, caplog):
    call = MagicMock(side_effect=error)
    with pytest.raises(AssessmentProviderError) as caught:
        request_assessment_json(call, kind="big_five", model="test")
    assert caught.value.code == code
    call.assert_called_once()
    assert "private answer" not in caplog.text
    assert "secret key" not in caplog.text


def test_empty_reply_has_a_two_attempt_ceiling():
    call = MagicMock(return_value='{"reply":"", "big_five":{}, "is_complete":false}')
    with pytest.raises(AssessmentProviderError, match="empty_reply"):
        request_assessment_json(call, kind="big_five", model="test")
    assert call.call_count == 2


def test_session_passes_only_own_questions_and_replays_a_lost_response(profiles, monkeypatch):
    profiles.insert_one({"user_id": "owner", "big_five": {"summary": "舊性格秘密"},
        "current_context": "不相關行程", "initial_interest": "舊興趣"})
    started = assessment.start_assessment_session(
        "owner", "big_five", idempotency_key="start", initial_interest="爬山",
    )
    session_id = started["session"]["session_id"]
    analyze = MagicMock(return_value=valid_response())
    monkeypatch.setattr(assessment, "analyze_big_five", analyze)
    first = assessment.advance_assessment_session("owner", session_id, "會先規劃", message_id="answer-1")
    replay = assessment.advance_assessment_session("owner", session_id, "會先規劃", message_id="answer-1")
    assert first["reply"] == replay["reply"]
    assert replay["status"] == "duplicate"
    analyze.assert_called_once()
    context = analyze.call_args.kwargs["assessment_context"]
    assert context["previous_question"] == started["reply"]
    assert context["initial_interest"] == "爬山"
    assert "舊性格秘密" not in str(analyze.call_args)
    assert "不相關行程" not in str(analyze.call_args)
    for index in range(2, 7):
        assessment.advance_assessment_session("owner", session_id, f"回答{index}", message_id=f"answer-{index}")
    saved = profiles.find_one({"user_id": "owner"})["agentic_assessment_session"]
    assert saved["turn_count"] == 6
    assert len(saved["recent_turns"]) == 3
    assert saved["recent_turns"][-1]["question"] == first["reply"]
    assessment.cancel_assessment_session("owner", session_id)
    terminal = profiles.find_one({"user_id": "owner"})
    assert "recent_turns" not in terminal["agentic_assessment_session"]
    assert terminal["big_five"]["summary"] == "舊性格秘密"


def test_onboarding_propagates_error_and_same_answer_can_retry(profiles, monkeypatch):
    initialized = chat_onboarding.chat_endpoint(ChatRequest(
        user_id="owner", state="big_five", message="", initialize=True, initial_interest="爬山",
    ))
    assert "爬山" in initialized["reply"]
    assert initialized["assessment_revision"] == 0
    analyze = MagicMock(side_effect=[AssessmentProviderError("rate_limited"), valid_response()])
    monkeypatch.setattr(assessment, "analyze_big_five", analyze)
    request = ChatRequest(user_id="owner", state="big_five", message="先查路線", client_message_id="answer-1")
    failure = chat_onboarding.chat_endpoint(request)
    assert failure["status"] == "error"
    assert failure["error_code"] == "rate_limited"
    assert failure["assessment_revision"] == 0
    assert failure["retryable"] is True
    success = chat_onboarding.chat_endpoint(request)
    assert success["status"] == "success"
    assert success["assessment_revision"] == 1
    assert chat_onboarding.chat_endpoint(request)["reply"] == success["reply"]
    assert analyze.call_count == 2


def test_model_prompt_contains_question_without_importing_unrelated_context(monkeypatch):
    generate = MagicMock(return_value=valid_response())
    monkeypatch.setattr(ai_service, "_generate_assessment_json", generate)
    ai_service.analyze_big_five("先查路線", {}, 0, None, assessment_context={
        "previous_question": "怎麼安排爬山？", "initial_interest": "爬山",
        "recent_turns": [{"question": "平常喜歡什麼？", "answer": "爬山"}],
        "long_term_memory": "不應該送出",
    })
    prompt = generate.call_args.args[0]
    assert "怎麼安排爬山" in prompt
    assert "先查路線" in prompt
    assert "不應該送出" not in prompt


def test_deep_profile_uses_the_same_typed_contract():
    result = {"reply": "你最在意哪個部分？", "deep_profile": {"values": ["真誠"]}, "is_complete": False}
    assert validate_assessment_response(result, "deep_profile") == result
    with pytest.raises(AssessmentProviderError, match="invalid_schema"):
        validate_assessment_response({**result, "deep_profile": {"values": "真誠"}}, "deep_profile")


def test_lost_commit_response_can_retry_without_starting_another_assessment(profiles, monkeypatch):
    assessment.handle_assessment_ui_message("owner", "big_five", "", initialize=True)
    result = {"reply": "完成", "big_five": {"O": 7, "C": 7, "E": 5, "A": 6, "N": 5, "summary": "重視規劃"}, "is_complete": True}
    analyze = MagicMock(return_value=result)
    monkeypatch.setattr(assessment, "analyze_big_five", analyze)
    assessment.handle_assessment_ui_message("owner", "big_five", "會先規劃", message_id="answer")
    committed = assessment.handle_assessment_ui_message("owner", "big_five", "確認", message_id="commit")
    before = profiles.find_one({"user_id": "owner"})
    replay = assessment.handle_assessment_ui_message("owner", "big_five", "確認", message_id="commit")
    assert committed["status"] == "committed"
    assert replay["status"] == "already_committed"
    assert profiles.find_one({"user_id": "owner"}) == before
    analyze.assert_called_once()
