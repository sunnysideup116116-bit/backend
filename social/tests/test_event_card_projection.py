import unittest

from services.event_card_projection import public_event_card


class EventCardProjectionTests(unittest.TestCase):
    def test_event_opportunity_exposes_only_public_card_fields(self):
        card = public_event_card({
            "proposal_source": "event_opportunity",
            "event_snapshot": {
                "event_id": "neo4j-internal-event-id",
                "title": "下一站，少女心市",
                "summary": "internal ranking context",
                "venue": "美麗島站",
                "region": "高雄",
                "category": "市集",
                "starts_at": 1786204800,
                "ends_at": 1786291199,
                "time_precision": "date",
                "source_url": "https://example.com/events/market",
            },
        })

        self.assertEqual(card, {
            "title": "下一站，少女心市",
            "venue": "美麗島站",
            "region": "高雄",
            "category": "市集",
            "starts_at": 1786204800.0,
            "ends_at": 1786291199.0,
            "time_precision": "date",
            "source_url": "https://example.com/events/market",
        })
        self.assertNotIn("event_id", card)
        self.assertNotIn("summary", card)

    def test_private_or_local_source_url_is_removed(self):
        card = public_event_card({
            "proposal_source": "event_opportunity",
            "event_snapshot": {
                "title": "Local test",
                "source_url": "http://127.0.0.1/private",
            },
        })

        self.assertEqual(card["source_url"], "")

    def test_multiple_sessions_are_exposed_as_bounded_public_dates(self):
        card = public_event_card({
            "proposal_source": "event_opportunity",
            "event_snapshot": {
                "title": "Two-night concert",
                "starts_at": 100,
                "ends_at": 200,
                "time_precision": "datetime",
                "session_starts": [100, 200],
                "session_ends": [150, 250],
                "session_precisions": ["datetime", "datetime"],
            },
        })

        self.assertEqual(card["sessions"], [
            {"starts_at": 100.0, "ends_at": 150.0, "time_precision": "datetime"},
            {"starts_at": 200.0, "ends_at": 250.0, "time_precision": "datetime"},
        ])

    def test_non_event_match_has_no_event_card(self):
        self.assertIsNone(public_event_card({
            "proposal_source": "ordinary_match",
            "event_snapshot": {"title": "Should not leak"},
        }))


if __name__ == "__main__":
    unittest.main()
