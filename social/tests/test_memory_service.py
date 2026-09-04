import unittest
from unittest.mock import Mock, patch

from services.memory_service import (
    MemoryWriteError, _sync_memory_projection, apply_memory_action,
    apply_profile_memory_proposals, get_graph_memory_snapshot,
)


class MemoryServiceTests(unittest.TestCase):
    def test_status_aware_graph_snapshot_distinguishes_empty_from_unavailable(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "success", "memories": []}
        with patch("services.memory_service.requests.get", return_value=response):
            self.assertEqual(get_graph_memory_snapshot("owner"), {
                "available": True, "items": [], "error_code": None,
            })
        response.json.return_value = {
            "status": "error", "error_code": "graph_read_failed", "memories": [],
        }
        with patch("services.memory_service.requests.get", return_value=response):
            self.assertFalse(get_graph_memory_snapshot("owner")["available"])

    def test_sync_clears_stale_preview_when_graph_is_available_and_empty(self):
        with patch("services.memory_service.get_graph_memory_snapshot", return_value={
            "available": True, "items": [], "error_code": None,
        }), patch("services.memory_service.profiles_coll.update_one") as update:
            result = _sync_memory_projection("owner", [{"key": "stale", "label": "舊記憶"}])
        self.assertEqual(result, [])
        self.assertEqual(update.call_args.args[1]["$set"]["profile_memory_preview"], [])
    def test_memory_action_error_payload_is_not_reported_as_success(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "not_found"}
        with patch("services.memory_service.requests.post", return_value=response), \
             patch("services.memory_service._sync_memory_projection") as sync:
            with self.assertRaises(MemoryWriteError) as raised:
                apply_memory_action("owner", "missing", "disable")
        self.assertEqual(raised.exception.error_code, "not_found")
        sync.assert_not_called()
    def test_agent_error_response_is_raised_and_queued(self):
        response = Mock(status_code=200)
        response.raise_for_status.return_value = None
        response.json.return_value = {"status": "error", "error_code": "Neo4jConnectionError", "memories": []}
        proposals = [{"key": "smoking_partner", "label": "會抽菸的人", "stance": "avoid", "category": "lifestyle", "confidence": 0.9}]
        with patch("services.memory_service.requests.post", return_value=response), \
             patch("services.memory_service._queue_memory_retry") as queue_retry:
            with self.assertRaises(MemoryWriteError) as raised:
                apply_profile_memory_proposals("demo_user", proposals, "global", "message-1")
        self.assertEqual(raised.exception.error_code, "Neo4jConnectionError")
        queue_retry.assert_called_once()

    def test_missing_apply_endpoint_fails_closed_without_direct_graph_fallback(self):
        response = Mock(status_code=404)
        proposals = [{"key": "coffee", "label": "咖啡", "stance": "like", "category": "lifestyle", "confidence": 0.95}]
        with patch("services.memory_service.requests.post", return_value=response), \
             patch("services.memory_service._queue_memory_retry") as queue_retry:
            with self.assertRaises(MemoryWriteError) as raised:
                apply_profile_memory_proposals("demo_user", proposals, "global", "message-404")
        self.assertEqual(raised.exception.error_code, "memory_apply_endpoint_not_found")
        queue_retry.assert_called_once()


if __name__ == "__main__":
    unittest.main()
