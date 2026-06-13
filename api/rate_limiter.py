"""
Rate limiter для Starvell API.
"""

from __future__ import annotations

import asyncio
import time


class RateLimiter:
    """Ограничитель частоты запросов с поддержкой burst."""

    def __init__(self, max_per_minute: int = 40) -> None:
        self._min_interval = 60.0 / max(1, min(max_per_minute, 60))
        self._lock = asyncio.Lock()
        self._next_allowed = 0.0

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            now = loop.time()
            if self._next_allowed <= 0:
                self._next_allowed = now
            delay = self._next_allowed - now
            if delay > 0:
                await asyncio.sleep(delay)
                now = loop.time()
            self._next_allowed = now + self._min_interval


class ExponentialBackoff:
    """Экспоненциальный backoff для HTTP 429 / 5xx."""

    def __init__(self, base: float = 1.0, factor: float = 2.0, max_delay: float = 60.0) -> None:
        self.base = base
        self.factor = factor
        self.max_delay = max_delay
        self._attempt = 0

    def reset(self) -> None:
        self._attempt = 0

    def next_delay(self) -> float:
        delay = min(self.base * (self.factor ** self._attempt), self.max_delay)
        self._attempt += 1
        return delay

    async def sleep(self) -> None:
        await asyncio.sleep(self.next_delay())
