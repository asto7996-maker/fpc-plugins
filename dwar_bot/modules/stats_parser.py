"""Парсер состояния персонажа: профиль, ресурсы, рюкзак, уведомления.

Читает игровые страницы через :class:`BrowserManager` и приводит данные к
типизированным структурам. Все обращения к DOM защищены: отсутствующие
элементы дают значения по умолчанию, а не исключения.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from ..config import CONFIG
from ..core.browser import BrowserManager
from ..logger import get_logger, log_exception

logger = get_logger(__name__)


_NUMBER_RE = re.compile(r"-?\d[\d\s.,]*")


def parse_int(text: str, default: int = 0) -> int:
    """Извлекает первое целое из строки, игнорируя пробелы-разделители."""
    if not text:
        return default
    match = _NUMBER_RE.search(text)
    if not match:
        return default
    cleaned = match.group(0).replace(" ", "").replace("\u00a0", "").replace(",", "")
    # Отбрасываем дробную часть, если попалась.
    cleaned = cleaned.split(".")[0]
    try:
        return int(cleaned)
    except ValueError:
        return default


def parse_current_max(text: str) -> Tuple[int, int]:
    """Разбирает строку вида ``123/456`` -> (123, 456)."""
    if not text:
        return (0, 0)
    parts = re.split(r"[\/из]+", text, maxsplit=1)
    if len(parts) == 2:
        return (parse_int(parts[0]), parse_int(parts[1]))
    value = parse_int(text)
    return (value, value)


@dataclass
class Resource:
    """Текущее/максимальное значение ресурса (HP, MP, энергия)."""

    current: int = 0
    maximum: int = 0

    @property
    def percent(self) -> float:
        if self.maximum <= 0:
            return 0.0
        return round(self.current / self.maximum * 100.0, 1)


@dataclass
class InventoryItem:
    name: str
    count: int = 1


@dataclass
class CharacterStats:
    """Снимок состояния персонажа."""

    name: str = ""
    level: int = 0
    hp: Resource = field(default_factory=Resource)
    mp: Resource = field(default_factory=Resource)
    energy: Resource = field(default_factory=Resource)
    experience: int = 0
    gold: int = 0
    silver: int = 0
    inventory: List[InventoryItem] = field(default_factory=list)
    notifications: List[str] = field(default_factory=list)

    @property
    def hp_percent(self) -> float:
        return self.hp.percent

    @property
    def mp_percent(self) -> float:
        return self.mp.percent

    @property
    def is_alive(self) -> bool:
        return self.hp.current > 0

    def has_item(self, needle: str) -> bool:
        needle_low = needle.lower()
        return any(needle_low in item.name.lower() for item in self.inventory)

    def item_count(self, needle: str) -> int:
        needle_low = needle.lower()
        return sum(
            item.count for item in self.inventory if needle_low in item.name.lower()
        )


class StatsParser:
    """Считывает и агрегирует статистику персонажа."""

    def __init__(self, browser: BrowserManager) -> None:
        self._browser = browser
        self._sel = CONFIG.selectors

    async def read_stats(self, navigate: bool = True) -> CharacterStats:
        """Читает основные статы с главной страницы персонажа."""
        stats = CharacterStats()
        try:
            if navigate:
                await self._browser.goto(CONFIG.game.main_url)

            stats.name = await self._browser.query_text(self._sel.profile_name)
            stats.level = parse_int(
                await self._browser.query_text(self._sel.profile_level)
            )

            stats.hp = await self._read_resource(
                self._sel.hp_value, self._sel.hp_bar
            )
            stats.mp = await self._read_resource(
                self._sel.mp_value, self._sel.mp_bar
            )
            stats.energy = self._resource_from_text(
                await self._browser.query_text(self._sel.energy_value)
            )

            stats.experience = parse_int(
                await self._browser.query_text(self._sel.experience)
            )
            stats.gold = parse_int(
                await self._browser.query_text(self._sel.money_gold)
            )
            stats.silver = parse_int(
                await self._browser.query_text(self._sel.money_silver)
            )

            stats.notifications = await self.read_notifications(navigate=False)

            logger.info(
                "Статы: %s (ур.%d) HP %d/%d MP %d/%d золото=%d",
                stats.name or "?",
                stats.level,
                stats.hp.current,
                stats.hp.maximum,
                stats.mp.current,
                stats.mp.maximum,
                stats.gold,
            )
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "Ошибка чтения статов", exc)
            if CONFIG.runtime.screenshot_on_error:
                await self._browser.screenshot("stats_error")
        return stats

    async def _read_resource(self, value_selector: str, bar_selector: str) -> Resource:
        """Читает ресурс: сначала пробует текст значения, затем data-атрибуты бара."""
        text = await self._browser.query_text(value_selector)
        if text:
            resource = self._resource_from_text(text)
            if resource.maximum > 0:
                return resource

        # Фолбэк: пытаемся достать из атрибутов прогресс-бара.
        try:
            element = await self._browser.page.query_selector(bar_selector)
            if element is not None:
                for cur_attr, max_attr in (
                    ("data-current", "data-max"),
                    ("aria-valuenow", "aria-valuemax"),
                    ("value", "max"),
                ):
                    cur = await element.get_attribute(cur_attr)
                    mx = await element.get_attribute(max_attr)
                    if cur is not None and mx is not None:
                        return Resource(parse_int(cur), parse_int(mx))
        except Exception as exc:  # noqa: BLE001
            logger.debug("Не удалось прочитать ресурс из бара %s: %s", bar_selector, exc)

        return self._resource_from_text(text)

    @staticmethod
    def _resource_from_text(text: str) -> Resource:
        current, maximum = parse_current_max(text)
        return Resource(current=current, maximum=maximum)

    async def read_inventory(self, navigate: bool = True) -> List[InventoryItem]:
        """Читает содержимое рюкзака."""
        items: List[InventoryItem] = []
        try:
            if navigate:
                await self._browser.goto(CONFIG.game.inventory_url)

            container = await self._browser.wait_for_selector(
                self._sel.inventory_container, timeout_ms=6000
            )
            if container is None:
                logger.debug("Контейнер инвентаря не найден")
                return items

            elements = await self._browser.page.query_selector_all(
                self._sel.inventory_item
            )
            for element in elements:
                try:
                    name = await element.get_attribute(self._sel.item_name_attr)
                    if not name:
                        name = (await element.inner_text()).strip()
                    if not name:
                        continue
                    count = 1
                    count_el = await element.query_selector(self._sel.item_count)
                    if count_el is not None:
                        count = parse_int(await count_el.inner_text(), default=1) or 1
                    items.append(InventoryItem(name=name.strip(), count=count))
                except Exception:  # noqa: BLE001
                    continue

            logger.info("Инвентарь: %d предметов", len(items))
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "Ошибка чтения инвентаря", exc)
        return items

    async def read_notifications(self, navigate: bool = False) -> List[str]:
        """Читает список игровых уведомлений/системных сообщений."""
        notifications: List[str] = []
        try:
            if navigate:
                await self._browser.goto(CONFIG.game.main_url)
            container = await self._browser.page.query_selector(
                self._sel.notifications_container
            )
            if container is None:
                return notifications
            elements = await container.query_selector_all(
                self._sel.notification_item
            )
            for element in elements:
                try:
                    text = (await element.inner_text()).strip()
                    if text:
                        notifications.append(text)
                except Exception:  # noqa: BLE001
                    continue
            if notifications:
                logger.debug("Уведомлений: %d", len(notifications))
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "Ошибка чтения уведомлений", exc)
        return notifications

    async def full_snapshot(self) -> CharacterStats:
        """Полный снимок: статы + инвентарь + уведомления."""
        stats = await self.read_stats(navigate=True)
        stats.inventory = await self.read_inventory(navigate=True)
        return stats


__all__ = [
    "StatsParser",
    "CharacterStats",
    "Resource",
    "InventoryItem",
    "parse_int",
    "parse_current_max",
]
