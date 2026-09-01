import unittest
from unittest.mock import MagicMock, patch
import requests

from services import event_lifecycle_service as lifecycle
from services import match_decision_service as decisions


class _Cursor:
    def __init__(self, rows):
        self.rows = rows

    def sort(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        return self.rows[:value]


class EventLifecycleServiceTests(unittest.TestCase):
    def test_event_proposal_expiry_uses_revision_guard_and_preserves_accepted(self):
        matches = MagicMock()
        matches.find.return_value = _Cursor([{
            "_id": "match_1", "status": "pending", "proposal_revision": 4,
            "event_snapshot": {"event_id": "event_1", "starts_at": 100},
        }])
        matches.find_one_and_update.return_value = {
            "_id": "match_1", "status": "expired", "proposal_revision": 5,
        }
        with patch.object(decisions, "matches_coll", matches):
            result = decisions.expire_event_proposals(now=200)

        self.assertEqual(result["expired_count"], 1)
        find_query = matches.find.call_args.args[0]
        status_cond = next(c for c in find_query["$and"] if "status" in c)
        self.assertEqual(set(status_cond["status"]["$in"]), {"draft", "pending"})
        update_query = matches.find_one_and_update.call_args.args[0]
        update = matches.find_one_and_update.call_args.args[1]
        self.assertEqual(update_query["proposal_revision"], 4)
        self.assertEqual(update["$set"]["status"], "expired")
        self.assertEqual(update["$inc"]["proposal_revision"], 1)
        self.assertIn("live_participants", update["$unset"])
        self.assertIn("event_snapshot.actionable_until", str(find_query))

    def test_multi_session_proposal_uses_last_session_for_expiry_query(self):
        matches = MagicMock()
        matches.find.return_value = _Cursor([])
        with patch.object(decisions, "matches_coll", matches):
            decisions.expire_event_proposals(now=200)

        query = matches.find.call_args.args[0]
        due_conditions = next(
            condition["$or"] for condition in query["$and"]
            if "$or" in condition and any("event_snapshot" in str(item) for item in condition["$or"])
        )
        self.assertEqual(
            due_conditions[0]["event_snapshot.actionable_until"]["$lte"], 200,
        )
        self.assertEqual(
            due_conditions[1]["event_snapshot.actionable_until"], {"$exists": False},
        )

    @patch.object(lifecycle, "expire_event_proposals")
    @patch.object(lifecycle.requests, "post")
    def test_lifecycle_passes_deleted_event_ids_to_proposal_expiry(self, post, expire):
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success", "deleted_count": 2,
            "event_ids": ["event_1", "event_2"],
        }
        post.return_value = response
        expire.return_value = {"expired_count": 1, "stale_count": 0}

        summary = lifecycle.run_event_lifecycle_once()

        self.assertEqual(summary["deleted_event_count"], 2)
        self.assertEqual(summary["expired_proposal_count"], 1)
        expire.assert_called_once()
        self.assertEqual(expire.call_args.kwargs["event_ids"], ["event_1", "event_2"])

    @patch.object(lifecycle, "expire_event_proposals")
    @patch.object(lifecycle.requests, "post")
    def test_reconcile_failure_still_expires_past_events_locally(self, post, expire):
        post.side_effect = requests.RequestException("network_down")
        expire.return_value = {"expired_count": 2, "stale_count": 0}

        summary = lifecycle.run_event_lifecycle_once()

        self.assertEqual(summary["deleted_event_count"], 0)
        self.assertEqual(summary["expired_proposal_count"], 2)
        self.assertEqual(summary["cleanup_error"], "RequestException")
        expire.assert_called_once()
        self.assertEqual(expire.call_args.kwargs["event_ids"], [])


if __name__ == "__main__":
    unittest.main()
