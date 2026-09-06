import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bson.objectid import ObjectId

from models import DirectChatRequest
from routers import public_chat
from routers.chat_messages import _strip_internal_message_use
from services import profile_skills
from services.ayue_agent import proactive_care
from services.ayue_agent.contracts import (
    AgentResult,
    AgentTurnContext,
    PublicAgentTurnContext,
)
from services.ayue_agent.v3 import scheduler
from services.ayue_agent.v3.contracts import Plan, SubTask
from services.ayue_agent.v3.planner import PlannerMetrics
from services.ayue_agent.v3.synthesizer import SynthesizerMetrics
from services.conversation_compaction_service import _prompt_messages
from services.message_use_service import (
    MessageUse,
    metadata_for_use,
    message_use,
    mark_message_use,
    use_from_turn,
)
from services.profile_skills import process_profile_message


class MessageUseServiceTests(unittest.TestCase):
    def test_unmarked_message_fails_closed(self):
        self.assertIs(message_use({"metadata": {}}), MessageUse.UNKNOWN)
        self.assertIs(
            use_from_turn("calendar_operation", owner_message="新增週五行程"),
            MessageUse.CALENDAR_OPERATION,
        )
        self.assertIs(
            use_from_turn("casual", owner_message="不要記住這件事"),
            MessageUse.NO_MEMORY,
        )

    def test_calendar_operation_and_assessment_are_excluded(self):
        calendar = {
            "sender_id": "owner", "content": "新增行程",
            "metadata": {"message_use": metadata_for_use("calendar_operation")},
        }
        assessment = {
            "sender_id": "owner", "content": "探索答案",
            "metadata": {"message_use": metadata_for_use("assessment")},
        }
        ordinary = {
            "sender_id": "owner", "content": "我最近想爬山",
            "metadata": {"message_use": metadata_for_use("ordinary")},
        }
        self.assertEqual(_prompt_messages("owner", [calendar, assessment, ordinary]), [
            {"role": "owner", "content": "我最近想爬山"},
        ])

    def test_mark_message_use_is_owner_and_room_scoped(self):
        message_id = ObjectId()
        with patch(
            "services.message_use_service.messages_coll.update_one",
            return_value=SimpleNamespace(matched_count=1, modified_count=1),
        ) as update:
            self.assertTrue(mark_message_use(
                str(message_id),
                user_id="owner",
                room_id="room",
                use=MessageUse.CALENDAR_OPERATION,
                reason="calendar_operation",
            ))
        self.assertEqual(update.call_args.args[0], {
            "_id": message_id,
            "room_id": "room",
            "sender_id": "owner",
        })
        marker = update.call_args.args[1]["$set"]["metadata.message_use"]
        self.assertEqual(marker["use"], "calendar_operation")
        self.assertEqual(marker["status"], "excluded")

    def test_profile_excludes_calendar_source_before_model_call(self):
        message_id = "64b64c8f0000000000000099"
        source = {
            "content": "幫我新增週五聚餐",
            "metadata": {
                "message_use": metadata_for_use("calendar_operation"),
            },
            "timestamp": 1.0,
        }
        with patch(
            "services.profile_skills.messages_coll.find_one",
            return_value=source,
        ), patch("services.profile_skills.profile_skills_mode_for_user", return_value="on"), patch(
            "services.profile_skills._claim_profile_message"
        ) as claim, patch(
            "services.profile_skills.analyze_profile_message"
        ) as analyze, patch("services.profile_skills._trace"):
            result = process_profile_message(
                "owner", "幫我新增週五聚餐", message_id, "global",
            )
        self.assertEqual(result["reason"], "message_use_calendar_operation")
        claim.assert_not_called()
        analyze.assert_not_called()

    def test_public_history_drops_internal_marker_but_keeps_other_metadata(self):
        messages = _strip_internal_message_use([{
            "content": "新增行程",
            "metadata": {
                "message_use": metadata_for_use("calendar_operation"),
                "event_type": "calendar_command_result",
            },
        }])
        self.assertEqual(messages[0]["metadata"], {"event_type": "calendar_command_result"})

    def test_profile_claim_does_not_update_attempt_count_with_two_operators(self):
        with patch.object(
            profile_skills.PROFILE_RUNS,
            "update_one",
            return_value=SimpleNamespace(upserted_id="run", modified_count=0),
        ) as update:
            token = profile_skills._claim_profile_message(
                "owner", "64b64c8f0000000000000101", "on",
            )
        self.assertTrue(token)
        operation = update.call_args.args[1]
        self.assertNotIn("attempt_count", operation["$setOnInsert"])
        self.assertEqual(operation["$inc"], {"attempt_count": 1})

    def test_expired_profile_claim_stops_before_owner_data_writes(self):
        message_id = "64b64c8f0000000000000102"
        source = {
            "content": "我最近想學做菜",
            "timestamp": 1.0,
            "metadata": {"message_use": metadata_for_use("ordinary")},
        }
        decision = {
            "recent_context": {
                "should_update": True,
                "reason_code": "accepted",
                "fields": {},
                "message_kind": "real_world_update",
            },
            "memories": [{"key": "cooking"}],
            "memory_codes": [],
            "policy_versions": {},
        }
        with patch.object(profile_skills, "profile_skills_mode_for_user", return_value="on"), \
             patch.object(profile_skills.messages_coll, "find_one", return_value=source), \
             patch.object(profile_skills, "_claim_profile_message", return_value="lease"), \
             patch.object(profile_skills, "_profile_claim_is_current", return_value=True), \
             patch.object(profile_skills, "analyze_profile_message", return_value=decision), \
             patch.object(profile_skills.PROFILE_RUNS, "find_one", return_value={"attempt_count": 1}), \
             patch.object(profile_skills, "_renew_profile_claim", return_value=False), \
             patch.object(profile_skills, "apply_recent_context") as apply_recent, \
             patch("services.memory_service.apply_profile_memory_proposals") as apply_memory:
            result = process_profile_message(
                "owner", "我最近想學做菜", message_id, "global",
            )
        self.assertEqual(result, {"status": "skipped", "reason": "stale_claim"})
        apply_recent.assert_not_called()
        apply_memory.assert_not_called()

    def test_slow_recent_projection_rechecks_claim_at_profile_write(self):
        message_id = "64b64c8f0000000000000106"
        source = {
            "content": "我最近想學做菜",
            "timestamp": 1.0,
            "metadata": {"message_use": metadata_for_use("ordinary")},
        }
        decision = {
            "recent_context": {
                "should_update": True,
                "reason_code": "accepted",
                "fields": {
                    "activity": {
                        "operation": "set",
                        "value": "學做菜",
                        "evidence_span": "學做菜",
                    },
                },
                "message_kind": "real_world_update",
                "context_action": "update",
                "episode_relation": "new",
            },
            "memories": [{"key": "cooking"}],
            "memory_codes": [],
            "policy_versions": {},
        }
        with patch.object(profile_skills, "profile_skills_mode_for_user", return_value="on"), \
             patch.object(profile_skills.messages_coll, "find_one", return_value=source), \
             patch.object(profile_skills.profiles_coll, "find_one", return_value={}), \
             patch.object(profile_skills.profiles_coll, "update_one") as profile_write, \
             patch.object(profile_skills, "_claim_profile_message", return_value="lease"), \
             patch.object(profile_skills, "_profile_claim_is_current", return_value=True), \
             patch.object(profile_skills, "analyze_profile_message", return_value=decision), \
             patch.object(profile_skills.PROFILE_RUNS, "find_one", return_value={"attempt_count": 1}), \
             patch.object(
                 profile_skills,
                 "_renew_profile_claim",
                 side_effect=[True, False, False],
             ) as renew, patch.object(
                 profile_skills,
                 "_compose_recent_context_summary",
                 return_value="近期活動：學做菜",
             ), patch.object(profile_skills, "get_embedding", return_value=[]), \
             patch("services.memory_service.apply_profile_memory_proposals") as apply_memory:
            result = process_profile_message(
                "owner", "我最近想學做菜", message_id, "global",
            )
        self.assertEqual(result, {"status": "skipped", "reason": "stale_claim"})
        self.assertEqual(renew.call_count, 3)
        profile_write.assert_not_called()
        apply_memory.assert_not_called()

    def test_prior_calendar_reference_does_not_exclude_unrelated_turn(self):
        raw = AgentTurnContext(
            user_id="owner", room_id="room", message="我最近想學做菜",
        )
        turn = PublicAgentTurnContext(
            user_id="owner",
            room_id="room",
            message=raw.message,
            calendar_recent_reference={"label": "明天聚餐"},
        )
        plan = Plan(tasks=[
            SubTask(
                id="synth", agent="synthesizer", depends_on=[],
                task_brief="陪使用者聊做菜",
            ),
        ])
        with patch.object(scheduler, "build_public_agent_turn_context", return_value=turn), \
             patch.object(scheduler, "plan_turn", return_value=(plan, PlannerMetrics())), \
             patch.object(scheduler, "resolve_message_reference", return_value={"status": "none"}), \
             patch.object(scheduler.ConfirmationManager, "resolve_for_continuation", return_value=None), \
             patch.object(scheduler.ConfirmationManager, "list_active", return_value=[]), \
             patch.object(scheduler.ConfirmationManager, "choice_for_run", return_value=None), \
             patch.object(scheduler, "active_guidance_offer", return_value=None), \
             patch.object(scheduler, "active_assessment_session", return_value=None), \
             patch.object(scheduler, "awaiting_assessment_commit", return_value=None), \
             patch.object(
                 scheduler.synthesizer,
                 "synthesize",
                 return_value=("你想從哪一道菜開始？", None, SynthesizerMetrics()),
             ), patch.object(scheduler, "_persist_trace"):
            result = scheduler.run_public_agent_turn_v3(raw)
        self.assertEqual(result.profile_write_reason, "casual")
        self.assertIs(
            use_from_turn(result.profile_write_reason, owner_message=raw.message),
            MessageUse.ORDINARY,
        )

    def test_proactive_context_requires_reusable_field_evidence(self):
        cursor = MagicMock()
        cursor.sort.return_value.limit.return_value = [{
            "sender_id": "owner",
            "content": "嗨",
            "metadata": {"message_use": metadata_for_use("ordinary")},
        }]
        with patch.object(proactive_care, "most_recent_ai_room", return_value="room"), \
             patch.object(proactive_care.messages_coll, "find", return_value=cursor), \
             patch.object(proactive_care.messages_coll, "find_one") as find_source:
            legacy = proactive_care.build_proactive_care_context(
                "owner", {"current_context": "明天去看牙醫"},
            )
        self.assertEqual(legacy.recent_context, "")
        find_source.assert_not_called()

        evidence_id = "64b64c8f0000000000000103"
        profile = {
            "current_context": "近期想學做菜",
            "recent_context_state": {"fields": {
                "activity": {
                    "value": "學做菜",
                    "evidence_message_id": evidence_id,
                },
            }},
        }
        with patch.object(proactive_care, "most_recent_ai_room", return_value="room"), \
             patch.object(proactive_care.messages_coll, "find", return_value=cursor), \
             patch.object(proactive_care.messages_coll, "find_one", return_value={
                 "metadata": {"message_use": metadata_for_use("ordinary")},
             }) as find_source:
            validated = proactive_care.build_proactive_care_context("owner", profile)
        self.assertEqual(validated.recent_context, "近期想學做菜")
        self.assertEqual(find_source.call_args.args[0]["_id"], ObjectId(evidence_id))

    def test_proactive_context_rejects_partially_unproven_state(self):
        cursor = MagicMock()
        cursor.sort.return_value.limit.return_value = [{
            "sender_id": "owner",
            "content": "嗨",
            "metadata": {"message_use": metadata_for_use("ordinary")},
        }]
        evidence_id = "64b64c8f0000000000000107"
        profile = {
            "current_context": "明天想看牙醫",
            "recent_context_state": {"fields": {
                "activity": {"value": "看牙醫"},
                "timing": {
                    "value": "明天",
                    "evidence_message_id": evidence_id,
                },
            }},
        }
        with patch.object(proactive_care, "most_recent_ai_room", return_value="room"), \
             patch.object(proactive_care.messages_coll, "find", return_value=cursor), \
             patch.object(proactive_care.messages_coll, "find_one") as find_source:
            context = proactive_care.build_proactive_care_context("owner", profile)
        self.assertEqual(context.recent_context, "")
        find_source.assert_not_called()

    def test_assistant_marker_failure_keeps_saved_reply_available(self):
        history = MagicMock()
        history.sort.return_value.limit.return_value = []
        req = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="最近想學做菜",
        )
        with patch.object(public_chat.messages_coll, "find", return_value=history), \
             patch.object(public_chat.profiles_coll, "find_one", return_value={}), \
             patch.object(
                 public_chat,
                 "run_public_agent_turn_v3",
                 return_value=AgentResult(handled=True, reply="聽起來很有趣。"),
             ), patch.object(
                 public_chat,
                 "mark_message_use_from_turn",
                 return_value=MessageUse.ORDINARY,
             ), patch.object(public_chat, "complete_public_ayue_onboarding"), \
             patch.object(
                 public_chat,
                 "save_message",
                 return_value={"message_id": "64b64c8f0000000000000104", "content": "聽起來很有趣。"},
             ), patch.object(
                 public_chat,
                 "mark_message_use",
                 side_effect=RuntimeError("marker unavailable"),
             ):
            response = public_chat._complete_public_turn(
                req,
                "room",
                [],
                background_tasks=None,
                user_message_id="64b64c8f0000000000000105",
            )
        self.assertEqual(response["reply"], "聽起來很有趣。")


if __name__ == "__main__":
    unittest.main()
