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

    def test_relative_weekday_variants_resolve_exact_weekday(self):
        clock = build_turn_clock(
            "下週一 下週三 下禮拜三 下星期日 這週三 本週四 星期天",
            datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(clock.temporal_references["下週一"], "2026-08-10")
        self.assertEqual(clock.temporal_references["下週三"], "2026-08-12")
        self.assertEqual(clock.temporal_references["下禮拜三"], "2026-08-12")
        self.assertEqual(clock.temporal_references["下星期日"], "2026-08-16")
        self.assertEqual(clock.temporal_references["這週三"], "2026-08-05")
        self.assertEqual(clock.temporal_references["本週四"], "2026-08-06")
        self.assertEqual(clock.temporal_references["星期天"], "2026-08-09")

    def test_two_weeks_later_weekday_variants_do_not_match_next_week_prefix(self):
        clock = build_turn_clock(
            "下下週一 下下週四 下下星期日 下下禮拜二 下下周四",
            datetime(2026, 8, 8, 12, tzinfo=timezone.utc),
        )
        self.assertEqual(clock.temporal_references["下下週一"], "2026-08-17")
        self.assertEqual(clock.temporal_references["下下週四"], "2026-08-20")
        self.assertEqual(clock.temporal_references["下下星期日"], "2026-08-23")
        self.assertEqual(clock.temporal_references["下下禮拜二"], "2026-08-18")
        self.assertEqual(clock.temporal_references["下下周四"], "2026-08-20")
        self.assertNotIn("下週", clock.temporal_references)
        self.assertNotIn("週四", clock.temporal_references)


if __name__ == "__main__":
    unittest.main()
