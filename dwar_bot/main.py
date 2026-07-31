"""Главный асинхронный цикл и оркестратор модулей бота Dwar.

Связывает воедино:
    * авторизацию по куки (:class:`CookieManager` + :class:`BrowserManager`);
    * периодическое чтение статов (:class:`StatsParser`);
    * автобой (:class:`CombatEngine`);
    * прохождение диалогов/квестов (:class:`QuestTracker`);
    * учёт калдаунов (:class:`TimersManager`);
    * human-like задержки (:class:`HumanBehavior`).

Цикл отказоустойчив: любое исключение в итерации логируется (с traceback и,
опционально, скриншотом), после чего бот выдерживает паузу и продолжает.
Поддерживается корректное завершение по SIGINT/SIGTERM.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Optional

from .auth.cookie_manager import CookieManager, NoSessionsAvailableError
from .config import CONFIG
from .core.anti_bot import HumanBehavior
from .core.browser import BrowserManager
from .logger import get_logger, log_exception
from .modules.combat_engine import CombatEngine, CombatResult
from .modules.quest_tracker import QuestTracker
from .modules.stats_parser import CharacterStats, StatsParser
from .modules.timers_manager import TimersManager

logger = get_logger(__name__)


class DwarBot:
    """Оркестратор всех модулей."""

    def __init__(self) -> None:
        self._cookie_manager = CookieManager()
        self._browser = BrowserManager(cookie_manager=self._cookie_manager)
        self._human = HumanBehavior()
        self._timers = TimersManager(self._browser)
        self._stats_parser: Optional[StatsParser] = None
        self._combat: Optional[CombatEngine] = None
        self._quests: Optional[QuestTracker] = None

        self._stop_event = asyncio.Event()
        self._last_stats: Optional[CharacterStats] = None
        # Счётчик последовательных сбоев для эскалации (ротация/пауза).
        self._consecutive_errors = 0

    # ------------------------------------------------------------------ #
    #  Инициализация                                                      #
    # ------------------------------------------------------------------ #
    async def setup(self) -> bool:
        """Запускает браузер, авторизуется и инициализирует модули."""
        await self._browser.start()
        self._human.bind(self._browser.page)

        self._stats_parser = StatsParser(self._browser)
        self._combat = CombatEngine(self._browser, self._human)
        self._quests = QuestTracker(self._browser, self._human)

        try:
            authorized = await self._browser.ensure_authorized()
        except NoSessionsAvailableError as exc:
            log_exception(logger, "Авторизация невозможна", exc)
            return False

        if not authorized:
            logger.error("Не удалось авторизоваться — проверьте файлы куки")
            return False

        await self._human.read_pause()
        return True

    # ------------------------------------------------------------------ #
    #  Основной цикл                                                      #
    # ------------------------------------------------------------------ #
    async def run(self) -> None:
        """Точка входа: setup + бесконечный цикл до сигнала остановки."""
        ok = await self.setup()
        if not ok:
            await self.shutdown()
            return

        logger.info("Бот запущен. Основной цикл активен.")
        try:
            while not self._stop_event.is_set():
                try:
                    await self._tick()
                    self._consecutive_errors = 0
                except Exception as exc:  # noqa: BLE001
                    await self._handle_tick_error(exc)

                # Пауза между итерациями + шанс длинного перерыва.
                await self._human.maybe_long_break()
                await self._interruptible_sleep_loop()
        finally:
            await self.shutdown()

    async def _tick(self) -> None:
        """Одна итерация оркестрации."""
        # 1. Реакция на бой — высший приоритет.
        if await self._combat.in_combat():
            report = await self._combat.fight()
            self._timers.mark("combat")
            logger.info(
                "Итог боя: %s (раундов: %d)", report.result.value, report.rounds
            )
            if report.result in (CombatResult.LOSE, CombatResult.ERROR):
                # После поражения/ошибки — читаем статы, чтобы решить о лечении.
                await self._refresh_stats(force=True)
            return

        # 2. Диалоги/квесты, если активны.
        if await self._quests.dialog_active():
            steps = await self._quests.run_dialog()
            if steps:
                self._timers.mark("quest_poll")
            return

        # 3. Периодическое обновление статов.
        if self._timers.is_ready("stats_refresh"):
            await self._refresh_stats()

        # 4. Синхронизация игровых таймеров и опрос квестов при готовности.
        await self._timers.sync_from_page()
        if self._timers.is_ready("quest_poll"):
            await self._poll_quests()

        # Небольшая фоновая активность мыши для естественности.
        await self._human.idle_wander()

    # ------------------------------------------------------------------ #
    #  Подзадачи                                                          #
    # ------------------------------------------------------------------ #
    async def _refresh_stats(self, force: bool = False) -> None:
        if self._stats_parser is None:
            return
        if not force and not self._timers.is_ready("stats_refresh"):
            return
        stats = await self._stats_parser.read_stats(navigate=True)
        self._last_stats = stats
        self._timers.mark("stats_refresh")

        # Предупреждение о низком HP уходит в Telegram (WARNING).
        if stats.hp.maximum > 0 and stats.hp_percent < CONFIG.combat.heal_hp_percent:
            logger.warning(
                "Низкое HP вне боя: %d/%d (%.0f%%)",
                stats.hp.current,
                stats.hp.maximum,
                stats.hp_percent,
            )
        await self._human.read_pause()

    async def _poll_quests(self) -> None:
        """Заходит на главную и, если есть диалог, проходит его."""
        if self._quests is None:
            return
        await self._browser.goto(CONFIG.game.main_url)
        await self._human.read_pause()
        if await self._quests.dialog_active():
            await self._quests.run_dialog()
        self._timers.mark("quest_poll")

    # ------------------------------------------------------------------ #
    #  Обработка ошибок и восстановление                                  #
    # ------------------------------------------------------------------ #
    async def _handle_tick_error(self, exc: BaseException) -> None:
        self._consecutive_errors += 1
        log_exception(
            logger,
            f"Ошибка в итерации цикла (подряд: {self._consecutive_errors})",
            exc,
        )
        if CONFIG.runtime.screenshot_on_error:
            await self._browser.screenshot("tick_error")

        # Проверяем, не разлогинило ли нас.
        try:
            if not await self._browser.is_logged_in():
                logger.warning("Похоже, сессия истекла — пробую ротацию куки")
                await self._recover_session()
        except Exception as inner:  # noqa: BLE001
            log_exception(logger, "Ошибка при проверке авторизации", inner)

        # Эскалация: при серии ошибок — увеличенная пауза.
        if self._consecutive_errors >= CONFIG.runtime.max_retries:
            backoff = CONFIG.runtime.retry_backoff_base * self._consecutive_errors
            logger.warning("Серия ошибок — пауза %.0f сек", backoff)
            await asyncio.sleep(backoff)

    async def _recover_session(self) -> None:
        """Пробует восстановить авторизацию ротацией сессий."""
        try:
            authorized = await self._browser.ensure_authorized()
            if authorized:
                logger.info("Сессия восстановлена ротацией куки")
                self._consecutive_errors = 0
            else:
                logger.error("Ротация не помогла — валидных сессий нет")
                self._stop_event.set()
        except NoSessionsAvailableError:
            logger.error("Закончились валидные сессии — останавливаюсь")
            self._stop_event.set()

    async def _interruptible_sleep_loop(self) -> None:
        """Пауза между итерациями, прерываемая сигналом остановки."""
        delay = await self._pick_loop_delay()
        try:
            await asyncio.wait_for(self._stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    async def _pick_loop_delay(self) -> float:
        """Выбирает задержку цикла с учётом ближайшего калдауна."""
        import random

        base = random.uniform(CONFIG.delays.loop_min, CONFIG.delays.loop_max)
        name, remaining = self._timers.next_due()
        # Не спим дольше, чем до ближайшего важного калдауна (в разумных пределах).
        if name and 0 < remaining < base:
            return max(remaining, CONFIG.delays.loop_min * 0.5)
        return base

    # ------------------------------------------------------------------ #
    #  Завершение                                                         #
    # ------------------------------------------------------------------ #
    def request_stop(self) -> None:
        logger.info("Получен сигнал остановки")
        self._stop_event.set()

    async def shutdown(self) -> None:
        logger.info("Завершение работы бота…")
        await self._browser.close()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, bot: DwarBot) -> None:
    """Устанавливает обработчики SIGINT/SIGTERM для корректной остановки."""
    for sig_name in ("SIGINT", "SIGTERM"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, bot.request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows / окружения без поддержки — полагаемся на KeyboardInterrupt.
            pass


async def async_main() -> None:
    bot = DwarBot()
    loop = asyncio.get_running_loop()
    _install_signal_handlers(loop, bot)
    await bot.run()


def main() -> None:
    """Синхронная точка входа (``python -m dwar_bot.main``)."""
    try:
        asyncio.run(async_main())
    except KeyboardInterrupt:
        logger.info("Прервано пользователем (KeyboardInterrupt)")


if __name__ == "__main__":
    main()
