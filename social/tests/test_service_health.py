import unittest
from unittest.mock import patch

from fastapi import Response

import main
from routers.frontend import serve_frontend


class ServiceHealthTests(unittest.TestCase):
    def test_main_app_root_is_available_with_stubbed_startup_services(self):
        names = (
            "ensure_calendar_indexes",
            "ensure_calendar_draft_indexes",
            "ensure_calendar_reference_indexes",
            "ensure_relationship_reference_indexes",
            "ensure_place_reference_indexes",
            "ensure_ayue_agent_indexes",
            "ensure_map_cache_indexes",
            "ensure_conversation_compaction_indexes",
            "ensure_profile_skill_indexes",
            "ensure_context_graph_indexes",
            "ensure_match_indexes",
            "ensure_event_opportunity_indexes",
            "ensure_event_discovery_job_indexes",
            "ensure_event_discovery_cache_indexes",
            "ensure_interactive_priority_indexes",
            "start_match_search_worker",
            "start_proactive_care_scheduler",
            "start_context_graph_worker",
            "start_concept_embedding_worker",
            "start_event_lifecycle_worker",
            "start_event_discovery_worker",
            "stop_proactive_care_scheduler",
            "stop_match_search_worker",
            "stop_context_graph_worker",
            "stop_concept_embedding_worker",
            "stop_event_lifecycle_worker",
            "stop_event_discovery_worker",
        )
        patches = [patch.object(main, name) for name in names]
        started = []
        try:
            for item in patches:
                started.append(item.start())
            main.setup_calendar_indexes()
            response = Response()
            content = serve_frontend(response)
            self.assertIn("<!DOCTYPE html>", content)
            self.assertEqual(response.headers["Cache-Control"], "no-cache, no-store, must-revalidate")
        finally:
            main.stop_background_services()
            for item in reversed(patches[:len(started)]):
                item.stop()


if __name__ == "__main__":
    unittest.main()
