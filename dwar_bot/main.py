"""
Главный оркестратор бота «Легенда: Наследие Драконов».

Инициализирует браузер и cookie-сессию, крутит асинхронный цикл:
статы → бой → лечение → квесты/задачи → антибот-паузы.
Корректно завершается по Ctrl+C с сохранением cookie и закрытием Playwright.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from dwar_bot.config import DATA_DIR, BotConfig, config, load_config
from dwar_bot.auth.cookie_manager import CookieManager, SessionRotationError
from dwar_bot.core.anti_bot import AntiBot
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError
from dwar_bot.logger import (
    get_logger,
    log_exception,
    notify_telegram,
    setup_logging,
    start_telegram_notifier,
    stop_telegram_notifier,
)
from dwar_bot.modules.combat_engine import CombatEngine
from dwar_bot.modules.quest_tracker import QuestTracker
from dwar_bot.modules.stats_parser import BackpackItem, PlayerStats, StatsParser
from dwar_bot.modules.timers_manager import (
    TIMER_ANTI_BOT,
    TIMER_IDLE_PAUSE,
    TIMER_POTION,
    TIMER_QUEST_STEP,
    TimersManager,
)

STATE_FILE = DATA_DIR / "runtime_state.json"
DEFAULT_HP_HEAL_PCT = 55.0
DEFAULT_HP_CRITICAL_PCT = 35.0


class BotOrchestrator:
    """Связывает все модули в единый рабочий цикл."""

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        quest_script: Optional[List[dict]] = None,
        hp_heal_pct: float = DEFAULT_HP_HEAL_PCT,
        hp_critical_pct: float = DEFAULT_HP_CRITICAL_PCT,
    ) -> None:
        self.config = bot_config or config
        self.quest_script = list(quest_script or [])
        self.hp_heal_pct = hp_heal_pct
        self.hp_critical_pct = hp_critical_pct

        self.logger = get_logger("dwar_bot.main")
        self.cookies = CookieManager(self.config)
        self.browser = BrowserEngine(self.config, cookie_manager=self.cookies)
        self.stats_parser = StatsParser(self.config)
        self.timers = TimersManager(self.config)
        self.combat = CombatEngine(
            self.config,
            browser=self.browser,
            stats_parser=self.stats_parser,
        )
        self.quests = QuestTracker(
            self.config,
            browser=self.browser,
            combat_engine=self.combat,
            stats_parser=self.stats_parser,
        )
        self.anti_bot = AntiBot(self.config, browser=self.browser)

        self._stop_event = asyncio.Event()
        self._started = False
        self._consecutive_errors = 0
        self._quest_queue: List[dict] = list(self.quest_script)
        self._last_stats: Optional[PlayerStats] = None
        self._last_backpack: List[BackpackItem] = []
        self._fights_won = 0
        self._fights_lost = 0
        self._loops = 0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        self.config.ensure_directories()
        self._restore_runtime_state()

        await start_telegram_notifier()
        self.logger.info(
            "Старт бота: server=%s headless=%s cookies=%s",
            self.config.server.server,
            self.config.browser.headless,
            self.config.cookies.cookies_file,
        )

        try:
            await self.cookies.initialize(validate=True)
        except SessionRotationError as exc:
            self.logger.warning(
                "Валидация cookie не прошла (%s) — пробуем без validate",
                exc,
            )
            await self.cookies.initialize(validate=False)

        await self.browser.start(apply_cookies=True)
        await self.browser.open_game()
        self._started = True
        await notify_telegram(
            f"Бот запущен (server={self.config.server.server})",
            critical=False,
        )

    async def stop(self) -> None:
        """Graceful shutdown: состояние, cookie, браузер, telegram."""
        self.logger.info("Остановка бота (graceful shutdown)...")
        self._stop_event.set()

        try:
            self._save_runtime_state()
        except Exception as exc:
            log_exception(self.logger, "Не удалось сохранить runtime state", exc)

        try:
            if self.browser.is_started:
                await self.cookies.sync_from_playwright(self.browser.context)
                self.cookies.save_cookies()
                self.logger.info("Cookie экспортированы в %s", self.cookies.cookies_file)
        except Exception as exc:
            log_exception(self.logger, "Ошибка сохранения cookie", exc)

        try:
            await self.browser.stop()
        except Exception as exc:
            log_exception(self.logger, "Ошибка закрытия браузера", exc)

        try:
            await self.cookies.close()
        except Exception as exc:
            log_exception(self.logger, "Ошибка закрытия CookieManager", exc)

        try:
            await notify_telegram(
                f"Бот остановлен. loops={self._loops} "
                f"wins={self._fights_won} losses={self._fights_lost}",
                critical=False,
            )
        except Exception:
            pass

        await stop_telegram_notifier()
        self._started = False
        self.logger.info("Бот полностью остановлен")

    def request_stop(self) -> None:
        self.logger.warning("Получен сигнал остановки")
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def main_loop(self) -> None:
        """Основной асинхронный цикл оркестратора."""
        if not self._started:
            await self.start()

        self.logger.info("Вход в main_loop")
        while not self._stop_event.is_set():
            loop_started = time.monotonic()
            self._loops += 1
            in_combat = False
            hp_critical = False
            has_tasks = bool(self._quest_queue)

            try:
                page = self.browser.page

                # 0) Антибот / капча
                if self.timers.is_ready(TIMER_ANTI_BOT):
                    challenged = await self.anti_bot.handle_challenge(page)
                    if challenged:
                        self.timers.set_cooldown(TIMER_ANTI_BOT, 30.0)
                        await notify_telegram(
                            "Капча/антибот! Требуется ручное вмешательство.",
                            critical=True,
                        )
                        await asyncio.sleep(
                            self.timers.optimal_sleep(in_combat=False, hp_critical=True)
                        )
                        continue
                    self.timers.set_cooldown(TIMER_ANTI_BOT, random.uniform(8.0, 15.0))

                # 1) Сканирование состояния
                stats = await self.stats_parser.parse_player_stats(page)
                self._last_stats = stats
                in_combat = bool(stats.in_combat)
                hp_pct = stats.hp_ratio * 100.0
                hp_critical = hp_pct > 0 and hp_pct < self.hp_critical_pct

                self.logger.info(
                    "Состояние: HP %.0f%% (%s/%s) combat=%s loc=%s stale=%s | %s",
                    hp_pct,
                    stats.hp_current,
                    stats.hp_max,
                    in_combat,
                    stats.location or "?",
                    stats.stale,
                    self.timers.status_line(),
                )

                # Детект смерти
                if stats.hp_max > 0 and stats.hp_current <= 0:
                    self.logger.critical("Персонаж погиб (HP=0)!")
                    await notify_telegram(
                        f"Смерть персонажа! loc={stats.location}",
                        critical=True,
                    )
                    self.timers.set_cooldown(TIMER_IDLE_PAUSE, random.uniform(20.0, 40.0))
                    await asyncio.sleep(self.timers.remaining(TIMER_IDLE_PAUSE) or 20.0)
                    continue

                # 2) Приоритет: бой
                combat_state = await self.combat.parse_combat_state(page)
                if stats.in_combat or combat_state.in_combat:
                    in_combat = True
                    self.logger.info(
                        "Бой обнаружен (%s) — CombatEngine.process_fight",
                        combat_state.enemy_name or "?",
                    )
                    self.combat.reset_combo()
                    won = await self.combat.process_fight(
                        page,
                        target_combo=None,
                        hp_threshold_pct=self.hp_critical_pct,
                    )
                    if won:
                        self._fights_won += 1
                        self.logger.info("Бой выигран (всего побед: %s)", self._fights_won)
                    else:
                        self._fights_lost += 1
                        self.logger.warning(
                            "Бой проигран/сбой (поражений: %s)", self._fights_lost
                        )
                        await notify_telegram(
                            f"Бой завершился неудачей. losses={self._fights_lost}",
                            critical=True,
                        )
                    self._consecutive_errors = 0
                    await asyncio.sleep(
                        self.timers.optimal_sleep(in_combat=False, has_pending_tasks=has_tasks)
                    )
                    continue

                # 3) Лечение вне боя
                if stats.hp_max > 0 and hp_pct < self.hp_heal_pct:
                    healed = await self._heal_out_of_combat(stats)
                    if not healed:
                        # Ждём естественного восстановления
                        wait = self.timers.optimal_sleep(hp_critical=hp_critical)
                        self.logger.info(
                            "HP %.0f%% — ожидание восстановления %.1f сек",
                            hp_pct,
                            wait,
                        )
                        await asyncio.sleep(wait)
                        continue

                # 4) Задачи: квесты / перемещения
                if self._quest_queue and self.timers.is_ready(TIMER_QUEST_STEP):
                    step = self._quest_queue[0]
                    self.logger.info("Выполнение квест-шага: %s", step)
                    ok = await self.quests.execute_quest_sequence(page, [step])
                    if ok:
                        self._quest_queue.pop(0)
                        self.logger.info(
                            "Шаг выполнен, осталось в очереди: %s",
                            len(self._quest_queue),
                        )
                    else:
                        self.logger.error("Шаг квеста провален — откладываем")
                        # Переставляем в конец, чтобы не зациклиться
                        failed = self._quest_queue.pop(0)
                        self._quest_queue.append(failed)
                    self.timers.set_cooldown(
                        TIMER_QUEST_STEP, random.uniform(2.0, 5.0)
                    )
                elif not self._quest_queue and self._loops % 20 == 0:
                    self.logger.debug("Очередь квестов пуста — idle")

                # 5) Случайные паузы / антибот
                await self.anti_bot.tick(page)

                self._consecutive_errors = 0

            except asyncio.CancelledError:
                self.logger.info("main_loop cancelled")
                raise
            except BrowserEngineError as exc:
                self._consecutive_errors += 1
                log_exception(self.logger, "BrowserEngineError в main_loop", exc)
                await self._on_loop_error(exc)
            except Exception as exc:
                self._consecutive_errors += 1
                log_exception(self.logger, "Ошибка в main_loop", exc)
                await self._on_loop_error(exc)

            if self._consecutive_errors >= self.config.max_consecutive_errors:
                self.logger.critical(
                    "Слишком много ошибок подряд (%s) — остановка",
                    self._consecutive_errors,
                )
                await notify_telegram(
                    f"Аварийная остановка: {self._consecutive_errors} ошибок подряд",
                    critical=True,
                )
                self._stop_event.set()
                break

            # Оптимальный sleep до следующей итерации
            sleep_for = self.timers.optimal_sleep(
                has_pending_tasks=bool(self._quest_queue),
                in_combat=in_combat,
                hp_critical=hp_critical,
            )
            # Учитываем уже потраченное время итерации
            elapsed = time.monotonic() - loop_started
            sleep_for = max(0.2, sleep_for - elapsed * 0.25)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_for)
            except asyncio.TimeoutError:
                pass

        self.logger.info("Выход из main_loop")

    async def _heal_out_of_combat(self, stats: PlayerStats) -> bool:
        """Пьёт банку из рюкзака, если кулдаун позволяет."""
        if not self.timers.is_ready(TIMER_POTION):
            self.logger.info(
                "Лечение на cooldown ещё %.1f сек",
                self.timers.remaining(TIMER_POTION),
            )
            return False

        page = self.browser.page
        try:
            backpack = await self.stats_parser.parse_backpack(page)
            self._last_backpack = backpack
        except Exception as exc:
            self.logger.warning("Не удалось прочитать рюкзак: %s", exc)
            backpack = self._last_backpack

        used = await self.combat.use_potion_if_needed(
            page,
            stats,
            backpack,
            hp_threshold_pct=self.hp_heal_pct,
        )
        if used:
            self.timers.set_cooldown(TIMER_POTION, self.combat.potion_cooldown_sec)
            self.logger.info("Эликсир использован вне боя")
            return True
        return False

    async def _on_loop_error(self, exc: BaseException) -> None:
        backoff = min(30.0, 1.5 * self._consecutive_errors)
        backoff *= random.uniform(0.8, 1.3)
        self.logger.warning(
            "Пауза после ошибки %.1f сек (err#%s): %s",
            backoff,
            self._consecutive_errors,
            exc,
        )
        try:
            await self.browser.capture_error_screenshot(prefix="main_loop_error")
        except Exception:
            pass
        await asyncio.sleep(backoff)

    # ------------------------------------------------------------------
    # Runtime state
    # ------------------------------------------------------------------

    def _save_runtime_state(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        payload: Dict[str, Any] = {
            "saved_at": time.time(),
            "loops": self._loops,
            "fights_won": self._fights_won,
            "fights_lost": self._fights_lost,
            "quest_queue": self._quest_queue,
            "timers": self.timers.snapshot(),
            "last_location": (
                self._last_stats.location if self._last_stats else ""
            ),
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(STATE_FILE)
        self.logger.info("Runtime state сохранён: %s", STATE_FILE)

    def _restore_runtime_state(self) -> None:
        if not STATE_FILE.is_file():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if not self._quest_queue and isinstance(data.get("quest_queue"), list):
                self._quest_queue = list(data["quest_queue"])
            self._fights_won = int(data.get("fights_won") or 0)
            self._fights_lost = int(data.get("fights_lost") or 0)
            if isinstance(data.get("timers"), list):
                self.timers.restore(data["timers"])
            self.logger.info(
                "Runtime state восстановлен: queue=%s wins=%s",
                len(self._quest_queue),
                self._fights_won,
            )
        except Exception as exc:
            self.logger.warning("Не удалось восстановить state: %s", exc)


def _load_quest_script(path: Optional[str]) -> List[dict]:
    if not path:
        return []
    file_path = Path(path).expanduser().resolve()
    if not file_path.is_file():
        raise FileNotFoundError(f"Файл квест-сценария не найден: {file_path}")
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "steps" in data:
        data = data["steps"]
    if not isinstance(data, list):
        raise ValueError("Квест-сценарий должен быть JSON-списком шагов")
    return [step for step in data if isinstance(step, dict)]


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dwar bot — Легенда: Наследие Драконов"
    )
    parser.add_argument(
        "--quest-script",
        type=str,
        default="",
        help="Путь к JSON со списком шагов квеста",
    )
    parser.add_argument(
        "--hp-heal",
        type=float,
        default=DEFAULT_HP_HEAL_PCT,
        help="Порог HP%% для лечения вне боя",
    )
    parser.add_argument(
        "--hp-critical",
        type=float,
        default=DEFAULT_HP_CRITICAL_PCT,
        help="Критический порог HP%% в бою",
    )
    parser.add_argument(
        "--server",
        type=str,
        choices=("w1", "w2"),
        default="",
        help="Игровой сервер (переопределяет DWAR_SERVER)",
    )
    return parser.parse_args(argv)


async def async_main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    if args.server:
        import os

        os.environ["DWAR_SERVER"] = args.server

    bot_config = load_config()
    setup_logging(bot_config)
    logger = get_logger("dwar_bot.main")

    try:
        quest_script = _load_quest_script(args.quest_script or None)
    except Exception as exc:
        logger.error("Не удалось загрузить квест-сценарий: %s", exc)
        return 2

    orchestrator = BotOrchestrator(
        bot_config,
        quest_script=quest_script,
        hp_heal_pct=args.hp_heal,
        hp_critical_pct=args.hp_critical,
    )

    loop = asyncio.get_running_loop()

    def _signal_handler() -> None:
        orchestrator.request_stop()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            # Windows
            signal.signal(sig, lambda *_: orchestrator.request_stop())

    exit_code = 0
    try:
        await orchestrator.start()
        await orchestrator.main_loop()
    except KeyboardInterrupt:
        logger.warning("KeyboardInterrupt — graceful shutdown")
        orchestrator.request_stop()
    except Exception as exc:
        log_exception(logger, "Фатальная ошибка", exc)
        exit_code = 1
    finally:
        try:
            await asyncio.wait_for(
                orchestrator.stop(),
                timeout=bot_config.graceful_shutdown_timeout_sec,
            )
        except asyncio.TimeoutError:
            logger.error("Таймаут graceful shutdown")
            exit_code = exit_code or 1
        except Exception as exc:
            log_exception(logger, "Ошибка при stop()", exc)
            exit_code = exit_code or 1

    return exit_code


def main() -> None:
    try:
        code = asyncio.run(async_main())
    except KeyboardInterrupt:
        # Двойной Ctrl+C
        print("\nInterrupted", file=sys.stderr)
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
