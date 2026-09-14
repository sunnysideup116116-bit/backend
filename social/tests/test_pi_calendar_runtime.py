"""Pi Calendar separation, schema, preparation and presentation contracts."""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from services.ayue_agent.contracts import TurnClockV1
from services.ayue_agent.pi import calendar_tools
from services.ayue_agent.pi import runtime as pi_runtime
from services.ayue_agent.shared import calendar_mutations
from services.ayue_agent.shared.confirmation_layout import confirmation_layout
from services.ayue_agent.shared import calendar_commands


@pytest.mark.parametrize(
    "tool_name,arguments,action,count",
    [
        ("calendar.prepare_create", {"events": [{"title": "駁二", "start_date": "2026-09-24"}]}, "create", 1),
        ("calendar.prepare_create", {"events": [{"title": "駁二", "duration_minutes": 60}]}, "create", 1),
        ("calendar.prepare_create", {"events": [{"activity": "駁二", "start_date": "2026-09-24"}]}, "create", 1),
        ("calendar.prepare_create", {"events": [{"title": "旅行", "all_day": True}]}, "create", 1),
        ("calendar.prepare_create", {"events": [{"title": "旅行", "start_date": "2026-09-24", "end_date": "2026-09-26"}]}, "create", 1),
        ("calendar.prepare_create", {"events": [{"title": "A"}, {"title": "B"}]}, "create", 2),
        ("calendar.prepare_update", {"changes": [{"target_hint": "駁二", "title": "駁二散步"}]}, "update", 1),
        ("calendar.prepare_update", {"changes": [{"target_hint": "駁二", "start_time": "17:00"}]}, "update", 1),
        ("calendar.prepare_update", {"changes": [{"target_hint": "駁二", "new_start_time": "17:00", "new_end_time": "18:00"}]}, "update", 1),
        ("calendar.prepare_update", {"changes": [{"target_hint": "駁二", "time_shift_minutes": 60}]}, "update", 1),
        ("calendar.prepare_update", {"changes": [{"target_hint": "駁二", "start_date": "2026-09-25"}]}, "update", 1),
        ("calendar.prepare_update", {"changes": [{"target_selector": {"date": "2026-09-24"}, "location": "駁二"}]}, "update", 1),
        ("calendar.prepare_cancel", {"targets": [{"target_hint": "駁二"}]}, "cancel", 1),
        ("calendar.prepare_cancel", {"targets": [{"event_hint": "駁二", "date": "2026-09-24"}]}, "cancel", 1),
        ("calendar.prepare_cancel", {"targets": [{"target_selector": {"date": "2026-09-24"}}]}, "cancel", 1),
        ("calendar.prepare_cancel", {"targets": [{"target_hint": "A"}, {"target_hint": "B"}]}, "cancel", 2),
        ("calendar.prepare_cancel", {"targets": ["A", "B"]}, "cancel", 2),
        ("calendar.prepare_cancel_many", {"target_hints": ["A", "B"]}, "cancel_selected", 1),
        ("calendar.prepare_cancel_many", {"all_upcoming": True}, "cancel_all_upcoming", 1),
    ],
)
def test_pi_calendar_input_matrix(tool_name, arguments, action, count):
    commands = calendar_tools._commands(tool_name, arguments)
    assert len(commands) == count
    assert all(command.action == action for command in commands)


@pytest.mark.parametrize(
    "tool_name,arguments",
    [
        ("calendar.prepare_create", {"events": []}),
        ("calendar.prepare_create", {"events": [{"title": "A", "user_id": "forged"}]}),
        ("calendar.prepare_create", {"events": [{"title": "A", "duration_minutes": 0}]}),
        ("calendar.prepare_create", {"events": [{"title": "A", "activity": "B"}]}),
        ("calendar.prepare_update", {"changes": [{"target_hint": "A", "event_id": "forged"}]}),
        ("calendar.prepare_cancel", {"targets": [{"target_hint": "A", "revision": 2}]}),
        ("calendar.prepare_cancel", {"targets": [{"target_hint": "A", "event_hint": "B"}]}),
        ("calendar.prepare_cancel_many", {"target_hints": ["A"]}),
        ("calendar.prepare_cancel_many", {"target_hints": ["A", "B"], "all_upcoming": True}),
    ],
)
def test_pi_calendar_rejects_invalid_or_authority_arguments(tool_name, arguments):
    with pytest.raises((ValidationError, ValueError)):
        calendar_tools._commands(tool_name, arguments)


def test_pi_calendar_has_split_write_tools_and_no_legacy_submit_schema():
    names = {tool["name"] for tool in pi_runtime.tool_schemas()}
    assert calendar_tools.WRITE_TOOLS <= names
    assert "calendar.submit_commands" not in names
    assert all("$ref" not in str(tool["parameters"]) for tool in pi_runtime.tool_schemas())


def test_pi_calendar_does_not_import_dag_calendar_agent_or_runtime():
    source = inspect.getsource(calendar_tools)
    assert "v3.calendar_runtime" not in source
    assert "sub_agents.calendar_agent" not in source
    assert "synthesizer" not in source.lower()
    assert "planner" not in source.lower()


@pytest.mark.parametrize("start_time,end_time", [("16:00", "17:00"), ("17:00", "18:00")])
def test_boer_followup_duration_prepares_expected_confirmation(monkeypatch, start_time, end_time):
    monkeypatch.setattr(calendar_commands, "calendar_access_enabled", lambda _uid: True)
    monkeypatch.setattr(calendar_commands, "conflicts_for_viewer", lambda *_a, **_k: [])
    created = []
    turn = SimpleNamespace(
        user_id="candy",
        room_id="room",
        message="大概下午四點 一小時結束",
        clock=TurnClockV1(
            timezone="Asia/Taipei",
            local_date="2026-09-14",
            local_time="14:34",
            local_iso="2026-09-14T14:34:00+08:00",
            utc_iso="2026-09-14T06:34:00+00:00",
            weekday_zh_tw="星期一",
        ),
    )
    command = calendar_mutations.CalendarCommand(
        action="create",
        title="駁二逛街",
        date="2026-09-24",
        start_time=start_time,
        duration_minutes=60,
    )
    result = calendar_mutations.prepare_pi_calendar_confirmation(
        turn,
        [command],
        run_id="a" * 32,
        create_confirmation=lambda **kwargs: created.append(kwargs),
        supersede_confirmation=lambda **_kwargs: {"status": "none"},
    )
    assert result.status == "pending_confirmation"
    assert len(created) == 1
    payload = created[0]["payload"]
    assert payload["source_engine"] == "pi"
    assert payload["tool_protocol_version"] == "pi_calendar.v1"
    assert payload["plans"][0]["form"]["start_time"] == start_time
    assert payload["plans"][0]["form"]["end_time"] == end_time


def test_missing_calendar_fields_return_clarification_without_confirmation(monkeypatch):
    monkeypatch.setattr(calendar_commands, "calendar_access_enabled", lambda _uid: True)
    created = []
    turn = SimpleNamespace(user_id="candy", room_id="room", message="加到行事曆", clock=None)
    result = calendar_mutations.prepare_pi_calendar_confirmation(
        turn,
        [calendar_mutations.CalendarCommand(action="create", title="駁二")],
        run_id="b" * 32,
        create_confirmation=lambda **kwargs: created.append(kwargs),
        supersede_confirmation=lambda **_kwargs: {"status": "none"},
    )
    assert result.status == "needs_clarification"
    assert result.observation["calendar_command_result"]["clarification"]["missing_fields"]
    assert created == []


def test_pi_calendar_uses_concrete_visible_place_without_reference_store(monkeypatch):
    captured = []

    def preflight(_turn, commands):
        captured.extend(commands)
        return SimpleNamespace(
            status="ready",
            plans=[SimpleNamespace(model_dump=lambda **_k: {
                "action": "create",
                "form": commands[0].model_dump(exclude_none=True),
            })],
            preview="要新增港邊咖啡嗎？",
        )

    monkeypatch.setattr(calendar_mutations, "preflight_calendar_commands", preflight)
    turn = SimpleNamespace(
        user_id="candy",
        room_id="room",
        message="第二間加到行事曆",
        clock=TurnClockV1(
            timezone="Asia/Taipei", local_date="2026-09-14", local_time="17:00",
            local_iso="2026-09-14T17:00:00+08:00", utc_iso="2026-09-14T09:00:00+00:00",
            weekday_zh_tw="星期一",
        ),
        # A stale legacy attribute must not alter the Pi command.
        place_reference_resolution={"status": "resolved", "reference": "place_ref_" + "a" * 24},
    )
    result = calendar_mutations.prepare_pi_calendar_confirmation(
        turn,
        [calendar_mutations.CalendarCommand(
            action="create", title="港邊咖啡", location="高雄市港邊咖啡",
            date="2026-09-24", start_time="16:00", end_time="17:00",
        )],
        run_id="c" * 32,
        create_confirmation=lambda **_kwargs: None,
        supersede_confirmation=lambda **_kwargs: {"status": "none"},
    )
    assert result.status == "pending_confirmation"
    assert captured[0].title == "港邊咖啡"
    assert captured[0].location == "高雄市港邊咖啡"


def test_confirmation_marker_places_card_between_messages():
    layout = confirmation_layout(
        "我整理好了。[[confirmation]]時間不對可以直接告訴我。",
        fallback_preview="要新增駁二行程嗎？",
    )
    assert layout.used_fallback is False
    assert layout.messages == ["我整理好了。", "時間不對可以直接告訴我。"]
    assert [block["type"] for block in layout.interaction_blocks_v1] == ["text", "confirmation", "text"]


@pytest.mark.parametrize(
    "model_text",
    [
        "沒有標記",
        "[[confirmation]][[confirmation]]",
        "[[confirmation]]只有後文",
        "我已新增完成。[[confirmation]]",
        "[訊息時間：2026-09-14]我整理好了。[[confirmation]]",
        "要新增駁二行程嗎？[[confirmation]]",
    ],
)
def test_invalid_confirmation_layout_uses_one_safe_fallback_card(model_text):
    layout = confirmation_layout(model_text, fallback_preview="要新增駁二行程嗎？")
    assert layout.used_fallback is True
    assert layout.messages == ["要新增駁二行程嗎？"]
    assert [block["type"] for block in layout.interaction_blocks_v1].count("confirmation") == 1
