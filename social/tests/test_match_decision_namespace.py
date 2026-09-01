import unittest
from unittest.mock import MagicMock, patch

from services import match_decision_service as decisions


MATCH_ID = "64f000000000000000000000"


class MatchDecisionNamespaceTests(unittest.TestCase):
    def _pending_event(self):
        return {
            "_id": MATCH_ID,
            "from_user": "owner",
            "to_user": "other",
            "status": "pending",
            "proposal_revision": 1,
            "proposal_namespace": "event_invitation",
            "relationship_establishing": True,
        }

    def test_wrong_namespace_fails_closed_before_transition(self):
        matches = MagicMock()
        matches.find_one.return_value = self._pending_event()
        with patch.object(decisions, "matches_coll", matches):
            result = decisions.apply_match_decision(
                user_id="other", match_id=MATCH_ID, action="accept",
                expected_status="pending", expected_revision=1,
                expected_namespace="relationship_match",
            )
        self.assertTrue(result["stale"])
        self.assertEqual(result["current_namespace"], "event_invitation")
        matches.find_one_and_update.assert_not_called()

    def test_repeated_pair_event_acceptance_does_not_become_second_relationship_anchor(self):
        matches = MagicMock()
        matches.find_one.return_value = self._pending_event()
        matches.count_documents.return_value = 1
        matches.find_one_and_update.return_value = {
            **self._pending_event(),
            "status": "accepted",
            "proposal_revision": 2,
            "relationship_establishing": False,
        }
        with patch.object(decisions, "matches_coll", matches):
            result = decisions.apply_match_decision(
                user_id="other", match_id=MATCH_ID, action="accept",
                expected_status="pending", expected_revision=1,
                expected_namespace="event_invitation",
            )
        update = matches.find_one_and_update.call_args.args[1]
        self.assertFalse(update["$set"]["relationship_establishing"])
        self.assertEqual(result["proposal_namespace"], "event_invitation")
        self.assertTrue(result["chat_reused"])

    def test_decline_records_pair_cooldown_timestamp(self):
        current = {
            **self._pending_event(), "status": "draft", "proposal_revision": 0,
        }
        matches = MagicMock()
        matches.find_one.return_value = current
        matches.find_one_and_update.return_value = {
            **current, "status": "declined", "proposal_revision": 1,
        }
        with patch.object(decisions, "matches_coll", matches):
            decisions.apply_match_decision(
                user_id="owner", match_id=MATCH_ID, action="decline",
                expected_status="draft", expected_revision=0,
                expected_namespace="event_invitation",
            )
        update = matches.find_one_and_update.call_args.args[1]
        self.assertGreater(update["$set"]["declined_at"], 0)

    def test_cancel_does_not_record_pair_decline_cooldown(self):
        current = self._pending_event()
        matches = MagicMock()
        matches.find_one.return_value = current
        matches.find_one_and_update.return_value = {
            **current, "status": "declined", "proposal_revision": 2,
        }
        with patch.object(decisions, "matches_coll", matches):
            decisions.apply_match_decision(
                user_id="owner", match_id=MATCH_ID, action="cancel",
                expected_status="pending", expected_revision=1,
                expected_namespace="event_invitation",
            )
        update = matches.find_one_and_update.call_args.args[1]
        self.assertNotIn("declined_at", update["$set"])
        self.assertGreater(update["$set"]["cancelled_at"], 0)


if __name__ == "__main__":
    unittest.main()
