"""
Sliding-window rate limiter (in-memory) with self-test harness.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Deque, DefaultDict


@dataclass(frozen=True)
class LoadSimResult:
    allowed: int
    denied: int
    ok: bool
    detail: str


class RateLimiter:
    """
    Per-key sliding window: at most `limit` events in the last `window_sec` seconds.
    """

    def __init__(self, *, limit: int = 5, window_sec: float = 60.0) -> None:
        if limit < 1:
            raise ValueError("limit must be >= 1")
        if window_sec <= 0:
            raise ValueError("window_sec must be > 0")
        self.limit = limit
        self.window_sec = float(window_sec)
        self._events: DefaultDict[str, Deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> None:
        q = self._events[key]
        cutoff = now - self.window_sec
        while q and q[0] <= cutoff:
            q.popleft()

    def allow(self, key: str, *, now: float) -> bool:
        self._prune(key, now)
        q = self._events[key]
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True

    def remaining(self, key: str, *, now: float) -> int:
        self._prune(key, now)
        return max(0, self.limit - len(self._events[key]))

    def simulate_load(
        self,
        *,
        events: int = 20,
        key: str = "sim",
        start: float = 1_000.0,
        step: float = 1.0,
    ) -> LoadSimResult:
        """
        Feed an emulated stream and verify sliding-window invariants.
        """
        probe = RateLimiter(limit=self.limit, window_sec=self.window_sec)
        allowed = 0
        denied = 0
        invariant_ok = True

        for i in range(events):
            t = start + i * step
            if probe.allow(key, now=t):
                allowed += 1
            else:
                denied += 1
            probe._prune(key, t)
            if len(probe._events[key]) > self.limit:
                invariant_ok = False

        # Dense stream (step << window): first `limit` must be allowed
        first_burst_ok = True
        if step * self.limit < self.window_sec and events >= self.limit:
            dense = RateLimiter(limit=self.limit, window_sec=self.window_sec)
            for i in range(self.limit):
                if not dense.allow(key, now=start + i * step):
                    first_burst_ok = False
            # Next immediate event in same window must be denied
            if dense.allow(key, now=start + self.limit * step):
                first_burst_ok = False

        ok = invariant_ok and first_burst_ok and (allowed + denied == events)
        detail = (
            f"allowed={allowed} denied={denied} limit={self.limit} "
            f"window={self.window_sec}s invariant={invariant_ok} "
            f"burst_ok={first_burst_ok}"
        )
        return LoadSimResult(
            allowed=allowed, denied=denied, ok=ok, detail=detail
        )
