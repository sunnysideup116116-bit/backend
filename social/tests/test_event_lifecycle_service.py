import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import requests

from matchmaker_agent import event_temporal as temporal
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


TAIPEI = timezone(timedelta(hours=8))


def at(*parts):
    return datetime(*parts, tzinfo=TAIPEI).timestamp()


def snapshot(event_id, starts_at, ends_at, *, sessions=None, legacy_until=None):
    session_starts = [start for start, _ in (sessions or [])] or [starts_at]
    session_ends = [end for _, end in (sessions or [])] or [ends_at]
    return {
        "event_id": event_id,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "session_starts": session_starts,
        "session_ends": session_ends,
        "actionable_until": max(session_starts) if legacy_until is None else legacy_until,
    }


class EventProposalTemporalLifecycleTests(unittest.TestCase):
    def _expire(self, event_snapshot, *, now, **kwargs):
        row = {
            "_id": "match_1", "status": "draft", "proposal_revision": 0,
            "event_snapshot": event_snapshot,
        }
        matches = MagicMock()
        matches.find.return_value = _Cursor([row])
        matches.find_one_and_update.return_value = {
            "_id": "match_1", "status": "expired", "proposal_revision": 1,
        }
        with patch.object(decisions, "matches_coll", matches):
            result = decisions.expire_event_proposals(now=now, **kwargs)
        return matches, result

    def test_future_event_invitation_stays_live(self):
        now = at(2026, 10, 12)
        matches, result = self._expire(snapshot("event_1", now + 86400, now + 7 * 86400), now=now)
        self.assertEqual(result["expired_count"], 0)
        matches.find_one_and_update.assert_not_called()

    def test_ongoing_event_invitation_is_not_expired_for_its_start(self):
        now = at(2026, 10, 12)
        matches, result = self._expire(
            snapshot("event_1", now - 86400, now + 30 * 86400, legacy_until=now - 86400),
            now=now,
        )
        self.assertEqual(result["expired_count"], 0)
        matches.find_one_and_update.assert_not_called()

    def test_run33_shape_invitation_stays_live(self):
        now = at(2026, 10, 12, 8)
        matches, result = self._expire(
            snapshot("web_run33", at(2026, 9, 15), at(2026, 12, 17, 23, 59, 59),
                     legacy_until=at(2026, 9, 15)),
            now=now,
        )
        self.assertEqual(result["expired_count"], 0)
        matches.find_one_and_update.assert_not_called()

    def test_ongoing_multi_session_invitation_is_live_after_earlier_sessions(self):
        now = at(2026, 10, 18, 12)
        matches, result = self._expire(
            snapshot(
                "event_1", at(2026, 10, 3), at(2026, 10, 19, 23, 59, 59),
                sessions=[
                    (at(2026, 10, 3), at(2026, 10, 5, 23, 59, 59)),
                    (at(2026, 10, 10), at(2026, 10, 12, 23, 59, 59)),
                    (at(2026, 10, 17), at(2026, 10, 19, 23, 59, 59)),
                ],
                legacy_until=at(2026, 10, 17),
            ),
            now=now,
        )
        self.assertEqual(result["expired_count"], 0)
        matches.find_one_and_update.assert_not_called()

    def test_ended_event_expires_with_event_ended_reason(self):
        now = at(2026, 10, 12)
        matches, result = self._expire(
            snapshot("event_1", now - 10 * 86400, now - 86400, legacy_until=now - 10 * 86400),
            now=now,
        )
        self.assertEqual(result["expired_count"], 1)
        update = matches.find_one_and_update.call_args.args[1]
        self.assertEqual(update["$set"]["status"], "expired")
        self.assertEqual(update["$set"]["expired_reason"], "event_ended")

    def test_end_boundary_expires(self):
        now = at(2026, 10, 12)
        matches, result = self._expire(snapshot("event_1", now - 3600, now), now=now)
        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(
            matches.find_one_and_update.call_args.args[1]["$set"]["expired_reason"],
            "event_ended",
        )

    def test_started_event_without_reliable_end_keeps_fail_closed_reason(self):
        now = at(2026, 10, 12)
        matches, result = self._expire(
            {"event_id": "legacy", "starts_at": now - 7200, "actionable_until": now - 7200},
            now=now,
        )
        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(
            matches.find_one_and_update.call_args.args[1]["$set"]["expired_reason"],
            "event_started",
        )

    def test_inventory_refresh_still_expires_ongoing_event(self):
        now = at(2026, 10, 12)
        matches, result = self._expire(
            snapshot("event_1", now - 86400, now + 30 * 86400),
            now=now, expire_all=True,
        )
        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(
            matches.find_one_and_update.call_args.args[1]["$set"]["expired_reason"],
            "inventory_refreshed",
        )

    def test_removed_graph_event_still_expires_ongoing_event(self):
        now = at(2026, 10, 12)
        matches, result = self._expire(
            snapshot("event_1", now - 86400, now + 30 * 86400),
            now=now, event_ids=["event_1"],
        )
        self.assertEqual(result["expired_count"], 1)
        self.assertEqual(
            matches.find_one_and_update.call_args.args[1]["$set"]["expired_reason"],
            "event_removed",
        )

    def test_general_card_ttl_stays_namespaced_and_never_touches_event_invitations(self):
        now = at(2026, 10, 12)
        matches, result = self._expire(
            snapshot("event_1", now - 90 * 86400, now + 30 * 86400, legacy_until=now - 90 * 86400),
            now=now,
        )
        self.assertEqual(result["expired_count"], 0)
        general = MagicMock()
        general.find.return_value = _Cursor([])
        with patch.object(decisions, "matches_coll", general):
            general_result = decisions.expire_unresolved_proposals(now=now)
        self.assertEqual(general_result["expired_count"], 0)
        general_query = general.find.call_args.args[0]
        self.assertIn("relationship_match", str(general_query))
        self.assertNotIn("event_invitation", str(general_query))


class EventTemporalConsistencyContractTests(unittest.TestCase):
    """Proposal temporal eligibility and invitation lifecycle must agree."""

    def test_rechecked_event_at_t_is_never_immediately_expired_at_t(self):
        now = at(2026, 10, 12, 8)
        event_snapshot = snapshot(
            "web_run33", at(2026, 9, 15), at(2026, 12, 17, 23, 59, 59),
            legacy_until=at(2026, 9, 15),
        )
        self.assertTrue(temporal.is_temporally_eligible(event_snapshot, now=now))
        row = {
            "_id": "match_1", "status": "draft", "proposal_revision": 0,
            "event_snapshot": event_snapshot,
        }
        matches = MagicMock()
        matches.find.return_value = _Cursor([row])
        with patch.object(decisions, "matches_coll", matches):
            result = decisions.expire_event_proposals(now=now)
        self.assertEqual(result["expired_count"], 0)
        matches.find_one_and_update.assert_not_called()

    def test_ended_event_is_ineligible_and_expired_at_the_same_time(self):
        now = at(2026, 10, 12, 8)
        event_snapshot = snapshot(
            "ended_event", at(2026, 9, 1), at(2026, 10, 11, 23, 59, 59),
            legacy_until=at(2026, 9, 1),
        )
        self.assertFalse(temporal.is_temporally_eligible(event_snapshot, now=now))
        row = {
            "_id": "match_1", "status": "draft", "proposal_revision": 0,
            "event_snapshot": event_snapshot,
        }
        matches = MagicMock()
        matches.find.return_value = _Cursor([row])
        matches.find_one_and_update.return_value = {"_id": "match_1", "status": "expired"}
        with patch.object(decisions, "matches_coll", matches):
            result = decisions.expire_event_proposals(now=now)
        self.assertEqual(result["expired_count"], 1)


if __name__ == "__main__":
    unittest.main()
