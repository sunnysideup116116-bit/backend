import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from app_voice_assistant.drafts import draft_question, spoken_draft_patch, clean_draft
from app_voice_assistant.tests.test_task_service import service
from app_voice_assistant.tests.test_duplex_runtime import (
    FakeLive, FakeWebSocket, FakeProvider, FakeLimiter, message, wait_until,
)
from app_voice_assistant.duplex_runtime import run_duplex_session


def calendar_values():
    return {"title": "喝咖啡", "date": (datetime.now(ZoneInfo("Asia/Taipei")) + timedelta(days=2)).date().isoformat(),
            "start_time": "18:00", "end_time": "20:00", "location": "車站"}


def test_partial_draft_only_asks_for_missing_field_and_rejects_authority_fields():
    tasks = service()
    row = tasks.save_draft("owner", session_id="s1", intent="chat.request_send", values={"contact_name": "小安"})
    assert row["status"] == "waiting_input"
    assert draft_question(row["capability_id"], row["arguments"]) == "想傳什麼內容？"
    assert not tasks.project(row)["retryable"]
    with pytest.raises(ValueError):
        clean_draft("chat.request_send", {"user_id": "someone"})
    with pytest.raises(ValueError):
        tasks.retry("owner", row["task_ref"])


def test_patch_is_owner_scoped_revisioned_and_clears_old_contact_binding():
    tasks = service()
    row = tasks.save_draft("owner", session_id="s1", intent="chat.request_send",
                           values={"contact_name": "小安", "message": "一起喝咖啡？", "target_ref": "contact_old"})
    with pytest.raises(ValueError):
        tasks.save_draft("other", session_id="s2", intent="chat.request_send", values={"contact_name": "小美"}, task_ref=row["task_ref"])
    updated = tasks.save_draft("owner", session_id="s1", intent="chat.request_send", values={"contact_name": "小美"},
                               task_ref=row["task_ref"], expected_revision=row["revision"])
    assert updated["arguments"] == {"contact_name": "小美", "message": "一起喝咖啡？"}
    assert tasks.project(updated)["draft"]["changed_fields"] == ["contact_name"]
    with pytest.raises(ValueError, match="revision_stale"):
        tasks.save_draft("owner", session_id="s1", intent="chat.request_send", values={"message": "stale"},
                         task_ref=row["task_ref"], expected_revision=row["revision"])


@pytest.mark.parametrize("stage", ["waiting_device", "device_result_unknown", "failed", "completed"])
def test_dispatched_or_unknown_writes_cannot_be_edited_or_replayed(stage):
    tasks = service()
    row = tasks.save_draft("owner", session_id="s1", intent="calendar.create", values=calendar_values())
    status = "waiting_input" if stage == "device_result_unknown" else stage
    row = tasks.update(row["task_id"], status=status, stage=stage)
    with pytest.raises(ValueError):
        tasks.save_draft("owner", session_id="s2", intent="calendar.create", values={"title": "another"}, task_ref=row["task_ref"])
    assert not tasks.project(row)["retryable"]


def test_resume_revalidates_dates_and_retains_other_fields():
    tasks = service()
    row = tasks.save_draft("owner", session_id="s1", intent="calendar.create", values=calendar_values())
    row = tasks.save_draft("owner", session_id="s2", intent="calendar.create", values={"start_time": "19:00"}, task_ref=row["task_ref"], paused=True)
    assert row["stage"] == "draft_paused"
    assert row["arguments"]["location"] == "車站"
    projected = tasks.list_tasks("owner")[0]
    assert projected["draft"]["values"]["start_time"] == "19:00"
    row = tasks.save_draft("owner", session_id="s2", intent="calendar.create", values={"date": "2000-01-01"}, task_ref=row["task_ref"])
    assert "日期已經過了" in draft_question(row["capability_id"], row["arguments"])
    assert not tasks.project(row)["retryable"]


def test_narrow_spoken_corrections_preserve_period_and_do_not_consume_detours():
    assert spoken_draft_patch("改七點", "calendar.create", calendar_values()) == {"start_time": "19:00"}
    assert spoken_draft_patch("改成七點，地點改成公園", "calendar.create", calendar_values()) == {"start_time": "19:00", "location": "公園"}
    assert spoken_draft_patch("改成七點，順便傳給小安", "calendar.create", calendar_values()) is None
    assert spoken_draft_patch("先查天氣", "chat.request_send", {"contact_name": "小安"}) is None
    assert spoken_draft_patch("取消", "chat.request_send", {"contact_name": "小安"}) is None


class Conversation:
    def __init__(self, tasks=None):
        self.live, self.ws, self.events = FakeLive(), FakeWebSocket(), []
        self.tasks = tasks or service()
        self.counter = 0

    async def __aenter__(self):
        self.job = asyncio.create_task(run_duplex_session(
            self.ws, provider=FakeProvider(self.live), limiter=FakeLimiter(), identity="owner",
            user_id="owner", voice_session_id="test", max_session_seconds=60,
            initial_context={"scope": "global", "revision": 1,
                "feature_status": {"conversation_drafts": True},
                "permissions": {"calendar_read": True, "calendar_write": True, "chat_list": True, "chat_send": True}},
            send_event=self.emit, routing_mode="template", task_service=self.tasks,
        ))
        await wait_until(lambda: bool(self.live.text))
        return self

    async def emit(self, event):
        self.events.append(event)

    async def tool(self, name, args):
        self.counter += 1
        call_id = f"call-{self.counter}"
        await self.live.incoming.put(message(tool_calls=[SimpleNamespace(id=call_id, name=name, args=args)]))
        await wait_until(lambda: any(item[0] == call_id for item in self.live.tool_responses))
        return next(item[2] for item in self.live.tool_responses if item[0] == call_id)

    async def control(self, **payload):
        await self.ws.incoming.put({"text": json.dumps(payload)})

    def confirmations(self):
        return [e for e in self.events if e["type"] == "confirmation_required"]

    async def __aexit__(self, *_):
        await self.ws.incoming.put({"type": "websocket.disconnect"})
        await asyncio.wait_for(self.job, 2)


def test_partial_then_spoken_patch_replaces_confirmation_and_old_button_cannot_send():
    async def scenario():
        async with Conversation() as c:
            result = await c.tool("manage_voice_draft", {"action": "start", "intent": "chat.request_send", "fields": {"contact_name": "小安"}})
            assert result["spoken_prompt"] == "想傳什麼內容？"
            assert not c.confirmations()
            await c.control(type="utterance", text="內容改成週六一起喝咖啡？")
            await wait_until(lambda: len(c.confirmations()) == 1)
            old = c.confirmations()[0]
            await c.control(type="utterance", text="換成小美")
            await wait_until(lambda: len(c.confirmations()) == 2)
            new = c.confirmations()[-1]
            assert old["confirmation_id"] != new["confirmation_id"]
            assert new["arguments"]["contact_name"] == "小美"
            assert new["arguments"]["message"] == "週六一起喝咖啡？"
            await c.control(type="confirmation_response", confirmation_id=old["confirmation_id"], accepted=True, spoken_phrase=old["phrase"])
            await c.control(type="confirmation_response", confirmation_id=new["confirmation_id"], accepted=True, spoken_phrase=new["phrase"])
            await wait_until(lambda: any(e["type"] == "action_proposal" for e in c.events))
            sent = [e for e in c.events if e["type"] == "action_proposal"]
            assert len(sent) == 1 and sent[0]["arguments"]["contact_name"] == "小美"
            await c.control(type="action_result", action_id=sent[0]["action_id"], success=True, message="已送出")
            await wait_until(lambda: c.tasks.get_task("owner", new["task_id"])["status"] == "completed")
    asyncio.run(scenario())


def test_detour_pauses_draft_and_resume_requires_fresh_confirmation():
    async def scenario():
        async with Conversation() as c:
            await c.tool("write_app_action", {"action": "calendar_create", **calendar_values()})
            old = c.confirmations()[0]
            # Weather unavailable still must preserve the task, not count as complete.
            await c.tool("read_weather", {"location": "台北"})
            row = c.tasks.get_task("owner", old["task_id"])
            assert row["stage"] == "draft_paused"
            assert not any(e["type"] == "action_proposal" for e in c.events)
            await c.control(type="utterance", text="繼續剛才")
            await wait_until(lambda: len(c.confirmations()) == 2)
            assert c.confirmations()[-1]["arguments"]["location"] == "車站"
            assert c.confirmations()[-1]["confirmation_id"] != old["confirmation_id"]
    asyncio.run(scenario())


def test_restart_restores_draft_without_running_and_resume_keeps_parameters():
    async def scenario():
        tasks = service()
        async with Conversation(tasks) as c:
            await c.tool("write_app_action", {"action": "calendar_create", **calendar_values()})
        async with Conversation(tasks) as c:
            assert not c.confirmations()
            result = await c.tool("manage_voice_draft", {"action": "resume"})
            assert result["status"] == "waiting_confirmation"
            assert len(c.confirmations()) == 1
            assert not any(e["type"] == "action_proposal" for e in c.events)
    asyncio.run(scenario())


def test_model_cannot_confirm_its_own_new_or_corrected_draft():
    async def scenario():
        async with Conversation() as c:
            await c.tool("write_app_action", {"action": "calendar_create", **calendar_values()})
            result = await c.tool("confirm_pending_action", {"spoken_phrase": "確認"})
            assert result["status"] == "confirmation_mismatch"
            assert not any(e["type"] == "action_proposal" for e in c.events)
            await c.control(type="utterance", text="改成七點")
            await wait_until(lambda: len(c.confirmations()) == 2)
            result = await c.tool("confirm_pending_action", {"spoken_phrase": "確認"})
            assert result["status"] == "confirmation_mismatch"
            assert not any(e["type"] == "action_proposal" for e in c.events)
    asyncio.run(scenario())


def test_multiple_saved_drafts_require_selection_and_permission_is_rechecked():
    async def scenario():
        tasks = service()
        for title in ("喝咖啡", "看展覽"):
            tasks.save_draft("owner", session_id="old", intent="calendar.create", values={**calendar_values(), "title": title})
        async with Conversation(tasks) as c:
            result = await c.tool("manage_voice_draft", {"action": "resume"})
            assert result["status"] == "needs_input" and len(result["drafts"]) == 2
            assert not c.confirmations()
            ref = result["drafts"][0]["task_ref"]
            await c.control(type="context_changed", context={"scope": "global", "revision": 2,
                "feature_status": {"conversation_drafts": True}, "permissions": {"calendar_read": True}})
            await wait_until(lambda: len(getattr(c.live, "screen_contexts", [])) >= 2)
            result = await c.tool("manage_voice_draft", {"action": "resume", "task_ref": ref})
            assert result["status"] == "permission_denied"
            assert not c.confirmations()
    asyncio.run(scenario())


def test_another_connection_correction_invalidates_original_confirmation_atomically():
    async def scenario():
        tasks = service()
        async with Conversation(tasks) as first:
            await first.tool("write_app_action", {"action": "calendar_create", **calendar_values()})
            old = first.confirmations()[0]
            async with Conversation(tasks) as second:
                result = await second.tool("manage_voice_draft", {
                    "action": "update", "task_ref": f"task_{old['task_id']}", "fields": {"start_time": "19:00"},
                })
                assert result["status"] == "waiting_confirmation"
                new = second.confirmations()[-1]
                await first.control(type="confirmation_response", confirmation_id=old["confirmation_id"], accepted=True, spoken_phrase=old["phrase"])
                await first.tool("describe_current_screen", {})  # drain the first receiver
                assert not any(e["type"] == "action_proposal" for e in first.events)
                assert tasks.get_task("owner", old["task_id"])["action_id"] == new["confirmation_id"]
                await second.control(type="confirmation_response", confirmation_id=new["confirmation_id"], accepted=True, spoken_phrase=new["phrase"])
                await wait_until(lambda: any(e["type"] == "action_proposal" for e in second.events))
                assert [e for e in second.events if e["type"] == "action_proposal"][0]["arguments"]["start_time"] == "19:00"
    asyncio.run(scenario())


def test_closing_old_connection_cannot_expire_new_confirmation():
    async def scenario():
        tasks = service()
        async with Conversation(tasks) as first:
            await first.tool("write_app_action", {"action": "calendar_create", **calendar_values()})
            old = first.confirmations()[0]
            async with Conversation(tasks) as second:
                await second.tool("manage_voice_draft", {"action": "update", "task_ref": f"task_{old['task_id']}", "fields": {"start_time": "19:00"}})
                new = second.confirmations()[-1]
                await first.ws.incoming.put({"type": "websocket.disconnect"})
                await asyncio.wait_for(first.job, 2)
                row = tasks.get_task("owner", old["task_id"])
                assert row["action_id"] == new["confirmation_id"]
                assert row["status"] == "waiting_confirmation"
    asyncio.run(scenario())


def test_stale_target_asks_to_select_again_instead_of_claiming_confirmation():
    async def scenario():
        async with Conversation() as c:
            result = await c.tool("write_app_action", {
                "action": "chat_send", "contact_name": "小安", "message": "你好",
                "target_ref": "surface-1-2-1",
            })
            assert result["status"] == "waiting_input"
            assert "重新選擇" in result["spoken_prompt"]
            assert not c.confirmations()
    asyncio.run(scenario())
