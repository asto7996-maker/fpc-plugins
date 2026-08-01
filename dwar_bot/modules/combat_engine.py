"""
Combat engine — automates battle sequences.

Flow per battle tick
--------------------
1. Check if a battle screen is active.
2. Read enemy HP and log entry (detect special effects / stuns).
3. If own HP < threshold → drink HP elixir.
4. If own MP < threshold → drink MP elixir.
5. If HP < retreat threshold → flee.
6. Otherwise pick skill or basic attack and execute.
7. Detect battle result (win/lose) and handle accordingly.
"""

from __future__ import annotations

import logging
import random
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from playwright.async_api import Page

from dwar_bot.config import COMBAT, DELAY_COMBAT, SELECTORS
from dwar_bot.core.anti_bot import (
    human_click,
    sleep_random,
    wait_for_selector_safe,
)
from dwar_bot.modules.stats_parser import StatsParser, CharStats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class BattleResult(Enum):
    ONGOING = auto()
    WIN = auto()
    LOSE = auto()
    FLED = auto()
    NO_BATTLE = auto()


@dataclass
class BattleStats:
    battles_fought: int = 0
    wins: int = 0
    losses: int = 0
    fled: int = 0
    elixirs_used: int = 0
    total_damage_dealt: int = 0

    @property
    def win_rate(self) -> float:
        total = self.wins + self.losses
        return self.wins / total * 100 if total else 0.0


# ---------------------------------------------------------------------------
# CombatEngine
# ---------------------------------------------------------------------------

class CombatEngine:
    """
    Drives combat interactions on the game page.

    Parameters
    ----------
    page:         Active Playwright page.
    stats_parser: Shared StatsParser instance.
    """

    def __init__(self, page: Page, stats_parser: StatsParser) -> None:
        self._page = page
        self._stats = stats_parser
        self.session_stats = BattleStats()
        self._consecutive_battles = 0

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    async def is_in_battle(self) -> bool:
        """Return True if the combat UI is currently visible."""
        el = await wait_for_selector_safe(
            self._page, SELECTORS.combat_attack_btn, timeout_ms=2_000
        )
        return el is not None

    async def run_battle_tick(self, char_stats: CharStats) -> BattleResult:
        """
        Execute one combat decision cycle.

        Should be called repeatedly until it returns anything other than
        ``BattleResult.ONGOING``.
        """
        if not await self.is_in_battle():
            return BattleResult.NO_BATTLE

        # --- Safety check: retreat if HP critically low ---
        if char_stats.hp_percent < COMBAT.hp_retreat_threshold:
            logger.warning(
                "HP %.1f%% below retreat threshold (%.1f%%) — fleeing!",
                char_stats.hp_percent, COMBAT.hp_retreat_threshold,
            )
            self.session_stats.fled += 1
            self._consecutive_battles = 0
            return BattleResult.FLED

        # --- Use elixirs if needed ---
        if char_stats.hp_percent < COMBAT.hp_elixir_threshold:
            await self._use_elixir("hp")

        if char_stats.mp_percent < COMBAT.mp_elixir_threshold:
            await self._use_elixir("mp")

        # --- Decide attack ---
        if COMBAT.prefer_skills:
            attacked = await self._try_skill_attack()
            if not attacked:
                await self._basic_attack()
        else:
            await self._basic_attack()

        # --- Check battle result ---
        result = await self._check_result()
        if result == BattleResult.WIN:
            self.session_stats.wins += 1
            self.session_stats.battles_fought += 1
            self._consecutive_battles += 1
            logger.info(
                "Battle WON! (session: %d wins / %d fights, %.1f%% WR)",
                self.session_stats.wins,
                self.session_stats.battles_fought,
                self.session_stats.win_rate,
            )
            await self._handle_win()
        elif result == BattleResult.LOSE:
            self.session_stats.losses += 1
            self.session_stats.battles_fought += 1
            self._consecutive_battles = 0
            logger.warning(
                "Battle LOST! (session losses: %d)", self.session_stats.losses
            )
            await self._handle_result_screen()

        return result

    async def needs_rest(self) -> bool:
        """Return True when the bot should stop fighting and rest."""
        return self._consecutive_battles >= COMBAT.max_consecutive_battles

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _basic_attack(self) -> None:
        logger.debug("Executing basic attack.")
        try:
            await human_click(self._page, SELECTORS.combat_attack_btn, timeout_ms=5_000)
            await sleep_random(DELAY_COMBAT.min, DELAY_COMBAT.max)
        except Exception as exc:
            logger.warning("basic_attack failed: %s", exc)

    async def _try_skill_attack(self) -> bool:
        """
        Click a random available skill button.

        Returns True if a skill was used, False if no skill buttons found.
        """
        try:
            buttons = await self._page.query_selector_all(SELECTORS.combat_skill_btns)
            if not buttons:
                return False

            # Filter out disabled buttons
            enabled = []
            for btn in buttons:
                try:
                    disabled = await btn.get_attribute("disabled")
                    cls = await btn.get_attribute("class") or ""
                    if disabled is None and "disabled" not in cls and "cooldown" not in cls:
                        enabled.append(btn)
                except Exception:
                    pass

            if not enabled:
                return False

            chosen = random.choice(enabled)
            await chosen.click()
            logger.debug("Used skill (%d available, 1 chosen).", len(enabled))
            await sleep_random(DELAY_COMBAT.min, DELAY_COMBAT.max)
            return True
        except Exception as exc:
            logger.debug("_try_skill_attack failed: %s", exc)
            return False

    async def _use_elixir(self, kind: str) -> None:
        """
        Click the HP or MP elixir button.

        Parameters
        ----------
        kind: ``"hp"`` or ``"mp"``
        """
        selector = SELECTORS.elixir_hp if kind == "hp" else SELECTORS.elixir_mp
        try:
            el = await wait_for_selector_safe(
                self._page, selector, timeout_ms=3_000
            )
            if el is None:
                logger.warning("No %s elixir found in combat bar.", kind.upper())
                return
            await el.click()
            self.session_stats.elixirs_used += 1
            logger.info("Used %s elixir (total used: %d).", kind.upper(), self.session_stats.elixirs_used)
            await sleep_random(0.5, 1.2)
        except Exception as exc:
            logger.warning("_use_elixir('%s') failed: %s", kind, exc)

    async def _check_result(self) -> BattleResult:
        """Detect win/loss screens; return ONGOING if neither is visible."""
        win_el = await wait_for_selector_safe(
            self._page, SELECTORS.combat_result_win, timeout_ms=500
        )
        if win_el is not None:
            return BattleResult.WIN

        lose_el = await wait_for_selector_safe(
            self._page, SELECTORS.combat_result_lose, timeout_ms=500
        )
        if lose_el is not None:
            return BattleResult.LOSE

        return BattleResult.ONGOING

    async def _handle_win(self) -> None:
        """Auto-loot (if enabled) and dismiss the result screen."""
        if COMBAT.auto_loot:
            loot_btn = await wait_for_selector_safe(
                self._page, ".btn-loot, .auto-loot, [data-action='loot']", timeout_ms=2_000
            )
            if loot_btn:
                await loot_btn.click()
                await sleep_random(0.3, 0.8)
        await self._handle_result_screen()

    async def _handle_result_screen(self) -> None:
        """Click the OK/continue button on the battle result popup."""
        try:
            btn = await wait_for_selector_safe(
                self._page, SELECTORS.combat_result_btn, timeout_ms=5_000
            )
            if btn:
                await sleep_random(0.5, 1.5)
                await btn.click()
                await sleep_random(1.0, 2.5)
        except Exception as exc:
            logger.debug("_handle_result_screen failed: %s", exc)

    async def read_combat_log(self) -> list[str]:
        """Return the last N lines of the battle log."""
        entries: list[str] = []
        try:
            items = await self._page.query_selector_all(SELECTORS.combat_log_entry)
            for item in items[-10:]:
                text = (await item.inner_text()).strip()
                if text:
                    entries.append(text)
        except Exception as exc:
            logger.debug("read_combat_log failed: %s", exc)
        return entries
