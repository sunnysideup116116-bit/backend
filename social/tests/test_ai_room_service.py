import unittest
from unittest.mock import MagicMock, patch

from services import ai_room_service
from services.chat_service import (
    ai_room_owner,
    generate_ai_room_id,
    is_ai_room,
)


class AiRoomIdTests(unittest.TestCase):
    def test_generated_room_id_is_owned_by_the_user_and_is_ai_prefixed(self):
        room_id = generate_ai_room_id("owner")
        self.assertTrue(is_ai_room(room_id))
        self.assertEqual(ai_room_owner(room_id), "owner")

    def test_foreign_room_id_is_not_recognized_as_ai_room(self):
        self.assertFalse(is_ai_room("owner_other"))
        self.assertIsNone(ai_room_owner("owner_other"))


class AiRoomServiceTests(unittest.TestCase):
    def _patch_colls(self):
        rooms = MagicMock()
        messages = MagicMock()
        return (
            patch.object(ai_room_service, "ai_rooms_coll", rooms),
            patch.object(ai_room_service, "messages_coll", messages),
            rooms,
            messages,
        )

    def test_create_room_persists_a_document_owned_by_the_user(self):
        with self._patch_colls()[0], self._patch_colls()[1]:
            with patch.object(ai_room_service, "generate_ai_room_id", return_value="ai_room::owner::x"):
                room = ai_room_service.create_room("owner")
            self.assertEqual(room["room_id"], "ai_room::owner::x")
            self.assertIsNone(room["title"])
            self.assertFalse(room["needs_title"])
            self.assertFalse(room["is_legacy"])
            ai_room_service.ai_rooms_coll.insert_one.assert_called_once()
            doc = ai_room_service.ai_rooms_coll.insert_one.call_args.args[0]
            self.assertEqual(doc["user_id"], "owner")

    def test_get_room_rejects_foreign_room_ids(self):
        with self._patch_colls()[0], self._patch_colls()[1]:
            # Another user's room: owner embedded in id is not the requester.
            self.assertIsNone(ai_room_service.get_room("ai_room::intruder::x", "owner"))

    def test_get_room_synthesizes_the_legacy_room(self):
        with self._patch_colls()[0], self._patch_colls()[1]:
            ai_room_service.messages_coll.find_one.return_value = None
            room = ai_room_service.get_room(ai_room_service.legacy_ai_room_id("owner"), "owner")
        self.assertIsNotNone(room)
        self.assertTrue(room["is_legacy"])
        self.assertEqual(room["title"], ai_room_service.LEGACY_AI_ROOM_TITLE)

    def test_rename_room_blocks_legacy_room_and_foreign_owners(self):
        with self._patch_colls()[0], self._patch_colls()[1]:
            self.assertIsNone(ai_room_service.rename_room(ai_room_service.legacy_ai_room_id("owner"), "owner", "新名"))
            self.assertIsNone(ai_room_service.rename_room("ai_room::intruder::x", "owner", "新名"))
            ai_room_service.ai_rooms_coll.update_one.assert_not_called()

    def test_rename_room_updates_title_for_owned_room(self):
        rooms_p, messages_p, rooms, _ = self._patch_colls()
        with rooms_p, messages_p:
            rooms.find_one.return_value = {"room_id": "ai_room::owner::x", "title": "新名", "needs_title": False}
            room = ai_room_service.rename_room("ai_room::owner::x", "owner", "新名")
        self.assertIsNotNone(room)
        rooms.update_one.assert_called_once()
        self.assertEqual(rooms.update_one.call_args.args[1]["$set"]["title"], "新名")

    def test_delete_room_blocks_legacy_and_foreign_rooms(self):
        with self._patch_colls()[0], self._patch_colls()[1]:
            self.assertFalse(ai_room_service.delete_room(ai_room_service.legacy_ai_room_id("owner"), "owner"))
            self.assertFalse(ai_room_service.delete_room("ai_room::intruder::x", "owner"))
            ai_room_service.ai_rooms_coll.delete_one.assert_not_called()

    def test_delete_room_removes_only_the_room_document_not_messages(self):
        rooms_p, messages_p, rooms, messages = self._patch_colls()
        with rooms_p, messages_p:
            rooms.delete_one.return_value = MagicMock(deleted_count=1)
            ok = ai_room_service.delete_room("ai_room::owner::x", "owner")
        self.assertTrue(ok)
        rooms.delete_one.assert_called_once()
        messages.delete_many.assert_not_called()

    def test_mark_first_message_sets_needs_title_only_on_first_user_message(self):
        rooms_p, messages_p, rooms, messages = self._patch_colls()
        with rooms_p, messages_p:
            messages.count_documents.return_value = 1
            self.assertTrue(ai_room_service.mark_first_message_for_title("ai_room::owner::x", "owner"))
            messages.count_documents.return_value = 2
            self.assertFalse(ai_room_service.mark_first_message_for_title("ai_room::owner::x", "owner"))
            # Legacy room never flagged.
            self.assertFalse(ai_room_service.mark_first_message_for_title(ai_room_service.legacy_ai_room_id("owner"), "owner"))

    def test_ensure_room_title_skips_legacy_and_foreign_rooms(self):
        rooms_p, messages_p, rooms, _ = self._patch_colls()
        with rooms_p, messages_p:
            ai_room_service.ensure_room_title(ai_room_service.legacy_ai_room_id("owner"), "owner", "嗨")
            ai_room_service.ensure_room_title("ai_room::intruder::x", "owner", "嗨")
            rooms.find_one.assert_not_called()

    def test_ensure_room_title_marks_needs_title_on_llm_failure(self):
        rooms_p, messages_p, rooms, _ = self._patch_colls()
        with rooms_p, messages_p:
            rooms.find_one.return_value = {"needs_title": True}
            with patch("services.ai_service.generate_chat_completion", side_effect=RuntimeError("no key")):
                ai_room_service.ensure_room_title("ai_room::owner::x", "owner", "我想聊聊")
        # Two update_one calls: one for the failure path marking needs_title True.
        self.assertGreaterEqual(rooms.update_one.call_count, 1)
        last_set = rooms.update_one.call_args.args[1]["$set"]
        self.assertTrue(last_set["needs_title"])

    def test_ensure_room_title_writes_generated_title_on_success(self):
        rooms_p, messages_p, rooms, _ = self._patch_colls()
        with rooms_p, messages_p:
            rooms.find_one.return_value = {"needs_title": True}
            fake = MagicMock(content="關於旅行的事")
            with patch("services.ai_service.generate_chat_completion", return_value=fake):
                ai_room_service.ensure_room_title("ai_room::owner::x", "owner", "我想聊聊旅行")
        rooms.update_one.assert_called_once()
        self.assertEqual(rooms.update_one.call_args.args[1]["$set"]["title"], "關於旅行的事")
        self.assertFalse(rooms.update_one.call_args.args[1]["$set"]["needs_title"])

    def test_most_recent_ai_room_falls_back_to_legacy_when_no_rooms_have_messages(self):
        rooms_p, messages_p, rooms, messages = self._patch_colls()
        with rooms_p, messages_p:
            rooms.find.return_value = iter([])
            messages.find_one.return_value = None
            room_id = ai_room_service.most_recent_ai_room("owner")
        self.assertEqual(room_id, ai_room_service.legacy_ai_room_id("owner"))


if __name__ == "__main__":
    unittest.main()