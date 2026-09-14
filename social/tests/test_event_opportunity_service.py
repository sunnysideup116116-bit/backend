import unittest
import os
from unittest.mock import MagicMock, patch

from services import event_opportunity_service as service


def agent_payload(first_user="owner", second_user="candidate"):
    return {
        "status": "success",
        "first_user_id": first_user,
        "second_user_id": second_user,
        "first_hook": "我看到一場戶外市集，也想到有個人和你都喜歡散步。要不要先讓我幫你問問？",
        "second_hook": "我看到一場戶外市集，也想到有個人和你都喜歡逛市集。你有興趣認識嗎？",
        "match": {
            "user_id": "owner", "candidate_id": "candidate",
            "event_id": "event_1", "event_name": "港邊生活市集",
            "event_description": "週末港邊市集", "event_location": "高雄港",
            "event_region": "高雄", "event_category": "市集",
            "starts_at": 1_800_000_000, "ends_at": 1_800_003_600,
            "session_starts": [1_800_000_000, 1_800_086_400],
            "session_ends": [1_800_003_600, 1_800_090_000],
            "session_precisions": ["datetime", "datetime"],
            "session_count": 2,
            "source_url": "https://example.com/event",
            "target_links": ["戶外"], "candidate_links": ["市集"],
        },
    }


class EventOpportunityServiceTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        blocker = patch.object(
            service.risk_block_service,
            "excluded_user_ids",
            return_value=set(),
        )
        blocker.start()
        self.addCleanup(blocker.stop)

    def _profiles(self):
        profiles = MagicMock()
        profiles.find.return_value = [
            {"user_id": "owner", "current_context": "週末想出門", "big_five": {}},
            {"user_id": "candidate", "current_context": "想逛市集", "big_five": {}},
        ]
        return profiles

    @patch.object(service, "queue_mediator_event", return_value={"event_id": "queued"})
    @patch.object(service.requests, "post")
    def test_creates_draft_for_lower_evidence_party_and_queues_only_first(self, post, queue):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = agent_payload(first_user="candidate", second_user="owner")
        post.return_value = response
        matches = MagicMock()
        matches.count_documents.return_value = 0
        matches.find_one.return_value = None
        matches.insert_one.return_value.inserted_id = "match_1"

        with patch.object(service, "profiles_coll", self._profiles()), \
             patch.object(service, "matches_coll", matches):
            result = service.create_event_opportunity("owner")

        self.assertEqual(result["status"], "created")
        self.assertEqual(result["first_party"], "other")
        document = matches.insert_one.call_args.args[0]
        self.assertEqual(document["from_user"], "candidate")
        self.assertEqual(document["to_user"], "owner")
        self.assertEqual(document["status"], "draft")
        self.assertEqual(document["proposal_namespace"], "event_invitation")
        self.assertEqual(document["proposal_source"], "event_opportunity")
        self.assertTrue(document["participant_pair_key"])
        self.assertEqual(document["event_snapshot"]["session_count"], 2)
        self.assertEqual(document["event_snapshot"]["actionable_until"], 1_800_086_400)
        self.assertNotIn("seed_user", document["reason"])
        queue.assert_called_once()
        self.assertEqual(queue.call_args.args[0], "candidate")

    @patch.object(service.requests, "post")
    def test_existing_live_proposal_blocks_event_write(self, post):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = agent_payload()
        post.return_value = response
        matches = MagicMock()
        matches.count_documents.side_effect = [1, 0]

        with patch.object(service, "profiles_coll", self._profiles()), \
             patch.object(service, "matches_coll", matches):
            result = service.create_event_opportunity("owner")

        self.assertEqual(result["status"], "already_active")
        matches.insert_one.assert_not_called()

    def test_scan_respects_limit_and_skips_recent_or_active_users(self):
        profiles = MagicMock()
        profiles.find.return_value = [
            {"user_id": "owner"}, {"user_id": "candidate"},
            {"user_id": "recent"}, {"user_id": "active"},
        ]
        matches = MagicMock()
        matches.find.return_value = [
            {"live_participants": ["recent", "active"]},
        ]
        with patch.object(service, "profiles_coll", profiles), \
             patch.object(service, "matches_coll", matches), \
             patch.object(service, "create_event_opportunity", return_value={"status": "created"}) as create:
            result = service.scan_event_opportunities(max_proposals=1)

        self.assertEqual(result["created_count"], 1)
        self.assertEqual(result["skipped_user_count"], 2)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["excluded_user_ids"], {"recent", "active"})

    @patch.object(service.requests, "post")
    def test_recent_declined_pair_is_excluded_before_agent_call(self, post):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "no_match"}
        post.return_value = response
        matches = MagicMock()
        matches.count_documents.return_value = 0
        matches.find.return_value = [{"from_user": "owner", "to_user": "recent"}]
        with patch.dict(os.environ, {"EVENT_PAIR_DECLINE_COOLDOWN_DAYS": "7"}), \
             patch.object(service, "matches_coll", matches):
            result = service.create_event_opportunity("owner")
        self.assertEqual(result["status"], "no_match")
        self.assertIn("recent", post.call_args.kwargs["json"]["excluded_user_ids"])
        self.assertEqual(
            matches.find.call_args.args[0]["last_decision.action"], "decline",
        )
        self.assertIn("event_invitation", str(matches.find.call_args.args[0]))

    @patch.object(service.requests, "post")
    def test_selected_pair_is_rechecked_against_decline_cooldown(self, post):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = agent_payload()
        post.return_value = response
        matches = MagicMock()
        matches.count_documents.side_effect = [0, 0, 1]
        matches.find.return_value = []
        with patch.dict(os.environ, {"EVENT_PAIR_DECLINE_COOLDOWN_DAYS": "7"}), \
             patch.object(service, "profiles_coll", self._profiles()), \
             patch.object(service, "matches_coll", matches):
            result = service.create_event_opportunity("owner")
        self.assertEqual(result["status"], "pair_cooldown")
        matches.insert_one.assert_not_called()
        pair_query = matches.count_documents.call_args_list[-1].args[0]
        self.assertIn({"last_decision.action": "decline"}, pair_query["$and"])
        self.assertIn("event_invitation", str(pair_query))

    def test_pair_decline_cooldown_defaults_to_seven_days(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(service._pair_decline_cooldown_seconds(), 7 * 86400)

    @patch.object(service, "queue_mediator_event", return_value={"event_id": "queued"})
    @patch.object(service.requests, "post")
    def test_existing_accepted_pair_can_receive_event_without_reestablishing_relationship(
        self, post, _queue,
    ):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = agent_payload()
        post.return_value = response
        matches = MagicMock()
        matches.count_documents.return_value = 0
        matches.find.return_value = []
        matches.find_one.side_effect = [None, {"_id": "accepted-relation"}]
        matches.insert_one.return_value.inserted_id = "event-invitation"

        with patch.object(service, "profiles_coll", self._profiles()), \
             patch.object(service, "matches_coll", matches):
            result = service.create_event_opportunity("owner")

        self.assertEqual(result["status"], "created")
        document = matches.insert_one.call_args.args[0]
        self.assertFalse(document["relationship_establishing"])


if __name__ == "__main__":
    unittest.main()
