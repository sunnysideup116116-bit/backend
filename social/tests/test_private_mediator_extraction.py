import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class PrivateMediatorExtractionTests(unittest.TestCase):
    def test_private_runtime_modules_do_not_depend_on_chat_router_for_probe_writes(self):
        for relative_path in (
            "services/ayue_agent/private_calendar.py",
            "services/ayue_agent/private_v2.py",
        ):
            source = (ROOT / relative_path).read_text(encoding="utf-8")
            self.assertNotIn("from routers.chat import", source)

    def test_private_router_keeps_the_aggregate_router_out_of_its_import_graph(self):
        source = (ROOT / "routers/private_mediator.py").read_text(encoding="utf-8")
        self.assertNotIn("from routers.chat import", source)
        self.assertIn("from services.relationship_engagement_service import", source)
        self.assertIn("from services.profile_task_service import queue_profile_skills", source)


if __name__ == "__main__":
    unittest.main()
