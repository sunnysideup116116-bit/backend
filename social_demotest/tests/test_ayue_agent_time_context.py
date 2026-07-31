import unittest
from datetime import datetime, timezone

from services.ayue_agent.time_context import build_turn_clock


class TurnClockTests(unittest.TestCase):
    def test_taipei_clock_and_relative_dates_are_deterministic(self):
        clock = build_turn_clock(
            "我後天有約嗎？", datetime(2026, 7, 30, 6, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(clock.local_date, "2026-07-30")
        self.assertEqual(clock.local_time, "14:30")
        self.assertEqual(clock.weekday_zh_tw, "星期四")
        self.assertEqual(clock.temporal_references, {"後天": "2026-08-01"})

    def test_relative_date_crosses_year_boundary(self):
        clock = build_turn_clock(
            "明天要出發", datetime(2026, 12, 31, 15, 30, tzinfo=timezone.utc)
        )
        self.assertEqual(clock.local_date, "2026-12-31")
        self.assertEqual(clock.temporal_references["明天"], "2027-01-01")


if __name__ == "__main__":
    unittest.main()
