"""
Тесты расписания: разбор интервала и расчёт паузы между циклами.
Запуск: python -m pytest tests/ -q
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from database import (  # noqa: E402
    SETTING_INTERVAL_HOURS,
    SETTING_INTERVAL_SECONDS,
    Database,
)
from scheduling import (  # noqa: E402
    REASON_CATCHUP,
    REASON_ERROR,
    REASON_FLOOD,
    REASON_IDLE,
    REASON_INTERVAL,
    fair_window_limit,
    humanize_duration,
    parse_duration,
    plan_next_delay,
    slice_timeout,
)


class ParseDurationTests(unittest.TestCase):
    def test_bare_number_is_minutes(self) -> None:
        self.assertEqual(parse_duration("10"), 600.0)

    def test_units(self) -> None:
        self.assertEqual(parse_duration("30s"), 30.0)
        self.assertEqual(parse_duration("30 сек"), 30.0)
        self.assertEqual(parse_duration("15мин"), 900.0)
        self.assertEqual(parse_duration("2ч"), 7200.0)
        self.assertEqual(parse_duration("2 hours"), 7200.0)
        self.assertEqual(parse_duration("1д"), 86400.0)

    def test_fraction_and_combo(self) -> None:
        self.assertEqual(parse_duration("1.5h"), 5400.0)
        self.assertEqual(parse_duration("1,5ч"), 5400.0)
        self.assertEqual(parse_duration("1ч 30мин"), 5400.0)
        self.assertEqual(parse_duration("1д 6ч"), 108000.0)

    def test_rejects_garbage(self) -> None:
        for raw in ("", "потом", "0", "-5м", "1 век"):
            with self.assertRaises(ValueError):
                parse_duration(raw)

    def test_rejects_extremes(self) -> None:
        with self.assertRaises(ValueError):
            parse_duration("1s")
        with self.assertRaises(ValueError):
            parse_duration("60д")


class HumanizeTests(unittest.TestCase):
    def test_humanize(self) -> None:
        self.assertEqual(humanize_duration(45), "45 сек")
        self.assertEqual(humanize_duration(60), "1 мин")
        self.assertEqual(humanize_duration(900), "15 мин")
        self.assertEqual(humanize_duration(3600), "1 ч")
        self.assertEqual(humanize_duration(5400), "1 ч 30 мин")
        self.assertEqual(humanize_duration(86400), "1 д")
        self.assertEqual(humanize_duration(108000), "1 д 6 ч")


class PlanNextDelayTests(unittest.TestCase):
    def test_interval_is_respected_even_when_large(self) -> None:
        """Регрессия: раньше интервал > 1 часа подменялся на 10 секунд."""
        plan = plan_next_delay(published=3, interval_seconds=7200)
        self.assertEqual(plan.delay, 7200.0)
        self.assertEqual(plan.reason, REASON_INTERVAL)

        plan = plan_next_delay(published=1, interval_seconds=9 * 3600)
        self.assertEqual(plan.delay, 9 * 3600.0)

    def test_small_interval_is_respected(self) -> None:
        plan = plan_next_delay(published=1, interval_seconds=30)
        self.assertEqual(plan.delay, 30.0)
        self.assertEqual(plan.reason, REASON_INTERVAL)

    def test_catchup_only_while_backlog(self) -> None:
        plan = plan_next_delay(
            published=2,
            interval_seconds=3600,
            catchup=True,
            catchup_seconds=30,
            backlog=120,
        )
        self.assertEqual(plan.delay, 30.0)
        self.assertEqual(plan.reason, REASON_CATCHUP)

        plan = plan_next_delay(
            published=2,
            interval_seconds=3600,
            catchup=True,
            catchup_seconds=30,
            backlog=0,
        )
        self.assertEqual(plan.delay, 3600.0)
        self.assertEqual(plan.reason, REASON_INTERVAL)

    def test_catchup_never_slower_than_interval(self) -> None:
        plan = plan_next_delay(
            published=1,
            interval_seconds=60,
            catchup=True,
            catchup_seconds=600,
            backlog=10,
        )
        self.assertEqual(plan.delay, 60.0)

    def test_idle_backoff_capped_by_interval(self) -> None:
        first = plan_next_delay(published=0, interval_seconds=3600, idle_streak=1)
        third = plan_next_delay(published=0, interval_seconds=3600, idle_streak=5)
        self.assertEqual(first.reason, REASON_IDLE)
        self.assertLess(first.delay, third.delay)
        self.assertEqual(third.delay, 60.0)

        short = plan_next_delay(published=0, interval_seconds=20, idle_streak=5)
        self.assertEqual(short.delay, 20.0)

    def test_error_backoff(self) -> None:
        plan = plan_next_delay(published=0, interval_seconds=60, error_streak=3)
        self.assertEqual(plan.reason, REASON_ERROR)
        self.assertEqual(plan.delay, 90.0)
        capped = plan_next_delay(published=0, interval_seconds=60, error_streak=100)
        self.assertEqual(capped.delay, 300.0)

    def test_flood_wins(self) -> None:
        plan = plan_next_delay(
            published=0, interval_seconds=60, error_streak=5, flood_seconds=600
        )
        self.assertEqual(plan.reason, REASON_FLOOD)
        self.assertEqual(plan.delay, 605.0)


class IntervalStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "s.db"

    def _db(self) -> Database:
        return Database(self.path)

    def test_defaults_in_seconds(self) -> None:
        db = self._db()
        db.ensure_defaults(
            caption="x",
            posts_per_cycle=2,
            source_channel="@a",
            target_channel="@b",
            interval_seconds=1800,
        )
        self.assertEqual(db.get_settings().interval_seconds, 1800.0)
        self.assertAlmostEqual(db.get_settings().interval_hours, 0.5)

    def test_legacy_hours_migrated(self) -> None:
        db = self._db()
        db.ensure_defaults(
            caption="x",
            posts_per_cycle=2,
            source_channel="@a",
            target_channel="@b",
            interval_seconds=60,
        )
        # Так выглядела старая база: интервал в часах
        db.set(SETTING_INTERVAL_HOURS, "2.5")
        db.migrate_interval()
        self.assertEqual(db.get_settings().interval_seconds, 9000.0)
        self.assertIsNone(db.get(SETTING_INTERVAL_HOURS))
        self.assertEqual(db.get(SETTING_INTERVAL_SECONDS), "9000.0")

    def test_interval_setters(self) -> None:
        db = self._db()
        db.ensure_defaults(
            caption="x", posts_per_cycle=1, source_channel="@a", target_channel="@b"
        )
        db.set_interval_seconds(45)
        self.assertEqual(db.get_settings().interval_seconds, 45.0)
        db.set_interval_hours(3)
        self.assertEqual(db.get_settings().interval_seconds, 10800.0)
        with self.assertRaises(ValueError):
            db.set_interval_seconds(0)

    def test_next_run_state(self) -> None:
        db = self._db()
        db.ensure_defaults(
            caption="x", posts_per_cycle=1, source_channel="@a", target_channel="@b"
        )
        self.assertEqual(db.get_next_run(), 0.0)
        db.set_next_run(1_700_000_000, "interval")
        self.assertEqual(db.get_next_run(), 1_700_000_000.0)
        self.assertEqual(db.get_next_run_reason(), "interval")
        db.run_asap()
        self.assertEqual(db.get_next_run(), 0.0)

    def test_backlog_and_toggles(self) -> None:
        db = self._db()
        db.ensure_defaults(
            caption="x", posts_per_cycle=1, source_channel="@a", target_channel="@b"
        )
        db.set_progress_id(90)
        db.set_latest_source_id(100)
        self.assertEqual(db.backlog(), 10)

        self.assertFalse(db.get_settings().catchup_enabled)
        db.set_catchup(True)
        db.set_catchup_seconds(45)
        db.set_notify_cycles(True)
        s = db.get_settings()
        self.assertTrue(s.catchup_enabled)
        self.assertEqual(s.catchup_seconds, 45.0)
        self.assertTrue(s.notify_cycles)


class WindowBudgetTests(unittest.TestCase):
    def test_fair_split(self) -> None:
        self.assertEqual(fair_window_limit(10, 2, 4), 2)
        self.assertEqual(fair_window_limit(1, 8, 16), 1)
        self.assertEqual(fair_window_limit(5, 1, 3), 3)
        self.assertEqual(fair_window_limit(5, 4, 0), 0)

    def test_slice_timeout(self) -> None:
        self.assertEqual(slice_timeout(90, 180), 90)
        self.assertEqual(slice_timeout(90, 20), 20)
        self.assertEqual(slice_timeout(90, 3), 0.0)


if __name__ == "__main__":
    unittest.main()
