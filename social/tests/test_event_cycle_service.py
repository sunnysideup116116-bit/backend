import unittest
from unittest.mock import ANY, MagicMock, patch

from services import event_cycle_service as cycle


class _Rows(list):
    def limit(self, value):
        return _Rows(self[:value])


class EventCycleServiceTests(unittest.TestCase):
    @patch.object(cycle, "expire_event_proposals")
    @patch.object(cycle, "requests")
    @patch.object(cycle, "profiles_coll")
    @patch.object(cycle, "matches_coll")
    def test_reset_expires_live_invitations_and_preserves_history(
        self, matches, profiles, requests_module, expire,
    ):
        matches.find.return_value = _Rows([{"_id": "match-1"}])
        response = MagicMock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "success", "events_deleted": 9,
            "orphan_concepts_deleted": 3, "event_ids": ["event-1"],
        }
        requests_module.post.return_value = response
        expire.return_value = {"expired_count": 1, "stale_count": 0}
        profiles.update_many.return_value = MagicMock(modified_count=2)

        result = cycle.reset_event_inventory()

        self.assertEqual(result["graph"]["events_deleted"], 9)
        self.assertTrue(result["mongo"]["terminal_history_preserved"])
        expire.assert_called_once_with(
            now=ANY, event_ids=["event-1"], limit=1000,
            expire_all=True,
        )
        matches.delete_many.assert_not_called()

    @patch.object(cycle, "scan_event_opportunities")
    @patch.object(cycle, "wait_for_event_relevance")
    @patch.object(cycle, "discover_and_ingest_events")
    @patch.object(cycle, "reset_event_inventory")
    def test_weekly_cycle_orders_reset_discovery_and_scan(
        self, reset, discover, readiness, scan,
    ):
        calls = []
        reset.side_effect = lambda: calls.append("reset") or {"status": "success"}
        discover.side_effect = lambda **_kwargs: calls.append("discover") or {
            "status": "success", "active_category_counts": {"市集": 4},
        }
        readiness.side_effect = lambda: calls.append("relevance") or {
            "status": "ready", "ready": True,
        }
        scan.side_effect = lambda **_kwargs: calls.append("scan") or {
            "status": "success", "created_count": 1,
        }

        result = cycle.run_weekly_event_cycle(
            region="高雄", window_days=30, categories=["市集"],
        )

        self.assertEqual(calls, ["reset", "discover", "relevance", "scan"])
        self.assertEqual(result["job_kind"], "weekly_cycle")


if __name__ == "__main__":
    unittest.main()
