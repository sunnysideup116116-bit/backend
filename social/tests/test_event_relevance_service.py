import unittest
from unittest.mock import patch

from services import event_relevance_service as relevance


class EventRelevanceServiceTests(unittest.TestCase):
    @patch.object(relevance, "refresh_semantic_event_links")
    def test_new_events_refresh_reusable_vectors_without_embedding_in_request(self, refresh):
        refresh.return_value = {
            "status": "success", "relevance_count": 2, "link_count": 2,
            "pending_count": 1,
        }
        result = relevance.project_event_relevance([{"event_id": "event_1"}])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["event_count"], 1)
        refresh.assert_called_once_with()

    @patch.object(relevance, "process_pending_concept_embeddings")
    def test_rebuild_processes_at_most_twenty_concepts(self, process):
        process.return_value = {
            "status": "success", "embedded_count": 20, "pending_count": 4,
            "relevance_count": 3, "avoidance_count": 1, "link_count": 4,
        }
        result = relevance.rebuild_all_event_relevance(limit=100)
        process.assert_called_once_with(batch_size=20)
        self.assertEqual(result["embedded_count"], 20)

    @patch.object(relevance, "process_pending_concept_embeddings")
    def test_quota_limit_is_deferred_instead_of_failing_rebuild(self, process):
        process.return_value = {"status": "rate_limited", "retry_after": 46.0}
        result = relevance.rebuild_all_event_relevance()
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(result["retry_after"], 46.0)

    @patch.object(relevance, "refresh_semantic_event_links")
    @patch.object(relevance, "process_pending_concept_embeddings")
    def test_idle_rebuild_refreshes_existing_links(self, process, refresh):
        process.return_value = {"status": "idle", "pending_count": 0}
        refresh.return_value = {"status": "success", "link_count": 7}
        result = relevance.rebuild_all_event_relevance()
        refresh.assert_called_once_with()
        self.assertEqual(result["link_count"], 7)


if __name__ == "__main__":
    unittest.main()
