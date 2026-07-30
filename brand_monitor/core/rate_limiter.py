"""Sliding-window rate limiter and inter-action pause for support agents."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    reason: str = ""
    retry_after_seconds: float = 0.0


class SlidingWindowRateLimiter:
    """In-memory sliding window counters keyed by agent_id."""

    def __init__(
        self,
        *,
        max_per_hour: int = 4,
        max_per_day: int = 18,
        min_pause_sec: float = 600.0,
        max_pause_sec: float = 1500.0,
    ) -> None:
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day
        self.min_pause_sec = min_pause_sec
        self.max_pause_sec = max_pause_sec
        self._events: dict[int, list[datetime]] = {}
        self._next_allowed: dict[int, datetime] = {}

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _prune(self, agent_id: int, now: datetime) -> None:
        cutoff = now - timedelta(hours=24)
        events = self._events.get(agent_id, [])
        self._events[agent_id] = [ts for ts in events if ts >= cutoff]

    def check(self, agent_id: int, now: Optional[datetime] = None) -> RateLimitDecision:
        now = now or self._now()
        self._prune(agent_id, now)

        next_allowed = self._next_allowed.get(agent_id)
        if next_allowed and now < next_allowed:
            wait = (next_allowed - now).total_seconds()
            return RateLimitDecision(
                False,
                reason="min_pause",
                retry_after_seconds=wait,
            )

        events = self._events.get(agent_id, [])
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(hours=24)
        hour_count = sum(1 for ts in events if ts >= hour_ago)
        day_count = sum(1 for ts in events if ts >= day_ago)

        if hour_count >= self.max_per_hour:
            oldest_hour = min(ts for ts in events if ts >= hour_ago)
            wait = (oldest_hour + timedelta(hours=1) - now).total_seconds()
            return RateLimitDecision(False, "hourly_limit", max(wait, 1.0))

        if day_count >= self.max_per_day:
            oldest_day = min(ts for ts in events if ts >= day_ago)
            wait = (oldest_day + timedelta(hours=24) - now).total_seconds()
            return RateLimitDecision(False, "daily_limit", max(wait, 1.0))

        return RateLimitDecision(True)

    def register_action(self, agent_id: int, now: Optional[datetime] = None) -> float:
        """Record a successful reply and schedule the next random pause. Returns pause seconds."""
        now = now or self._now()
        self._prune(agent_id, now)
        self._events.setdefault(agent_id, []).append(now)
        pause = random.uniform(self.min_pause_sec, self.max_pause_sec)
        self._next_allowed[agent_id] = now + timedelta(seconds=pause)
        return pause

    def counts(self, agent_id: int, now: Optional[datetime] = None) -> tuple[int, int]:
        now = now or self._now()
        self._prune(agent_id, now)
        events = self._events.get(agent_id, [])
        hour_ago = now - timedelta(hours=1)
        day_ago = now - timedelta(hours=24)
        return (
            sum(1 for ts in events if ts >= hour_ago),
            sum(1 for ts in events if ts >= day_ago),
        )
