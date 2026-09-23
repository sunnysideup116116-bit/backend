import json
import ast
from copy import deepcopy
from pathlib import Path
import subprocess
import pytest
import matchmaker

from matchmaker import MatchmakerAgent, provider_search_context, safe_search_context
from concept_identity import PreferenceTextError, canonicalize_concept


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
    assert safe["canonical_preference_key"] == canonicalize_concept("K-pop").key
    assert safe["canonicalization_version"] == "v2"
    assert safe["semantic_text"] == "K-pop"
    assert "canonical_preference_key" not in provider_search_context(raw)
    assert provider_search_context(raw) == {
        "search_intent": "preference",
        "normalized_topic": "K-pop",
        "query_text": "幫我找喜歡 K-pop 的人",
    }


def test_preference_context_without_canonical_topic_fails_closed():
    with pytest.raises(ValueError, match="preference_search_invalid"):
        safe_search_context({
            "search_intent": "preference",
            "query_text": "幫我找人",
            "private_field": "must-not-survive",
        })


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


def test_exact_only_llm_messages_are_identical_to_frozen_p0():
    root = Path(__file__).resolve().parents[1]
    try:
        source = subprocess.check_output([
            "git", "show", "7f130f60bb895020e67486c7f6467a921a2ec396:matchmaker_agent/matchmaker.py",
        ], cwd=root, text=True, stderr=subprocess.PIPE)
    except subprocess.CalledProcessError:
        pytest.skip("P0 parity requires the frozen merge object in local git history")
    cls = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef) and node.name == "MatchmakerAgent")
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "_match_messages")
    env = dict(matchmaker.__dict__)
    exec(compile(ast.Module(body=[method], type_ignores=[]), "p0_matchmaker", "exec"), env)
    agent = MatchmakerAgent.__new__(MatchmakerAgent)
    agent.system_prompt = "BASE [GRAPH_MEMORY_PLACEHOLDER] [GLOBAL_HEURISTICS_PLACEHOLDER] [DEEP_PROFILE_PLACEHOLDER]"
    args = ({"user_id": "owner"}, [{"user_id": "z-first"}, {"user_id": "a-second"}])
    kwargs = {"search_context": {"search_intent": "preference", "normalized_topic": "K-pop", "query_text": "找喜歡 Kpop 的人"}}
    assert agent._match_messages(*args, **kwargs) == env["_match_messages"](agent, *args, **kwargs)


def test_semantic_candidate_evidence_is_role_bound_and_sanitized_for_model():
    agent = MatchmakerAgent.__new__(MatchmakerAgent)
    agent.system_prompt = "BASE [GRAPH_MEMORY_PLACEHOLDER]"
    candidates = [
        {"user_id": "exact"},
        {"user_id": "related", "preference_retrieval_evidence": [{
            "kind": "semantic_related", "concept_key": "korean_pop", "similarity": .91,
            "label": "private-label", "raw_memory": "raw-owner-message",
        }]},
    ]
    before = deepcopy(candidates)
    messages = agent._match_messages({"user_id": "owner"}, candidates, search_context={
        "search_intent": "preference", "normalized_topic": "K-pop",
    })
    assert "semantic-related" in messages[0]["content"]
    payload = json.loads(messages[1]["content"])
    assert "preference_retrieval_evidence" not in payload["candidates"][0]
    assert payload["candidates"][1]["preference_retrieval_evidence"] == [{
        "kind": "semantic_related", "concept_key": "korean_pop", "similarity": .91,
    }]
    assert "private-label" not in str(messages) and "raw-owner-message" not in str(messages)
    assert candidates == before
