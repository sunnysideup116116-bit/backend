from unittest.mock import MagicMock
import pytest
from services import memory_service as memory
from services.ayue_agent.contracts import AgentTurnContext, ToolCall
from services.ayue_agent.tools import execute_tool
from services.ayue_agent.v3.planner import _PLANNER_SYSTEM
from services.ayue_agent.v3.synthesizer import _synthesizer_system_prompt


def test_search_finds_memory_outside_hot_preview_and_exposes_no_ids(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"status": "success", "memories": [
        {"key": "swimming", "label": "游泳", "stance": "like", "owner_user_id": "owner"},
    ], "truncated": False}
    get = MagicMock(return_value=response)
    monkeypatch.setattr(memory.requests, "get", get)
    writes = MagicMock()
    monkeypatch.setattr(memory.profiles_coll, "update_one", writes)
    context = AgentTurnContext(user_id="owner", room_id="room", message="我對游泳有什麼偏好?",
                               user_profile={"profile_memory_preview": [{"label": "咖啡", "stance": "like"}]})
    result = execute_tool(ToolCall(name="memory.search_my_profile", arguments={"query": "游泳 游水"}), context)
    assert result.ok
    assert result.data["preferences"] == ["喜歡：游泳"]
    assert result.data["source"] == "graph"
    assert get.call_args.kwargs["params"]["query"] == "游泳 游水"
    assert "swimming" not in str(result.data)
    assert "owner_user_id" not in str(result.data)
    writes.assert_not_called()


def test_search_outage_is_not_presented_as_absent_memory(monkeypatch):
    monkeypatch.setattr(memory.requests, "get", MagicMock(side_effect=memory.requests.Timeout()))
    result = memory.search_owner_memory("owner", "游泳", {"profile_memory_preview": [{"label": "咖啡", "stance": "avoid"}]})
    assert result["status"] == "unavailable"
    assert result["source"] == "cache"
    assert result["truncated"] is True
    assert result["preferences"] == ["避免：咖啡"]


def test_search_reports_partial_results(monkeypatch):
    response = MagicMock()
    response.json.return_value = {"status": "success", "memories": [{"label": "散步", "stance": "like"}], "truncated": True}
    monkeypatch.setattr(memory.requests, "get", lambda *a, **k: response)
    assert memory.search_owner_memory("owner", "", {})["truncated"]


def test_search_rejects_model_supplied_owner(monkeypatch):
    get = MagicMock(side_effect=AssertionError("should not execute"))
    monkeypatch.setattr(memory.requests, "get", get)
    result = execute_tool(ToolCall(name="memory.search_my_profile", arguments={"user_id": "other", "query": "游泳"}),
                          AgentTurnContext(user_id="owner", room_id="room", message="test"))
    assert not result.ok
    get.assert_not_called()


def test_answer_prompts_keep_assistant_hypotheses_unconfirmed():
    assert "使用者未確認" in _PLANNER_SYSTEM
    prompt = _synthesizer_system_prompt("grounded_result", False)
    assert "Assistant hypotheses/questions are not user facts" in prompt
    assert "truncated" in prompt
