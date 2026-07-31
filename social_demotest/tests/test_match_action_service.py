import unittest
from unittest.mock import patch

from fastapi import BackgroundTasks
from models import MatchDecisionRequest
from routers.match import _apply_match_decision
from services import match_action_service
from services.ayue_agent.contracts import AgentTurnContext, AgentTurnContextV2
from services.ayue_agent.runtime import _decide_active_proposal


class MatchActionServiceTests(unittest.TestCase):
    def test_search_fails_closed_without_registered_executor(self):
        with patch.object(match_action_service, "_search_executor", None):
            result = match_action_service.start_match_search("owner", source="agent_v2", force_new=True)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["detail"], "match_search_executor_unavailable")

    def test_search_uses_the_registered_domain_executor(self):
        calls = []

        def executor(user_id, source, force_new):
            calls.append((user_id, source, force_new))
            return {"status": "queued"}

        with patch.object(match_action_service, "_search_executor", executor):
            result = match_action_service.start_match_search("owner", source="agent_v2", force_new=True)
        self.assertEqual(result, {"status": "queued"})
        self.assertEqual(calls, [("owner", "agent_v2", True)])

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

    @patch("services.match_action_service.queue_mediator_event")
    @patch("services.match_action_service.save_message")
    @patch("services.match_action_service.generate_peer_first_message", return_value="你好")
    @patch("services.match_action_service.profiles_coll")
    def test_agent_acceptance_runs_shared_chat_and_notification_effects(
        self, profiles, _generate, save, queue,
    ):
        profiles.find_one.side_effect = [{"user_id": "owner"}, {"user_id": "other"}]
        match_doc = {
            "_id": "proposal", "from_user": "owner", "to_user": "other",
            "status": "accepted", "reason": "有共同話題",
        }
        match_action_service.apply_transition_effects(
            match_doc, "accept", "pending", [], schedule_task=None,
        )
        save.assert_called_once()
        self.assertEqual(queue.call_count, 2)
        self.assertEqual({call.args[2] for call in queue.call_args_list}, {"match_connected"})

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

    @patch("services.ayue_agent.runtime.decide_active_proposal")
    def test_stale_agent_decision_answers_the_latest_terminal_state(self, decide):
        decide.return_value = {"status": "stale", "stale": True, "current_status": "accepted"}
        ctx = AgentTurnContext(user_id="owner", room_id="room", message="有興趣")
        turn = AgentTurnContextV2(
            user_id="owner", room_id="room", message="有興趣",
            active_proposal={"user_can_decide": True, "proposal_revision": 3},
        )
        ok, reply, code = _decide_active_proposal(
            ctx, turn, "run", 0, {"decision": "interested"},
        )
        self.assertTrue(ok)
        self.assertEqual(code, "stale_revision")
        self.assertIn("互相接受", reply)


if __name__ == "__main__":
    unittest.main()
