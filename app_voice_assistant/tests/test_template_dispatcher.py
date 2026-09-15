from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from app_voice_assistant.template_dispatcher import (
    TEMPLATE_TOOL_COUNT,
    template_live_tools,
    template_proposal_from_function,
    authoritative_template_proposal,
    template_tool_call_for_proposal,
)
from app_voice_assistant.contracts import VoiceProposal


class FakeFunctionDeclaration:
    def __init__(self, *, name, description, parameters_json_schema):
        self.name = name
        self.description = description
        self.parameters_json_schema = parameters_json_schema


class FakeTool:
    def __init__(self, *, function_declarations):
        self.function_declarations = function_declarations


class FakeTypes:
    FunctionDeclaration = FakeFunctionDeclaration
    Tool = FakeTool


def call(name, args):
    return SimpleNamespace(name=name, args=args, id="template-call")


def test_template_tool_surface_is_bounded_and_direct():
    tools = template_live_tools(FakeTypes)
    declarations = tools[0].function_declarations
    names = [item.name for item in declarations]

    assert len(names) == TEMPLATE_TOOL_COUNT
    assert names == [
        "navigate_app", "read_app_data", "read_weather", "ask_app_ayue",
        "write_app_action", "open_chat", "get_voice_capabilities",
        "describe_current_screen", "select_screen_target", "read_tasks",
        "cancel_task", "confirm_pending_action", "resolve_pending_interaction",
        "cancel_current_action", "close_voice_mode",
    ]
    assert all(item.parameters_json_schema["additionalProperties"] is False for item in declarations)


def test_template_read_tools_map_to_existing_validated_proposals():
    calendar = template_proposal_from_function(
        call("read_app_data", {
            "domain": "calendar",
            "source": "google",
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
        }),
        revision=4,
    )
    matching = template_proposal_from_function(
        call("read_app_data", {"domain": "matching", "view": "status"}),
        revision=4,
    )
    private = template_proposal_from_function(
        call("ask_app_ayue", {
            "domain": "private",
            "contact_name": "小美",
            "question": "請讀取我們的聊天內容",
        }),
        revision=4,
    )
    post = template_proposal_from_function(
        call("read_app_data", {
            "domain": "posts",
            "target_ref": "surface-4-5-1",
        }),
        revision=4,
    )

    assert calendar is not None
    assert calendar.intent == "calendar.query"
    assert calendar.arguments == {
        "start_date": "2026-09-01",
        "end_date": "2026-09-30",
        "source": "google",
    }
    assert matching is not None
    assert matching.intent == "match.query"
    assert matching.arguments == {"view": "status"}
    assert private is not None
    assert private.intent == "ayue.private_query"
    assert private.arguments == {
        "contact_name": "小美",
        "question": "請讀取我們的聊天內容",
    }
    assert post is not None
    assert post.intent == "post.open"
    assert post.arguments == {"target_ref": "surface-4-5-1"}


def test_template_write_tool_keeps_existing_validation_and_confirmation_boundary():
    setting = template_proposal_from_function(
        call("write_app_action", {
            "action": "setting",
            "key": "notifications.global",
            "enabled": False,
        }),
        revision=2,
    )
    calendar = template_proposal_from_function(
        call("write_app_action", {
            "action": "calendar_create",
            "title": "看電影",
            "date": "2026-09-20",
            "start_time": "19:30",
        }),
        revision=2,
    )
    invalid = template_proposal_from_function(
        call("write_app_action", {"action": "chat_send", "contact_name": "小美"}),
        revision=2,
    )

    assert setting is not None
    assert setting.intent == "settings.set"
    assert setting.arguments == {"key": "notifications.global", "enabled": False}
    assert calendar is not None
    assert calendar.intent == "calendar.create"
    assert calendar.arguments["title"] == "看電影"
    assert invalid is None


def test_template_fast_path_preserves_deterministic_calendar_arguments():
    proposal = VoiceProposal(
        "calendar.query",
        {
            "source": "all",
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
        },
        "",
        4,
    )

    assert template_tool_call_for_proposal(proposal) == (
        "read_app_data",
        {
            "domain": "calendar",
            "source": "all",
            "start_date": "2026-09-01",
            "end_date": "2026-09-30",
        },
    )


def test_template_authoritative_router_expands_natural_month_to_inclusive_dates():
    proposal = authoritative_template_proposal(
        "查這個月行事曆",
        context={"revision": 4},
    )
    today = datetime.now(ZoneInfo("Asia/Taipei")).date()
    expected_start = date(today.year, today.month, 1).isoformat()
    next_month = date(
        today.year + (1 if today.month == 12 else 0),
        1 if today.month == 12 else today.month + 1,
        1,
    )
    expected_end = (next_month - timedelta(days=1)).isoformat()

    assert proposal is not None
    assert proposal.intent == "calendar.query"
    assert proposal.arguments == {
        "source": "all",
        "start_date": expected_start,
        "end_date": expected_end,
    }
