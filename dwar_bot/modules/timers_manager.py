"""
Timers manager — cooldowns, profession timers, energy/HP regeneration.

Everything is tracked from the HTTP API:
* ``common|dummy``       — server_time, flags (in-fight / jailed / …)
* ``user.php``           — HP/MP values used to compute regeneration rate
* ``area.php``           — time_bonus_online, tech-works windows
* ``hunt_conf.php``      — event NPC ``time_left`` countdowns
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from dwar_bot.config import TIMERS
from dwar_bot.core.game_client import DwarGameClient

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Cooldown
# ---------------------------------------------------------------------------

@dataclass
class Cooldown:
    name: str
    ends_at: float
    description: str = ""

    @property
    def remaining(self) -> float:
        return max(0.0, self.ends_at - time.time())

    @property
    def is_ready(self) -> bool:
        return self.remaining <= 0.0

    def format_remaining(self) -> str:
        r = int(self.remaining)
        if r <= 0:
            return "готово"
        h, m, s = r // 3600, (r % 3600) // 60, r % 60
        if h:
            return f"{h}ч {m}м"
        if m:
            return f"{m}м {s}с"
        return f"{s}с"


@dataclass
class RegenTracker:
    """Tracks HP/MP regeneration rate from observed samples."""
    last_hp: int = 0
    last_mp: int = 0
    last_sample_at: float = 0.0
    hp_per_min: float = 0.0
    mp_per_min: float = 0.0

    def update(self, hp: int, mp: int) -> None:
        now = time.time()
        if self.last_sample_at > 0:
            dt_min = (now - self.last_sample_at) / 60.0
            if dt_min > 0.2:  # need at least 12s between samples
                dhp = hp - self.last_hp
                dmp = mp - self.last_mp
                if dhp > 0:
                    self.hp_per_min = dhp / dt_min
                if dmp > 0:
                    self.mp_per_min = dmp / dt_min
        self.last_hp = hp
        self.last_mp = mp
        self.last_sample_at = now

    def seconds_to_full_hp(self, hp: int, hp_max: int) -> float:
        if hp >= hp_max or self.hp_per_min <= 0:
            return 0.0
        return (hp_max - hp) / self.hp_per_min * 60.0


# ---------------------------------------------------------------------------
# TimersManager
# ---------------------------------------------------------------------------

class TimersManager:
    """Central registry of all time-based game state."""

    def __init__(self, client: DwarGameClient) -> None:
        self._client = client
        self._cooldowns: dict[str, Cooldown] = {}
        self.regen = RegenTracker()
        self._tasks: list[asyncio.Task] = []
        self._server_time_offset: float = 0.0
        self._time_bonus_seconds: int = 0

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    def register(self, name: str, seconds: float, description: str = "") -> Cooldown:
        cd = Cooldown(name=name, ends_at=time.time() + seconds, description=description)
        self._cooldowns[name] = cd
        logger.debug("Cooldown '%s' set for %.0fs.", name, seconds)
        return cd

    def is_ready(self, name: str) -> bool:
        cd = self._cooldowns.get(name)
        return cd is None or cd.is_ready

    def remaining(self, name: str) -> float:
        cd = self._cooldowns.get(name)
        return cd.remaining if cd else 0.0

    def all_cooldowns(self) -> list[Cooldown]:
        return list(self._cooldowns.values())

    def active_cooldowns(self) -> list[Cooldown]:
        return [cd for cd in self._cooldowns.values() if not cd.is_ready]

    def clear_expired(self) -> int:
        expired = [k for k, v in self._cooldowns.items() if v.is_ready]
        for k in expired:
            del self._cooldowns[k]
        return len(expired)

    # ------------------------------------------------------------------
    # Server-side timer scraping
    # ------------------------------------------------------------------

    async def sync_server_time(self) -> None:
        """Compute the local↔server clock offset."""
        try:
            state = await self._client.get_state()
            if state.server_time:
                self._server_time_offset = state.server_time - time.time()
                logger.debug("Server time offset: %.1fs", self._server_time_offset)
        except Exception as exc:
            logger.debug("sync_server_time error: %s", exc)

    @property
    def server_time(self) -> float:
        return time.time() + self._server_time_offset

    async def scrape_event_timers(
        self, hunt: Optional[dict] = None
    ) -> dict[str, int]:
        """
        Read event/NPC countdowns from hunt_conf (reuse ``hunt`` if provided).
        """
        timers: dict[str, int] = {}
        try:
            if hunt is None:
                hunt = await self._client.get_hunt_conf()
            for npc in hunt.get("npcs", []):
                left = int(npc.get("time_left", 0))
                if left <= 0:
                    continue
                name = f"npc_{npc.get('npc_id', '?')}"
                title = npc.get("title", "")
                timers[title] = left
                self.register(name, left, description=title)
        except Exception as exc:
            logger.debug("scrape_event_timers error: %s", exc)
        return timers

    async def scrape_area_timers(self) -> dict[str, int]:
        """
        Parse timers embedded in area.php:
        ``time_bonus_online``, tech-works windows, bank refresh, etc.
        """
        timers: dict[str, int] = {}
        try:
            resp = await self._client._get("/area.php")
            html = resp.text

            par_m = re.search(r"var par='([^']+)'", html)
            if not par_m:
                return timers

            import urllib.parse
            par = dict(urllib.parse.parse_qsl(par_m.group(1), keep_blank_values=True))

            # Online time bonus
            bonus = par.get("time_bonus_online")
            if bonus:
                try:
                    secs = int(bonus)
                    self._time_bonus_seconds = secs
                    timers["time_bonus"] = secs
                    self.register("time_bonus", secs, description="Подарок за время онлайн")
                except ValueError:
                    pass

            # Scheduled maintenance
            tw_start = par.get("tech_works_start")
            tw_stop = par.get("tech_works_stop")
            if tw_start and tw_stop:
                try:
                    start, stop = int(tw_start), int(tw_stop)
                    now = self.server_time
                    if start <= now <= stop:
                        timers["tech_works_active"] = int(stop - now)
                        self.register("tech_works", stop - now,
                                      description=par.get("tech_works_name", "Тех. работы"))
                except ValueError:
                    pass

            # Fight cooldown / count
            fc = par.get("fight_count")
            if fc:
                try:
                    timers["fight_count"] = int(fc)
                except ValueError:
                    pass

        except Exception as exc:
            logger.debug("scrape_area_timers error: %s", exc)
        return timers

    # ------------------------------------------------------------------
    # Regeneration
    # ------------------------------------------------------------------

    async def update_regen(self, hp: int, mp: int) -> None:
        """Feed the latest HP/MP sample into the regeneration tracker."""
        self.regen.update(hp, mp)

    def estimate_full_hp_wait(self, hp: int, hp_max: int) -> float:
        """Seconds until HP is expected to be full (0 if already full/unknown)."""
        return self.regen.seconds_to_full_hp(hp, hp_max)

    async def wait_for_hp(
        self,
        target_percent: float = 90.0,
        max_wait: float = 1800.0,
        *,
        allow_zero_hp: bool = False,
    ) -> bool:
        """
        Sleep (in poll increments) until HP reaches *target_percent*.
        Returns True when reached, False on timeout / ghost-stuck.

        If HP stays at 0 (ghost / dead, no regen), exits early unless
        ``allow_zero_hp=True`` — waiting 180s on a spirit is wasted time.
        """
        start = time.time()
        last_hp = -1
        stagnant_polls = 0
        zero_polls = 0
        while time.time() - start < max_wait:
            char = await self._client.get_char_stats()
            hp = int(char.hp or 0)
            hp_max = int(char.hp_max or 0)
            pct = float(char.hp_percent or 0)

            if hp_max and pct >= target_percent:
                logger.info("HP recovered to %.0f%%.", pct)
                return True

            # Ghost / dead: HP does not tick up — bail fast
            if hp_max > 0 and hp <= 0 and not allow_zero_hp:
                zero_polls += 1
                if zero_polls >= 2:
                    elapsed = time.time() - start
                    logger.warning(
                        "wait_for_hp aborted: HP stuck at 0 for %.0fs "
                        "(дух/смерть — нужен resurrect, не реген).",
                        elapsed,
                    )
                    return False
                await asyncio.sleep(5.0)
                continue

            # No progress for several polls while below target
            if hp == last_hp:
                stagnant_polls += 1
            else:
                stagnant_polls = 0
                last_hp = hp

            await self.update_regen(hp, char.mp)
            eta = self.estimate_full_hp_wait(hp, hp_max)
            # If tracker sees no regen rate and HP is critically low — don't sit forever
            if stagnant_polls >= 4 and pct < max(5.0, target_percent * 0.15):
                elapsed = time.time() - start
                logger.warning(
                    "wait_for_hp aborted: HP stagnant at %.0f%% for %.0fs "
                    "(no regen progress).",
                    pct, elapsed,
                )
                return False

            wait = min(TIMERS.energy_regen_poll_interval, max(15.0, eta / 4 if eta else 20.0))
            # Cap single sleep so we re-check death/ghost sooner
            wait = min(wait, 30.0)
            logger.debug(
                "HP %.0f%% — waiting %.0fs (ETA full: %.0fs)",
                pct, wait, eta,
            )
            await asyncio.sleep(wait)

        logger.warning("wait_for_hp timed out after %.0fs.", max_wait)
        return False

    # ------------------------------------------------------------------
    # Background polling tasks
    # ------------------------------------------------------------------

    def start_background_tasks(self) -> None:
        """Launch periodic polling coroutines."""
        loop_defs = [
            (self._heartbeat_loop, "heartbeat"),
            (self._event_poll_loop, "event_poll"),
            (self._area_poll_loop, "area_poll"),
        ]
        for coro_fn, name in loop_defs:
            task = asyncio.ensure_future(coro_fn())
            task.set_name(name) if hasattr(task, "set_name") else None
            self._tasks.append(task)
        logger.info("TimersManager background tasks started (%d).", len(self._tasks))

    async def stop_background_tasks(self) -> None:
        for t in self._tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._tasks.clear()
        logger.info("TimersManager background tasks stopped.")

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                self.clear_expired()
                active = self.active_cooldowns()
                if active:
                    summary = ", ".join(
                        f"{cd.description or cd.name}: {cd.format_remaining()}"
                        for cd in active[:5]
                    )
                    logger.info("Таймеры — %s", summary)
                else:
                    logger.debug("Heartbeat — no active cooldowns.")
                await asyncio.sleep(TIMERS.heartbeat_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("heartbeat error: %s", exc)
                await asyncio.sleep(30)

    async def _event_poll_loop(self) -> None:
        while True:
            try:
                if getattr(self._client, "auth_blocked", False):
                    await asyncio.sleep(30)
                    continue
                await self.scrape_event_timers()
                await asyncio.sleep(TIMERS.profession_recheck_interval)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                # Swallow TokenExpiredError from background — main loop handles it
                logger.debug("event poll error: %s", exc)
                await asyncio.sleep(60)

    async def _area_poll_loop(self) -> None:
        while True:
            try:
                if getattr(self._client, "auth_blocked", False):
                    await asyncio.sleep(30)
                    continue
                await self.scrape_area_timers()
                await self.sync_server_time()
                await asyncio.sleep(TIMERS.craft_poll_interval * 4)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("area poll error: %s", exc)
                await asyncio.sleep(120)

    # ------------------------------------------------------------------
    # Summary for Telegram
    # ------------------------------------------------------------------

    def summary(self) -> list[dict]:
        """Return active cooldowns as plain dicts (for the Telegram /timers command)."""
        return [
            {
                "name": cd.name,
                "description": cd.description or cd.name,
                "remaining": cd.format_remaining(),
                "seconds": int(cd.remaining),
            }
            for cd in sorted(self.active_cooldowns(), key=lambda c: c.remaining)
        ]
