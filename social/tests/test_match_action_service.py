import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks
from models import MatchDecisionRequest
from routers.match import _apply_match_decision
from services import match_action_service
from services.ayue_agent.contracts import AgentTurnContext, PublicAgentTurnContext
from services.ayue_agent.v3.write_executors import _decide_active_proposal


class MatchActionServiceTests(unittest.TestCase):
    @patch("services.match_search_job_service.enqueue_match_search")
    def test_search_enqueues_a_durable_domain_job(self, enqueue):
        enqueue.return_value = {"status": "queued"}
        result = match_action_service.start_match_search(
            "owner", source="agent_v2", force_new=True, idempotency_key="run:0",
        )
        self.assertEqual(result, {"status": "queued"})
        enqueue.assert_called_once_with(
            "owner", source="agent_v2", force_new=True, idempotency_key="run:0",
            origin_room_id="",
        )

    @patch("services.match_action_service.apply_match_decision")
    @patch("services.match_action_service.reconcile_live_match")
    def test_agent_proposal_decision_uses_the_same_compare_and_set_boundary(self, reconcile, apply):
        reconcile.return_value = {
            "_id": "proposal", "from_user": "owner", "to_user": "other", "status": "draft",
        }
        apply.return_value = {"status": "success", "new_status": "pending"}
        result = match_action_service.decide_active_proposal(
            user_id="owner", decision="interested", expected_revision=4, idempotency_key="run:0",
        )
        self.assertEqual(result["status"], "success")
        arguments = apply.call_args.kwargs
        self.assertEqual(arguments["user_id"], "owner")
        self.assertEqual(arguments["match_id"], "proposal")
        self.assertEqual(arguments["action"], "accept")
        self.assertEqual(arguments["expected_status"], "draft")
        self.assertEqual(arguments["expected_revision"], 4)
        self.assertEqual(arguments["idempotency_key"], "run:0")
        self.assertTrue(callable(arguments["after_transition"]))

    @patch("services.match_action_service.apply_match_decision")
    @patch("services.match_action_service.reconcile_live_match")
    def test_non_actionable_or_invalid_proposal_never_reaches_compare_and_set(self, reconcile, apply):
        reconcile.return_value = {
            "_id": "proposal", "from_user": "owner", "to_user": "other", "status": "pending",
        }
        stale = match_action_service.decide_active_proposal(
            user_id="owner", decision="interested", expected_revision=4, idempotency_key="run:0",
        )
        invalid = match_action_service.decide_active_proposal(
            user_id="owner", decision="maybe", expected_revision=4, idempotency_key="run:1",
        )
        self.assertTrue(stale["stale"])
        self.assertTrue(invalid["invalid"])
        apply.assert_not_called()

    @patch("services.match_action_service.apply_match_decision")
    @patch("services.match_action_service.reconcile_live_match")
    def test_waiting_initiator_can_cancel_exact_bound_proposal(self, reconcile, apply):
        reconcile.return_value = {
            "_id": "proposal", "from_user": "owner", "to_user": "other", "status": "pending",
        }
        apply.return_value = {"status": "success", "new_status": "declined"}

        result = match_action_service.decide_active_proposal(
            user_id="owner", decision="cancelled", expected_revision=5,
            expected_match_id="proposal", expected_status="pending",
            idempotency_key="run:cancel",
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(apply.call_args.kwargs["action"], "cancel")
        self.assertEqual(apply.call_args.kwargs["match_id"], "proposal")

    @patch("routers.match.decide_match_action")
    def test_match_decision_endpoint_uses_the_shared_action_boundary(self, decide):
        decide.return_value = {"status": "success", "new_status": "pending"}
        request = MatchDecisionRequest(
            user_id="owner", match_id="64f000000000000000000000", action="accept",
            expected_status="draft", expected_revision=4,
        )
        result = _apply_match_decision(request, BackgroundTasks())
        self.assertEqual(result["status"], "success")
        self.assertEqual(decide.call_args.kwargs["user_id"], "owner")
        self.assertEqual(decide.call_args.kwargs["expected_revision"], 4)
        self.assertTrue(callable(decide.call_args.kwargs["schedule_task"]))

    @patch("services.match_action_service.schedule_match_celebration_gifs")
    @patch("services.match_action_service.queue_mediator_event")
    @patch("services.match_action_service.profiles_coll")
    def test_agent_acceptance_delivers_only_private_mediator_effects(
        self, profiles, queue, schedule_gifs,
    ):
        profiles.find_one.side_effect = [{"user_id": "owner"}, {"user_id": "other"}]
        match_doc = {
            "_id": "proposal", "from_user": "owner", "to_user": "other",
            "status": "accepted", "reason": "有共同話題",
        }
        match_action_service.apply_transition_effects(
            match_doc, "accept", "pending", [], schedule_task=None,
        )
        schedule_gifs.assert_called_once_with(
            (("owner", "other"), ("other", "owner")),
            "proposal", schedule_task=None,
        )
        self.assertEqual(queue.call_count, 2)
        self.assertEqual({call.args[2] for call in queue.call_args_list}, {"match_connected"})
        for call in queue.call_args_list:
            self.assertNotIn("owner", call.args[1])
            self.assertNotIn("other", call.args[1])

    @patch("services.match_action_service.queue_mediator_event")
    @patch("services.match_action_service.profiles_coll")
    def test_pending_v4_transition_queues_intro_before_safe_proposal(self, profiles, queue):
        profiles.find_one.return_value = {"display_name": "小葵", "mediator_tone": "friend"}
        match_doc = {
            "_id": "proposal", "from_user": "owner", "to_user": "other", "status": "pending",
            "reason_version": "v4_friend_intro",
            "reason_items": [{"text": "不應送出"}],
            "receiver_reason_items": [{"text": "不應送出"}],
            "friend_intro_v4": {
                "initiator_preview": {
                    "viewer_id": "owner", "counterparty_id": "other", "viewer_text": "給發起人的理由。",
                },
                "receiver_invitation": {
                    "viewer_id": "other", "counterparty_id": "owner", "viewer_text": "給接收方的邀請。",
                },
            },
        }
        match_action_service.apply_transition_effects(match_doc, "accept", "draft", [])
        self.assertEqual(queue.call_count, 2)
        intro, proposal = queue.call_args_list
        self.assertEqual(intro.args[2], "incoming_match_intro")
        self.assertIn("小葵小葵", intro.args[1])
        self.assertNotIn("owner", intro.args[1])
        self.assertEqual(intro.kwargs["match_id"], "proposal")
        self.assertEqual(proposal.args[2], "incoming_match_interest")
        self.assertEqual(proposal.kwargs["proposal_role"], "receiver")
        self.assertEqual(proposal.kwargs["match_id"], "proposal")
        self.assertNotIn("other_id", proposal.kwargs)
        self.assertNotIn("matches", proposal.kwargs)

    @patch("services.match_action_service.queue_mediator_event")
    @patch("services.match_action_service.profiles_coll")
    def test_pending_event_transition_queues_only_actionable_proposal(
        self, profiles, queue,
    ):
        match_doc = {
            "_id": "event-proposal", "from_user": "owner", "to_user": "other",
            "status": "pending", "proposal_namespace": "event_invitation",
        }

        match_action_service.apply_transition_effects(
            match_doc, "accept", "draft", [],
        )

        profiles.find_one.assert_not_called()
        queue.assert_called_once()
        proposal = queue.call_args
        self.assertEqual(proposal.args[2], "incoming_match_interest")
        self.assertEqual(proposal.kwargs["match_id"], "event-proposal")
        self.assertEqual(proposal.kwargs["proposal_namespace"], "event_invitation")

    @patch("services.match_action_service.save_system_message_once")
    @patch("services.match_action_service.queue_mediator_event")
    def test_accepted_event_reuses_pair_room_with_one_grounded_opening(
        self, queue, save_system_message_once,
    ):
        match_doc = {
            "_id": "event-proposal",
            "from_user": "owner",
            "to_user": "other",
            "status": "accepted",
            "proposal_namespace": "event_invitation",
            "proposal_source": "event_opportunity",
            "relationship_establishing": False,
            "event_snapshot": {
                "title": "港邊音樂市集",
                "category": "市集",
                "region": "高雄",
                "venue": "駁二藝術特區",
                "starts_at": 1786204800,
                "ends_at": 1786212000,
                "time_precision": "datetime",
                "source_url": "https://example.com/event",
            },
        }

        match_action_service.apply_transition_effects(
            match_doc, "accept", "pending", [],
        )

        save_system_message_once.assert_called_once()
        args = save_system_message_once.call_args.args
        kwargs = save_system_message_once.call_args.kwargs
        self.assertEqual(args[0], "other_owner")
        self.assertIn("港邊音樂市集", args[1])
        self.assertEqual(kwargs["message_type"], "system")
        self.assertEqual(kwargs["event_key"], "event-invitation:event-proposal:pair-opening")
        metadata = kwargs["metadata"]
        self.assertEqual(metadata["event_type"], "event_invitation_accepted")
        self.assertEqual(metadata["notification_recipients"], ["owner", "other"])
        self.assertEqual(metadata["event"]["venue"], "駁二藝術特區")
        self.assertNotIn("event_id", metadata["event"])
        self.assertEqual(queue.call_count, 2)

    @patch("services.match_action_service.profiles_coll")
    @patch("services.match_action_service.queue_mediator_event")
    def test_optional_intro_failure_does_not_block_the_actionable_proposal(self, queue, profiles):
        profiles.find_one.return_value = {"display_name": "小葵"}
        queue.side_effect = [RuntimeError("intro failed"), {"event_id": "proposal"}]
        match_doc = {
            "_id": "proposal", "from_user": "owner", "to_user": "other", "status": "pending",
        }
        with patch("builtins.print"):
            match_action_service.apply_transition_effects(match_doc, "accept", "draft", [])
        self.assertEqual(queue.call_count, 2)
        self.assertEqual(queue.call_args_list[1].args[2], "incoming_match_interest")

    @patch("services.match_action_service.schedule_match_celebration_gifs")
    @patch("services.match_action_service.queue_mediator_event", side_effect=RuntimeError("write failed"))
    @patch("services.match_action_service.profiles_coll")
    def test_notification_failure_does_not_block_private_gifs(
        self, profiles, queue, schedule_gifs,
    ):
        profiles.find_one.side_effect = [{"user_id": "owner"}, {"user_id": "other"}]
        match_doc = {
            "_id": "proposal", "from_user": "owner", "to_user": "other", "status": "accepted",
        }
        with patch("builtins.print"):
            match_action_service.apply_transition_effects(match_doc, "accept", "pending", [])
        self.assertEqual(queue.call_count, 2)
        schedule_gifs.assert_called_once_with(
            (("owner", "other"), ("other", "owner")),
            "proposal", schedule_task=None,
        )

    @patch("services.match_action_service.apply_transition_effects", side_effect=RuntimeError("delivery failed"))
    @patch("services.match_action_service.apply_match_decision")
    def test_committed_transition_is_not_reported_as_failed_when_notification_fails(self, apply, _effects):
        def commit(**arguments):
            arguments["after_transition"](
                {"_id": "proposal", "from_user": "owner", "to_user": "other", "status": "accepted"},
                "accept", "pending", [],
            )
            return {"status": "success", "new_status": "accepted"}

        apply.side_effect = commit
        with patch("builtins.print"):
            result = match_action_service.decide_match(
                user_id="other", match_id="proposal", action="accept",
                expected_status="pending", expected_revision=4,
            )
        self.assertEqual(result, {"status": "success", "new_status": "accepted"})

    @patch("services.ayue_agent.v3.write_executors.decide_active_proposal")
    def test_stale_agent_decision_answers_the_latest_terminal_state(self, decide):
        decide.return_value = {"status": "stale", "stale": True, "current_status": "accepted"}
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="有興趣")
        turn = PublicAgentTurnContext(
            user_id="owner", room_id="room", message="有興趣",
            active_proposal={
                "user_can_decide": True,
                "allowed_actions": ["interested", "declined"],
                "proposal_revision": 3,
            },
        )
        ok, reply, code = _decide_active_proposal(
            ctx, turn, "run", 0, {"decision": "interested"},
            {
                "match_id": "proposal", "expected_status": "draft",
                "proposal_revision": 3, "proposal_namespace": "relationship_match",
            },
        )
        self.assertTrue(ok)
        self.assertEqual(code, "stale_revision")
        self.assertIn("互相接受", reply)


if __name__ == "__main__":
    unittest.main()
