"""Deterministic contract for the canonical Event temporal authority."""

import unittest
from datetime import datetime, timedelta, timezone

from matchmaker_agent import event_temporal as temporal


TAIPEI = timezone(timedelta(hours=8))


def at(*parts):
    return datetime(*parts, tzinfo=TAIPEI).timestamp()


def run33_snapshot():
    """The production Run #33 shape: 2026-09-15 -> 2026-12-17, seen on 2026-10-12."""
    return {
        "starts_at": at(2026, 9, 15),
        "ends_at": at(2026, 12, 17, 23, 59, 59),
        "session_starts": [at(2026, 9, 15)],
        "session_ends": [at(2026, 12, 17, 23, 59, 59)],
    }


class EventTemporalStateTests(unittest.TestCase):
    def test_future_event_is_upcoming_and_eligible(self):
        now = at(2026, 10, 12)
        event = {"starts_at": now + 86400, "ends_at": now + 7 * 86400}
        self.assertEqual(temporal.event_temporal_state(event, now=now), temporal.UPCOMING)
        self.assertTrue(temporal.is_temporally_eligible(event, now=now))

    def test_start_boundary_is_ongoing(self):
        now = at(2026, 10, 12)
        event = {"starts_at": now, "ends_at": now + 3600}
        self.assertEqual(temporal.event_temporal_state(event, now=now), temporal.ONGOING)

    def test_ongoing_multi_day_event_is_eligible_until_its_end(self):
        now = at(2026, 10, 18, 12)
        event = {
            "starts_at": at(2026, 10, 3),
            "ends_at": at(2026, 10, 19, 23, 59, 59),
            "session_starts": [at(2026, 10, 3), at(2026, 10, 10), at(2026, 10, 17)],
            "session_ends": [
                at(2026, 10, 5, 23, 59, 59),
                at(2026, 10, 12, 23, 59, 59),
                at(2026, 10, 19, 23, 59, 59),
            ],
        }
        self.assertEqual(temporal.event_temporal_state(event, now=now), temporal.ONGOING)
        self.assertTrue(temporal.is_temporally_eligible(event, now=now))
        self.assertEqual(temporal.event_live_until(event), at(2026, 10, 19, 23, 59, 59))

    def test_run33_shape_is_ongoing_on_2026_10_12(self):
        now = at(2026, 10, 12, 8)
        self.assertEqual(
            temporal.event_temporal_state(run33_snapshot(), now=now), temporal.ONGOING,
        )
        self.assertTrue(temporal.is_temporally_eligible(run33_snapshot(), now=now))

    def test_ended_event_is_not_eligible(self):
        now = at(2026, 10, 12)
        event = {"starts_at": now - 10 * 86400, "ends_at": now - 86400}
        self.assertEqual(temporal.event_temporal_state(event, now=now), temporal.ENDED)
        self.assertFalse(temporal.is_temporally_eligible(event, now=now))

    def test_end_boundary_is_ended(self):
        now = at(2026, 10, 12)
        event = {"starts_at": now - 3600, "ends_at": now}
        self.assertEqual(temporal.event_temporal_state(event, now=now), temporal.ENDED)
        self.assertFalse(temporal.is_temporally_eligible(event, now=now))

    def test_started_event_with_unreliable_end_is_unknown_and_fail_closed(self):
        now = at(2026, 10, 12)
        for broken_end in ("not-a-time", float("nan"), float("inf"), 0, -1, now - 7200):
            event = {"starts_at": now - 3600, "ends_at": broken_end}
            self.assertEqual(
                temporal.event_temporal_state(event, now=now), temporal.UNKNOWN,
                broken_end,
            )
            self.assertFalse(temporal.is_temporally_eligible(event, now=now), broken_end)

    def test_future_event_without_end_stays_upcoming(self):
        now = at(2026, 10, 12)
        event = {"starts_at": now + 86400}
        self.assertEqual(temporal.event_temporal_state(event, now=now), temporal.UPCOMING)
        self.assertTrue(temporal.is_temporally_eligible(event, now=now))

    def test_session_without_any_end_is_unknown_and_keeps_the_conservative_deadline(self):
        now = at(2026, 10, 12)
        event = {"session_starts": [now - 7200], "session_ends": []}
        self.assertIsNone(temporal.event_end(event))
        self.assertEqual(temporal.event_temporal_state(event, now=now), temporal.UNKNOWN)
        self.assertEqual(temporal.event_live_until(event), now - 7200)

    def test_later_session_end_extends_the_reliable_end(self):
        now = at(2026, 10, 12)
        event = {
            "starts_at": at(2026, 9, 15),
            "ends_at": at(2026, 9, 16),
            "session_starts": [at(2026, 9, 15), at(2026, 10, 17)],
            "session_ends": [at(2026, 9, 16), at(2026, 10, 19)],
        }
        self.assertEqual(temporal.event_end(event), at(2026, 10, 19))
        self.assertEqual(temporal.event_temporal_state(event, now=now), temporal.ONGOING)

    def test_trustworthy_end_wins_over_session_ends(self):
        now = at(2026, 10, 12)
        event = run33_snapshot()
        event["session_ends"] = [at(2026, 9, 16)]
        self.assertEqual(
            temporal.event_end(event), at(2026, 12, 17, 23, 59, 59),
        )
        self.assertEqual(temporal.event_temporal_state(event, now=now), temporal.ONGOING)

    def test_live_until_falls_back_to_conservative_start_deadline(self):
        now = at(2026, 10, 12)
        event = {
            "starts_at": now + 3600,
            "session_starts": [now + 3600, now + 86400],
        }
        self.assertEqual(temporal.event_live_until(event), now + 86400)
        self.assertIsNone(temporal.event_end(event))

    def test_live_until_is_zero_without_any_reliable_time(self):
        self.assertEqual(temporal.event_live_until({}), 0.0)
        self.assertEqual(temporal.event_live_until(None), 0.0)
        self.assertEqual(temporal.event_live_until({"starts_at": "junk"}), 0.0)


if __name__ == "__main__":
    unittest.main()
