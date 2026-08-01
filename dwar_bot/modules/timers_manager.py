"""
Timers manager — tracks in-game cooldowns, profession timers,
energy recovery, and provides non-blocking wait logic.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from playwright.async_api import Page

from dwar_bot.config import SELECTORS, TIMERS
from dwar_bot.core.anti_bot import sleep_random, wait_for_selector_safe

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Timer data structures
# ---------------------------------------------------------------------------

@dataclass
class Cooldown:
    name: str
    ends_at: float            # Unix timestamp
    callback: Optional[Callable] = field(default=None, repr=False)

    @property
    def remaining(self) -> float:
        return max(0.0, self.ends_at - time.time())

    @property
    def is_ready(self) -> bool:
        return self.remaining <= 0.0


# ---------------------------------------------------------------------------
# TimersManager
# ---------------------------------------------------------------------------

class TimersManager:
    """
    Central registry for all time-based game events.

    The manager tracks named cooldowns and provides helpers to:
    * Register new cooldowns programmatically or by DOM scraping
    * Query whether any cooldown is ready
    * Wait (non-blocking) until a specific cooldown fires
    * Run periodic background tasks (energy polling, crafting checks)
    """

    def __init__(self, page: Page) -> None:
        self._page = page
        self._cooldowns: Dict[str, Cooldown] = {}
        self._heartbeat_task: Optional[asyncio.Task] = None
        self._energy_task: Optional[asyncio.Task] = None
        self._profession_task: Optional[asyncio.Task] = None

    # ------------------------------------------------------------------
    # Cooldown registry
    # ------------------------------------------------------------------

    def register(
        self,
        name: str,
        duration_seconds: float,
        callback: Optional[Callable] = None,
    ) -> Cooldown:
        """Register a named cooldown that expires *duration_seconds* from now."""
        cd = Cooldown(
            name=name,
            ends_at=time.time() + duration_seconds,
            callback=callback,
        )
        self._cooldowns[name] = cd
        logger.debug("Cooldown registered: '%s' — %.0fs remaining.", name, duration_seconds)
        return cd

    def reset(self, name: str, duration_seconds: float) -> None:
        """Reset an existing cooldown (or create it if new)."""
        if name in self._cooldowns:
            self._cooldowns[name].ends_at = time.time() + duration_seconds
        else:
            self.register(name, duration_seconds)

    def is_ready(self, name: str) -> bool:
        """Return True if cooldown *name* has expired (or was never registered)."""
        cd = self._cooldowns.get(name)
        return cd is None or cd.is_ready

    def remaining(self, name: str) -> float:
        """Seconds remaining on cooldown *name*; 0.0 if not found or expired."""
        cd = self._cooldowns.get(name)
        return cd.remaining if cd else 0.0

    def ready_list(self) -> list[Cooldown]:
        """Return all cooldowns that have expired."""
        return [cd for cd in self._cooldowns.values() if cd.is_ready]

    # ------------------------------------------------------------------
    # DOM scraping
    # ------------------------------------------------------------------

    async def scrape_craft_timers(self) -> dict[str, float]:
        """
        Read active crafting/profession timers from the game UI.

        Returns a mapping of {timer_label: remaining_seconds}.
        """
        timers: dict[str, float] = {}
        try:
            timer_els = await self._page.query_selector_all(SELECTORS.timer_craft)
            for el in timer_els:
                raw = (await el.inner_text()).strip()
                secs = self._parse_timer_text(raw)
                if secs > 0:
                    label = await el.get_attribute("data-name") or raw
                    timers[label] = secs
                    self.reset(f"craft_{label}", secs)
        except Exception as exc:
            logger.debug("scrape_craft_timers failed: %s", exc)
        return timers

    async def scrape_energy_timer(self) -> float:
        """
        Return seconds until energy is fully restored, or 0 if already full.
        """
        try:
            el = await wait_for_selector_safe(
                self._page, SELECTORS.timer_energy_restore, timeout_ms=2_000
            )
            if el:
                raw = (await el.inner_text()).strip()
                secs = self._parse_timer_text(raw)
                if secs > 0:
                    self.reset("energy_restore", secs)
                    return secs
        except Exception as exc:
            logger.debug("scrape_energy_timer failed: %s", exc)
        return 0.0

    @staticmethod
    def _parse_timer_text(raw: str) -> float:
        """
        Parse timer strings like ``"1:23:45"``, ``"12:34"``, ``"45s"``, ``"2ч 15м"``.
        Returns total seconds as a float.
        """
        import re

        raw = raw.strip()

        # HH:MM:SS or MM:SS
        m = re.match(r"(\d+):(\d{2})(?::(\d{2}))?", raw)
        if m:
            parts = [int(x) for x in m.groups() if x is not None]
            if len(parts) == 3:
                return parts[0] * 3600 + parts[1] * 60 + parts[2]
            return parts[0] * 60 + parts[1]

        # Russian: "2ч 15м 30с" or "15м 30с" or "30с"
        total = 0.0
        for unit, mult in [("ч", 3600), ("м", 60), ("с", 1), ("h", 3600), ("m", 60), ("s", 1)]:
            m2 = re.search(r"(\d+)\s*" + unit, raw, re.IGNORECASE)
            if m2:
                total += int(m2.group(1)) * mult

        return total

    # ------------------------------------------------------------------
    # Async wait helpers
    # ------------------------------------------------------------------

    async def wait_until_ready(
        self,
        name: str,
        poll_interval: float = 5.0,
        max_wait: float = 3600.0,
    ) -> bool:
        """
        Suspend until cooldown *name* is ready.

        Returns True when the cooldown fires, False on timeout (*max_wait*).
        """
        start = time.time()
        while not self.is_ready(name):
            elapsed = time.time() - start
            if elapsed >= max_wait:
                logger.warning(
                    "wait_until_ready('%s') timed out after %.0fs.", name, max_wait
                )
                return False
            remaining = self.remaining(name)
            wait = min(poll_interval, remaining + 0.5)
            logger.debug("Waiting %.0fs for cooldown '%s' …", wait, name)
            await asyncio.sleep(wait)
        return True

    # ------------------------------------------------------------------
    # Background tasks
    # ------------------------------------------------------------------

    def start_background_tasks(self) -> None:
        """Start all periodic background polling coroutines."""
        if self._heartbeat_task is None or self._heartbeat_task.done():
            self._heartbeat_task = asyncio.ensure_future(self._heartbeat_loop())
        if self._energy_task is None or self._energy_task.done():
            self._energy_task = asyncio.ensure_future(self._energy_poll_loop())
        if self._profession_task is None or self._profession_task.done():
            self._profession_task = asyncio.ensure_future(self._profession_poll_loop())
        logger.info("TimersManager background tasks started.")

    async def stop_background_tasks(self) -> None:
        """Cancel all background tasks."""
        for task in (self._heartbeat_task, self._energy_task, self._profession_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        logger.info("TimersManager background tasks stopped.")

    async def _heartbeat_loop(self) -> None:
        """Log a heartbeat message periodically."""
        while True:
            try:
                ready = [cd.name for cd in self.ready_list()]
                logger.info(
                    "Heartbeat — tracked cooldowns: %d, ready: %s",
                    len(self._cooldowns),
                    ready or "none",
                )
                await asyncio.sleep(TIMERS.heartbeat_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Heartbeat error: %s", exc)
                await asyncio.sleep(10)

    async def _energy_poll_loop(self) -> None:
        """Poll energy timer and update internal cooldown."""
        while True:
            try:
                await self.scrape_energy_timer()
                await asyncio.sleep(TIMERS.energy_regen_poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Energy poll error: %s", exc)
                await asyncio.sleep(30)

    async def _profession_poll_loop(self) -> None:
        """Poll crafting timers and update internal cooldowns."""
        while True:
            try:
                timers = await self.scrape_craft_timers()
                if timers:
                    logger.debug("Craft timers updated: %s", timers)
                await asyncio.sleep(TIMERS.profession_recheck_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("Profession poll error: %s", exc)
                await asyncio.sleep(60)
