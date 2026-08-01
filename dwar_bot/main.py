"""
Main async entry point — orchestrates all bot modules in an infinite loop.

Start with:
    python -m dwar_bot.main
or:
    python dwar_bot/main.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Allow running as a script from the repo root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dwar_bot.config import (
    DELAY_COMBAT,
    DELAY_IDLE,
    DELAY_MAIN_LOOP,
    DELAY_RETRY,
    GAME_GAME_URL,
    IDLE_PAUSE_PROBABILITY,
    MAX_RETRIES,
    SCREENSHOT_ON_ERROR,
    TIMERS,
)
from dwar_bot.logger import setup_logging, log_exception
from dwar_bot.auth.cookie_manager import CookieManager, SessionExhaustedError
from dwar_bot.core.browser import BrowserManager
from dwar_bot.core.anti_bot import (
    action_delay,
    maybe_idle,
    random_mouse_wander,
    sleep_random,
)
from dwar_bot.modules.stats_parser import StatsParser
from dwar_bot.modules.combat_engine import CombatEngine, BattleResult
from dwar_bot.modules.quest_tracker import QuestTracker
from dwar_bot.modules.timers_manager import TimersManager

logger = logging.getLogger("dwar_bot.main")


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------

_shutdown_event = asyncio.Event()


def _handle_signal(signum, frame) -> None:
    logger.warning("Signal %d received — initiating graceful shutdown …", signum)
    _shutdown_event.set()


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------

class DwarBot:
    """Top-level orchestrator that drives all game modules."""

    def __init__(self) -> None:
        self._browser = BrowserManager()
        self._cookie_mgr = CookieManager()
        self._stats: StatsParser | None = None
        self._combat: CombatEngine | None = None
        self._quests: QuestTracker | None = None
        self._timers: TimersManager | None = None
        self._iteration = 0
        self._errors_in_row = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start browser, inject session, initialise modules."""
        await self._browser.start()
        page = await self._browser.new_page()

        logger.info("Injecting and verifying session cookies …")
        session_ok = await self._cookie_mgr.inject_and_verify(
            self._browser.context, page
        )
        if not session_ok:
            raise SessionExhaustedError(
                "Could not establish a valid game session.  "
                "Add fresh cookie files to the cookies/ directory."
            )

        # Navigate to the game
        await page.goto(GAME_GAME_URL, wait_until="domcontentloaded")
        await sleep_random(2.0, 4.0)

        # Initialise modules
        self._stats = StatsParser(page)
        self._combat = CombatEngine(page, self._stats)
        self._quests = QuestTracker(page)
        self._timers = TimersManager(page)
        self._timers.start_background_tasks()

        logger.info("DwarBot started successfully.")

    async def stop(self) -> None:
        """Clean shutdown of all subsystems."""
        if self._timers:
            await self._timers.stop_background_tasks()
        if self._browser:
            await self._browser.stop()
        logger.info("DwarBot stopped.")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Infinite main loop — runs until _shutdown_event is set."""
        while not _shutdown_event.is_set():
            self._iteration += 1
            logger.debug("--- Main loop iteration #%d ---", self._iteration)

            try:
                await self._loop_iteration()
                self._errors_in_row = 0

            except asyncio.CancelledError:
                break

            except Exception as exc:
                self._errors_in_row += 1
                log_exception(logger, f"Unhandled error in iteration #{self._iteration}", exc)
                await self._browser.safe_screenshot(f"error_iter{self._iteration}")

                if self._errors_in_row >= MAX_RETRIES:
                    logger.critical(
                        "%d consecutive errors — pausing for 5 minutes.", MAX_RETRIES
                    )
                    await sleep_random(280.0, 320.0)
                    self._errors_in_row = 0
                else:
                    await sleep_random(DELAY_RETRY.min, DELAY_RETRY.max)

            # Jitter between iterations
            if not _shutdown_event.is_set():
                await sleep_random(DELAY_MAIN_LOOP.min, DELAY_MAIN_LOOP.max)
                await maybe_idle()

    async def _loop_iteration(self) -> None:
        """One full cycle: read stats → handle quests → fight → rest."""
        assert self._stats and self._combat and self._quests and self._timers

        page = self._browser.page
        if page is None:
            raise RuntimeError("No active page.")

        # 1. Read current character state
        char = await self._stats.read_stats()
        if not char.name:
            logger.warning("Could not read character stats — may need re-login.")
            await self._try_relogin()
            return

        logger.info(
            "Char: %s Lv%d | HP %.0f%% | MP %.0f%% | EXP %.1f%% | Gold %d",
            char.name, char.level,
            char.hp_percent, char.mp_percent,
            char.exp_percent, char.gold,
        )

        # 2. Dismiss notifications
        dismissed = await self._stats.dismiss_notifications()
        if dismissed:
            logger.debug("Dismissed %d notification(s).", dismissed)

        # 3. Handle any open NPC dialogue
        if await self._quests.is_dialogue_open():
            handled = await self._quests.drain_all_dialogues(
                preferred_keywords=["принять", "accept", "продолжить", "continue"]
            )
            logger.info("Handled %d dialogue screen(s).", handled)
            await action_delay()

        # 4. Try to turn in completed quests
        turned_in = await self._quests.try_complete_quests()
        if turned_in:
            logger.info("Turned in %d quest(s).", turned_in)
            await action_delay()

        # 5. Combat cycle
        if await self._combat.is_in_battle():
            await self._run_combat_cycle(char)
        elif not self._combat.needs_rest():
            # Wander mouse to seem natural when not in battle
            await random_mouse_wander(page, moves=2)

        # 6. Check energy — wait if exhausted
        energy_wait = await self._timers.scrape_energy_timer()
        if energy_wait > 0 and char.energy_percent < TIMERS.energy_full_threshold:
            logger.info(
                "Energy %.0f%% — waiting %.0fs for regen.",
                char.energy_percent, energy_wait,
            )
            await asyncio.sleep(min(energy_wait, 120.0))

    async def _run_combat_cycle(self, char) -> None:
        """Drive a battle to completion."""
        assert self._combat and self._stats

        for _tick in range(300):   # safety cap
            if _shutdown_event.is_set():
                break

            # Refresh stats before each decision
            updated_char = await self._stats.read_stats()
            result = await self._combat.run_battle_tick(updated_char)

            if result in (BattleResult.WIN, BattleResult.LOSE, BattleResult.FLED):
                break
            if result == BattleResult.NO_BATTLE:
                break

            await sleep_random(DELAY_COMBAT.min, DELAY_COMBAT.max)

    async def _try_relogin(self) -> None:
        """Attempt to re-inject cookies and navigate back to the game."""
        logger.warning("Attempting re-login via cookie re-injection …")
        try:
            page = self._browser.page
            if page is None:
                return
            await self._browser.context.clear_cookies()
            session_ok = await self._cookie_mgr.inject_and_verify(
                self._browser.context, page
            )
            if session_ok:
                await page.goto(GAME_GAME_URL, wait_until="domcontentloaded")
                await sleep_random(2.0, 4.0)
                logger.info("Re-login successful.")
            else:
                logger.error("Re-login failed — session exhausted.")
                _shutdown_event.set()
        except Exception as exc:
            log_exception(logger, "Re-login error", exc)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def _wait_for_cookies(cookie_dir: Path, poll_interval: float = 30.0) -> None:
    """
    Block until at least one .json or .txt cookie file appears in *cookie_dir*.
    Logs a reminder every *poll_interval* seconds so the operator knows what to do.
    """
    while True:
        files = list(cookie_dir.glob("*.json")) + list(cookie_dir.glob("*.txt"))
        if files:
            logger.info("Cookie file detected: %s — proceeding.", files[0].name)
            return
        logger.warning(
            "Waiting for game session cookies in '%s'. "
            "Export your dwar.ru cookies with 'Cookie Editor' (browser extension) "
            "and place the JSON file as: %s/session_cookies.json",
            cookie_dir,
            cookie_dir,
        )
        await asyncio.sleep(poll_interval)


async def main() -> None:
    setup_logging(
        level=os.getenv("DWAR_LOG_LEVEL", "INFO"),
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        telegram_min_level=os.getenv("TELEGRAM_MIN_LEVEL", "WARNING"),
    )

    # Register OS signals for graceful shutdown
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    from dwar_bot.config import COOKIES_DIR
    await _wait_for_cookies(COOKIES_DIR)

    bot = DwarBot()
    try:
        await bot.start()
        await bot.run()
    except SessionExhaustedError as exc:
        logger.critical("Session exhausted: %s", exc)
        sys.exit(1)
    except Exception as exc:
        log_exception(logger, "Fatal error in main()", exc)
        sys.exit(2)
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(main())
