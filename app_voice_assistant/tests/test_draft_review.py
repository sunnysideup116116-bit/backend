import asyncio

from app_voice_assistant.drafts import draft_summary
from app_voice_assistant.tests.test_conversation_drafts import Conversation, calendar_values
from app_voice_assistant.tests.test_duplex_runtime import wait_until
from app_voice_assistant.tests.test_task_service import service


def test_confirmation_summary_describes_title_notes_and_explicit_clearing():
    summary = draft_summary("calendar.update", {
        "target": "晚餐", "title": "一起看展", "notes": "", "location": "美術館",
    })
    assert '修改「晚餐」' in summary
    assert '行程名稱：一起看展' in summary
    assert '地點：美術館' in summary
    assert '備註：清除' in summary
    assert '開始時間' not in summary
    assert '備註：靠窗' in draft_summary("calendar.create", {**calendar_values(), "notes": "靠窗"})


def test_old_draft_is_resumable_after_many_recent_completed_jobs():
    async def scenario():
        tasks = service()
        old = tasks.save_draft("owner", session_id="old", intent="calendar.create", values=calendar_values())
        for _ in range(25):
            row = tasks.save_draft("owner", session_id="later", intent="calendar.create", values=calendar_values())
            tasks.update(row["task_id"], status="completed", stage="completed")
        other = tasks.save_draft("other-owner", session_id="s", intent="calendar.create", values=calendar_values())
        assert old["task_ref"] not in {row["task_ref"] for row in tasks.list_tasks("owner", "all")}
        assert [row["task_ref"] for row in tasks.list_drafts("owner")] == [old["task_ref"]]
        assert other["task_ref"] not in {row["task_ref"] for row in tasks.list_drafts("owner")}
        async with Conversation(tasks) as c:
            result = await c.tool("manage_voice_draft", {"action": "resume"})
            assert result["task"]["task_ref"] == old["task_ref"]
            assert result["status"] == "waiting_confirmation"
            assert not any(event["type"] == "action_proposal" for event in c.events)
    asyncio.run(scenario())


def test_revoking_permission_without_changing_page_invalidates_confirmation():
    async def scenario():
        async with Conversation() as c:
            await c.tool("write_app_action", {"action": "calendar_create", **calendar_values()})
            confirmation = c.confirmations()[0]
            await c.control(type="context_changed", context={
                "scope": "global", "revision": 1,
                "feature_status": {"conversation_drafts": True},
                "permissions": {"calendar_read": True, "calendar_write": False},
            })
            await c.control(type="confirmation_response", confirmation_id=confirmation["confirmation_id"],
                            accepted=True, spoken_phrase=confirmation["phrase"])
            await wait_until(lambda: any(event["type"] == "confirmation_expired" for event in c.events))
            assert not any(event["type"] == "action_proposal" for event in c.events)
            row = c.tasks.get_task("owner", confirmation["task_id"])
            assert row["status"] == "waiting_input"
            assert row["stage"] == "permission_denied"
            assert row["arguments"] == calendar_values()
    asyncio.run(scenario())
