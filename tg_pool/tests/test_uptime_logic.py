"""Pytest: timezone-aware uptime / working-hours windows."""

from __future__ import annotations

from datetime import datetime, time, timezone

from tg_pool.core.uptime import UptimeWindow


def test_same_day_window_moscow() -> None:
    win = UptimeWindow(start=time(9, 0), end=time(18, 0), tz_name="Europe/Moscow")
    # 12:00 MSK = 09:00 UTC
    noon_msk = datetime(2026, 7, 30, 9, 0, tzinfo=timezone.utc)
    assert win.is_open(noon_msk) is True
    # 03:00 MSK = 00:00 UTC
    night = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
    assert win.is_open(night) is False


def test_overnight_window_tokyo() -> None:
    win = UptimeWindow(start=time(22, 0), end=time(6, 0), tz_name="Asia/Tokyo")
    # 23:30 JST = 14:30 UTC
    late = datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc)
    assert win.is_open(late) is True
    # 12:00 JST = 03:00 UTC
    midday = datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc)
    assert win.is_open(midday) is False


def test_utc_24h() -> None:
    win = UptimeWindow(start=time(0, 0), end=time(0, 0), tz_name="UTC")
    assert win.is_open(datetime(2026, 1, 1, 15, 0, tzinfo=timezone.utc)) is True


def test_seconds_until_open() -> None:
    win = UptimeWindow(start=time(9, 0), end=time(18, 0), tz_name="UTC")
    closed = datetime(2026, 7, 30, 3, 0, tzinfo=timezone.utc)
    assert win.is_open(closed) is False
    wait = win.seconds_until_open(closed)
    assert 0 < wait <= 6 * 3600
