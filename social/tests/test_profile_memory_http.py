from unittest.mock import patch
import pytest
from fastapi import HTTPException

from routers import system
from services.memory_service import MemoryWriteError


def test_nonempty_stale_preview_is_refreshed_from_available_graph():
    with patch.object(system.profiles_coll, "find_one", return_value={
        "profile_memory_preview": [{"key": "stale", "label": "舊記憶"}],
        "profile_memory_summary": "喜歡舊記憶",
    }), patch("services.memory_service.get_graph_memory_snapshot", return_value={
        "available": True, "items": [], "error_code": None,
    }), patch.object(system.profiles_coll, "update_one") as update:
        result = system.get_profile_memories("owner")
    assert result == {"memories": [], "summary": ""}
    assert update.call_args.args[1]["$set"]["profile_memory_preview"] == []


def test_graph_failure_keeps_the_bounded_cached_projection():
    cached = [{"key": "coffee", "label": "咖啡"}]
    with patch.object(system.profiles_coll, "find_one", return_value={
        "profile_memory_preview": cached, "profile_memory_summary": "喜歡咖啡",
    }), patch("services.memory_service.get_graph_memory_snapshot", return_value={
        "available": False, "items": [], "error_code": "graph_read_failed",
    }), patch.object(system.profiles_coll, "update_one") as update:
        result = system.get_profile_memories("owner")
    assert result == {"memories": cached, "summary": "喜歡咖啡"}
    update.assert_not_called()


def test_memory_action_failure_is_an_http_error_not_false_success():
    request = system.ProfileMemoryActionRequest(
        user_id="owner", key="missing", action="disable",
    )
    with patch("services.memory_service.apply_memory_action",
               side_effect=MemoryWriteError("not_found")):
        with pytest.raises(HTTPException) as raised:
            system.profile_memory_action(request)
    assert raised.value.status_code == 503
    assert raised.value.detail["code"] == "not_found"


def test_voice_memory_add_uses_validated_owner_memory_pipeline():
    request = system.ProfileMemoryAddRequest(
        user_id="owner",
        label="喜歡 打籃球",
        stance="like",
        request_id="voice-request-123",
    )
    learned = {
        "key": "voice_key",
        "label": "打籃球",
        "stance": "like",
        "category": "preference",
    }
    with patch(
        "services.memory_service.apply_profile_memory_proposals",
        return_value=[learned],
    ) as apply:
        result = system.add_profile_memory(request)

    assert result == {"status": "success", "memory": learned}
    args = apply.call_args.args
    assert args[0] == "owner"
    assert args[1][0]["label"] == "打籃球"
    assert args[1][0]["stance"] == "like"
    assert args[1][0]["confidence"] == 1.0
    assert args[2] == "app_voice"
    assert args[3] == "voice-memory:owner:voice-request-123"


def test_voice_memory_add_rejects_a_skipped_graph_write():
    request = system.ProfileMemoryAddRequest(
        user_id="owner",
        label="測試偏好",
        stance="like",
        request_id="voice-request-456",
    )
    with patch(
        "services.memory_service.apply_profile_memory_proposals",
        return_value=[],
    ):
        with pytest.raises(HTTPException) as raised:
            system.add_profile_memory(request)
    assert raised.value.status_code == 422
    assert raised.value.detail["code"] == "memory_rejected"
