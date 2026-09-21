from unittest.mock import patch

from routers import match as match_router
from routers.system import get_notifications
from services.profile_projection import (
    active_context_signals,
    active_recent_context,
    recent_context_is_active,
    without_expired_recent_context,
)


def test_explicit_expiry_removes_context_signals_and_embedding_from_read_projection():
    profile = {
        "current_context": "最近想看展",
        "context_signals": {"activity": "看展"},
        "context_embedding": [.1, .2],
        "context_embedding_source_hash": "old",
        "recent_context_expires_at": 99,
    }
    assert not recent_context_is_active(profile, now=100)
    assert active_recent_context(profile, now=100) == ""
    assert active_context_signals(profile, now=100) == {}
    projected = without_expired_recent_context(profile, now=100)
    assert projected["current_context"] == ""
    assert projected["context_signals"] == {}
    assert projected["context_embedding"] == []


def test_legacy_context_without_expiry_remains_readable_for_compatibility():
    profile = {"current_context": "最近想看展", "context_signals": {"activity": "看展"}}
    assert recent_context_is_active(profile, now=100)
    assert active_recent_context(profile, now=100) == "最近想看展"


def test_explicit_active_expiry_preserves_only_recent_context_fields():
    profile = {
        "current_context": "最近想看展",
        "context_signals": {"activity": "看展"},
        "context_embedding": [.1, .2],
        "recent_context_expires_at": 101,
        "deep_profile": {"values": ["真誠"]},
        "profile_memory_preview": [{"label": "K-pop"}],
    }
    projected = without_expired_recent_context(profile, now=100)
    assert projected["current_context"] == "最近想看展"
    assert projected["context_signals"] == {"activity": "看展"}
    assert projected["context_embedding"] == [.1, .2]
    assert projected["deep_profile"] == {"values": ["真誠"]}
    assert projected["profile_memory_preview"] == [{"label": "K-pop"}]


def test_malformed_expiry_fails_closed_without_crashing_readers():
    profile = {
        "current_context": "最近想看展",
        "recent_context_expires_at": {"invalid": True},
    }
    assert not recent_context_is_active(profile)
    assert active_recent_context(profile, "") == ""


def test_recovered_match_result_drops_expired_context_on_both_sides(monkeypatch):
    profiles = type("Profiles", (), {})()
    calls = []

    def find_one(_query, projection):
        calls.append(projection)
        return {
            "big_five": {"summary": "安靜"},
            "current_context": "上個月想看展",
            "recent_context_expires_at": 1,
        }

    profiles.find_one = find_one
    monkeypatch.setattr(match_router, "profiles_coll", profiles)
    result = match_router._existing_job_match_result(
        {"_id": "match", "from_user": "owner", "to_user": "candidate"},
        "owner",
        {"current_context": "上個月想爬山", "recent_context_expires_at": 1},
    )
    assert result["matches"][0]["current_context"] == ""
    assert result["matches"][0]["target_context"] == ""
    assert calls[0]["recent_context_expires_at"] == 1


def test_legacy_notification_does_not_rehydrate_expired_recent_context():
    proposal = {
        "_id": "match", "from_user": "sender", "to_user": "owner",
        "reason": "可以認識看看", "receiver_reason": "可以認識看看",
    }
    profile = {
        "display_name": "小樂", "current_context": "上個月想看展",
        "recent_context_expires_at": 1,
    }
    with patch("routers.system.matches_coll.find", return_value=[proposal]), \
            patch("routers.system.profiles_coll.find_one", return_value=profile):
        visible = get_notifications("owner")["notifications"][0]
    assert visible["from_user_context"] == ""
