"""Exercise template discovery through the existing Flutter event protocol."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import mongomock
import pytest

from app_voice_assistant.capability_proxy import CapabilityRefSigner
from app_voice_assistant.duplex_runtime import run_duplex_session
from app_voice_assistant.duplex_session import _live_tools
from app_voice_assistant.task_service import VoiceTaskService
from app_voice_assistant.template_dispatcher import TEMPLATE_TOOL_COUNT
from app_voice_assistant.tests.test_duplex_runtime import (
    FakeLimiter, FakeLive, FakeProvider, FakeWebSocket, message, wait_until,
)
from app_voice_assistant.tests.test_template_dispatcher import FakeTypes


@asynccontextmanager
async def template_session(context, *, tasks_enabled=True):
    live, socket, events = FakeLive(), FakeWebSocket(), []
    db = mongomock.MongoClient().db
    tasks = VoiceTaskService(db.batches, db.tasks, enabled=tasks_enabled)

    async def emit(event):
        events.append(event)

    async def invoke(call_id, name, args):
        previous = len(live.tool_responses)
        await live.incoming.put(message(tool_calls=[SimpleNamespace(
            id=call_id, name=name, args=args,
        )]))
        await wait_until(lambda: any(
            item[0] == call_id for item in live.tool_responses[previous:]
        ))
        return next(
            item[2] for item in live.tool_responses[previous:] if item[0] == call_id
        )

    async def control(payload):
        await socket.incoming.put({
            "type": "websocket.receive", "text": json.dumps(payload),
        })

    running = asyncio.create_task(run_duplex_session(
        socket, provider=FakeProvider(live), limiter=FakeLimiter(),
        identity="test", user_id="u1", voice_session_id="s1",
        capability_secret=b"secret", routing_mode="template", task_service=tasks,
        initial_context=context, max_session_seconds=30, send_event=emit,
    ))
    try:
        yield SimpleNamespace(
            live=live, events=events, db=db, invoke=invoke, control=control,
        )
    finally:
        await socket.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(running, timeout=2)
        assert live.closed


def voice_context(**changes):
    return {
        "scope": "global", "revision": 1,
        "permissions": {"status_read": True, "profile": True},
        **changes,
    }


def signed_operation(context, action_id, arguments, **identity):
    return {
        "operation_key": "op1",
        "capability_ref": CapabilityRefSigner(b"secret").issue(
            action_id, context=context,
            **{"user_id": "u1", "session_id": "s1", **identity},
        ),
        "arguments": arguments,
    }


def test_template_and_proxy_share_signed_contracts_without_changing_legacy():
    declarations = {
        mode: {d.name: d for d in _live_tools(FakeTypes, mode)[0].function_declarations}
        for mode in ("template", "proxy", "legacy")
    }
    assert len(declarations["template"]) == TEMPLATE_TOOL_COUNT == 21
    assert len(declarations["proxy"]) == 11
    for name in ("find_app_capabilities", "run_app_capabilities", "manage_voice_draft"):
        template = declarations["template"][name]
        assert template.parameters_json_schema == declarations["proxy"][name].parameters_json_schema
        assert template.behavior == "BLOCKING"
        assert name not in declarations["legacy"]


def test_template_daily_digest_completes_with_existing_device_result():
    async def scenario():
        async with template_session(voice_context()) as session:
            result = await session.invoke("digest", "find_app_capabilities", {
                "query": "我有什麼要處理", "mode": "perform",
            })
            assert result["status"] == "queued"
            proposal = next(e for e in session.events if e["type"] == "action_proposal")
            assert proposal["intent"] == "app.digest.query"
            assert proposal["arguments"] == {}
            assert not any(e["type"] == "confirmation_required" for e in session.events)
            await session.control({
                "type": "action_result", "action_id": proposal["action_id"],
                "success": True, "message": "今天有一則配對邀請。",
            })
            await wait_until(lambda: session.db.tasks.count_documents({"status": "completed"}) == 1)
            assert any(e["type"] == "task_update" for e in session.events)

    asyncio.run(scenario())


def test_template_discovers_saved_routine_and_deduplicates_execution():
    async def scenario():
        context = voice_context(voice_config={
            "routines": [{"name": "早安小幫手", "template_id": "daily_briefing"}],
        })
        async with template_session(context) as session:
            found = await session.invoke("find", "find_app_capabilities", {
                "query": "執行早安小幫手", "mode": "perform",
            })
            operation = next(
                action for match in found["matches"] for action in match["actions"]
                if action.get("capability_id") == "routine.run" and action.get("capability_ref")
            )
            arguments = {"operations": [{
                "operation_key": "routine", "capability_ref": operation["capability_ref"],
                "arguments": operation["suggested_arguments"],
            }]}
            first = await session.invoke("run", "run_app_capabilities", arguments)
            second = await session.invoke("run", "run_app_capabilities", arguments)
            assert first == second
            assert first["status"] == "queued"
            proposals = [e for e in session.events if e["type"] == "action_proposal"]
            assert len(proposals) == 1
            assert proposals[0]["intent"] == "routine.run"
            assert proposals[0]["arguments"] == {"name": "早安小幫手"}
            assert session.db.tasks.count_documents({}) == 1

    asyncio.run(scenario())


def test_template_workflow_keeps_write_confirmation_after_draft_step():
    async def scenario():
        context = voice_context()
        async with template_session(context) as session:
            result = await session.invoke("workflow", "run_app_capabilities", {
                "operations": [signed_operation(
                    context, "workflow.update_profile", {"changes": {"age": 25}},
                )],
            })
            assert result["status"] == "queued"
            assert [row["capability_id"] for row in result["tasks"]] == [
                "profile.patch", "profile.request_commit",
            ]
            draft = next(e for e in session.events if e["type"] == "action_proposal")
            assert draft["intent"] == "profile.patch"
            await session.control({
                "type": "action_result", "action_id": draft["action_id"],
                "success": True, "message": "個資草稿已更新。",
            })
            await wait_until(lambda: any(e["type"] == "confirmation_required" for e in session.events))
            confirmation = next(e for e in session.events if e["type"] == "confirmation_required")
            assert confirmation["intent"] == "profile.request_commit"
            assert len([e for e in session.events if e["type"] == "action_proposal"]) == 1
            await session.control({
                "type": "confirmation_response",
                "confirmation_id": confirmation["confirmation_id"],
                "accepted": True,
                "spoken_phrase": confirmation["phrase"],
            })
            await wait_until(lambda: len([e for e in session.events if e["type"] == "action_proposal"]) == 2)
            commit = [e for e in session.events if e["type"] == "action_proposal"][-1]
            assert commit["intent"] == "profile.request_commit"
            assert commit["confirmed"] is True

    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["foreign_owner", "expired", "stale", "tampered", "permission"])
def test_template_catalog_rejects_invalid_authority_without_device_actions(invalid):
    async def scenario():
        context = voice_context(permissions={} if invalid == "permission" else {"status_read": True})
        issued_context = {**context, "revision": 0} if invalid == "stale" else context
        operation = signed_operation(
            issued_context, "app.digest.query", {},
            **({"user_id": "another-user"} if invalid == "foreign_owner" else {}),
            **({"now": 1} if invalid == "expired" else {}),
        )
        if invalid == "tampered":
            operation["capability_ref"] += "x"
        async with template_session(context) as session:
            result = await session.invoke("invalid", "run_app_capabilities", {"operations": [operation]})
            assert result["status"] == "failed"
            assert result["error_code"] == {
                "foreign_owner": "capability_ref_owner_mismatch",
                "expired": "capability_ref_expired",
                "stale": "capability_ref_context_stale",
                "tampered": "capability_ref_invalid",
                "permission": "capability_ref_permission_denied",
            }[invalid]
            assert session.db.tasks.count_documents({}) == 0
            assert not any(e["type"] in {"action_proposal", "confirmation_required"} for e in session.events)

    asyncio.run(scenario())


def test_template_explain_returns_existing_guide_and_permission_cards_without_execution():
    async def scenario():
        async with template_session(voice_context()) as session:
            await session.invoke("help", "find_app_capabilities", {
                "query": "貼文怎麼發布", "mode": "explain",
            })
            assert any(e["type"] == "guide_update" for e in session.events)
            assert any(e["type"] == "permission_repair" for e in session.events)
            assert not any(e["type"] == "action_proposal" for e in session.events)

    asyncio.run(scenario())


def test_template_background_capability_reports_disabled_task_service():
    async def scenario():
        context = voice_context()
        async with template_session(context, tasks_enabled=False) as session:
            result = await session.invoke("digest", "run_app_capabilities", {
                "operations": [signed_operation(context, "app.digest.query", {})],
            })
            assert result["error_code"] == "voice_tasks_disabled"
            assert not any(e["type"] == "action_proposal" for e in session.events)

    asyncio.run(scenario())
