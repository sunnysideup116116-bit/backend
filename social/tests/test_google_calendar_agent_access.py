from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import BackgroundTasks

from models import DirectChatRequest
from routers import public_chat
from services.agent_calendar_bridge import (
    google_events_for_agent,
    merge_google_events,
)
from services.appwrite_identity_service import authenticated_owner_matches
from services.ayue_agent.contracts import PublicAgentRequestContext, ToolCall
from services.ayue_agent.private_v2 import PrivateAgentTurnContextV2, _viewer_availability
from services.ayue_agent.tools import execute_tool


START = datetime(2026, 9, 14, tzinfo=timezone.utc)
END = datetime(2026, 9, 16, tzinfo=timezone.utc)


class FakeGoogleCalendar:
    def __init__(self):
        self.status_calls = 0
        self.event_calls = 0

    def status(self, owner_id):
        self.status_calls += 1
        return {"state": "connected", "connected": True}

    def list_events(self, owner_id, start, end):
        self.event_calls += 1
        return {
            "events": [
                {
                    "event_id": "google_opaque",
                    "source_type": "google",
                    "status": "confirmed",
                    "title": "Google 晚餐",
                    "start_at": "2026-09-14T19:00:00+08:00",
                    "end_at": "2026-09-14T20:00:00+08:00",
                    "all_day": False,
                    "timezone": "Asia/Taipei",
                    "location": "台北 101",
                    "revision": 0,
                }
            ]
        }


def test_external_calendar_requires_both_owner_proof_and_calendar_consent():
    google = FakeGoogleCalendar()
    with patch(
        "services.agent_calendar_bridge.get_google_calendar_service",
        return_value=google,
    ), patch(
        "services.agent_calendar_bridge.calendar_access_enabled",
        return_value=True,
    ):
        assert google_events_for_agent("owner", START, END, authorized=False) == []

    assert google.status_calls == 0
    assert google.event_calls == 0


def test_google_events_are_ephemeral_read_only_agent_projections():
    google = FakeGoogleCalendar()
    internal = [{
        "event_id": "internal",
        "source_type": "personal",
        "status": "confirmed",
        "title": "APP 行程",
        "start_at": "2026-09-15T01:00:00+00:00",
        "end_at": "2026-09-15T02:00:00+00:00",
    }]
    with patch(
        "services.agent_calendar_bridge.get_google_calendar_service",
        return_value=google,
    ), patch(
        "services.agent_calendar_bridge.calendar_access_enabled",
        return_value=True,
    ):
        events = merge_google_events(
            "owner",
            internal,
            START,
            END,
            authorized=True,
        )

    assert [event["event_id"] for event in events] == ["google_opaque", "internal"]
    external = events[0]
    assert external["provider_read_only"] is True
    assert external["location"] == "台北 101"
    assert external["participants"] == ["owner"]
    assert external["revision"] == 0


def test_public_calendar_agent_reads_google_without_arming_a_write_reference():
    google = FakeGoogleCalendar()
    context = PublicAgentRequestContext(
        user_id="owner",
        room_id="room",
        message="9 月 14 日有什麼行程？",
        external_calendar_authorized=True,
    )
    with patch(
        "services.ayue_agent.tools.calendar_access_enabled",
        return_value=True,
    ), patch(
        "services.ayue_agent.tools.get_calendar_context",
        return_value={"viewer_events": []},
    ), patch(
        "services.agent_calendar_bridge.calendar_access_enabled",
        return_value=True,
    ), patch(
        "services.agent_calendar_bridge.get_google_calendar_service",
        return_value=google,
    ):
        result = execute_tool(
            ToolCall(
                name="calendar.list_my_events",
                arguments={
                    "start_date": "2026-09-14",
                    "end_date": "2026-09-14",
                },
            ),
            context,
        )

    assert result.ok is True
    assert result.data["events"] == [{
        "activity": "Google 晚餐",
        "date": "2026-09-14",
        "end_date": "2026-09-14",
        "all_day": False,
        "start_time": "19:00",
        "end_time": "20:00",
        "location": "台北 101",
        "event_kind": "google_read_only",
        "status": "confirmed",
    }]
    assert result.private_data == {}
    assert "external_calendar_authorized" not in context.model_dump()


def test_private_agent_gets_only_google_busy_intervals():
    context = PrivateAgentTurnContextV2(
        user_id="owner",
        other_id="other",
        room_id="private",
        message="我星期六有空嗎？",
        pair_revision=1,
        viewer_profile={},
        counterparty_shareable={},
        counterparty_advisory={},
        shared_history=[],
        private_history=[],
        shared_facts=[],
        local_time="2026-09-13 12:00",
        external_calendar_authorized=True,
    )
    google_event = {
        "event_id": "google_opaque",
        "title": "私密標題",
        "location": "私密地點",
        "start_at": START,
        "end_at": END,
    }
    with patch(
        "services.ayue_agent.private_v2.calendar_range_for_message",
        return_value=(START, END, False),
    ), patch(
        "services.calendar_service.calendar_access_enabled",
        return_value=True,
    ), patch(
        "services.calendar_service.get_calendar_context",
        return_value={"viewer_events": []},
    ), patch(
        "services.agent_calendar_bridge.merge_google_events",
        return_value=[google_event],
    ):
        result = _viewer_availability(context, "星期六")

    assert result == {
        "access": True,
        "busy": [{
            "start_at": str(START),
            "end_at": str(END),
            "busy": "true",
        }],
        "truncated": False,
    }
    assert "私密標題" not in repr(result)
    assert "私密地點" not in repr(result)


def test_owner_match_helper_fails_closed_on_a_different_appwrite_user():
    with patch(
        "services.appwrite_identity_service.authenticate_owner",
        return_value="another-owner",
    ):
        assert authenticated_owner_matches("Bearer jwt", "owner") is False
    with patch(
        "services.appwrite_identity_service.authenticate_owner",
        return_value="owner",
    ):
        assert authenticated_owner_matches("Bearer jwt", "owner") is True


def test_public_agent_route_passes_verified_owner_capability_to_the_turn():
    request = SimpleNamespace(headers={"Authorization": "Bearer jwt"})
    req = DirectChatRequest(
        user_id="owner",
        contact_id="ai_assistant",
        message="查一下今天行程",
    )
    with patch.object(
        public_chat,
        "_validated_requested_mentions",
        return_value=([], False),
    ), patch.object(
        public_chat,
        "_resolve_ai_room_id",
        return_value="room",
    ), patch.object(
        public_chat,
        "save_message",
        return_value={"message_id": "owner-message"},
    ), patch.object(
        public_chat,
        "record_owner_activity",
    ), patch.object(
        public_chat,
        "authenticated_owner_matches",
        return_value=True,
    ) as authenticate, patch.object(
        public_chat,
        "_complete_public_turn",
        return_value={"reply": "ok"},
    ) as complete:
        response = public_chat.direct_chat(req, BackgroundTasks(), request=request)

    assert response == {"reply": "ok"}
    authenticate.assert_called_once_with("Bearer jwt", "owner")
    assert complete.call_args.kwargs["external_calendar_authorized"] is True
