import os
import unittest
from unittest.mock import patch
from pydantic import ValidationError


os.environ.setdefault("NEO4J_URI", "bolt://stub.invalid:7687")
os.environ.setdefault("NEO4J_USERNAME", "stub")
os.environ.setdefault("NEO4J_PASSWORD", "stub")
os.environ.setdefault("LLM_API_KEY", "stub")
os.environ.setdefault("LLM_BASE_URL", "http://127.0.0.1:9")
os.environ.setdefault("LLM_MODEL_ID", "stub")

import agent_api


class EventIngestDeadlineTests(unittest.TestCase):
    def test_nonfinite_write_deadline_is_rejected(self):
        with self.assertRaises(ValidationError):
            agent_api.EventIngestRequest(write_deadline=float("nan"))

    def test_internal_endpoint_forwards_bounded_write_deadline(self):
        request = agent_api.EventIngestRequest(
            region="高雄", window_days=30, max_events=2,
            write_deadline=5000.0,
            search_results=[{
                "title": "測試活動", "snippet": "有日期的公開活動",
                "source_url": "https://example.com/event",
                "discovery_category": "運動",
            }],
        )
        with patch.object(agent_api.time, "time", return_value=100.0), \
             patch.object(agent_api.agent, "extract_and_ingest_search_results",
                          return_value={"events": [], "ingested_count": 0}) as ingest:
            result = agent_api.ingest_event_search_results(request)
        self.assertEqual(result["status"], "success")
        self.assertEqual(ingest.call_args.kwargs["write_deadline"], 1000.0)

    def test_old_internal_caller_without_deadline_remains_compatible(self):
        request = agent_api.EventIngestRequest(
            search_results=[{
                "title": "測試活動", "snippet": "有日期的公開活動",
                "source_url": "https://example.com/event",
                "discovery_category": "運動",
            }],
        )
        with patch.object(agent_api.agent, "extract_and_ingest_search_results",
                          return_value={"events": [], "ingested_count": 0}) as ingest:
            agent_api.ingest_event_search_results(request)
        self.assertIsNone(ingest.call_args.kwargs["write_deadline"])


if __name__ == "__main__":
    unittest.main()
