import unittest
from unittest.mock import MagicMock, patch

from services import relationship_quiz_service as quiz_service


def _match(quiz: dict | None = None) -> dict:
    return {
        "_id": "match-1",
        "from_user": "owner",
        "to_user": "other",
        "relationship_games": {"compatibility_quiz": quiz or {}},
    }


class RelationshipQuizServiceTests(unittest.TestCase):
    def test_active_quiz_start_is_idempotent_and_does_not_send_second_invite(self):
        active = {
            "status": "active", "round_id": "round-1", "expires_at": 20_000,
            "questions": quiz_service.QUIZ_QUESTIONS, "answers": {},
        }
        with patch.object(quiz_service.time, "time", return_value=10_000), \
             patch.object(quiz_service.matches_coll, "update_one") as update, \
             patch.object(quiz_service, "queue_mediator_event") as queue:
            result = quiz_service.start_quiz(_match(active), "owner", "other")

        self.assertEqual(result["status"], "active")
        self.assertEqual(result["round_id"], "round-1")
        update.assert_not_called()
        queue.assert_not_called()

    def test_second_answer_completes_once_and_publishes_one_shared_result(self):
        answers = {
            "owner": {
                "weekend": "安靜休息",
                "first_meet": "一起吃飯",
                "chat_rhythm": "慢慢聊",
            },
        }
        active = {
            "status": "active", "round_id": "round-1", "expires_at": 20_000,
            "revision": 0, "questions": quiz_service.QUIZ_QUESTIONS, "answers": answers,
        }
        match = _match(active)
        after_answer = _match({
            **active,
            "revision": 1,
            "answers": {**answers, "other": dict(answers["owner"])},
        })
        completed = _match({
            **active,
            "status": "completed", "revision": 2,
            "answers": {**answers, "other": dict(answers["owner"])},
            "result": {"match_count": 3, "matches": [], "total": 3},
        })
        write_answer = MagicMock(modified_count=1)
        complete_round = MagicMock(modified_count=1)
        with patch.object(quiz_service.time, "time", return_value=10_000), \
             patch.object(quiz_service.matches_coll, "update_one", side_effect=[write_answer, complete_round]) as update, \
             patch.object(quiz_service.matches_coll, "find_one", side_effect=[after_answer, completed]), \
             patch.object(quiz_service, "generate_room_id", return_value="pair-room"), \
             patch.object(quiz_service, "save_message") as save:
            result = quiz_service.answer_quiz(match, "other", dict(answers["owner"]))

        self.assertEqual(result["status"], "completed")
        self.assertEqual(update.call_count, 2)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[:3], ("pair-room", "ai_assistant", "你們這輪默契測驗有 3 題答得一樣：安靜休息、一起吃飯、慢慢聊"))
        self.assertEqual(save.call_args.kwargs["metadata"]["event_type"], "compatibility_quiz_result")

    def test_expired_quiz_state_is_marked_expired_without_revealing_result(self):
        expired = {
            "status": "active", "round_id": "round-1", "expires_at": 9_999,
            "questions": quiz_service.QUIZ_QUESTIONS, "answers": {"owner": {}},
            "result": {"match_count": 3},
        }
        with patch.object(quiz_service.time, "time", return_value=10_000), \
             patch.object(
                 quiz_service.matches_coll, "update_one",
                 return_value=MagicMock(modified_count=1),
             ) as update:
            result = quiz_service.public_quiz_state(_match(expired), "owner")

        self.assertEqual(result["status"], "expired")
        self.assertIsNone(result["result"])
        update.assert_called_once_with(
            {
                "_id": "match-1",
                "relationship_games.compatibility_quiz.status": "active",
                "relationship_games.compatibility_quiz.round_id": "round-1",
                "relationship_games.compatibility_quiz.expires_at": {"$lt": 10_000},
                "relationship_games.compatibility_quiz.revision": {"$exists": False},
            },
            {
                "$set": {"relationship_games.compatibility_quiz.status": "expired"},
                "$inc": {"relationship_games.compatibility_quiz.revision": 1},
            },
        )

    def test_concurrent_start_cas_loss_does_not_send_duplicate_invite(self):
        winner = {
            "status": "active", "round_id": "winner-round", "expires_at": 20_000,
            "revision": 1, "questions": quiz_service.QUIZ_QUESTIONS, "answers": {},
        }
        with patch.object(quiz_service.time, "time", return_value=10_000), \
             patch.object(
                 quiz_service.matches_coll, "update_one",
                 return_value=MagicMock(modified_count=0),
             ), \
             patch.object(quiz_service.matches_coll, "find_one", return_value=_match(winner)), \
             patch.object(quiz_service, "queue_mediator_event") as queue:
            result = quiz_service.start_quiz(_match(), "owner", "other")

        self.assertEqual(result["round_id"], "winner-round")
        queue.assert_not_called()

    def test_successful_start_uses_display_name_in_invite(self):
        with patch.object(quiz_service.time, "time", return_value=10_000), \
             patch.object(quiz_service, "uuid4", return_value=MagicMock(hex="12345678abcdef")), \
             patch.object(
                 quiz_service.matches_coll, "update_one",
                 return_value=MagicMock(modified_count=1),
             ), \
             patch.object(quiz_service.matches_coll, "find_one", return_value=None), \
             patch.object(quiz_service, "display_name", return_value="小安"), \
             patch.object(quiz_service, "queue_mediator_event") as queue:
            result = quiz_service.start_quiz(_match(), "seed_user_01", "other")

        self.assertEqual(result["status"], "active")
        self.assertNotIn("seed_user_01", queue.call_args.args[1])
        self.assertIn("小安", queue.call_args.args[1])

    def test_completion_cas_loss_does_not_publish_duplicate_result(self):
        owner_answers = {
            "weekend": "安靜休息",
            "first_meet": "一起吃飯",
            "chat_rhythm": "慢慢聊",
        }
        active = {
            "status": "active", "round_id": "round-1", "expires_at": 20_000,
            "revision": 0, "questions": quiz_service.QUIZ_QUESTIONS,
            "answers": {"owner": owner_answers},
        }
        after_answer = _match({
            **active, "revision": 1,
            "answers": {"owner": owner_answers, "other": dict(owner_answers)},
        })
        completed_elsewhere = _match({
            **after_answer["relationship_games"]["compatibility_quiz"],
            "status": "completed", "revision": 2,
            "result": {"match_count": 3, "matches": [], "total": 3},
        })
        with patch.object(quiz_service.time, "time", return_value=10_000), \
             patch.object(
                 quiz_service.matches_coll, "update_one",
                 side_effect=[MagicMock(modified_count=1), MagicMock(modified_count=0)],
             ), \
             patch.object(
                 quiz_service.matches_coll, "find_one",
                 side_effect=[after_answer, completed_elsewhere],
             ), \
             patch.object(quiz_service, "save_message") as save:
            result = quiz_service.answer_quiz(_match(active), "other", owner_answers)

        self.assertEqual(result["status"], "completed")
        save.assert_not_called()

    def test_cancel_does_not_try_to_overwrite_completed_round(self):
        completed = {
            "status": "completed", "round_id": "round-1", "revision": 2,
            "result": {"match_count": 1, "matches": [], "total": 3},
        }
        with patch.object(quiz_service.matches_coll, "update_one") as update:
            result = quiz_service.cancel_quiz(_match(completed), "owner")

        self.assertEqual(result["status"], "completed")
        update.assert_not_called()


if __name__ == "__main__":
    unittest.main()
