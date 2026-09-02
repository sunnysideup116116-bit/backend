import unittest
from unittest.mock import patch

from pydantic import ValidationError

from models import DirectChatRequest, MediatorPrivateRequest
from services.ayue_agent.v3.confirmation import (
    INTERACTION_BUBBLE,
    INTERACTION_LEGACY,
    SURFACE_PUBLIC,
    ConfirmationManager,
    interaction_mode_for_action,
)
from services.ayue_agent.v3.test_store import MemoryCollection
from services.ayue_agent.private_v2 import (
    PRIVATE_CONFIRMATIONS,
    PrivateAgentTurnContextV2,
    run_private_agent_turn_v2,
)


class ChatChoiceProtocolTests(unittest.TestCase):
    def setUp(self):
        self.store = MemoryCollection()
        self.manager = ConfirmationManager(self.store)

    def _create_and_present(self, room_id: str, run_id: str) -> str:
        choice_id = self.manager.create_confirmation(
            user_id="owner",
            agent_name="match",
            tool_name="match.start_search",
            arguments={},
            payload={},
            origin_run_id=run_id,
            preview="要開始找人嗎？",
            room_id=room_id,
            surface=SURFACE_PUBLIC,
        )
        self.assertTrue(self.manager.bind_final_preview(
            user_id="owner", origin_run_id=run_id,
            final_content="要開始找人嗎？",
        ))
        self.assertTrue(self.manager.mark_presented(
            user_id="owner", origin_run_id=run_id,
            message_id=f"message-{run_id}",
            persisted_content="要開始找人嗎？",
        ))
        return choice_id

    def test_button_choices_are_room_scoped_and_replace_only_same_room(self):
        first_a = self._create_and_present("room-a", "run-a1")
        first_b = self._create_and_present("room-b", "run-b1")
        second_a = self.manager.create_confirmation(
            user_id="owner", agent_name="calendar",
            tool_name="calendar.submit_commands", arguments={}, payload={},
            origin_run_id="run-a2", preview="要更新行程嗎？",
            room_id="room-a", surface=SURFACE_PUBLIC,
        )

        self.assertNotEqual(first_a, second_a)
        self.assertEqual(
            self.manager.choice_projection(
                user_id="owner", room_id="room-a", surface=SURFACE_PUBLIC,
                choice_id=first_a,
            )["state"],
            "superseded",
        )
        self.assertEqual(
            self.manager.choice_projection(
                user_id="owner", room_id="room-b", surface=SURFACE_PUBLIC,
                choice_id=first_b,
            )["state"],
            "pending",
        )

    def test_continuing_conversation_auto_cancels_exact_room(self):
        choice_a = self._create_and_present("room-a", "run-a")
        choice_b = self._create_and_present("room-b", "run-b")

        resolution = self.manager.resolve_for_continuation(
            user_id="owner", room_id="room-a", surface=SURFACE_PUBLIC,
        )

        self.assertEqual(resolution["id"], choice_a)
        self.assertEqual(resolution["state"], "auto_cancelled")
        self.assertEqual(
            self.manager.choice_projection(
                user_id="owner", room_id="room-b", surface=SURFACE_PUBLIC,
                choice_id=choice_b,
            )["state"],
            "pending",
        )

    def test_confirm_requires_exact_owner_room_and_choice_id(self):
        choice_id = self._create_and_present("room-a", "run-a")
        executor_calls = []

        wrong_room = self.manager.execute_confirmed(
            user_id="owner", room_id="room-b", surface=SURFACE_PUBLIC,
            interaction_mode=INTERACTION_BUBBLE, choice_id=choice_id,
            executor=lambda *args: executor_calls.append(args) or (True, "done", None),
        )
        accepted = self.manager.execute_confirmed(
            user_id="owner", room_id="room-a", surface=SURFACE_PUBLIC,
            interaction_mode=INTERACTION_BUBBLE, choice_id=choice_id,
            executor=lambda *args: executor_calls.append(args) or (True, "done", None),
        )
        replay = self.manager.execute_confirmed(
            user_id="owner", room_id="room-a", surface=SURFACE_PUBLIC,
            interaction_mode=INTERACTION_BUBBLE, choice_id=choice_id,
            executor=lambda *args: executor_calls.append(args) or (True, "done", None),
        )

        self.assertEqual(wrong_room, [])
        self.assertTrue(accepted[0]["ok"])
        self.assertEqual(replay, [])
        self.assertEqual(len(executor_calls), 1)

    def test_card_owned_match_decisions_remain_legacy_text(self):
        for action in (
            "calendar.submit_commands",
            "match.start_search",
            "profile.start_assessment",
            "profile.commit_assessment",
            "relationship.start_date_coordination",
            "private.date.start_coordination",
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    interaction_mode_for_action(action),
                    INTERACTION_BUBBLE,
                )
        self.assertEqual(
            interaction_mode_for_action("match.decide_active_proposal"),
            INTERACTION_LEGACY,
        )
        self.assertEqual(
            interaction_mode_for_action("match.decide_active_event_invitation"),
            INTERACTION_LEGACY,
        )

    def test_request_models_require_message_or_complete_choice(self):
        public = DirectChatRequest(
            user_id="owner", contact_id="ai_assistant", message="",
            choice_id="choice-1", choice_action="confirm",
        )
        private = MediatorPrivateRequest(
            user_id="owner", other_id="other", message="",
            choice_id="choice-2", choice_action="cancel",
        )
        self.assertEqual(public.choice_action, "confirm")
        self.assertEqual(private.choice_action, "cancel")
        with self.assertRaises(ValidationError):
            DirectChatRequest(
                user_id="owner", contact_id="ai_assistant",
                message="確認", choice_id="choice-1", choice_action="confirm",
            )
        with self.assertRaises(ValidationError):
            MediatorPrivateRequest(user_id="owner", other_id="other", message="")

    def test_private_button_confirmation_executes_without_a_text_message(self):
        PRIVATE_CONFIRMATIONS.clear()
        private_manager = ConfirmationManager(PRIVATE_CONFIRMATIONS)
        choice_id = private_manager.create_confirmation(
            user_id="owner", agent_name="private_relationship",
            tool_name="private.date.start_coordination", arguments={},
            payload={"other_id": "other", "pair_revision": 3},
            origin_run_id="private-run", preview="要發起約會協調嗎？",
            room_id="mediator_private::owner::other", surface="private_ayue",
        )
        private_manager.bind_final_preview(
            user_id="owner", origin_run_id="private-run",
            final_content="要發起約會協調嗎？",
        )
        private_manager.mark_presented(
            user_id="owner", origin_run_id="private-run", message_id="message-1",
            persisted_content="要發起約會協調嗎？",
        )
        context = PrivateAgentTurnContextV2(
            user_id="owner", other_id="other",
            room_id="mediator_private::owner::other", message="", pair_revision=3,
            viewer_profile={}, counterparty_shareable={}, counterparty_advisory={},
            shared_history=[], private_history=[], shared_facts=[], local_time="now",
        )
        with patch(
            "services.ayue_agent.private_v2.build_private_turn_context_v2",
            return_value=context,
        ), patch(
            "services.ayue_agent.private_v2._execute_write",
            return_value=(True, "date_invite_created"),
        ) as execute_write, patch(
            "services.ayue_agent.private_v2._plan",
        ) as planner, patch(
            "services.ayue_agent.private_v2._trace",
        ):
            result = run_private_agent_turn_v2(
                user_id="owner", other_id="other", message="",
                match_doc={"status": "accepted", "proposal_revision": 3},
                choice_id=choice_id, choice_action="confirm",
            )

        self.assertIn("已經先問對方", result.reply)
        self.assertEqual(result.choice_resolution["state"], "confirmed")
        execute_write.assert_called_once()
        planner.assert_not_called()

    def test_private_button_rejects_a_changed_relationship_revision(self):
        PRIVATE_CONFIRMATIONS.clear()
        manager = ConfirmationManager(PRIVATE_CONFIRMATIONS)
        choice_id = manager.create_confirmation(
            user_id="owner", agent_name="private_relationship",
            tool_name="private.date.start_coordination", arguments={},
            payload={"other_id": "other", "pair_revision": 3},
            origin_run_id="private-stale", preview="要發起約會協調嗎？",
            room_id="mediator_private::owner::other", surface="private_ayue",
        )
        manager.bind_final_preview(
            user_id="owner", origin_run_id="private-stale",
            final_content="要發起約會協調嗎？",
        )
        manager.mark_presented(
            user_id="owner", origin_run_id="private-stale", message_id="message-2",
            persisted_content="要發起約會協調嗎？",
        )
        context = PrivateAgentTurnContextV2(
            user_id="owner", other_id="other",
            room_id="mediator_private::owner::other", message="", pair_revision=4,
            viewer_profile={}, counterparty_shareable={}, counterparty_advisory={},
            shared_history=[], private_history=[], shared_facts=[], local_time="now",
        )
        with patch(
            "services.ayue_agent.private_v2.build_private_turn_context_v2",
            return_value=context,
        ), patch(
            "services.ayue_agent.private_v2._execute_write",
        ) as execute_write, patch(
            "services.ayue_agent.private_v2._trace",
        ):
            result = run_private_agent_turn_v2(
                user_id="owner", other_id="other", message="",
                match_doc={"status": "accepted", "proposal_revision": 4},
                choice_id=choice_id, choice_action="confirm",
            )

        self.assertEqual(result.choice_resolution["state"], "failed")
        self.assertIn("無法送出", result.reply)
        execute_write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
