"""Движок боёв: выбор ударов/блоков, лечение, касты и парсинг лога боя.

Логика раунда:
    1. Проверить, что мы в бою (наличие боевого контейнера).
    2. Оценить HP: при падении ниже порога — выпить эликсир/лечиться.
    3. При наличии маны и включённых спеллах — кастовать.
    4. Выбрать зоны атаки и блока (со случайностью для непредсказуемости).
    5. Подтвердить ход, дождаться обновления и распарсить лог.
    6. Повторять, пока бой не завершится или не превышен лимит раундов.

Все взаимодействия идут через :class:`HumanBehavior`, чтобы клики выглядели
естественно.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional

from ..config import CONFIG
from ..core.anti_bot import HumanBehavior
from ..core.browser import BrowserManager
from ..logger import get_logger, log_exception
from .stats_parser import parse_current_max

logger = get_logger(__name__)


class CombatResult(str, Enum):
    WIN = "win"
    LOSE = "lose"
    FLED = "fled"
    TIMEOUT = "timeout"
    NOT_IN_COMBAT = "not_in_combat"
    ERROR = "error"


@dataclass
class CombatReport:
    result: CombatResult
    rounds: int = 0
    log_lines: List[str] = field(default_factory=list)
    self_hp_end: int = 0
    enemy_hp_end: int = 0


class CombatEngine:
    """Управляет ходом боя от начала до завершения."""

    def __init__(self, browser: BrowserManager, human: HumanBehavior) -> None:
        self._browser = browser
        self._human = human
        self._sel = CONFIG.selectors
        self._cfg = CONFIG.combat

    # ------------------------------------------------------------------ #
    #  Проверки состояния                                                 #
    # ------------------------------------------------------------------ #
    async def in_combat(self) -> bool:
        """Проверяет, находится ли персонаж в бою."""
        return await self._browser.exists(self._sel.combat_container, timeout_ms=2000)

    async def _current_hp_percent(self) -> float:
        text = await self._browser.query_text(self._sel.self_hp_in_combat)
        current, maximum = parse_current_max(text)
        if maximum <= 0:
            return 100.0
        return current / maximum * 100.0

    async def _enemy_hp(self) -> int:
        text = await self._browser.query_text(self._sel.enemy_hp)
        current, _ = parse_current_max(text)
        return current

    # ------------------------------------------------------------------ #
    #  Основной цикл боя                                                  #
    # ------------------------------------------------------------------ #
    async def fight(self) -> CombatReport:
        """Проводит бой до завершения. Возвращает отчёт."""
        if not await self.in_combat():
            logger.debug("fight() вызван вне боя")
            return CombatReport(result=CombatResult.NOT_IN_COMBAT)

        logger.info("Начало боя")
        collected_log: List[str] = []
        rounds = 0

        try:
            while rounds < self._cfg.max_rounds:
                # Проверяем завершение боя.
                finished = await self._check_finished()
                if finished is not None:
                    collected_log.extend(await self._read_log())
                    report = CombatReport(
                        result=finished,
                        rounds=rounds,
                        log_lines=_dedupe(collected_log),
                        self_hp_end=int(await self._current_hp_percent()),
                        enemy_hp_end=await self._enemy_hp(),
                    )
                    logger.info(
                        "Бой завершён: %s за %d раундов", finished.value, rounds
                    )
                    return report

                if not await self.in_combat():
                    # Контейнер боя исчез без явного результата — считаем победой.
                    logger.info("Боевой контейнер исчез — вероятно, бой окончен")
                    collected_log.extend(await self._read_log())
                    return CombatReport(
                        result=CombatResult.WIN,
                        rounds=rounds,
                        log_lines=_dedupe(collected_log),
                    )

                rounds += 1
                logger.debug("Раунд %d", rounds)

                await self._maybe_heal()
                await self._maybe_cast_spell()
                await self._choose_and_submit_move()

                # Ждём обновления боя и собираем свежие строки лога.
                await self._human.sleep(1.0, 2.4)
                collected_log.extend(await self._read_log())

            logger.warning("Достигнут лимит раундов (%d)", self._cfg.max_rounds)
            return CombatReport(
                result=CombatResult.TIMEOUT,
                rounds=rounds,
                log_lines=_dedupe(collected_log),
            )
        except Exception as exc:  # noqa: BLE001
            log_exception(logger, "Ошибка в бою", exc)
            if CONFIG.runtime.screenshot_on_error:
                await self._browser.screenshot("combat_error")
            return CombatReport(
                result=CombatResult.ERROR,
                rounds=rounds,
                log_lines=_dedupe(collected_log),
            )

    async def _check_finished(self) -> Optional[CombatResult]:
        """Проверяет маркеры победы/поражения."""
        if await self._browser.exists(self._sel.combat_result_win, timeout_ms=800):
            return CombatResult.WIN
        if await self._browser.exists(self._sel.combat_result_lose, timeout_ms=800):
            return CombatResult.LOSE
        return None

    # ------------------------------------------------------------------ #
    #  Действия в бою                                                     #
    # ------------------------------------------------------------------ #
    async def _maybe_heal(self) -> None:
        """Использует эликсир/лечение, если HP ниже порога."""
        if not self._cfg.use_elixirs:
            return
        hp_percent = await self._current_hp_percent()
        if hp_percent >= self._cfg.heal_hp_percent:
            return
        logger.info("HP низкое (%.0f%%) — использую эликсир", hp_percent)
        used = await self._human.click(self._sel.elixir_button_prefix, timeout_ms=3000)
        if used:
            await self._human.action_pause()
        else:
            logger.debug("Кнопка эликсира не найдена")

    async def _maybe_cast_spell(self) -> None:
        """Кастует боевое заклинание, если хватает маны."""
        if not self._cfg.use_spells:
            return
        text = await self._browser.query_text(self._sel.mp_value)
        current, maximum = parse_current_max(text)
        if maximum > 0 and (current / maximum * 100.0) < self._cfg.min_mana_percent:
            logger.debug("Маны недостаточно для каста")
            return
        cast = await self._human.click(self._sel.spell_button_prefix, timeout_ms=2500)
        if cast:
            logger.info("Применено боевое заклинание")
            await self._human.action_pause()

    async def _choose_and_submit_move(self) -> None:
        """Выбирает зоны атаки/блока и подтверждает ход."""
        attack_zone = random.choice(self._cfg.attack_zones)
        block_zone = random.choice(self._cfg.block_zones)

        attack_selector = f"{self._sel.attack_zone_prefix}[value='{attack_zone}']"
        block_selector = f"{self._sel.block_zone_prefix}[value='{block_zone}']"

        attacked = await self._human.click(attack_selector, timeout_ms=2500)
        if not attacked:
            # Фолбэк: некоторые интерфейсы используют другой атрибут зоны.
            attacked = await self._human.click(
                f"{self._sel.attack_zone_prefix}[data-zone='{attack_zone}']",
                timeout_ms=1500,
            )
        await self._human.action_pause()

        blocked = await self._human.click(block_selector, timeout_ms=2500)
        if not blocked:
            await self._human.click(
                f"{self._sel.block_zone_prefix}[data-zone='{block_zone}']",
                timeout_ms=1500,
            )
        await self._human.action_pause()

        logger.debug("Ход: атака=%s блок=%s", attack_zone, block_zone)
        submitted = await self._human.click(self._sel.attack_submit, timeout_ms=4000)
        if not submitted:
            logger.warning("Не удалось подтвердить ход (кнопка атаки не найдена)")

    # ------------------------------------------------------------------ #
    #  Парсинг лога боя                                                   #
    # ------------------------------------------------------------------ #
    async def _read_log(self) -> List[str]:
        """Читает строки боевого лога."""
        try:
            container = await self._browser.page.query_selector(self._sel.combat_log)
            if container is None:
                return []
            rows = await container.query_selector_all(self._sel.combat_log_row)
            lines: List[str] = []
            for row in rows:
                try:
                    text = (await row.inner_text()).strip()
                    if text:
                        lines.append(text)
                except Exception:  # noqa: BLE001
                    continue
            return lines
        except Exception as exc:  # noqa: BLE001
            logger.debug("Ошибка чтения боевого лога: %s", exc)
            return []


def _dedupe(items: List[str]) -> List[str]:
    """Убирает подряд идущие дубликаты, сохраняя порядок."""
    result: List[str] = []
    seen = set()
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


__all__ = ["CombatEngine", "CombatReport", "CombatResult"]
