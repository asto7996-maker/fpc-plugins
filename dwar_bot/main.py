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
import os
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
from dwar_bot.core.recovery import CrashRecoveryManager
from dwar_bot.core.self_diagnostics import SelfDiagnostics
from dwar_bot.core.telegram_bot import RemoteControlState, TelegramRemoteControl
from dwar_bot.logger import (
    get_logger,
    log_exception,
    notify_telegram,
    setup_logging,
    start_telegram_notifier,
    stop_telegram_notifier,
)
from dwar_bot.modules.analytics_reporter import (
    EVENT_AUCTION_BUY,
    EVENT_AUCTION_SELL,
    EVENT_BATTLE_LOST,
    EVENT_BATTLE_WON,
    EVENT_CAPTCHA,
    EVENT_DOWNTIME,
    EVENT_POTION_USED,
    EVENT_RESOURCE_FARMED,
    AnalyticsReporter,
)
from dwar_bot.modules.background_scheduler import BackgroundScheduler
from dwar_bot.modules.combat_engine import CombatEngine
from dwar_bot.modules.profession_farm import FarmStats, ProfessionFarm
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
        self.profession_farm = ProfessionFarm(
            self.config,
            browser=self.browser,
            human=self.browser.human,
            stats_parser=self.stats_parser,
            combat_engine=self.combat,
            timers=self.timers,
        )

        # Удалённое управление Telegram (флаги shared с main_loop)
        self.remote_state = RemoteControlState()
        self.telegram = TelegramRemoteControl(
            self.config,
            state=self.remote_state,
            stats_provider=self.build_status_report,
            screenshot_provider=self.take_remote_screenshot,
        )
        self.recovery = CrashRecoveryManager(self.config)

        # Самодиагностика / crash dumps
        self.diagnostics = SelfDiagnostics(self.config, telegram=self.telegram)
        self.recovery.bind_diagnostics(self.diagnostics)

        # KPI / периодические отчёты (SQLite data/analytics.db)
        report_hours = float(os.getenv("DWAR_REPORT_INTERVAL_HOURS", "12") or "12")
        self.analytics = AnalyticsReporter(
            self.config,
            report_interval_hours=report_hours,
        )

        # Фоновая рутина: почта, daily, бафы, cleanup
        self.scheduler = BackgroundScheduler(
            self.config,
            browser=self.browser,
            human=self.browser.human,
            analytics=self.analytics,
            telegram=self.telegram,
        )
        self.scheduler.register_default_tasks()

        self._stop_event = asyncio.Event()
        self._started = False
        self._consecutive_errors = 0
        self._quest_queue: List[dict] = list(self.quest_script)
        self._last_stats: Optional[PlayerStats] = None
        self._last_backpack: List[BackpackItem] = []
        self._fights_won = 0
        self._fights_lost = 0
        self._loops = 0
        self._current_task: str = "idle"
        self._pause_started_at: Optional[float] = None
        self._in_combat_flag = False

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

        # Console listener + telegram для crash-репортов
        try:
            self.diagnostics.bind_telegram(self.telegram)
            self.diagnostics.attach_page(self.browser.page)
            self.diagnostics.cleanup_old_dumps(max_age_days=7, max_folder_size_mb=500)
        except Exception as exc:
            log_exception(self.logger, "SelfDiagnostics attach failed", exc)

        # Привязка провайдеров после старта браузера + фоновый Telegram
        self.telegram.bind_providers(
            stats_provider=self.build_status_report,
            screenshot_provider=self.take_remote_screenshot,
        )
        await self.telegram.start()

        # Фоновая рассылка KPI (каждые 12ч по умолчанию; DWAR_REPORT_INTERVAL_HOURS)
        try:
            await self.analytics.start_scheduler(self.telegram)
        except Exception as exc:
            log_exception(self.logger, "Не удалось запустить AnalyticsReporter", exc)

        # Фоновый планировщик рутины (почта / daily / бафы / cleanup)
        try:
            self.scheduler.bind_services(
                analytics=self.analytics,
                telegram=self.telegram,
                browser=self.browser,
            )
            await self.scheduler.start_background(
                self.browser.page,
                self._last_stats or PlayerStats(),
                in_combat_flag=False,
            )
            self.logger.info("BackgroundScheduler запущен: %s", self.scheduler.status_summary)
        except Exception as exc:
            log_exception(self.logger, "Не удалось запустить BackgroundScheduler", exc)

        await notify_telegram(
            f"Бот запущен (server={self.config.server.server})",
            critical=False,
        )
        await self.telegram.send_alert(
            f"🐉 DwarBot запущен (server={self.config.server.server})"
        )

    async def stop(self) -> None:
        """Graceful shutdown: состояние, cookie, браузер, telegram."""
        self.logger.info("Остановка бота (graceful shutdown)...")
        self._stop_event.set()
        self.remote_state.request_stop()

        try:
            await self.telegram.send_alert(
                f"⏹ Остановка бота. loops={self._loops} "
                f"wins={self._fights_won} losses={self._fights_lost}"
            )
        except Exception:
            pass

        try:
            await self.scheduler.stop()
        except Exception as exc:
            log_exception(self.logger, "Ошибка остановки BackgroundScheduler", exc)

        try:
            await self.analytics.stop_scheduler()
            # Финальный отчёт + закрытие сессии аналитики
            try:
                await self.analytics.send_report_now(self.telegram, timeframe_hours=24)
            except Exception:
                pass
            self.analytics.close()
        except Exception as exc:
            log_exception(self.logger, "Ошибка остановки AnalyticsReporter", exc)

        try:
            await self.telegram.stop()
        except Exception as exc:
            log_exception(self.logger, "Ошибка остановки TelegramRemoteControl", exc)

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
        self.remote_state.request_stop()

    def request_pause(self) -> None:
        self.remote_state.pause()
        self._current_task = "paused"
        self.remote_state.current_task = "paused"
        self._pause_started_at = time.monotonic()

    def request_resume(self) -> None:
        if self._pause_started_at is not None:
            downtime = max(0.0, time.monotonic() - self._pause_started_at)
            self.analytics.track_event(
                EVENT_DOWNTIME,
                {"seconds": downtime, "reason": "telegram_pause"},
            )
            self._pause_started_at = None
        self.remote_state.resume()
        if self._current_task == "paused":
            self._current_task = "idle"
            self.remote_state.current_task = "idle"

    # ------------------------------------------------------------------
    # Analytics helpers (фарм / аукцион / ручные вызовы)
    # ------------------------------------------------------------------

    def track_resource_farmed(self, name: str, count: int = 1) -> None:
        """Пример: после успешного сбора в ProfessionFarm."""
        self.analytics.track_event(
            EVENT_RESOURCE_FARMED,
            {"name": name, "count": max(1, int(count))},
        )

    def track_auction_buy(self, item_name: str, spent_gold: float) -> None:
        """Пример: после AuctionTrader.buy_underpriced_items."""
        self.analytics.track_event(
            EVENT_AUCTION_BUY,
            {"name": item_name, "gold": float(spent_gold)},
        )

    def track_auction_sell(self, item_name: str, earned_gold: float) -> None:
        """Пример: после AuctionTrader.post_item_for_sale / продажи."""
        self.analytics.track_event(
            EVENT_AUCTION_SELL,
            {"name": item_name, "gold": float(earned_gold)},
        )

    # ------------------------------------------------------------------
    # Telegram providers
    # ------------------------------------------------------------------

    async def build_status_report(self) -> str:
        """Текст для /stats."""
        stats = self._last_stats
        if self.browser.is_started:
            try:
                stats = await self.stats_parser.parse_player_stats(self.browser.page)
                self._last_stats = stats
            except Exception as exc:
                self.logger.debug("build_status_report parse: %s", exc)

        farm: FarmStats = self.profession_farm.stats
        snap = self.analytics.snapshot_session()
        lines = [
            f"Loops: {self._loops}",
            f"Бои: wins={self._fights_won} losses={self._fights_lost}",
            (
                f"KPI сессии: {snap.gold_earned:+.2f}з "
                f"({snap.gold_per_hour:+.2f}з/ч) WR={snap.winrate_pct:.0f}%"
            ),
            f"Scheduler: {self.scheduler.status_summary}",
            f"Очередь квестов: {len(self._quest_queue)}",
            f"Задача: {self._current_task}",
        ]
        if stats is not None:
            lines.extend(
                [
                    f"HP: {stats.hp_current}/{stats.hp_max} ({stats.hp_ratio*100:.0f}%)",
                    f"MP: {stats.mp_current}/{stats.mp_max}",
                    f"Золото: {stats.gold}з {stats.silver}с {stats.copper}м",
                    f"Бой: {'да' if stats.in_combat else 'нет'}",
                    f"Локация: {stats.location or '?'}",
                    f"Ник: {stats.nickname or '?'} lvl={stats.level}",
                ]
            )
        else:
            lines.append("Статы: ещё не считаны")

        lines.append(f"Фарм: {farm.summary()}")
        self.remote_state.farm_summary = farm.summary()
        self.remote_state.current_task = self._current_task
        return "\n".join(lines)

    async def take_remote_screenshot(self) -> Optional[Path]:
        """Скриншот для /screenshot."""
        if not self.browser.is_started:
            return None
        from datetime import datetime, timezone

        from dwar_bot.config import SCREENSHOTS_DIR

        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path = SCREENSHOTS_DIR / f"telegram_{stamp}.png"
        try:
            await self.browser.page.screenshot(path=str(path), full_page=True)
            return path
        except Exception as exc:
            self.logger.error("take_remote_screenshot: %s", exc, exc_info=True)
            try:
                return await self.browser.capture_error_screenshot(prefix="telegram")
            except Exception:
                return None

    async def main_loop(self) -> None:
        """
        Основной асинхронный цикл оркестратора.

        Обёрнут в CrashRecoveryManager: health-check каждую итерацию,
        авто-рестарт Playwright-сессии при зависании, safe_execute для
        критических парсеров. Позволяет работать неделями без ручного рестарта.
        """
        if not self._started:
            await self.start()

        self.logger.info("Вход в main_loop (с CrashRecovery)")
        while not self._stop_event.is_set() and not self.remote_state.should_stop:
            loop_started = time.monotonic()
            self._loops += 1
            in_combat = False
            hp_critical = False
            has_tasks = bool(self._quest_queue)

            try:
                # Telegram remote: /stop
                if self.remote_state.should_stop:
                    self.logger.warning("Остановка по команде Telegram /stop")
                    self._stop_event.set()
                    break

                # Telegram remote: /pause — ждём /resume в начале каждой итерации
                if self.remote_state.is_paused:
                    self._current_task = "paused"
                    self.remote_state.current_task = "paused"
                    self.logger.info(
                        "Главный цикл на паузе (Telegram /pause) — ожидание /resume"
                    )
                    await self.remote_state.wait_if_paused(poll_sec=1.5)
                    if self.remote_state.should_stop or self._stop_event.is_set():
                        self._stop_event.set()
                        break
                    self._current_task = "idle"
                    self.remote_state.current_task = "idle"
                    continue

                # --- Recovery: health-check / auto-restart ---
                healthy = await self.recovery.safe_execute(
                    self.recovery.ensure_healthy,
                    self.browser,
                    self.cookies,
                    max_retries=2,
                    base_delay=1.5,
                )
                if not healthy:
                    self.logger.error(
                        "Сессия нездорова после recovery — пауза и повтор"
                    )
                    await self.telegram.send_alert(
                        "⚠ Recovery: сессия не восстановилась, повторная попытка..."
                    )
                    await asyncio.sleep(random.uniform(5.0, 12.0))
                    continue

                page = self.browser.page
                self.remote_state.current_task = self._current_task
                self.remote_state.farm_summary = self.profession_farm.stats.summary()

                # 0) Антибот / капча
                if self.anti_bot.is_paused:
                    self.logger.critical(
                        "Главный цикл на паузе из-за капчи — ждём manual override"
                    )
                    captcha_wait_started = time.monotonic()
                    await self.telegram.send_alert(
                        "ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО: Капча! Пройдите проверку.",
                        photo_path=None,
                    )
                    await self.anti_bot.captcha.wait_until_resumed()
                    self.analytics.track_event(
                        EVENT_CAPTCHA,
                        {
                            "downtime_seconds": max(
                                0.0, time.monotonic() - captcha_wait_started
                            ),
                            "source": "paused_flag",
                        },
                    )
                    continue

                if self.timers.is_ready(TIMER_ANTI_BOT):
                    challenged = await self.anti_bot.handle_challenge(page)
                    if challenged:
                        self.timers.set_cooldown(TIMER_ANTI_BOT, 30.0)
                        self.analytics.track_event(
                            EVENT_CAPTCHA, {"source": "handle_challenge"}
                        )
                        await notify_telegram(
                            "ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО: Появилась капча!",
                            critical=True,
                        )
                        await self.telegram.send_alert(
                            "ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО: Появилась капча!",
                        )
                        if self.anti_bot.is_paused:
                            captcha_wait_started = time.monotonic()
                            await self.anti_bot.captcha.wait_until_resumed(
                                poll_sec=3.0
                            )
                            self.analytics.track_event(
                                EVENT_DOWNTIME,
                                {
                                    "seconds": max(
                                        0.0,
                                        time.monotonic() - captcha_wait_started,
                                    ),
                                    "reason": "captcha",
                                },
                            )
                        continue
                    self.timers.set_cooldown(TIMER_ANTI_BOT, random.uniform(8.0, 15.0))

                # 1) Сканирование состояния (через safe_execute)
                self._current_task = "scan_stats"
                self.remote_state.current_task = self._current_task
                stats = await self.recovery.safe_execute(
                    self.stats_parser.parse_player_stats,
                    page,
                    max_retries=3,
                    base_delay=1.0,
                    on_retry=self._recovery_on_retry,
                )
                self._last_stats = stats
                in_combat = bool(stats.in_combat)
                self._in_combat_flag = in_combat
                # Контекст для фонового планировщика (почта/daily не в бою)
                self.scheduler.set_context(page, stats, in_combat)
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
                    self._in_combat_flag = True
                    self.scheduler.set_in_combat(True)
                    self._current_task = "combat"
                    self.remote_state.current_task = "combat"
                    self.logger.info(
                        "Бой обнаружен (%s) — CombatEngine.process_fight",
                        combat_state.enemy_name or "?",
                    )
                    await self.telegram.send_alert(
                        f"⚔ Нападение: {combat_state.enemy_name or 'противник'}"
                    )
                    self.combat.reset_combo()
                    won = await self.combat.process_fight(
                        page,
                        target_combo=None,
                        hp_threshold_pct=self.hp_critical_pct,
                    )
                    self._in_combat_flag = False
                    self.scheduler.set_in_combat(False)
                    if won:
                        self._fights_won += 1
                        self.analytics.track_event(
                            EVENT_BATTLE_WON,
                            {
                                "enemy": combat_state.enemy_name or "",
                                "exp": 0,
                                "valor": 0,
                                "gold": 0.0,
                            },
                        )
                        self.logger.info("Бой выигран (всего побед: %s)", self._fights_won)
                    else:
                        self._fights_lost += 1
                        self.analytics.track_event(
                            EVENT_BATTLE_LOST,
                            {"enemy": combat_state.enemy_name or ""},
                        )
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
                    self._current_task = "quest"
                    self.remote_state.current_task = f"quest:{step}"
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
                await self._capture_crash_safe(exc, module_context="main.browser")
                try:
                    await self.recovery.restart_session(self.browser, self.cookies)
                    self.recovery.reset_restart_counter()
                    # После рестарта — новый page для console listener
                    try:
                        self.diagnostics.attach_page(self.browser.page)
                    except Exception:
                        pass
                    await self.telegram.send_alert(
                        f"♻ Recovery после BrowserEngineError: {exc}"
                    )
                except Exception as restart_exc:
                    log_exception(
                        self.logger, "Не удалось восстановить сессию", restart_exc
                    )
                    await self._capture_crash_safe(
                        restart_exc, module_context="main.recovery"
                    )
                    await self._on_loop_error(exc)
            except Exception as exc:
                self._consecutive_errors += 1
                log_exception(self.logger, "Ошибка в main_loop", exc)
                await self._capture_crash_safe(exc, module_context="main.loop")
                # Пытаемся вылечить «зависшую» сессию
                try:
                    ok = await self.recovery.ensure_healthy(self.browser, self.cookies)
                    if not ok:
                        await self.recovery.restart_session(self.browser, self.cookies)
                    try:
                        self.diagnostics.attach_page(self.browser.page)
                    except Exception:
                        pass
                except Exception as recovery_exc:
                    self.logger.error("Recovery в except: %s", recovery_exc)
                    await self._capture_crash_safe(
                        recovery_exc, module_context="main.recovery"
                    )
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

    async def _recovery_on_retry(self, attempt: int, exc: BaseException) -> None:
        """Хук safe_execute: при сетевых/DOM ошибках пробуем soft-restart."""
        self.logger.warning("Recovery retry #%s after %s", attempt, exc)
        if attempt >= 2:
            try:
                await self.recovery.restart_session(self.browser, self.cookies)
                try:
                    self.diagnostics.attach_page(self.browser.page)
                except Exception:
                    pass
                await self.telegram.send_alert(
                    f"♻ Авто-рестарт сессии (попытка {attempt}): {exc}"
                )
            except Exception as restart_exc:
                self.logger.error(
                    "restart_session во время retry не удался: %s",
                    restart_exc,
                    exc_info=True,
                )

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
            self.analytics.track_event(
                EVENT_POTION_USED,
                {"name": "эликсир", "count": 1, "context": "out_of_combat"},
            )
            self.logger.info("Эликсир использован вне боя")
            return True
        return False

    async def _capture_crash_safe(
        self,
        exc: BaseException,
        *,
        module_context: str,
        expected_selector: str = "",
    ) -> None:
        """Верхнеуровневый перехват: CrashDump + Telegram (без падения цикла)."""
        try:
            page = None
            if self.browser.is_started:
                try:
                    page = self.browser.page
                except Exception:
                    page = None
            await self.diagnostics.capture_crash(
                page,
                exc,
                module_context,
                expected_selector=expected_selector,
                send_telegram=True,
            )
        except Exception as dump_exc:
            self.logger.warning("capture_crash failed: %s", dump_exc)

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
        # Верхнеуровневый необработанный exception → CrashDump
        try:
            await orchestrator._capture_crash_safe(exc, module_context="main.fatal")
        except Exception:
            pass
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
