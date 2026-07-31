"""Менеджер калдаунов и таймеров.

Отвечает за учёт периодических действий (восстановление энергии, профессии,
опрос квестов, обновление статов). Позволяет:
    * регистрировать именованные калдауны с интервалом;
    * проверять готовность действия (``is_ready``);
    * отмечать выполнение (``mark``);
    * узнавать оставшееся время (``remaining``);
    * синхронизировать калдауны с игровыми таймерами, распарсенными со страницы.

Класс не зависит от Playwright напрямую для базовой логики (её можно тестировать
изолированно), но умеет читать игровые таймеры через :class:`BrowserManager`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from ..config import CONFIG
from ..core.browser import BrowserManager
from ..logger import get_logger, log_exception
from .stats_parser import parse_int

logger = get_logger(__name__)


@dataclass
class Cooldown:
    """Один именованный калдаун."""

    name: str
    interval: float          # длительность калдауна в секундах
    last_run: float = 0.0    # unix-время последнего выполнения (0 = никогда)

    def is_ready(self, now: Optional[float] = None) -> bool:
        now = now if now is not None else time.time()
        return (now - self.last_run) >= self.interval

    def remaining(self, now: Optional[float] = None) -> float:
        now = now if now is not None else time.time()
        left = self.interval - (now - self.last_run)
        return max(0.0, left)

    def mark(self, now: Optional[float] = None) -> None:
        self.last_run = now if now is not None else time.time()


class TimersManager:
    """Управляет набором калдаунов и синхронизацией с игрой."""

    def __init__(self, browser: Optional[BrowserManager] = None) -> None:
        self._browser = browser
        self._sel = CONFIG.selectors
        self._cooldowns: Dict[str, Cooldown] = {}
        # Инициализируем калдауны из конфигурации.
        for name, interval in CONFIG.cooldowns.items():
            self.register(name, interval)

    # ------------------------------------------------------------------ #
    #  Управление калдаунами                                              #
    # ------------------------------------------------------------------ #
    def register(self, name: str, interval: float, ready_now: bool = True) -> Cooldown:
        """Регистрирует (или обновляет) калдаун.

        ``ready_now=True`` -> действие доступно немедленно при первом запуске.
        """
        last_run = 0.0 if ready_now else time.time()
        cooldown = Cooldown(name=name, interval=float(interval), last_run=last_run)
        self._cooldowns[name] = cooldown
        return cooldown

    def get(self, name: str) -> Optional[Cooldown]:
        return self._cooldowns.get(name)

    def is_ready(self, name: str) -> bool:
        cooldown = self._cooldowns.get(name)
        if cooldown is None:
            logger.debug("Запрос неизвестного калдауна: %s", name)
            return False
        return cooldown.is_ready()

    def mark(self, name: str) -> None:
        cooldown = self._cooldowns.get(name)
        if cooldown is None:
            cooldown = self.register(name, CONFIG.cooldowns.get(name, 60.0))
        cooldown.mark()
        logger.debug("Калдаун '%s' отмечен, следующий через %.0f сек", name, cooldown.interval)

    def remaining(self, name: str) -> float:
        cooldown = self._cooldowns.get(name)
        return cooldown.remaining() if cooldown else 0.0

    def set_remaining(self, name: str, seconds: float) -> None:
        """Устанавливает оставшееся время калдауна напрямую (синхронизация с игрой)."""
        cooldown = self._cooldowns.get(name)
        if cooldown is None:
            cooldown = self.register(name, max(seconds, 1.0))
        # last_run = now - (interval - seconds) => remaining == seconds
        cooldown.interval = max(cooldown.interval, seconds)
        cooldown.last_run = time.time() - (cooldown.interval - seconds)

    def ready_actions(self) -> List[str]:
        """Список имён калдаунов, готовых к выполнению прямо сейчас."""
        now = time.time()
        return [name for name, cd in self._cooldowns.items() if cd.is_ready(now)]

    def next_due(self) -> Tuple[Optional[str], float]:
        """Возвращает (имя, секунды) ближайшего готового/наиболее близкого калдауна."""
        if not self._cooldowns:
            return (None, 0.0)
        now = time.time()
        best_name: Optional[str] = None
        best_remaining = float("inf")
        for name, cd in self._cooldowns.items():
            rem = cd.remaining(now)
            if rem < best_remaining:
                best_remaining = rem
                best_name = name
        return (best_name, best_remaining if best_remaining != float("inf") else 0.0)

    def snapshot(self) -> Dict[str, float]:
        """Словарь {имя: оставшиеся секунды} для логов/диагностики."""
        return {name: round(cd.remaining(), 1) for name, cd in self._cooldowns.items()}

    # ------------------------------------------------------------------ #
    #  Синхронизация с игровыми таймерами                                 #
    # ------------------------------------------------------------------ #
    async def sync_from_page(self) -> Dict[str, float]:
        """Читает таймеры со страницы и синхронизирует известные калдауны.

        Ищет элементы ``timer_container`` и берёт из них имя/секунды по
        настроенным атрибутам. Возвращает словарь распарсенных таймеров.
        """
        parsed: Dict[str, float] = {}
        if self._browser is None:
            return parsed
        try:
            elements = await self._browser.page.query_selector_all(
                self._sel.timer_container
            )
            for element in elements:
                try:
                    name = await element.get_attribute(self._sel.timer_label_attr)
                    seconds_raw = await element.get_attribute(self._sel.timer_seconds_attr)
                    if seconds_raw is None:
                        # Фолбэк: парсим из текста вида «05:30».
                        seconds = _parse_clock(await element.inner_text())
                    else:
                        seconds = float(parse_int(seconds_raw))
                    if not name:
                        name = (await element.inner_text()).strip()[:32] or "timer"
                    if seconds > 0:
                        parsed[name] = seconds
                        # Синхронизируем, если такой калдаун зарегистрирован.
                        if name in self._cooldowns:
                            self.set_remaining(name, seconds)
                except Exception:  # noqa: BLE001
                    continue
            if parsed:
                logger.debug("Синхронизированы игровые таймеры: %s", parsed)
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "Ошибка синхронизации таймеров", exc)
        return parsed


def _parse_clock(text: str) -> float:
    """Парсит строку времени ``HH:MM:SS`` / ``MM:SS`` в секунды."""
    if not text:
        return 0.0
    parts = [p for p in text.strip().split(":") if p.strip().isdigit()]
    if not parts:
        return 0.0
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(seconds)


__all__ = ["TimersManager", "Cooldown"]
