from copy import deepcopy
from unittest.mock import MagicMock

import mongomock
import pytest

from services import memory_service as memory
from services.owner_memory_projection import preference_wording
from services.ayue_agent.contracts import PublicAgentTurnContext
from services.ayue_agent.v3.context_slicer import slice_for_agent
from services.ayue_agent.v3.planner import _planner_prompt
from services.ayue_agent.v3.synthesizer import _build_prompt


def item(key="coffee", stance="like", **extra):
    return {"key": key, "label": key, "stance": stance, **extra}


@pytest.fixture
def store(monkeypatch):
    collection = mongomock.MongoClient().db.profiles
    collection.insert_one({"user_id": "a", "profile_memory_preview": [item("old")], "profile_memory_synced_at": 0})
    monkeypatch.setattr(memory, "profiles_coll", collection)
    return collection


def test_stance_owner_budget_and_short_term_are_preserved():
    values = [item("吵雜", "avoid"), item("咖啡"), item("散步", "want"),
              item("foreign", owner_user_id="b"), item("disabled", active=False),
              "untyped", item("seed_user_04"), *[item(f"hobby{x}") for x in range(20)]]
    words = preference_wording(values, owner_id="a")
    assert words[:2] == ["避免：吵雜", "喜歡：咖啡"]
    assert len(words) == 8
    assert not any(x in str(words) for x in ("foreign", "disabled", "散步", "untyped", "seed_user"))


def test_preferences_reach_both_answer_paths_and_relationship():
    words = preference_wording([item("熱鬧場所", "avoid"), item("咖啡")])
    turn = PublicAgentTurnContext(user_id="a", room_id="room", message="聊聊", relevant_memories=words)
    assert "避免" in _planner_prompt(turn)
    payload = slice_for_agent("synthesizer", turn, prior_observations=[]).payload
    assert payload["user_preferences"] == words
    assert "避免" in _build_prompt(payload, [])
    assert slice_for_agent("relationship", turn, prior_observations=[]).payload["relevant_memories"] == words


def test_stale_nonempty_cache_refreshes_and_fresh_cache_skips_network(monkeypatch, store):
    fetch = MagicMock(return_value={"available": True, "items": [item("new", "avoid")]})
    monkeypatch.setattr(memory, "get_graph_memory_snapshot", fetch)
    result = memory.refresh_owner_memory_profile("a", store.find_one({"user_id": "a"}))
    assert result["profile_memory_preview"] == [item("new", "avoid")]
    memory.refresh_owner_memory_profile("a", store.find_one({"user_id": "a"}))
    assert fetch.call_count == 1


def test_empty_truth_clears_cache_but_failure_preserves_it(monkeypatch, store):
    monkeypatch.setattr(memory, "get_graph_memory_snapshot", lambda *a, **k: {"available": False})
    before = store.find_one({"user_id": "a"})
    assert memory.refresh_owner_memory_profile("a", before)["profile_memory_preview"] == before["profile_memory_preview"]
    monkeypatch.setattr(memory, "get_graph_memory_snapshot", lambda *a, **k: {"available": True, "items": []})
    assert memory.refresh_owner_memory_profile("a", before, force=True)["profile_memory_preview"] == []


def test_old_read_cannot_resurrect_disabled_preference(monkeypatch, store):
    old = store.find_one({"user_id": "a"})
    def fetch(*args, **kwargs):
        memory._invalidate_memory_projection("a", "old")
        return {"available": True, "items": old["profile_memory_preview"]}
    monkeypatch.setattr(memory, "get_graph_memory_snapshot", fetch)
    result = memory.refresh_owner_memory_profile("a", old)
    assert result["profile_memory_preview"] == []
    assert store.find_one({"user_id": "a"})["profile_memory_preview"] == []


def test_failed_sync_merges_new_memory_without_erasing_existing(monkeypatch, store):
    monkeypatch.setattr(memory, "get_graph_memory_snapshot", lambda *a, **k: {"available": False})
    result = memory._sync_memory_projection("a", [item("new")])
    assert {x["key"] for x in result} == {"old", "new"}
    assert store.find_one({"user_id": "a"})["profile_memory_synced_at"] == 0


def test_snapshot_requests_durable_only_and_rejects_malformed(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"status": "success", "memories": [item("短期", "want"), item("長期")]}
    get = MagicMock(return_value=response)
    monkeypatch.setattr(memory.requests, "get", get)
    result = memory.get_graph_memory_snapshot("a")
    assert [x["key"] for x in result["items"]] == ["長期"]
    assert get.call_args.kwargs["params"]["durable_only"] == "true"
    response.json.return_value = {"status": "success", "memories": ["not an item"]}
    assert not memory.get_graph_memory_snapshot("a")["available"]


def test_foreign_profile_cannot_refresh_or_write(monkeypatch, store):
    fetch = MagicMock(side_effect=AssertionError("must not fetch"))
    monkeypatch.setattr(memory, "get_graph_memory_snapshot", fetch)
    foreign = {"user_id": "b", "profile_memory_preview": []}
    assert memory.refresh_owner_memory_profile("a", foreign) == foreign
