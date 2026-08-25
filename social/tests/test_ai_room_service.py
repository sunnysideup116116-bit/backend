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

    def test_room_list_projects_active_assessment_only_onto_its_owner_room(self):
        rooms_p, messages_p, rooms, messages = self._patch_colls()
        with rooms_p, messages_p:
            rooms.find.return_value = iter([{
                "room_id": "ai_room::owner::a", "user_id": "owner",
                "title": "價值觀", "needs_title": False,
                "created_at": 1.0, "updated_at": 2.0,
            }])
            messages.find_one.return_value = None
            projected = ai_room_service.list_rooms(
                "owner",
                assessment_profile={"agentic_assessment_session": {
                    "session_id": "assessment-a", "kind": "deep_profile",
                    "status": "active", "revision": 3,
                    "room_id": "ai_room::owner::a",
                }},
            )

        by_id = {room["room_id"]: room for room in projected}
        self.assertEqual(by_id["ai_room::owner::a"]["assessment_kind"], "deep_profile")
        self.assertEqual(by_id["ai_room::owner::a"]["assessment_state"], "active")
        legacy = by_id[ai_room_service.legacy_ai_room_id("owner")]
        self.assertIsNone(legacy["assessment_state"])

    def test_create_proposal_room_upserts_deterministic_room_with_title(self):
        rooms_p, messages_p, rooms, messages = self._patch_colls()
        with rooms_p, messages_p, patch.object(ai_room_service, "save_system_message_once") as save:
            rooms.find_one.return_value = {
                "room_id": "ai_room::owner::proposal::match-1", "title": "牽線提案",
                "needs_title": False, "is_legacy": False, "is_new": True,
                "created_at": 1.0, "updated_at": 1.0,
            }
            room = ai_room_service.create_proposal_room(
                "owner", "match-1", "有位人選想認識你。",
                metadata={"event_type": "incoming_match_interest"},
            )
        self.assertEqual(room["room_id"], "ai_room::owner::proposal::match-1")
        self.assertEqual(room["title"], ai_room_service.PROPOSAL_AI_ROOM_TITLE)
        rooms.update_one.assert_called_once()
        set_on_insert = rooms.update_one.call_args.args[1]["$setOnInsert"]
        self.assertEqual(set_on_insert["match_id"], "match-1")
        self.assertTrue(set_on_insert["is_proposal_room"])
        self.assertFalse(set_on_insert["is_legacy"])
        self.assertEqual(rooms.update_one.call_args.args[1]["$set"]["is_new"], True)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], "ai_room::owner::proposal::match-1")
        self.assertEqual(save.call_args.args[1], "有位人選想認識你。")
        self.assertEqual(save.call_args.kwargs["message_type"], "mediator_card")

    def test_create_proposal_room_uses_same_room_for_same_match(self):
        from services.chat_service import generate_proposal_ai_room_id

        room_id = generate_proposal_ai_room_id("owner", "match-1")
        self.assertTrue(room_id.startswith("ai_room::owner::proposal::"))
        self.assertIn("match-1", room_id)
        self.assertEqual(generate_proposal_ai_room_id("owner", "match-1"), room_id)
        self.assertNotEqual(generate_proposal_ai_room_id("owner", "match-2"), room_id)

    def test_mark_room_read_clears_new_flag_for_owned_room_only(self):
        rooms_p, messages_p, rooms, _ = self._patch_colls()
        with rooms_p, messages_p:
            rooms.update_one.return_value = MagicMock(modified_count=1)
            self.assertTrue(ai_room_service.mark_room_read("ai_room::owner::x", "owner"))
            self.assertEqual(
                rooms.update_one.call_args.args[1]["$unset"],
                {"is_new": 1},
            )
            # Legacy room and foreign rooms are never touched.
            self.assertFalse(ai_room_service.mark_room_read(ai_room_service.legacy_ai_room_id("owner"), "owner"))
            self.assertFalse(ai_room_service.mark_room_read("ai_room::intruder::x", "owner"))


if __name__ == "__main__":
    unittest.main()
