import unittest
from unittest.mock import Mock, patch

from services.memory_service import MemoryWriteError, apply_profile_memory_proposals


class MemoryServiceTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()