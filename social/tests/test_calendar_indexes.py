import unittest
from unittest.mock import patch

from services import calendar_service


class CalendarIndexTests(unittest.TestCase):
    @patch.object(calendar_service, "calendar_events_coll")
    def test_coordination_index_uses_valid_partial_unique_options(self, collection):
        calendar_service.ensure_calendar_indexes()

        coordination_call = collection.create_index.call_args_list[1]
        self.assertEqual(coordination_call.args[0], "coordination_id")
        self.assertTrue(coordination_call.kwargs["unique"])
        self.assertNotIn("sparse", coordination_call.kwargs)
        self.assertEqual(
            coordination_call.kwargs["partialFilterExpression"],
            {
                "source_type": "date",
                "coordination_id": {"$type": "string"},
            },
        )


if __name__ == "__main__":
    unittest.main()
