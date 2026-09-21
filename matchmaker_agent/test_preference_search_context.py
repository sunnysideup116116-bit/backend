import json

from matchmaker import MatchmakerAgent, provider_search_context, safe_search_context


def test_preference_context_survives_the_matchmaker_boundary_without_internal_key():
    raw = {
        "search_intent": "preference",
        "normalized_topic": "K-pop",
        "canonical_preference_key": "k_pop",
        "query_text": "幫我找喜歡 K-pop 的人",
        "source_message_id": "private-source-id",
    }
    safe = safe_search_context(raw)
    assert safe["search_intent"] == "preference"
    assert safe["normalized_topic"] == "K-pop"
    assert "canonical_preference_key" not in safe
    assert provider_search_context(raw) == {
        "search_intent": "preference",
        "normalized_topic": "K-pop",
        "query_text": "幫我找喜歡 K-pop 的人",
    }


def test_preference_context_without_canonical_topic_fails_closed():
    assert safe_search_context({
        "search_intent": "preference",
        "query_text": "幫我找人",
        "private_field": "must-not-survive",
    }) == {}


def test_preference_match_prompt_receives_only_verified_semantics():
    agent = MatchmakerAgent.__new__(MatchmakerAgent)
    agent.system_prompt = (
        "BASE [GRAPH_MEMORY_PLACEHOLDER] [GLOBAL_HEURISTICS_PLACEHOLDER] "
        "[DEEP_PROFILE_PLACEHOLDER]"
    )
    messages = agent._match_messages(
        {"user_id": "owner"}, [{"user_id": "candidate"}],
        search_context={
            "search_intent": "preference",
            "normalized_topic": "K-pop",
            "query_text": "幫我找喜歡 K-pop 的人",
            "canonical_preference_key": "k_pop",
            "source_message_id": "private-source-id",
        },
    )
    assert "server 驗證過的偏好精確搜尋" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert payload["search_context"] == {
        "search_intent": "preference",
        "normalized_topic": "K-pop",
        "query_text": "幫我找喜歡 K-pop 的人",
    }
