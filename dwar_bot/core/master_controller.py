"""
Мастер-контроллер DwarBot — конечный автомат (FSM), связывающий все модули.

Приоритеты resolve_next_state:
  CAPTCHA > COMBAT > HEALING > ROUTINE > primary (FARM/TRADE/QUEST) > IDLE
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from dwar_bot.auth.cookie_manager import CookieManager, SessionRotationError
from dwar_bot.config import DATA_DIR, BotConfig, config
from dwar_bot.core.anti_bot import AntiBot
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError
from dwar_bot.core.recovery import CrashRecoveryManager
from dwar_bot.core.self_diagnostics import SelfDiagnostics
from dwar_bot.core.telegram_bot import RemoteControlState, TelegramRemoteControl
from dwar_bot.logger import (
    get_logger,
    log_exception,
    notify_telegram,
    start_telegram_notifier,
    stop_telegram_notifier,
)
from dwar_bot.modules.analytics_reporter import (
    EVENT_AUCTION_BUY,
    EVENT_BATTLE_LOST,
    EVENT_BATTLE_WON,
    EVENT_CAPTCHA,
    EVENT_DOWNTIME,
    EVENT_POTION_USED,
    EVENT_RESOURCE_FARMED,
    AnalyticsReporter,
)
from dwar_bot.modules.auction_trader import AuctionTrader, TradeOffer
from dwar_bot.modules.background_scheduler import (
    PRIORITY_HIGH,
    BackgroundScheduler,
)
from dwar_bot.modules.combat_engine import CombatEngine
from dwar_bot.modules.profession_farm import ProfessionFarm
from dwar_bot.modules.quest_tracker import QuestTracker
from dwar_bot.modules.stats_parser import BackpackItem, PlayerStats, StatsParser
from dwar_bot.modules.timers_manager import (
    TIMER_ANTI_BOT,
    TIMER_POTION,
    TIMER_QUEST_STEP,
    TimersManager,
)

logger = logging.getLogger(__name__)

STATE_FILE = DATA_DIR / "runtime_state.json"
DEFAULT_HP_HEAL_PCT = 55.0
DEFAULT_HP_CRITICAL_PCT = 35.0


# ---------------------------------------------------------------------------
# Enum / Dataclass
# ---------------------------------------------------------------------------


class BotState(str, Enum):
    """Состояния конечного автомата."""

    INITIALIZING = "initializing"
    IDLE = "idle"
    IN_COMBAT = "in_combat"
    HEALING = "healing"
    FARMING = "farming"
    TRADING = "trading"
    EXECUTING_QUEST = "executing_quest"
    EXECUTING_ROUTINE = "executing_routine"
    HANDLING_CAPTCHA = "handling_captcha"
    PAUSED = "paused"
    STOPPING = "stopping"


class PrimaryMode(str, Enum):
    """Основной режим, когда нет срочных приоритетов."""

    FARMING = "farming"
    TRADING = "trading"
    QUESTS = "quests"
    IDLE = "idle"


@dataclass
class GlobalContext:
    """Единые ссылки на все подсистемы бота."""

    config: BotConfig
    cookies: CookieManager
    browser: BrowserEngine
    stats_parser: StatsParser
    timers: TimersManager
    combat: CombatEngine
    farm: ProfessionFarm
    quests: QuestTracker
    auction: AuctionTrader
    anti_bot: AntiBot
    telegram: TelegramRemoteControl
    remote_state: RemoteControlState
    recovery: CrashRecoveryManager
    diagnostics: SelfDiagnostics
    analytics: AnalyticsReporter
    scheduler: BackgroundScheduler
    stats: Optional[PlayerStats] = None
    backpack: List[BackpackItem] = field(default_factory=list)


# ---------------------------------------------------------------------------
# MasterController
# ---------------------------------------------------------------------------


class MasterController:
    """
    Связующий FSM-контроллер экосистемы DwarBot.

    ``run_state_machine()`` — главный цикл; ``graceful_shutdown()`` —
    безопасная остановка всех фоновых задач и Playwright.
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        quest_script: Optional[List[dict]] = None,
        hp_heal_pct: float = DEFAULT_HP_HEAL_PCT,
        hp_critical_pct: float = DEFAULT_HP_CRITICAL_PCT,
        primary_mode: PrimaryMode = PrimaryMode.FARMING,
        trade_watch_list: Optional[List[TradeOffer]] = None,
        farm_targets: Optional[Sequence[str]] = None,
    ) -> None:
        self.config = bot_config or config
        self.hp_heal_pct = float(hp_heal_pct)
        self.hp_critical_pct = float(hp_critical_pct)
        self.primary_mode = primary_mode
        self.trade_watch_list = list(trade_watch_list or [])
        self.farm_targets = [t.strip() for t in (farm_targets or []) if t.strip()]
        self.quest_script = list(quest_script or [])

        self.logger = get_logger("dwar_bot.master")
        self.state = BotState.INITIALIZING
        self.is_running = False
        self._started = False
        self._stop_event = asyncio.Event()
        self._state_lock = asyncio.Lock()
        self._consecutive_errors = 0
        self._loops = 0
        self._fights_won = 0
        self._fights_lost = 0
        self._quest_queue: List[dict] = list(self.quest_script)
        self._pause_started_at: Optional[float] = None
        self._last_state_change = time.monotonic()
        self._shutdown_done = False

        # --- сборка модулей ---
        cookies = CookieManager(self.config)
        browser = BrowserEngine(self.config, cookie_manager=cookies)
        stats_parser = StatsParser(self.config)
        timers = TimersManager(self.config)
        combat = CombatEngine(
            self.config, browser=browser, stats_parser=stats_parser
        )
        quests = QuestTracker(
            self.config,
            browser=browser,
            combat_engine=combat,
            stats_parser=stats_parser,
        )
        anti_bot = AntiBot(self.config, browser=browser)
        farm = ProfessionFarm(
            self.config,
            browser=browser,
            human=browser.human,
            stats_parser=stats_parser,
            combat_engine=combat,
            timers=timers,
        )
        auction = AuctionTrader(
            self.config,
            browser=browser,
            human=browser.human,
            stats_parser=stats_parser,
        )

        remote_state = RemoteControlState()
        telegram = TelegramRemoteControl(
            self.config,
            state=remote_state,
            stats_provider=self.build_status_report,
            screenshot_provider=self.take_remote_screenshot,
        )
        recovery = CrashRecoveryManager(self.config)
        diagnostics = SelfDiagnostics(self.config, telegram=telegram)
        recovery.bind_diagnostics(diagnostics)

        report_hours = float(os.getenv("DWAR_REPORT_INTERVAL_HOURS", "12") or "12")
        analytics = AnalyticsReporter(
            self.config, report_interval_hours=report_hours
        )
        scheduler = BackgroundScheduler(
            self.config,
            browser=browser,
            human=browser.human,
            analytics=analytics,
            telegram=telegram,
        )
        scheduler.register_default_tasks()

        self.ctx = GlobalContext(
            config=self.config,
            cookies=cookies,
            browser=browser,
            stats_parser=stats_parser,
            timers=timers,
            combat=combat,
            farm=farm,
            quests=quests,
            auction=auction,
            anti_bot=anti_bot,
            telegram=telegram,
            remote_state=remote_state,
            recovery=recovery,
            diagnostics=diagnostics,
            analytics=analytics,
            scheduler=scheduler,
        )

        # Удобные алиасы (совместимость с прежним оркестратором)
        self.browser = browser
        self.telegram = telegram
        self.remote_state = remote_state
        self.analytics = analytics
        self.scheduler = scheduler
        self.diagnostics = diagnostics
        self.recovery = recovery
        self.cookies = cookies
        self.anti_bot = anti_bot
        self.combat = combat
        self.profession_farm = farm
        self.quests = quests
        self.stats_parser = stats_parser
        self.timers = timers

    # ------------------------------------------------------------------
    # Public control
    # ------------------------------------------------------------------

    def request_stop(self) -> None:
        self.logger.warning("MasterController: запрос остановки")
        self._stop_event.set()
        self.remote_state.request_stop()
        self.is_running = False

    def request_pause(self) -> None:
        self.remote_state.pause()
        self._pause_started_at = time.monotonic()
        self._set_state(BotState.PAUSED)

    def request_resume(self) -> None:
        if self._pause_started_at is not None:
            self.analytics.track_event(
                EVENT_DOWNTIME,
                {
                    "seconds": max(0.0, time.monotonic() - self._pause_started_at),
                    "reason": "telegram_pause",
                },
            )
            self._pause_started_at = None
        self.remote_state.resume()

    # ------------------------------------------------------------------
    # resolve_next_state
    # ------------------------------------------------------------------

    def resolve_next_state(self) -> BotState:
        """
        Определить следующее состояние по приоритету угроз / задач.
        Не меняет ``self.state`` — только вычисляет.
        """
        if self._stop_event.is_set() or self.remote_state.should_stop:
            return BotState.STOPPING

        if self.remote_state.is_paused:
            return BotState.PAUSED

        if self.ctx.anti_bot.is_paused or self.ctx.anti_bot.captcha.is_paused:
            return BotState.HANDLING_CAPTCHA

        stats = self.ctx.stats
        if stats is not None and stats.in_combat:
            return BotState.IN_COMBAT

        if stats is not None and stats.hp_max > 0:
            hp_pct = stats.hp_ratio * 100.0
            if hp_pct < self.hp_heal_pct and hp_pct > 0:
                return BotState.HEALING
            if stats.hp_current <= 0:
                return BotState.HEALING

        # Критическая фоновая рутина (высокий приоритет + due)
        if self._has_critical_routine():
            return BotState.EXECUTING_ROUTINE

        if self.primary_mode == PrimaryMode.QUESTS and self._quest_queue:
            return BotState.EXECUTING_QUEST
        if self.primary_mode == PrimaryMode.TRADING:
            return BotState.TRADING
        if self.primary_mode == PrimaryMode.FARMING:
            return BotState.FARMING
        if self.primary_mode == PrimaryMode.QUESTS:
            return BotState.EXECUTING_QUEST if self._quest_queue else BotState.IDLE

        return BotState.IDLE

    def _has_critical_routine(self) -> bool:
        try:
            for task in self.scheduler._tasks.values():  # noqa: SLF001
                if (
                    task.is_active
                    and task.is_due()
                    and int(task.priority) >= PRIORITY_HIGH
                    and task.task_id in self.scheduler._handlers  # noqa: SLF001
                ):
                    return True
        except Exception:
            return False
        return False

    def _set_state(self, new_state: BotState) -> None:
        if new_state == self.state:
            return
        self.logger.info("FSM: %s → %s", self.state.value, new_state.value)
        self.state = new_state
        self._last_state_change = time.monotonic()
        self.remote_state.current_task = new_state.value

    # ------------------------------------------------------------------
    # run_state_machine
    # ------------------------------------------------------------------

    async def run_state_machine(self) -> None:
        """Главный бесконечный цикл FSM без взаимных блокировок модулей."""
        if not self._started:
            await self.initialize()

        self.is_running = True
        self._set_state(BotState.IDLE)
        self.logger.info(
            "FSM запущен (primary=%s heal=%.0f%% critical=%.0f%%)",
            self.primary_mode.value,
            self.hp_heal_pct,
            self.hp_critical_pct,
        )

        try:
            while self.is_running and not self._stop_event.is_set():
                self._loops += 1
                loop_started = time.monotonic()

                try:
                    # Telegram /stop
                    if self.remote_state.should_stop:
                        self._set_state(BotState.STOPPING)
                        break

                    # Health
                    healthy = await self.recovery.safe_execute(
                        self.recovery.ensure_healthy,
                        self.browser,
                        self.cookies,
                        max_retries=2,
                        base_delay=1.5,
                    )
                    if not healthy:
                        await self.telegram.send_alert(
                            "⚠ Recovery: сессия нездорова, повтор…"
                        )
                        await asyncio.sleep(random.uniform(5.0, 12.0))
                        continue

                    page = self.browser.page

                    # Лёгкий anti-bot poll (не блокирует FSM надолго)
                    if self.timers.is_ready(TIMER_ANTI_BOT):
                        challenged = False
                        try:
                            challenged = await self.anti_bot.handle_challenge(page)
                            if challenged:
                                self.analytics.track_event(
                                    EVENT_CAPTCHA, {"source": "fsm_poll"}
                                )
                                self.timers.set_cooldown(TIMER_ANTI_BOT, 30.0)
                        except Exception as exc:
                            self.logger.debug("anti_bot poll: %s", exc)
                        if not challenged:
                            self.timers.set_cooldown(
                                TIMER_ANTI_BOT, random.uniform(8.0, 15.0)
                            )

                    # Обновить статы
                    stats = await self.recovery.safe_execute(
                        self.stats_parser.parse_player_stats,
                        page,
                        max_retries=3,
                        base_delay=1.0,
                        on_retry=self._recovery_on_retry,
                    )
                    self.ctx.stats = stats
                    self.scheduler.set_context(
                        page, stats, in_combat=bool(stats.in_combat)
                    )

                    # Детект боя через combat frame (надёжнее флага статов)
                    try:
                        combat_state = await self.combat.parse_combat_state(page)
                        if combat_state.in_combat:
                            stats.in_combat = True
                            self.ctx.stats = stats
                    except Exception:
                        pass

                    next_state = self.resolve_next_state()
                    self._set_state(next_state)

                    async with self._state_lock:
                        await self._dispatch_state(next_state)

                    self._consecutive_errors = 0

                except asyncio.CancelledError:
                    self.logger.info("FSM cancelled")
                    raise
                except BrowserEngineError as exc:
                    self._consecutive_errors += 1
                    log_exception(self.logger, "BrowserEngineError в FSM", exc)
                    await self._capture_crash(exc, "fsm.browser")
                    try:
                        await self.recovery.restart_session(self.browser, self.cookies)
                        self.diagnostics.attach_page(self.browser.page)
                        self.recovery.reset_restart_counter()
                    except Exception as restart_exc:
                        await self._capture_crash(restart_exc, "fsm.recovery")
                        await self._error_backoff(exc)
                except Exception as exc:
                    self._consecutive_errors += 1
                    log_exception(self.logger, "Ошибка FSM", exc)
                    await self._capture_crash(exc, "fsm.loop")
                    try:
                        await self.recovery.ensure_healthy(self.browser, self.cookies)
                        self.diagnostics.attach_page(self.browser.page)
                    except Exception:
                        pass
                    await self._error_backoff(exc)

                if self._consecutive_errors >= self.config.max_consecutive_errors:
                    self.logger.critical(
                        "Слишком много ошибок (%s) — STOPPING",
                        self._consecutive_errors,
                    )
                    await notify_telegram(
                        f"Аварийная остановка FSM: {self._consecutive_errors} ошибок",
                        critical=True,
                    )
                    self._set_state(BotState.STOPPING)
                    break

                # Мягкий sleep между тиками (не держим lock)
                elapsed = time.monotonic() - loop_started
                sleep_for = self.timers.optimal_sleep(
                    has_pending_tasks=bool(self._quest_queue)
                    or self.primary_mode != PrimaryMode.IDLE,
                    in_combat=bool(self.ctx.stats and self.ctx.stats.in_combat),
                    hp_critical=bool(
                        self.ctx.stats
                        and self.ctx.stats.hp_max > 0
                        and self.ctx.stats.hp_ratio * 100 < self.hp_critical_pct
                    ),
                )
                sleep_for = max(0.25, sleep_for - elapsed * 0.2)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_for)
                except asyncio.TimeoutError:
                    pass

        finally:
            self.is_running = False
            await self.graceful_shutdown()

    async def _dispatch_state(self, state: BotState) -> None:
        handlers = {
            BotState.PAUSED: self._handle_paused,
            BotState.HANDLING_CAPTCHA: self._handle_captcha,
            BotState.IN_COMBAT: self._handle_combat,
            BotState.HEALING: self._handle_healing,
            BotState.EXECUTING_ROUTINE: self._handle_routine,
            BotState.EXECUTING_QUEST: self._handle_quest,
            BotState.FARMING: self._handle_farming,
            BotState.TRADING: self._handle_trading,
            BotState.IDLE: self._handle_idle,
            BotState.STOPPING: self._handle_stopping,
            BotState.INITIALIZING: self._handle_idle,
        }
        handler = handlers.get(state, self._handle_idle)
        await handler()

    # ------------------------------------------------------------------
    # State handlers
    # ------------------------------------------------------------------

    async def _handle_paused(self) -> None:
        self.logger.info("FSM PAUSED — ожидание /resume")
        await self.remote_state.wait_if_paused(poll_sec=1.5)
        if self.remote_state.should_stop:
            self._stop_event.set()

    async def _handle_captcha(self) -> None:
        self.logger.critical("FSM HANDLING_CAPTCHA")
        started = time.monotonic()
        await self.telegram.send_alert(
            "ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО: Капча! Пройдите проверку."
        )
        await notify_telegram(
            "ТРЕБУЕТСЯ ВМЕШАТЕЛЬСТВО: Появилась капча!", critical=True
        )
        await self.anti_bot.captcha.wait_until_resumed(poll_sec=3.0)
        self.analytics.track_event(
            EVENT_CAPTCHA,
            {
                "downtime_seconds": max(0.0, time.monotonic() - started),
                "source": "fsm",
            },
        )

    async def _handle_combat(self) -> None:
        page = self.browser.page
        self.scheduler.set_in_combat(True)
        combat_state = await self.combat.parse_combat_state(page)
        await self.telegram.send_alert(
            f"⚔ Бой: {combat_state.enemy_name or 'противник'}"
        )
        self.combat.reset_combo()
        won = await self.combat.process_fight(
            page,
            target_combo=None,
            hp_threshold_pct=self.hp_critical_pct,
        )
        self.scheduler.set_in_combat(False)
        if won:
            self._fights_won += 1
            self.analytics.track_event(
                EVENT_BATTLE_WON,
                {"enemy": combat_state.enemy_name or "", "gold": 0.0},
            )
        else:
            self._fights_lost += 1
            self.analytics.track_event(
                EVENT_BATTLE_LOST,
                {"enemy": combat_state.enemy_name or ""},
            )
        if self.ctx.stats is not None:
            self.ctx.stats.in_combat = False

    async def _handle_healing(self) -> None:
        stats = self.ctx.stats or PlayerStats()
        if stats.hp_max > 0 and stats.hp_current <= 0:
            self.logger.critical("Персонаж погиб (HP=0)")
            await notify_telegram(
                f"Смерть персонажа! loc={stats.location}", critical=True
            )
            await asyncio.sleep(random.uniform(20.0, 40.0))
            return

        if not self.timers.is_ready(TIMER_POTION):
            await asyncio.sleep(max(0.5, self.timers.remaining(TIMER_POTION) or 1.0))
            return

        page = self.browser.page
        try:
            backpack = await self.stats_parser.parse_backpack(page)
            self.ctx.backpack = backpack
        except Exception as exc:
            self.logger.warning("backpack parse: %s", exc)
            backpack = self.ctx.backpack

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
                {"name": "эликсир", "count": 1, "context": "fsm_heal"},
            )
        else:
            await asyncio.sleep(random.uniform(2.0, 5.0))

    async def _handle_routine(self) -> None:
        """Выполнить due high-priority задачу планировщика один раз."""
        page = self.browser.page
        stats = self.ctx.stats or PlayerStats()
        self.scheduler.set_context(page, stats, in_combat=False)
        # Один tick планировщика (выполнит due-задачи по приоритету)
        try:
            await self.scheduler._tick_once()  # noqa: SLF001
        except Exception as exc:
            self.logger.warning("routine tick: %s", exc)

    async def _handle_quest(self) -> None:
        if not self._quest_queue:
            return
        if not self.timers.is_ready(TIMER_QUEST_STEP):
            await asyncio.sleep(max(0.3, self.timers.remaining(TIMER_QUEST_STEP) or 0.5))
            return

        page = self.browser.page
        step = self._quest_queue[0]
        self.logger.info("FSM QUEST step: %s", step)
        ok = await self.quests.execute_quest_sequence(page, [step])
        if ok:
            self._quest_queue.pop(0)
        else:
            failed = self._quest_queue.pop(0)
            self._quest_queue.append(failed)
        self.timers.set_cooldown(TIMER_QUEST_STEP, random.uniform(2.0, 5.0))

    async def _handle_farming(self) -> None:
        page = self.browser.page
        nodes = await self.profession_farm.scan_location_resources(page)
        if self.farm_targets:
            targets = {t.lower() for t in self.farm_targets}
            nodes = [
                n
                for n in nodes
                if n.is_available and any(t in n.name.lower() for t in targets)
            ]
        else:
            nodes = [n for n in nodes if n.is_available]

        if not nodes:
            await self.anti_bot.tick(page)
            await asyncio.sleep(random.uniform(1.5, 3.5))
            return

        node = nodes[0]
        ok = await self.profession_farm.harvest_resource(page, node)
        if ok:
            self.analytics.track_event(
                EVENT_RESOURCE_FARMED,
                {"name": node.name, "count": 1},
            )

    async def _handle_trading(self) -> None:
        page = self.browser.page
        if not self.trade_watch_list:
            # Нет watch-list — лёгкий скан торгового чата
            offers = await self.ctx.auction.parse_trade_chat(page)
            self.logger.info("FSM TRADING: chat offers=%s", len(offers))
            await asyncio.sleep(random.uniform(2.0, 4.0))
            return

        bought = await self.ctx.auction.buy_underpriced_items(
            page, self.trade_watch_list
        )
        if bought:
            self.analytics.track_event(
                EVENT_AUCTION_BUY,
                {"count": bought, "source": "fsm"},
            )

    async def _handle_idle(self) -> None:
        page = self.browser.page
        await self.anti_bot.tick(page)
        await asyncio.sleep(random.uniform(1.0, 2.5))

    async def _handle_stopping(self) -> None:
        self._stop_event.set()
        self.is_running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self) -> None:
        """INITIALIZING: cookies, browser, telegram, scheduler, analytics."""
        self._set_state(BotState.INITIALIZING)
        self.config.ensure_directories()
        self._restore_runtime_state()

        await start_telegram_notifier()
        self.logger.info(
            "Init: server=%s headless=%s cookies=%s",
            self.config.server.server,
            self.config.browser.headless,
            self.config.cookies.cookies_file,
        )

        try:
            await self.cookies.initialize(validate=True)
        except SessionRotationError as exc:
            self.logger.warning("Cookie validate fail (%s) — без validate", exc)
            await self.cookies.initialize(validate=False)

        await self.browser.start(apply_cookies=True)
        await self.browser.open_game()
        self._started = True

        try:
            self.diagnostics.bind_telegram(self.telegram)
            self.diagnostics.attach_page(self.browser.page)
            self.diagnostics.cleanup_old_dumps(max_age_days=7, max_folder_size_mb=500)
        except Exception as exc:
            log_exception(self.logger, "SelfDiagnostics attach", exc)

        self.telegram.bind_providers(
            stats_provider=self.build_status_report,
            screenshot_provider=self.take_remote_screenshot,
        )
        await self.telegram.start()

        try:
            await self.analytics.start_scheduler(self.telegram)
        except Exception as exc:
            log_exception(self.logger, "Analytics scheduler", exc)

        try:
            self.scheduler.bind_services(
                analytics=self.analytics,
                telegram=self.telegram,
                browser=self.browser,
            )
            await self.scheduler.start_background(
                self.browser.page,
                self.ctx.stats or PlayerStats(),
                in_combat_flag=False,
            )
        except Exception as exc:
            log_exception(self.logger, "BackgroundScheduler", exc)

        await notify_telegram(
            f"MasterController online (server={self.config.server.server})",
            critical=False,
        )
        await self.telegram.send_alert(
            f"🐉 DwarBot FSM запущен ({self.config.server.server}) "
            f"mode={self.primary_mode.value}"
        )

    async def graceful_shutdown(self) -> None:
        """
        Безопасное завершение: фон → аналитика → cookie → Playwright → Telegram.
        Идемпотентен (повторный вызов безопасен).
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        self._set_state(BotState.STOPPING)
        self.logger.info("graceful_shutdown…")
        self._stop_event.set()
        self.remote_state.request_stop()

        try:
            await self.telegram.send_alert(
                f"⏹ Остановка FSM. loops={self._loops} "
                f"wins={self._fights_won} losses={self._fights_lost}"
            )
        except Exception:
            pass

        try:
            await self.scheduler.stop()
        except Exception as exc:
            log_exception(self.logger, "scheduler stop", exc)

        try:
            await self.analytics.stop_scheduler()
            try:
                await self.analytics.send_report_now(self.telegram, timeframe_hours=24)
            except Exception:
                pass
            self.analytics.close()
        except Exception as exc:
            log_exception(self.logger, "analytics stop", exc)

        try:
            await self.telegram.stop()
        except Exception as exc:
            log_exception(self.logger, "telegram stop", exc)

        try:
            self._save_runtime_state()
        except Exception as exc:
            log_exception(self.logger, "runtime state", exc)

        try:
            if self.browser.is_started:
                await self.cookies.sync_from_playwright(self.browser.context)
                self.cookies.save_cookies()
                self.logger.info("Cookies → %s", self.cookies.cookies_file)
        except Exception as exc:
            log_exception(self.logger, "cookie save", exc)

        try:
            await self.browser.stop()
        except Exception as exc:
            log_exception(self.logger, "browser stop", exc)

        try:
            await self.cookies.close()
        except Exception as exc:
            log_exception(self.logger, "cookies close", exc)

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
        self.logger.info("graceful_shutdown завершён")

    # ------------------------------------------------------------------
    # Telegram providers / helpers
    # ------------------------------------------------------------------

    async def build_status_report(self) -> str:
        stats = self.ctx.stats
        if self.browser.is_started:
            try:
                stats = await self.stats_parser.parse_player_stats(self.browser.page)
                self.ctx.stats = stats
            except Exception as exc:
                self.logger.debug("status parse: %s", exc)

        snap = self.analytics.snapshot_session()
        lines = [
            f"FSM: {self.state.value}",
            f"Mode: {self.primary_mode.value}",
            f"Loops: {self._loops}",
            f"Бои: {self._fights_won}W/{self._fights_lost}L",
            (
                f"KPI: {snap.gold_earned:+.2f}з "
                f"({snap.gold_per_hour:+.2f}з/ч) WR={snap.winrate_pct:.0f}%"
            ),
            f"Scheduler: {self.scheduler.status_summary}",
            f"Квесты в очереди: {len(self._quest_queue)}",
        ]
        if stats is not None:
            lines.extend(
                [
                    f"HP: {stats.hp_current}/{stats.hp_max} ({stats.hp_ratio*100:.0f}%)",
                    f"Золото: {stats.gold}з {stats.silver}с {stats.copper}м",
                    f"Бой: {'да' if stats.in_combat else 'нет'}",
                    f"Локация: {stats.location or '?'}",
                ]
            )
        lines.append(f"Фарм: {self.profession_farm.stats.summary()}")
        self.remote_state.farm_summary = self.profession_farm.stats.summary()
        return "\n".join(lines)

    async def take_remote_screenshot(self) -> Optional[Path]:
        if not self.browser.is_started:
            return None
        from dwar_bot.config import SCREENSHOTS_DIR

        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        path = SCREENSHOTS_DIR / f"telegram_{stamp}.png"
        try:
            await self.browser.page.screenshot(path=str(path), full_page=True)
            return path
        except Exception:
            try:
                return await self.browser.capture_error_screenshot(prefix="telegram")
            except Exception:
                return None

    async def _capture_crash(
        self, exc: BaseException, module_context: str
    ) -> None:
        try:
            page = None
            if self.browser.is_started:
                try:
                    page = self.browser.page
                except Exception:
                    page = None
            await self.diagnostics.capture_crash(
                page, exc, module_context, send_telegram=True
            )
        except Exception as dump_exc:
            self.logger.warning("capture_crash: %s", dump_exc)

    async def _recovery_on_retry(self, attempt: int, exc: BaseException) -> None:
        self.logger.warning("Recovery retry #%s after %s", attempt, exc)
        if attempt >= 2:
            try:
                await self.recovery.restart_session(self.browser, self.cookies)
                self.diagnostics.attach_page(self.browser.page)
                await self.telegram.send_alert(
                    f"♻ Авто-рестарт (попытка {attempt}): {exc}"
                )
            except Exception as restart_exc:
                self.logger.error("restart failed: %s", restart_exc, exc_info=True)

    async def _error_backoff(self, exc: BaseException) -> None:
        backoff = min(30.0, 1.5 * self._consecutive_errors) * random.uniform(0.8, 1.3)
        self.logger.warning("Backoff %.1fs after %s", backoff, exc)
        try:
            await self.browser.capture_error_screenshot(prefix="fsm_error")
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
            "primary_mode": self.primary_mode.value,
            "fsm_state": self.state.value,
            "timers": self.timers.snapshot(),
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(STATE_FILE)

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
            mode = data.get("primary_mode")
            if mode in {m.value for m in PrimaryMode}:
                self.primary_mode = PrimaryMode(mode)
        except Exception as exc:
            self.logger.warning("restore state: %s", exc)


def primary_mode_from_env(default: PrimaryMode = PrimaryMode.FARMING) -> PrimaryMode:
    raw = (os.getenv("DWAR_PRIMARY_MODE") or default.value).strip().lower()
    mapping = {
        "farm": PrimaryMode.FARMING,
        "farming": PrimaryMode.FARMING,
        "trade": PrimaryMode.TRADING,
        "trading": PrimaryMode.TRADING,
        "auction": PrimaryMode.TRADING,
        "quest": PrimaryMode.QUESTS,
        "quests": PrimaryMode.QUESTS,
        "idle": PrimaryMode.IDLE,
    }
    return mapping.get(raw, default)


__all__ = [
    "BotState",
    "PrimaryMode",
    "GlobalContext",
    "MasterController",
    "primary_mode_from_env",
]
