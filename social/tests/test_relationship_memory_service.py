import json
import unittest
from unittest.mock import MagicMock, patch

from services import relationship_memory_service as service


class RelationshipMemoryServiceTests(unittest.TestCase):
    def test_extractor_requires_exact_evidence_and_subjective_statement(self):
        response = MagicMock(content=json.dumps({
            "action": "create", "existing_slot": None, "topic": "聊天投入",
            "statement": "對方聊天時不太專心", "evidence_span": "一直看手機",
            "confidence": .95,
        }))
        with patch.object(service, "list_relationship_memories", return_value=[]), \
             patch.object(service, "generate_chat_completion", return_value=response):
            result = service._extract(
                {"owner_user_id": "owner", "other_user_id": "other"},
                {"content": "我覺得他一直看手機，但其他部分還不錯"},
            )
        self.assertEqual(result["action"], "create")
        self.assertTrue(result["statement"].startswith("本人覺得"))

    def test_extractor_rejects_unrelated_or_unanchored_output(self):
        response = MagicMock(content=json.dumps({
            "action": "create", "topic": "天氣", "statement": "本人覺得會下雨",
            "evidence_span": "不存在", "confidence": .99,
        }))
        with patch.object(service, "list_relationship_memories", return_value=[]), \
             patch.object(service, "generate_chat_completion", return_value=response):
            result = service._extract(
                {"owner_user_id": "owner", "other_user_id": "other"},
                {"content": "幫我查明天天氣"},
            )
        self.assertEqual(result["action"], "none")

    def test_context_fails_closed_when_relationship_is_not_active(self):
        with patch.object(service, "_active_match", return_value=None), \
             patch.object(service, "list_relationship_memories") as memories:
            self.assertEqual(service.relationship_memory_context("owner", "other"), [])
        memories.assert_not_called()

    def test_delete_removes_content_and_adds_source_suppression(self):
        current = {
            "_id": "memory-1", "owner_user_id": "owner", "other_user_id": "other",
            "source_message_id": "source-1", "version": 2,
        }
        updated = {**current, "status": "deleted", "version": 3}
        with patch.object(service.RELATIONSHIP_MEMORIES, "find_one", return_value=current), \
             patch.object(service.RELATIONSHIP_MEMORIES, "find_one_and_update", return_value=updated) as update, \
             patch.object(service.RELATIONSHIP_MEMORY_SUPPRESSIONS, "update_one") as suppress:
            result = service.update_relationship_memory("owner", "memory-1", expected_version=2, delete=True)
        self.assertEqual(result["status"], "deleted")
        self.assertIn("statement", update.call_args.args[1]["$unset"])
        suppress.assert_called_once()


if __name__ == "__main__":
    unittest.main()
