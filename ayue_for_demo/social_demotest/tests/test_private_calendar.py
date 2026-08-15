import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from services.ayue_agent.private_calendar import calendar_range_for_message, partner_busy


class PrivateCalendarTests(unittest.TestCase):
    def test_calendar_range_is_bounded_and_taiwan_local(self):
        start, end, truncated = calendar_range_for_message(
            "下週哪天有空", datetime(2026, 8, 9, 2, 0, tzinfo=timezone.utc),
        )
        self.assertEqual((end - start).days, 7)
        self.assertFalse(truncated)
        self.assertEqual(start.tzinfo, timezone.utc)

    @patch("services.ayue_agent.private_calendar.calendar_access_enabled", return_value=True)
    @patch("services.ayue_agent.private_calendar.get_calendar_context")
    def test_partner_busy_returns_only_busy_free_projection(self, get_context, _access):
        get_context.return_value = {
            "partner_busy": [
                {"start_at": "2026-08-10T01:00:00Z", "end_at": "2026-08-10T02:00:00Z", "event_id": "secret"},
            ],
        }
        result = partner_busy("owner", "other", datetime.now(timezone.utc), datetime.now(timezone.utc))
        self.assertEqual(result, (True, [{
            "start_at": "2026-08-10T01:00:00Z",
            "end_at": "2026-08-10T02:00:00Z",
            "busy": "true",
        }]))


if __name__ == "__main__":
    unittest.main()
