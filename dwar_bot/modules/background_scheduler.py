"""
Фоновый планировщик рутины DwarBot.

Регистрирует периодические задачи (почта, суточные награды, бафы,
очистка captchas/analytics), откладывает некритичные действия во время
боя и шлёт краткие отчёты в AnalyticsReporter + Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from bs4 import BeautifulSoup
from playwright.async_api import (
    Error as PlaywrightError,
    Frame,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from dwar_bot.config import (
    CAPTCHAS_DIR,
    DATA_DIR,
    LOGS_DIR,
    SCREENSHOTS_DIR,
    BotConfig,
    config,
    get_delay_range,
)
from dwar_bot.core.anti_bot import HumanBehavior
from dwar_bot.core.browser import BrowserEngine, BrowserEngineError
from dwar_bot.modules.analytics_reporter import (
    EVENT_CUSTOM,
    EVENT_GOLD_EARNED,
    AnalyticsReporter,
)
from dwar_bot.modules.stats_parser import PlayerStats

logger = logging.getLogger(__name__)

TaskHandler = Callable[..., Awaitable[Any]]

PRIORITY_LOW = 1
PRIORITY_NORMAL = 5
PRIORITY_HIGH = 10

TASK_CHECK_MAIL = "check_mail"
TASK_DAILY_GIFT = "daily_gift"
TASK_REFRESH_BUFFS = "refresh_buffs"
TASK_CLEANUP = "cleanup_storage"

DEFAULT_MAIL_INTERVAL = 15 * 60
DEFAULT_DAILY_INTERVAL = 6 * 60 * 60
DEFAULT_BUFFS_INTERVAL = 10 * 60
DEFAULT_CLEANUP_INTERVAL = 24 * 60 * 60
ANALYTICS_RETENTION_DAYS = 30
CAPTCHA_RETENTION_DAYS = 7
SCREENSHOT_RETENTION_DAYS = 14

MAIL_FRAME_NAMES: Tuple[str, ...] = (
    "main",
    "user",
    "pers",
    "mail",
    "post",
    "message",
)
MAIL_ENTRY_SELECTORS: Tuple[str, ...] = (
    "a[href*='mail']",
    "a[href*='post']",
    "a[href*='message']",
    "#mail",
    ".mail-link",
    "[data-panel='mail']",
    "text=/почта|письм/i",
)
MAIL_ATTACH_SELECTORS: Tuple[str, ...] = (
    "a[href*='attach']",
    "a[href*='take']",
    "a[href*='get']",
    "button[data-action='take']",
    ".attach-take",
    ".mail-attach a",
    "input[value*='Забрать']",
    "a:has-text('Забрать')",
    "button:has-text('Забрать')",
)
MAIL_DELETE_SELECTORS: Tuple[str, ...] = (
    "a[href*='delete']",
    "a[href*='del']",
    "button[data-action='delete']",
    ".mail-delete",
    "input[value*='Удалить']",
    "a:has-text('Удалить')",
    "button:has-text('Удалить')",
)
MAIL_OPEN_SELECTORS: Tuple[str, ...] = (
    "tr.mail a",
    ".mail-item a",
    "a[href*='mail_id']",
    "a[href*='msg_id']",
    ".letter a",
    "#mail_list a",
)
DAILY_SELECTORS: Tuple[str, ...] = (
    "a[href*='daily']",
    "a[href*='gift']",
    "a[href*='bonus']",
    "a[href*='calendar']",
    "a[href*='reward']",
    "a[href*='chest']",
    "#daily_gift",
    ".daily-reward",
    ".login-bonus",
    ".calendar-day.active",
    "[data-daily]",
    "a:has-text('Получить')",
    "button:has-text('Получить')",
    "a:has-text('Забрать')",
    "text=/ежедневн|суточн|подарок|календар/i",
)
BUFF_PANEL_SELECTORS: Tuple[str, ...] = (
    "#buffs",
    ".buffs",
    ".effects",
    "#effects",
    "[data-panel='buffs']",
    ".status-effects",
)
BUFF_ITEM_SELECTORS: Tuple[str, ...] = (
    ".buff",
    ".effect",
    "[data-buff]",
    ".status-effect",
    "img[title*='баф']",
)
BUFF_EXPIRED_HINTS: Tuple[str, ...] = (
    "истёк",
    "истек",
    "expired",
    "закончил",
    "00:00",
    "0:00",
)
BUFF_USE_SELECTORS: Tuple[str, ...] = (
    "a[href*='use']",
    "button[data-action='use']",
    ".use-buff",
    "a:has-text('Выпить')",
    "a:has-text('Использовать')",
    "button:has-text('Использовать')",
)

RE_GOLD_ATTACH = re.compile(
    r"(?P<gold>\d+)\s*(?:зол|з\.|gold)|(?P<silver>\d+)\s*(?:сер|с\.|silver)",
    re.IGNORECASE,
)
RE_SUCCESS = re.compile(
    r"(?:получен|забран|забрали|успешн|claimed|received|taken)",
    re.IGNORECASE,
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ScheduledTask:
    """Описание фоновой задачи планировщика."""

    task_id: str
    interval_seconds: int = 3600
    cron_expression: str = ""  # опционально: "every:3600" / зарезервировано
    last_run: Optional[datetime] = None
    next_run: Optional[datetime] = None
    priority: int = PRIORITY_NORMAL
    is_active: bool = True
    skip_in_combat: bool = True
    description: str = ""

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0 and self.cron_expression:
            # Простой парсер every:N
            m = re.match(r"every:(\d+)", self.cron_expression.strip(), re.I)
            if m:
                self.interval_seconds = max(1, int(m.group(1)))
        if self.interval_seconds <= 0:
            self.interval_seconds = 3600
        if self.next_run is None:
            # Небольшая джиттер-задержка перед первым запуском
            jitter = random.uniform(5.0, min(60.0, self.interval_seconds * 0.1 + 5))
            self.next_run = _utcnow() + timedelta(seconds=jitter)

    def mark_ran(self, *, success: bool = True) -> None:
        now = _utcnow()
        self.last_run = now
        # При ошибке — короткий retry; при успехе — полный интервал ± jitter
        if success:
            jitter = random.uniform(0.0, min(120.0, self.interval_seconds * 0.05))
            self.next_run = now + timedelta(seconds=self.interval_seconds + jitter)
        else:
            self.next_run = now + timedelta(seconds=min(300.0, self.interval_seconds * 0.25))

    def is_due(self, now: Optional[datetime] = None) -> bool:
        if not self.is_active:
            return False
        now = now or _utcnow()
        if self.next_run is None:
            return True
        nr = self.next_run
        if nr.tzinfo is None:
            nr = nr.replace(tzinfo=timezone.utc)
        return now >= nr

    def as_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["last_run"] = self.last_run.isoformat() if self.last_run else None
        data["next_run"] = self.next_run.isoformat() if self.next_run else None
        return data


# ---------------------------------------------------------------------------
# BackgroundScheduler
# ---------------------------------------------------------------------------


class BackgroundScheduler:
    """
    Фоновый планировщик игровой рутины.

    Запускается через ``asyncio.create_task(scheduler.run_scheduler_loop(...))``
    или ``await scheduler.start_background(...)``.
    """

    def __init__(
        self,
        bot_config: Optional[BotConfig] = None,
        *,
        browser: Optional[BrowserEngine] = None,
        human: Optional[HumanBehavior] = None,
        analytics: Optional[AnalyticsReporter] = None,
        telegram: Any = None,
        poll_interval_sec: float = 5.0,
        analytics_retention_days: int = ANALYTICS_RETENTION_DAYS,
        captcha_retention_days: int = CAPTCHA_RETENTION_DAYS,
    ) -> None:
        self._config = bot_config or config
        self._browser = browser
        self._human = human or (
            browser.human
            if browser is not None and hasattr(browser, "human")
            else HumanBehavior(self._config)
        )
        self._analytics = analytics
        self._telegram = telegram
        self.poll_interval_sec = max(1.0, float(poll_interval_sec))
        self.analytics_retention_days = max(1, int(analytics_retention_days))
        self.captcha_retention_days = max(1, int(captcha_retention_days))

        self._tasks: Dict[str, ScheduledTask] = {}
        self._handlers: Dict[str, TaskHandler] = {}
        self._stop_event = asyncio.Event()
        self._task: Optional[asyncio.Task[None]] = None
        self._running = False
        self._busy = False

        # Контекст, обновляемый главным циклом
        self._page: Optional[Page] = None
        self._stats: Optional[PlayerStats] = None
        self._in_combat = False
        self._lock = asyncio.Lock()

        self._last_mail_ok = False
        self._last_daily_ok = False
        self._last_buffs_ok = False
        self._cleanup_stats: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Registration / context
    # ------------------------------------------------------------------

    def register_task(
        self,
        task: ScheduledTask,
        coroutine: TaskHandler,
    ) -> None:
        """Зарегистрировать асинхронный обработчик для задачи."""
        if not task.task_id:
            raise ValueError("task_id не может быть пустым")
        if not callable(coroutine):
            raise TypeError("coroutine должен быть async callable")
        self._tasks[task.task_id] = task
        self._handlers[task.task_id] = coroutine
        logger.info(
            "Scheduler: зарегистрирована задача %s (every %ss, prio=%s)",
            task.task_id,
            task.interval_seconds,
            task.priority,
        )

    def unregister_task(self, task_id: str) -> None:
        self._tasks.pop(task_id, None)
        self._handlers.pop(task_id, None)

    def set_context(
        self,
        page: Optional[Page] = None,
        stats: Optional[PlayerStats] = None,
        in_combat: Optional[bool] = None,
    ) -> None:
        """Обновить page/stats/флаг боя из главного цикла оркестратора."""
        if page is not None:
            self._page = page
        if stats is not None:
            self._stats = stats
        if in_combat is not None:
            self._in_combat = bool(in_combat)

    def set_in_combat(self, flag: bool) -> None:
        self._in_combat = bool(flag)

    def bind_services(
        self,
        *,
        analytics: Optional[AnalyticsReporter] = None,
        telegram: Any = None,
        browser: Optional[BrowserEngine] = None,
    ) -> None:
        if analytics is not None:
            self._analytics = analytics
        if telegram is not None:
            self._telegram = telegram
        if browser is not None:
            self._browser = browser
            if hasattr(browser, "human"):
                self._human = browser.human

    def register_default_tasks(self) -> None:
        """Стандартный набор рутины: почта, daily, бафы, cleanup."""
        self.register_task(
            ScheduledTask(
                task_id=TASK_CHECK_MAIL,
                interval_seconds=DEFAULT_MAIL_INTERVAL,
                priority=PRIORITY_NORMAL,
                skip_in_combat=True,
                description="Проверка почты и вложений",
            ),
            self._handler_check_mail,
        )
        self.register_task(
            ScheduledTask(
                task_id=TASK_DAILY_GIFT,
                interval_seconds=DEFAULT_DAILY_INTERVAL,
                priority=PRIORITY_NORMAL,
                skip_in_combat=True,
                description="Суточные награды / календарь",
            ),
            self._handler_daily,
        )
        self.register_task(
            ScheduledTask(
                task_id=TASK_REFRESH_BUFFS,
                interval_seconds=DEFAULT_BUFFS_INTERVAL,
                priority=PRIORITY_HIGH,
                skip_in_combat=True,
                description="Обновление длительных бафов",
            ),
            self._handler_buffs,
        )
        self.register_task(
            ScheduledTask(
                task_id=TASK_CLEANUP,
                interval_seconds=DEFAULT_CLEANUP_INTERVAL,
                priority=PRIORITY_LOW,
                skip_in_combat=False,  # можно в бою — только файлы
                description="Очистка captchas и ротация analytics.db",
            ),
            self._handler_cleanup,
        )

    # ------------------------------------------------------------------
    # Public game actions
    # ------------------------------------------------------------------

    async def check_and_mail(self, page: Page) -> bool:
        """
        Проверить почтовый ящик: забрать вложения, удалить прочитанные
        системные письма. Клики — через HumanBehavior / human_click.
        """
        try:
            frame = await self._resolve_main_frame(page)
            opened = await self._open_mail(page, frame)
            if not opened:
                logger.debug("check_and_mail: почта не найдена")
                return False

            await self._human_pause("navigation")
            frame = await self._resolve_main_frame(page) or frame

            taken = 0
            # Открываем письма по одному (ограничение за проход)
            for _ in range(8):
                opened_letter = await self._click_first(
                    page, frame, MAIL_OPEN_SELECTORS
                )
                if not opened_letter:
                    break
                await self._human_pause("action")
                frame = await self._resolve_main_frame(page) or frame

                if await self._click_first(page, frame, MAIL_ATTACH_SELECTORS):
                    taken += 1
                    await self._human_pause("click")
                    logger.info("Почта: вложение забрано (#%s)", taken)

                # Удаляем прочитанное системное
                html = await self._frame_html(page, frame)
                if self._looks_like_system_mail(html):
                    if await self._click_first(page, frame, MAIL_DELETE_SELECTORS):
                        await self._human_pause("click")
                        logger.debug("Почта: системное письмо удалено")

            # Иногда «забрать всё» одной кнопкой
            if taken == 0:
                bulk = (
                    "a:has-text('Забрать всё')",
                    "button:has-text('Забрать всё')",
                    "a[href*='take_all']",
                    "input[value*='Забрать все']",
                )
                if await self._click_first(page, frame, bulk):
                    taken = max(taken, 1)
                    await self._human_pause("action")

            self._last_mail_ok = True
            if taken and self._analytics is not None:
                # Грубая оценка — факт получения; точное золото парсится отдельно
                self._analytics.track_event(
                    EVENT_CUSTOM,
                    {"task": TASK_CHECK_MAIL, "attachments": taken},
                )
            logger.info("check_and_mail: ok, attachments=%s", taken)
            return True

        except Exception as exc:
            self._last_mail_ok = False
            logger.error("check_and_mail: %s", exc, exc_info=True)
            return False

    async def claim_daily_rewards(self, page: Page) -> bool:
        """Забрать суточные сундуки, календарь наград и подарки за вход."""
        try:
            frame = await self._resolve_main_frame(page)
            claimed = 0

            # Сначала ищем точку входа в daily/calendar
            entry = (
                "a[href*='daily']",
                "a[href*='calendar']",
                "a[href*='bonus']",
                "a[href*='gift']",
                "text=/ежедневн|календар|подарок за вход/i",
            )
            await self._click_first(page, frame, entry)
            await self._human_pause("navigation")
            frame = await self._resolve_main_frame(page) or frame

            for _ in range(6):
                if await self._click_first(page, frame, DAILY_SELECTORS):
                    claimed += 1
                    await self._human_pause("action")
                    frame = await self._resolve_main_frame(page) or frame
                else:
                    break

            html = await self._frame_html(page, frame)
            ok = claimed > 0 or bool(RE_SUCCESS.search(html))
            # Уже получено сегодня — тоже считаем успехом (не спамим ошибками)
            if not ok and re.search(
                r"уже\s+получен|завтра|next\s+reward|claimed\s+today",
                html,
                re.I,
            ):
                ok = True
                logger.info("claim_daily_rewards: награда уже получена сегодня")

            self._last_daily_ok = ok
            if ok and self._analytics is not None:
                self._analytics.track_event(
                    EVENT_CUSTOM,
                    {"task": TASK_DAILY_GIFT, "claimed": claimed},
                )
                if claimed:
                    self._analytics.track_event(
                        EVENT_GOLD_EARNED,
                        {"gold": 0.0, "source": "daily_gift", "claimed": claimed},
                    )
            logger.info("claim_daily_rewards: ok=%s claimed=%s", ok, claimed)
            return ok

        except Exception as exc:
            self._last_daily_ok = False
            logger.error("claim_daily_rewards: %s", exc, exc_info=True)
            return False

    async def refresh_buffs_and_casts(
        self, page: Page, stats: PlayerStats
    ) -> bool:
        """
        Проверить активные бафы/эликсиры длительного действия и
        обновить при истечении таймера.
        """
        try:
            frame = await self._resolve_user_frame(page)
            html = await self._frame_html(page, frame)
            soup = BeautifulSoup(html, "html.parser")

            expired_names = self._find_expired_buffs(soup, html)
            if not expired_names:
                # Нет явных истекших — лёгкая проверка панели
                logger.debug(
                    "refresh_buffs: истекших бафов не видно (hp=%.0f%%)",
                    stats.hp_ratio * 100.0 if stats else -1,
                )
                self._last_buffs_ok = True
                return True

            refreshed = 0
            for name in expired_names[:5]:
                if await self._try_refresh_buff(page, frame, name):
                    refreshed += 1
                    await self._human_pause("action")
                    frame = await self._resolve_user_frame(page) or frame

            # Фоллбек: общая кнопка «использовать» длительный эликсир
            if refreshed == 0:
                if await self._click_first(page, frame, BUFF_USE_SELECTORS):
                    refreshed = 1
                    await self._human_pause("click")

            ok = refreshed > 0
            self._last_buffs_ok = ok
            if self._analytics is not None:
                self._analytics.track_event(
                    EVENT_CUSTOM,
                    {
                        "task": TASK_REFRESH_BUFFS,
                        "expired": list(expired_names)[:5],
                        "refreshed": refreshed,
                    },
                )
            logger.info(
                "refresh_buffs_and_casts: expired=%s refreshed=%s",
                expired_names,
                refreshed,
            )
            return ok

        except Exception as exc:
            self._last_buffs_ok = False
            logger.error("refresh_buffs_and_casts: %s", exc, exc_info=True)
            return False

    # ------------------------------------------------------------------
    # Scheduler loop
    # ------------------------------------------------------------------

    async def run_scheduler_loop(
        self,
        page: Page,
        stats: PlayerStats,
        in_combat_flag: bool = False,
    ) -> None:
        """
        Фоновый цикл: проверяет due-задачи и выполняет их по приоритету.

        Если ``in_combat_flag`` / актуальный флаг боя True — некритичные
        задачи (skip_in_combat) откладываются. Контекст обновляйте через
        ``set_context`` / ``set_in_combat`` из main_loop.
        """
        self.set_context(page, stats, in_combat_flag)
        self._stop_event = asyncio.Event()
        self._running = True
        logger.info(
            "BackgroundScheduler loop started (%s tasks)",
            len(self._tasks),
        )

        try:
            while not self._stop_event.is_set():
                try:
                    await self._tick_once()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.error("scheduler tick error: %s", exc, exc_info=True)

                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self.poll_interval_sec,
                    )
                    break
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            logger.info("BackgroundScheduler loop cancelled")
            raise
        finally:
            self._running = False
            logger.info("BackgroundScheduler loop stopped")

    async def start_background(
        self,
        page: Page,
        stats: PlayerStats,
        in_combat_flag: bool = False,
    ) -> asyncio.Task[None]:
        """Запустить ``run_scheduler_loop`` как ``asyncio.create_task``."""
        await self.stop()
        self._stop_event = asyncio.Event()
        self._task = asyncio.create_task(
            self.run_scheduler_loop(page, stats, in_combat_flag),
            name="background-scheduler",
        )
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None
        self._running = False

    async def _tick_once(self) -> None:
        if self._busy:
            return
        page = self._page
        if page is None:
            return

        due = [
            t
            for t in self._tasks.values()
            if t.is_due() and t.task_id in self._handlers
        ]
        if not due:
            return

        # Высокий приоритет первым
        due.sort(key=lambda t: (-int(t.priority), t.next_run or _utcnow()))

        in_combat = bool(self._in_combat)
        for task in due:
            if self._stop_event.is_set():
                return
            if in_combat and task.skip_in_combat:
                logger.debug(
                    "Scheduler: отложен '%s' — идёт бой (prio=%s)",
                    task.task_id,
                    task.priority,
                )
                # Сдвигаем next_run чуть вперёд, чтобы не крутить зря
                task.next_run = _utcnow() + timedelta(seconds=random.uniform(15, 45))
                continue

            await self._execute_task(task)

    async def _execute_task(self, task: ScheduledTask) -> None:
        handler = self._handlers.get(task.task_id)
        if handler is None:
            return
        page = self._page
        stats = self._stats or PlayerStats()
        if page is None:
            return

        self._busy = True
        started = time.monotonic()
        success = False
        error_msg = ""
        try:
            async with self._lock:
                logger.info("Scheduler: запуск задачи '%s'", task.task_id)
                result = await handler(page, stats)
                success = bool(result) if result is not None else True
        except Exception as exc:
            error_msg = str(exc)
            logger.error(
                "Scheduler: ошибка задачи '%s': %s",
                task.task_id,
                exc,
                exc_info=True,
            )
            success = False
        finally:
            task.mark_ran(success=success)
            self._busy = False

        elapsed = time.monotonic() - started
        await self._report_task_result(task, success=success, elapsed=elapsed, error=error_msg)

    async def _report_task_result(
        self,
        task: ScheduledTask,
        *,
        success: bool,
        elapsed: float,
        error: str = "",
    ) -> None:
        payload = {
            "task": task.task_id,
            "success": success,
            "elapsed_sec": round(elapsed, 2),
            "priority": task.priority,
            "error": error[:200],
        }
        if self._analytics is not None:
            try:
                self._analytics.track_event(EVENT_CUSTOM, payload)
            except Exception as exc:
                logger.debug("analytics track scheduler: %s", exc)

        status = "✅" if success else "⚠️"
        text = (
            f"{status} Фоновая задача `{task.task_id}`: "
            f"{'OK' if success else 'FAIL'} за {elapsed:.1f}с"
        )
        if error:
            text += f"\n{error[:120]}"
        await self._notify_telegram(text)

    async def _notify_telegram(self, text: str) -> None:
        tg = self._telegram
        if tg is None:
            return
        try:
            if hasattr(tg, "send_alert"):
                await tg.send_alert(text)
        except Exception as exc:
            logger.debug("scheduler telegram notify: %s", exc)

    # ------------------------------------------------------------------
    # Default handlers
    # ------------------------------------------------------------------

    async def _handler_check_mail(self, page: Page, stats: PlayerStats) -> bool:
        return await self.check_and_mail(page)

    async def _handler_daily(self, page: Page, stats: PlayerStats) -> bool:
        return await self.claim_daily_rewards(page)

    async def _handler_buffs(self, page: Page, stats: PlayerStats) -> bool:
        return await self.refresh_buffs_and_casts(page, stats)

    async def _handler_cleanup(self, page: Page, stats: PlayerStats) -> bool:
        return await self.cleanup_storage()

    # ------------------------------------------------------------------
    # Cleanup / retention
    # ------------------------------------------------------------------

    async def cleanup_storage(self) -> bool:
        """
        Удалить старые скриншоты captchas/screenshots и события
        analytics.db старше ``analytics_retention_days``.
        """
        stats = {
            "captchas_deleted": 0,
            "screenshots_deleted": 0,
            "logs_deleted": 0,
            "analytics_rows_deleted": 0,
        }
        try:
            # Docker может монтировать /app/captchas — чистим оба пути
            captcha_dirs = [CAPTCHAS_DIR]
            root_captchas = Path("/app/captchas")
            if root_captchas.exists() and root_captchas not in captcha_dirs:
                captcha_dirs.append(root_captchas)
            # также dwar_bot/data/captchas уже в CAPTCHAS_DIR

            for folder in captcha_dirs:
                stats["captchas_deleted"] += self._delete_old_files(
                    folder,
                    days=self.captcha_retention_days,
                    patterns=("*.png", "*.jpg", "*.jpeg", "*.webp"),
                )

            stats["screenshots_deleted"] = self._delete_old_files(
                SCREENSHOTS_DIR,
                days=SCREENSHOT_RETENTION_DAYS,
                patterns=("*.png", "*.jpg", "*.jpeg", "*.webp"),
            )
            stats["logs_deleted"] = self._delete_old_files(
                LOGS_DIR,
                days=self.analytics_retention_days,
                patterns=("*.log.*", "*.gz"),
            )
            stats["analytics_rows_deleted"] = self._rotate_analytics_db(
                days=self.analytics_retention_days
            )

            self._cleanup_stats = stats
            logger.info("cleanup_storage: %s", stats)
            if self._analytics is not None:
                self._analytics.track_event(
                    EVENT_CUSTOM,
                    {"task": TASK_CLEANUP, **stats},
                )
            return True
        except Exception as exc:
            logger.error("cleanup_storage: %s", exc, exc_info=True)
            return False

    @staticmethod
    def _delete_old_files(
        folder: Path,
        *,
        days: int,
        patterns: Sequence[str],
    ) -> int:
        if not folder.exists():
            return 0
        cutoff = time.time() - days * 86400
        deleted = 0
        for pattern in patterns:
            for path in folder.glob(pattern):
                try:
                    if not path.is_file():
                        continue
                    if path.stat().st_mtime < cutoff:
                        path.unlink(missing_ok=True)
                        deleted += 1
                except OSError as exc:
                    logger.debug("delete %s: %s", path, exc)
        return deleted

    def _rotate_analytics_db(self, *, days: int) -> int:
        db_path = DATA_DIR / "analytics.db"
        if self._analytics is not None and hasattr(self._analytics, "db_path"):
            db_path = Path(self._analytics.db_path)
        if not db_path.is_file():
            return 0
        cutoff = time.time() - days * 86400
        try:
            conn = sqlite3.connect(str(db_path), timeout=30.0)
            try:
                cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                deleted = int(cur.rowcount or 0)
                conn.commit()
                conn.execute("VACUUM")
                conn.commit()
                return max(0, deleted)
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("analytics rotate failed: %s", exc)
            return 0

    # ------------------------------------------------------------------
    # DOM helpers
    # ------------------------------------------------------------------

    async def _open_mail(self, page: Page, frame: Optional[Frame]) -> bool:
        if await self._click_first(page, frame, MAIL_ENTRY_SELECTORS):
            return True
        # Уже на странице почты?
        html = await self._frame_html(page, frame)
        return bool(
            re.search(r"почт|mail|письм|inbox", html, re.I)
        )

    def _looks_like_system_mail(self, html: str) -> bool:
        text = BeautifulSoup(html, "html.parser").get_text(" ", strip=True).lower()
        return any(
            k in text
            for k in (
                "система",
                "system",
                "аукцион",
                "auction",
                "уведомлен",
                "администрац",
            )
        )

    def _find_expired_buffs(
        self, soup: BeautifulSoup, html: str
    ) -> List[str]:
        found: List[str] = []
        for sel in BUFF_ITEM_SELECTORS:
            try:
                nodes = soup.select(sel)
            except Exception:
                continue
            for node in nodes:
                blob = " ".join(
                    filter(
                        None,
                        [
                            node.get_text(" ", strip=True),
                            str(node.get("title") or ""),
                            str(node.get("alt") or ""),
                            " ".join(node.get("class", []) or []),
                        ],
                    )
                )
                low = blob.lower()
                if any(h in low for h in BUFF_EXPIRED_HINTS):
                    name = (
                        str(node.get("title") or node.get("data-buff") or blob[:40])
                        .strip()
                    )
                    if name and name not in found:
                        found.append(name)
        # Текстовый фоллбек
        if not found and any(h in html.lower() for h in BUFF_EXPIRED_HINTS):
            found.append("buff_expired")
        return found

    async def _try_refresh_buff(
        self, page: Page, frame: Optional[Frame], name: str
    ) -> bool:
        safe = name.replace("'", "\\'")[:40]
        selectors = [
            f"[title*='{safe}' i]",
            f"a:has-text('{safe}')",
            f"button:has-text('{safe}')",
            *BUFF_USE_SELECTORS,
        ]
        # Долгие эликсиры в рюкзаке / поясе
        selectors.extend(
            [
                "a[href*='elixir']",
                "a[href*='buff']",
                ".inv-item[data-type='elixir']",
                "[data-item-type='buff']",
            ]
        )
        return await self._click_first(page, frame, selectors)

    async def _click_first(
        self,
        page: Page,
        frame: Optional[Frame],
        selectors: Sequence[str],
    ) -> bool:
        for sel in selectors:
            if await self._human_click(page, sel, frame=frame):
                return True
        return False

    async def _human_click(
        self,
        page: Page,
        selector: str,
        *,
        frame: Optional[Frame] = None,
    ) -> bool:
        if not selector:
            return False
        owner: Any = frame or page

        if ":has-text" in selector or selector.startswith("text=") or " i]" in selector:
            try:
                loc = owner.locator(selector)
                if await loc.count() == 0:
                    return False
                handle = await loc.first.element_handle()
                if handle is None:
                    return False
                return await self._human_click_handle(page, handle, frame=frame)
            except Exception as exc:
                logger.debug("locator click %s: %s", selector[:60], exc)
                return False

        try:
            handle = await owner.query_selector(selector)
            if handle is None:
                return False
            if self._browser is not None:
                await self._browser.human_click(selector, page=page, frame=frame)
                return True
            await self._human.bezier_mouse_move(
                page, selector, frame=frame, timeout_ms=4_000
            )
            await asyncio.sleep(random.uniform(0.08, 0.22))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.04, 0.1))
            await page.mouse.up()
            return True
        except Exception as exc:
            logger.debug("human_click %s: %s", selector[:60], exc)
            return False

    async def _human_click_handle(
        self,
        page: Page,
        handle: Any,
        *,
        frame: Optional[Frame] = None,
    ) -> bool:
        try:
            if self._browser is not None:
                await self._browser.human_click(handle, page=page, frame=frame)
                return True
            box = await handle.bounding_box()
            if box is None:
                await handle.click(timeout=4_000)
                return True
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.08, 0.2))
            await page.mouse.down()
            await asyncio.sleep(random.uniform(0.04, 0.1))
            await page.mouse.up()
            return True
        except (PlaywrightError, BrowserEngineError) as exc:
            logger.debug("human_click handle: %s", exc)
            return False

    async def _human_pause(self, kind: str = "action") -> None:
        try:
            lo, hi = get_delay_range(
                kind if kind in {"click", "action", "navigation", "combat"} else "action"
            )
        except KeyError:
            lo, hi = 0.5, 1.5
        await asyncio.sleep(random.uniform(lo, hi))

    async def _resolve_main_frame(self, page: Page) -> Optional[Frame]:
        return await self._resolve_frame(
            page,
            css=self._config.selectors.main_frame,
            names=MAIL_FRAME_NAMES,
        )

    async def _resolve_user_frame(self, page: Page) -> Optional[Frame]:
        return await self._resolve_frame(
            page,
            css=getattr(self._config.selectors, "backpack_frame", "")
            or self._config.selectors.main_frame,
            names=("user", "pers", "main", "character", "stats"),
        )

    async def _resolve_frame(
        self,
        page: Page,
        *,
        css: str,
        names: Sequence[str],
    ) -> Optional[Frame]:
        wanted = {n.lower() for n in names}
        try:
            for fr in page.frames:
                fname = (fr.name or "").lower()
                if fname in wanted:
                    return fr
                url = (fr.url or "").lower()
                if any(n in url for n in wanted):
                    return fr
        except PlaywrightError:
            pass
        if self._browser is not None:
            try:
                fr = await self._browser.get_frame(css)
                if fr is not None:
                    return fr
            except Exception:
                pass
        return page.main_frame

    async def _frame_html(
        self, page: Page, frame: Optional[Frame]
    ) -> str:
        target: Union[Page, Frame] = frame or page
        try:
            return await target.content()
        except PlaywrightError:
            try:
                return await page.content()
            except PlaywrightError:
                return ""

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    def list_tasks(self) -> List[Dict[str, Any]]:
        return [t.as_dict() for t in self._tasks.values()]

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def status_summary(self) -> str:
        parts = [
            f"tasks={len(self._tasks)}",
            f"running={self._running}",
            f"combat={self._in_combat}",
            f"mail={self._last_mail_ok}",
            f"daily={self._last_daily_ok}",
            f"buffs={self._last_buffs_ok}",
        ]
        return "scheduler[" + " ".join(parts) + "]"


__all__ = [
    "ScheduledTask",
    "BackgroundScheduler",
    "PRIORITY_LOW",
    "PRIORITY_NORMAL",
    "PRIORITY_HIGH",
    "TASK_CHECK_MAIL",
    "TASK_DAILY_GIFT",
    "TASK_REFRESH_BUFFS",
    "TASK_CLEANUP",
]
