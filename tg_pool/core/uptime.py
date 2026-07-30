"""
Uptime / working-hours window helpers (timezone-aware).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class UptimeWindow:
    """
    Inclusive daily working window in a given IANA timezone.

    Examples
    --------
    09:00–18:00 Europe/Moscow
    22:00–06:00 (overnight wrap) Asia/Tokyo
    """

    start: time
    end: time
    tz_name: str = "UTC"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.tz_name)

    def is_open(self, when: datetime | None = None) -> bool:
        when = when or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        local = when.astimezone(self.tz)
        current = local.timetz().replace(tzinfo=None)
        start, end = self.start, self.end
        if start == end:
            # 24h window
            return True
        if start < end:
            return start <= current < end
        # Overnight: e.g. 22:00–06:00
        return current >= start or current < end

    def seconds_until_open(self, when: datetime | None = None) -> int:
        """0 if currently open; otherwise seconds until next open instant."""
        when = when or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if self.is_open(when):
            return 0
        local = when.astimezone(self.tz)
        # probe forward minute-by-minute up to 48h (simple & correct)
        from datetime import timedelta

        cursor = local.replace(second=0, microsecond=0)
        for _ in range(48 * 60):
            cursor += timedelta(minutes=1)
            if self.is_open(cursor.astimezone(timezone.utc)):
                delta = cursor - local
                return max(0, int(delta.total_seconds()))
        return 48 * 3600
