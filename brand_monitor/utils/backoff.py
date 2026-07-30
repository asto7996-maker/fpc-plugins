"""Exponential backoff helpers for network reconnect logic."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Awaitable, Callable, Optional, TypeVar

T = TypeVar("T")
logger = logging.getLogger(__name__)


class ExponentialBackoff:
    """Stateful exponential backoff with jitter."""

    def __init__(
        self,
        base: float = 1.0,
        maximum: float = 60.0,
        max_retries: int = 8,
        factor: float = 2.0,
        jitter: float = 0.2,
    ) -> None:
        self.base = base
        self.maximum = maximum
        self.max_retries = max_retries
        self.factor = factor
        self.jitter = jitter
        self.attempt = 0

    def reset(self) -> None:
        self.attempt = 0

    @property
    def exhausted(self) -> bool:
        return self.attempt >= self.max_retries

    def next_delay(self) -> float:
        """Compute next delay and advance attempt counter."""
        delay = min(self.base * (self.factor ** self.attempt), self.maximum)
        if self.jitter:
            spread = delay * self.jitter
            delay = max(0.0, delay + random.uniform(-spread, spread))
        self.attempt += 1
        return delay

    async def wait(self) -> float:
        delay = self.next_delay()
        logger.debug("Backoff sleep %.2fs (attempt %s/%s)", delay, self.attempt, self.max_retries)
        await asyncio.sleep(delay)
        return delay


async def retry_with_backoff(
    operation: Callable[[], Awaitable[T]],
    *,
    base: float = 1.0,
    maximum: float = 60.0,
    max_retries: int = 8,
    retry_exceptions: tuple[type[BaseException], ...] = (Exception,),
    on_retry: Optional[Callable[[int, BaseException, float], Awaitable[None]]] = None,
) -> T:
    """Execute async operation with exponential backoff on retryable errors."""
    backoff = ExponentialBackoff(base=base, maximum=maximum, max_retries=max_retries)
    while True:
        try:
            result = await operation()
            backoff.reset()
            return result
        except retry_exceptions as exc:
            if backoff.exhausted:
                raise
            delay = backoff.next_delay()
            if on_retry is not None:
                await on_retry(backoff.attempt, exc, delay)
            await asyncio.sleep(delay)
