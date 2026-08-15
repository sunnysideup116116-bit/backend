import unittest
from pathlib import Path


SERVER_ROOT = Path(__file__).resolve().parents[2]
CALENDAR = SERVER_ROOT / "ayue_for_demo" / "social_demotest" / "routers" / "calendar.py"
CALENDAR_SERVICE = SERVER_ROOT / "ayue_for_demo" / "social_demotest" / "services" / "calendar_service.py"
DATES = SERVER_ROOT / "ayue_for_demo" / "social_demotest" / "routers" / "relationship_dates.py"
QUIZ = SERVER_ROOT / "ayue_for_demo" / "social_demotest" / "routers" / "relationship_quiz.py"
MODELS = SERVER_ROOT / "ayue_for_demo" / "social_demotest" / "models.py"
DOCUMENT = SERVER_ROOT / "docs" / "api" / "ayue-v3-calendar-relationship.md"


class CalendarRelationshipMobileContractTests(unittest.TestCase):
    def test_calendar_routes_and_revision_fields_remain_typed(self):
        source = CALENDAR.read_text(encoding="utf-8")
        for route in (
            '@router.get("/events")', '@router.post("/events")',
            '@router.patch("/events/{event_id}")',
            '@router.post("/events/{event_id}/cancel")',
            '@router.post("/events/{event_id}/reschedule")',
            '@router.post("/events/{event_id}/reschedule/cancel")',
            '@router.get("/settings")', '@router.patch("/settings")',
        ):
            self.assertIn(route, source)
        models = MODELS.read_text(encoding="utf-8")
        self.assertIn("class CalendarEventUpdateRequest(BaseModel):", models)
        self.assertIn("expected_revision: int | None = None", models)
        self.assertIn("class CalendarActionRequest(BaseModel):", models)

    def test_date_coordination_requires_identity_and_revision_for_writes(self):
        source = DATES.read_text(encoding="utf-8")
        for route in (
            "/relationship/date/invite/respond", "/relationship/date/state",
            "/relationship/date/update", "/relationship/date/confirm",
            "/relationship/date/cancel",
        ):
            self.assertIn(route, source)
        models = MODELS.read_text(encoding="utf-8")
        self.assertIn("coordination_id: str", models)
        self.assertIn("revision: int", models)

    def test_coordination_index_does_not_mix_sparse_and_partial_options(self):
        source = CALENDAR_SERVICE.read_text(encoding="utf-8")
        start = source.index('"coordination_id", unique=True')
        index_call = source[start:source.index(")", start) + 1]
        self.assertIn('partialFilterExpression={"source_type": "date"}', index_call)
        self.assertNotIn("sparse=True", index_call)

    def test_quiz_is_limited_to_accepted_matches_and_topic_route_is_absent(self):
        source = QUIZ.read_text(encoding="utf-8")
        self.assertIn("_accepted_match_or_403", source)
        for route in (
            "/relationship/quiz/start", "/relationship/quiz/answer",
            "/relationship/quiz/cancel",
        ):
            self.assertIn(route, source)
        self.assertNotIn("/relationship/topic", source)
        document = DOCUMENT.read_text(encoding="utf-8")
        self.assertIn("must be hidden", document)


if __name__ == "__main__":
    unittest.main()
