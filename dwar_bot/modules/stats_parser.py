"""
Stats parser — reads the player's profile, inventory, currency,
and notifications from the live game page.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from playwright.async_api import Page

from dwar_bot.config import SELECTORS, PAGE_TIMEOUT_MS
from dwar_bot.core.anti_bot import wait_for_selector_safe, sleep_random

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class CharStats:
    name: str = ""
    level: int = 0
    hp: int = 0
    hp_max: int = 0
    mp: int = 0
    mp_max: int = 0
    energy: int = 0
    energy_max: int = 0
    exp_percent: float = 0.0
    gold: int = 0
    silver: int = 0

    @property
    def hp_percent(self) -> float:
        return (self.hp / self.hp_max * 100) if self.hp_max else 0.0

    @property
    def mp_percent(self) -> float:
        return (self.mp / self.mp_max * 100) if self.mp_max else 0.0

    @property
    def energy_percent(self) -> float:
        return (self.energy / self.energy_max * 100) if self.energy_max else 0.0


@dataclass
class InventoryItem:
    name: str = ""
    count: int = 1
    slot: int = 0


@dataclass
class Notification:
    text: str = ""
    category: str = "info"


# ---------------------------------------------------------------------------
# Helper: safe text extraction
# ---------------------------------------------------------------------------

async def _text(page: Page, selector: str, default: str = "") -> str:
    """Return trimmed inner text of the first matching element or *default*."""
    try:
        el = await page.query_selector(selector)
        if el:
            val = await el.inner_text()
            return val.strip()
    except Exception as exc:
        logger.debug("_text('%s') failed: %s", selector, exc)
    return default


def _parse_int(raw: str) -> int:
    """Extract the first integer found in *raw*; return 0 on failure."""
    import re
    m = re.search(r"\d[\d\s]*", raw.replace("\u00a0", ""))
    if m:
        try:
            return int(m.group().replace(" ", "").strip())
        except ValueError:
            pass
    return 0


def _parse_float(raw: str) -> float:
    """Extract the first float (or int) found in *raw*; return 0.0 on failure."""
    import re
    m = re.search(r"\d+\.?\d*", raw)
    if m:
        try:
            return float(m.group())
        except ValueError:
            pass
    return 0.0


# ---------------------------------------------------------------------------
# StatsParser
# ---------------------------------------------------------------------------

class StatsParser:
    """
    Reads character stats, inventory and notifications from the game page.

    All methods are safe — they return zeroed/empty objects on any DOM error
    rather than raising, so the main loop can continue.
    """

    def __init__(self, page: Page) -> None:
        self._page = page

    async def read_stats(self) -> CharStats:
        """Parse current character stats from the game UI."""
        stats = CharStats()
        try:
            stats.name = await _text(self._page, SELECTORS.char_name)
            stats.level = _parse_int(await _text(self._page, SELECTORS.char_level))

            hp_cur_raw = await _text(self._page, SELECTORS.char_hp_current)
            hp_max_raw = await _text(self._page, SELECTORS.char_hp_max)
            stats.hp = _parse_int(hp_cur_raw)
            stats.hp_max = _parse_int(hp_max_raw) or stats.hp

            mp_cur_raw = await _text(self._page, SELECTORS.char_mp_current)
            mp_max_raw = await _text(self._page, SELECTORS.char_mp_max)
            stats.mp = _parse_int(mp_cur_raw)
            stats.mp_max = _parse_int(mp_max_raw) or stats.mp

            energy_cur_raw = await _text(self._page, SELECTORS.char_energy)
            energy_max_raw = await _text(self._page, SELECTORS.char_energy_max)
            stats.energy = _parse_int(energy_cur_raw)
            stats.energy_max = _parse_int(energy_max_raw) or stats.energy

            exp_raw = await _text(self._page, SELECTORS.char_exp_percent)
            stats.exp_percent = _parse_float(exp_raw)

            stats.gold = _parse_int(await _text(self._page, SELECTORS.char_gold))
            stats.silver = _parse_int(await _text(self._page, SELECTORS.char_silver))

            logger.debug(
                "Stats: %s Lv%d HP=%d/%d MP=%d/%d EXP=%.1f%%",
                stats.name, stats.level, stats.hp, stats.hp_max,
                stats.mp, stats.mp_max, stats.exp_percent,
            )
        except Exception as exc:
            logger.warning("read_stats failed: %s", exc, exc_info=True)
        return stats

    async def read_inventory(self) -> list[InventoryItem]:
        """Parse inventory slots and return a list of InventoryItem."""
        items: list[InventoryItem] = []
        try:
            slots = await self._page.query_selector_all(SELECTORS.inventory_slot)
            for idx, slot in enumerate(slots):
                try:
                    name_el = await slot.query_selector(SELECTORS.inventory_item_name)
                    count_el = await slot.query_selector(SELECTORS.inventory_item_count)
                    name = (await name_el.inner_text()).strip() if name_el else ""
                    count_raw = (await count_el.inner_text()).strip() if count_el else "1"
                    if name:
                        items.append(InventoryItem(
                            name=name,
                            count=_parse_int(count_raw) or 1,
                            slot=idx,
                        ))
                except Exception as slot_exc:
                    logger.debug("Slot %d parse error: %s", idx, slot_exc)
            logger.debug("Inventory: %d items found.", len(items))
        except Exception as exc:
            logger.warning("read_inventory failed: %s", exc)
        return items

    async def count_hp_elixirs(self) -> int:
        """Return the total count of HP elixirs in inventory."""
        total = 0
        try:
            elixirs = await self._page.query_selector_all(SELECTORS.elixir_hp)
            for el in elixirs:
                try:
                    count_el = await el.query_selector(SELECTORS.inventory_item_count)
                    raw = (await count_el.inner_text()).strip() if count_el else "1"
                    total += _parse_int(raw) or 1
                except Exception:
                    total += 1
        except Exception as exc:
            logger.debug("count_hp_elixirs failed: %s", exc)
        return total

    async def count_mp_elixirs(self) -> int:
        """Return the total count of MP elixirs in inventory."""
        total = 0
        try:
            elixirs = await self._page.query_selector_all(SELECTORS.elixir_mp)
            for el in elixirs:
                try:
                    count_el = await el.query_selector(SELECTORS.inventory_item_count)
                    raw = (await count_el.inner_text()).strip() if count_el else "1"
                    total += _parse_int(raw) or 1
                except Exception:
                    total += 1
        except Exception as exc:
            logger.debug("count_mp_elixirs failed: %s", exc)
        return total

    async def read_notifications(self) -> list[Notification]:
        """Parse visible in-game notifications."""
        notifications: list[Notification] = []
        try:
            items = await self._page.query_selector_all(SELECTORS.notification_item)
            for item in items:
                try:
                    text = (await item.inner_text()).strip()
                    if text:
                        notifications.append(Notification(text=text))
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("read_notifications failed: %s", exc)
        return notifications

    async def dismiss_notifications(self) -> int:
        """Click all notification close buttons. Returns count dismissed."""
        dismissed = 0
        try:
            buttons = await self._page.query_selector_all(SELECTORS.notification_close)
            for btn in buttons:
                try:
                    await btn.click()
                    dismissed += 1
                    await sleep_random(0.2, 0.5)
                except Exception:
                    pass
        except Exception as exc:
            logger.debug("dismiss_notifications failed: %s", exc)
        return dismissed
