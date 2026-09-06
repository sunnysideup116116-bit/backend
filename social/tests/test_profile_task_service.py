import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from bson.objectid import ObjectId

from services import profile_task_service as profile_tasks
from services.message_use_service import metadata_for_use


class ProfileTaskServiceTests(unittest.TestCase):
    def test_coverage_accepts_an_owned_new_ai_room(self):
        tasks = MagicMock()
        message_id = ObjectId()
        with patch.object(profile_tasks, "profile_skills_mode_for_user", return_value="on"), \
             patch.object(profile_tasks, "is_owned_public_ai_room", return_value=True), \
             patch.object(profile_tasks.messages_coll, "find", return_value=[{
                 "_id": message_id, "content": "我喜歡安靜咖啡廳",
                 "metadata": {"message_use": metadata_for_use("ordinary", reason="test")},
             }]) as find_messages, \
             patch.object(profile_tasks.PROFILE_RUNS, "find", return_value=[]), \
             patch.object(profile_tasks, "process_profile_message") as process:
            result = profile_tasks.queue_profile_coverage(
                tasks, "owner", "ai_room::owner::topic", [str(message_id)],
            )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["requeued_count"], 1)
        self.assertEqual(
            find_messages.call_args.args[0]["room_id"], "ai_room::owner::topic",
        )
        tasks.add_task.assert_called_once_with(
            process, "owner", "我喜歡安靜咖啡廳", str(message_id), "global", None,
        )

    def test_coverage_rejects_a_foreign_room_before_message_read(self):
        tasks = MagicMock()
        with patch.object(profile_tasks, "profile_skills_mode_for_user", return_value="on"), \
             patch.object(profile_tasks, "is_owned_public_ai_room", return_value=False), \
             patch.object(profile_tasks.messages_coll, "find") as find_messages:
            result = profile_tasks.queue_profile_coverage(
                tasks, "owner", "ai_room::other::topic", [str(ObjectId())],
            )
        self.assertEqual(result["status"], "invalid_scope")
        find_messages.assert_not_called()
        tasks.add_task.assert_not_called()
    def test_enabled_pipeline_schedules_only_typed_profile_extractor(self):
        tasks = MagicMock()
        with patch.object(profile_tasks, "profile_skills_mode_for_user", return_value="on"), \
             patch.object(profile_tasks, "process_profile_message") as process:
            mode = profile_tasks.queue_profile_skills(
                tasks, "owner", "我最近想去爬山", "message-1", "global",
            )

        self.assertEqual(mode, "on")
        tasks.add_task.assert_called_once_with(
            process, "owner", "我最近想去爬山", "message-1", "global", None,
        )

    def test_disabled_pipeline_does_not_schedule_legacy_observer(self):
        tasks = MagicMock()
        mode = profile_tasks.queue_profile_skills(
            tasks, "owner", "我最近想去爬山", "message-1", "global",
            mode_resolver=lambda _user_id: "off",
        )

        self.assertEqual(mode, "off")
        tasks.add_task.assert_not_called()

    def test_followup_only_mode_queues_without_profile_progress_projection(self):
        tasks = MagicMock()
        with patch.object(profile_tasks, "profile_skills_mode_for_user", return_value="off"), \
             patch.object(profile_tasks, "followup_mode_for_user", return_value="shadow"), \
             patch.object(profile_tasks, "process_profile_message") as process, \
             patch.object(profile_tasks.profiles_coll, "update_one") as update:
            mode = profile_tasks.queue_profile_skills(
                tasks, "owner", "週末想去看展", "message-1", "global",
                progress_token="d" * 32,
            )
        self.assertEqual(mode, "shadow")
        update.assert_not_called()
        tasks.add_task.assert_called_once_with(
            process, "owner", "週末想去看展", "message-1", "global", None,
        )

    def test_progress_run_queues_isolated_profile_process_without_message_content(self):
        tasks = MagicMock()
        with patch.object(profile_tasks, "profile_skills_mode_for_user", return_value="on"), \
             patch.object(profile_tasks.profiles_coll, "update_one") as update:
            mode = profile_tasks.queue_profile_skills(
                tasks, "owner", "我最近想去爬山", "message-1", "global",
                progress_token="a" * 32,
            )

        self.assertEqual(mode, "on")
        process = update.call_args.args[1]["$set"]["agentic_profile_process"]
        self.assertEqual(process["kind"], "recent_context")
        self.assertEqual(process["state"], "queued")
        self.assertNotIn("message", process)
        self.assertNotIn("result", process)
        tasks.add_task.assert_called_once_with(
            profile_tasks._run_profile_process,
            "owner", "我最近想去爬山", "message-1", "global", None, "a" * 32,
        )

    def test_profile_process_publishes_only_safe_updated_outcome(self):
        with patch.object(
            profile_tasks.profiles_coll, "update_one",
            side_effect=[SimpleNamespace(modified_count=1), SimpleNamespace(modified_count=1)],
        ) as update, patch.object(
            profile_tasks, "process_profile_message", return_value={"recent_changed": True},
        ):
            profile_tasks._run_profile_process(
                "owner", "我最近想去爬山", "message-1", "global", None, "b" * 32,
            )

        completed = update.call_args_list[1].args[1]["$set"]
        self.assertEqual(completed["agentic_profile_process.state"], "completed")
        self.assertEqual(completed["agentic_profile_process.outcome"], "updated")
        self.assertNotIn("我最近想去爬山", str(completed))

    def test_profile_process_hides_extractor_error_details(self):
        with patch.object(
            profile_tasks.profiles_coll, "update_one",
            side_effect=[SimpleNamespace(modified_count=1), SimpleNamespace(modified_count=1)],
        ) as update, patch.object(
            profile_tasks, "process_profile_message",
            side_effect=RuntimeError("seed_user_08 raw provider failure"),
        ):
            profile_tasks._run_profile_process(
                "owner", "我的原始訊息", "message-1", "global", None, "c" * 32,
            )

        completed = update.call_args_list[1].args[1]["$set"]
        self.assertEqual(completed["agentic_profile_process.outcome"], "error")
        self.assertNotIn("seed_user_08", str(completed))
        self.assertNotIn("provider", str(completed))

    def test_superseded_progress_never_suppresses_saved_message_extraction(self):
        with patch.object(
            profile_tasks.profiles_coll, "update_one",
            side_effect=[SimpleNamespace(modified_count=0), SimpleNamespace(modified_count=0)],
        ), patch.object(
            profile_tasks, "process_profile_message", return_value={"recent_changed": False},
        ) as process:
            profile_tasks._run_profile_process(
                "owner", "第一句仍要處理", "message-1", "global", None, "d" * 32,
            )

        process.assert_called_once_with(
            "owner", "第一句仍要處理", "message-1", "global", None,
        )

    def test_progress_storage_failure_never_suppresses_saved_message_extraction(self):
        with patch.object(
            profile_tasks.profiles_coll, "update_one",
            side_effect=[RuntimeError("progress unavailable"), RuntimeError("still unavailable")],
        ), patch.object(
            profile_tasks, "process_profile_message", return_value={"recent_changed": True},
        ) as process:
            profile_tasks._run_profile_process(
                "owner", "這句仍要處理", "message-2", "global", None, "e" * 32,
            )

        process.assert_called_once_with(
            "owner", "這句仍要處理", "message-2", "global", None,
        )

    def test_due_profile_retry_reuses_validated_source(self):
        cursor = MagicMock()
        cursor.sort.return_value.limit.return_value = [{
            "message_id": "64b64c8f0000000000000011",
            "user_id": "owner",
            "surface": "global",
        }]
        source = {
            "content": "我最近想爬山",
            "metadata": {"message_use": metadata_for_use("ordinary", reason="test")},
        }
        with patch.object(profile_tasks.PROFILE_RUNS, "find", return_value=cursor), \
             patch.object(profile_tasks.messages_coll, "find_one", return_value=source), \
             patch.object(profile_tasks, "process_profile_message", return_value={"recent_changed": True}) as process:
            result = profile_tasks.run_due_profile_retries_once(now=100.0)
        self.assertEqual(result["attempted"], 1)
        self.assertEqual(result["completed"], 1)
        process.assert_called_once_with(
            "owner", "我最近想爬山", "64b64c8f0000000000000011", "global", None,
        )

    def test_due_profile_retry_excludes_calendar_source(self):
        cursor = MagicMock()
        cursor.sort.return_value.limit.return_value = [{
            "message_id": "64b64c8f0000000000000012",
            "user_id": "owner",
        }]
        source = {
            "content": "幫我新增週五聚餐",
            "metadata": {"message_use": metadata_for_use("calendar_operation")},
        }
        with patch.object(profile_tasks.PROFILE_RUNS, "find", return_value=cursor), \
             patch.object(profile_tasks.messages_coll, "find_one", return_value=source), \
             patch.object(profile_tasks.PROFILE_RUNS, "update_one") as update, \
             patch.object(profile_tasks, "process_profile_message") as process:
            result = profile_tasks.run_due_profile_retries_once(now=100.0)
        self.assertEqual(result["excluded"], 1)
        process.assert_not_called()
        self.assertEqual(update.call_args.args[1]["$set"]["status"], "excluded")


if __name__ == "__main__":
    unittest.main()
