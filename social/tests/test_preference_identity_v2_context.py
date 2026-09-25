"""Full owner preference constraints reach context without widening ownership."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from matchmaker_agent.concept_identity import canonicalize_concept
from services.ayue_agent import context
from services.ayue_agent.contracts import AgentTurnContext
from services.ayue_agent.shared import calendar_state, date_coordination_state


@pytest.fixture
def isolated_context(monkeypatch):
    collection = Mock()
    collection.find_one.return_value = None
    collection.count_documents.return_value = 0
    monkeypatch.setattr(context, "matches_coll", collection)
    monkeypatch.setattr(context, "load_match_state", lambda _owner: {
        "active_proposal": None, "ambiguous": False, "search": {},
    })
    monkeypatch.setattr(context, "load_validated_conversation_continuity", lambda *_a: None)
    monkeypatch.setattr(context, "validated_mentioned_contact_ids", lambda *_a: ([], False))
    monkeypatch.setattr(context, "mentioned_contact_refs", lambda *_a: [])
    monkeypatch.setattr(calendar_state, "get_recent_mutation", lambda *_a: None)
    monkeypatch.setattr(date_coordination_state, "date_coordination_summary", lambda *_a: (None, None))


@pytest.mark.parametrize("builder,field", [
    (context.build_public_context, "relevant_preferences"),
    (context.build_public_agent_turn_context, "relevant_memories"),
])
def test_context_retains_full_preference_suffix_and_item_bound(isolated_context, builder, field):
    text = "Exploring interesting historical gardens and riverside walking routes with friends using step-free wheelchair access only"
    memories = [
        {**canonicalize_concept(f"{text} option {index}").as_dict(), "owner_user_id": "owner", "stance": "require"}
        for index in range(10)
    ]
    memories.insert(0, {**canonicalize_concept("Private other-owner preference").as_dict(), "owner_user_id": "other", "stance": "like"})
    profile = {"profile_memory_preview": memories, "current_context": "expired hiking context", "recent_context_expires_at": 1}
    before = deepcopy(profile)
    ctx = AgentTurnContext(user_id="owner", room_id="room", message="記得我的偏好嗎", user_profile=profile)
    output = builder(ctx)
    projection = output if isinstance(output, dict) else output.model_dump()
    actual = projection[field]
    assert len(actual) == 8
    assert actual[0] == "需要：" + text + " option 0"
    assert actual[-1].endswith("step-free wheelchair access only option 7")
    assert not any("other-owner" in item for item in actual)
    assert projection.get("current_context", projection.get("recent_context")) == ""
    assert profile == before


def test_private_owner_context_keeps_full_constraints_but_not_inactive_or_other_owner(monkeypatch):
    from services import mediator_context_service as private_context

    text = "Exploring historical architecture and peaceful riverside gardens with step-free wheelchair access only and no guided tours"
    identity = canonicalize_concept(text).as_dict()
    memory = {**identity, "stance": "require", "owner_user_id": "owner", "active": True}
    profile = {
        "profile_memory_preview": [
            {**memory, "owner_user_id": "other"}, {**memory, "active": False}, memory,
        ],
        "initial_interest": "x" * 200, "big_five": {"summary": "y" * 300},
    }
    before = deepcopy(profile)
    collection = Mock()
    collection.find_one.return_value = profile
    monkeypatch.setattr(private_context, "profiles_coll", collection)
    output = private_context.private_viewer_profile_context("owner")
    assert output["memories"] == ["需要：" + text]
    assert collection.find_one.call_args.args[0] == {"user_id": "owner"}
    assert len(output["initial_interest"]) == 120
    assert len(output["personality_summary"]) == 180
    assert profile == before

    # The counterparty projection is deliberately a separate privacy surface;
    # this owner-only change must not expand its content or schema.
    other = private_context.private_counterparty_strategy_context("owner")
    assert all(len(item["label"]) <= 60 for item in other["memories"])
    assert all(set(item) == {"label", "stance", "category"} for item in other["memories"])
