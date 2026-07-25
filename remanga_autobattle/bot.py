"""
bot.py — точка входа Telegram-бота управления автобоями Remanga.

Стек:
- aiogram 3.x (роутеры, middleware, inline/reply-клавиатуры)
- APScheduler (AsyncIOScheduler) для периодических боёв
- BrowserService (Playwright Persistent Context)

Запуск:
    1) python browser_service.py   # разовый setup (ручной вход)
    2) python bot.py               # основной режим
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from browser_service import BattleOutcome, BattleResult, BrowserService
from config import Config, load_config

logger = logging.getLogger(__name__)


# ======================================================================
# Состояние автобоя (в памяти процесса)
# ======================================================================


@dataclass
class AutobattleState:
    """Сводная статистика и флаги текущего сеанса."""

    running: bool = False
    total_battles: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    skipped: int = 0
    errors: int = 0
    last_result: Optional[BattleResult] = field(default=None)
    job_id: str = "autobattle_job"

    def register(self, result: BattleResult) -> None:
        """Учесть исход боя в счётчиках."""
        self.last_result = result
        if result.outcome == BattleOutcome.SKIPPED:
            self.skipped += 1
            return
        if result.outcome == BattleOutcome.ERROR:
            self.errors += 1
            return

        self.total_battles += 1
        if result.outcome == BattleOutcome.WIN:
            self.wins += 1
        elif result.outcome == BattleOutcome.LOSE:
            self.losses += 1
        elif result.outcome == BattleOutcome.DRAW:
            self.draws += 1

    def status_text(self, interval_sec: int) -> str:
        """Текст для команды «Статус»."""
        flag = "🟢 Активен" if self.running else "🔴 Остановлен"
        lines = [
            f"<b>Статус автобоя:</b> {flag}",
            f"⏱ Интервал: {interval_sec} сек",
            f"⚔️ Проведено боёв: {self.total_battles}",
            f"🏆 Победы: {self.wins}",
            f"💀 Поражения: {self.losses}",
            f"🤝 Ничьи: {self.draws}",
            f"⏸ Пропуски: {self.skipped}",
            f"⚠️ Ошибки: {self.errors}",
        ]
        if self.last_result:
            lines.append("")
            lines.append("<b>Последний бой:</b>")
            # to_telegram без HTML — экранируем минимально через замену
            last = (
                self.last_result.to_telegram()
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            lines.append(f"<code>{last}</code>")
        return "\n".join(lines)


# ======================================================================
# Middleware: доступ только для TELEGRAM_ADMIN_ID
# ======================================================================


class AdminOnlyMiddleware(BaseMiddleware):
    """
    Пропускает апдейты только от пользователя с admin_id.
    Остальным — тихий отказ (или короткое сообщение на /start).
    """

    def __init__(self, admin_id: int) -> None:
        self.admin_id = admin_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or user.id != self.admin_id:
            # На личные сообщения можно ответить отказом
            if isinstance(event, Message):
                await event.answer("⛔ Доступ запрещён. Этот бот приватный.")
            elif isinstance(event, CallbackQuery):
                await event.answer("⛔ Нет доступа", show_alert=True)
            logger.warning(
                "Отклонён запрос от user_id=%s",
                getattr(user, "id", None),
            )
            return None
        return await handler(event, data)


# ======================================================================
# Клавиатуры
# ======================================================================


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Постоянная клавиатура под полем ввода."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="▶️ Запустить автобой"),
                KeyboardButton(text="⏹ Остановить автобой"),
            ],
            [
                KeyboardButton(text="⚔️ Сделать 1 бой"),
                KeyboardButton(text="📊 Статус"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def main_inline_keyboard() -> InlineKeyboardMarkup:
    """Дублирующие inline-кнопки (удобно в закреплённом сообщении)."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ Запустить", callback_data="start_auto"),
                InlineKeyboardButton(text="⏹ Стоп", callback_data="stop_auto"),
            ],
            [
                InlineKeyboardButton(text="⚔️ 1 бой", callback_data="one_battle"),
                InlineKeyboardButton(text="📊 Статус", callback_data="status"),
            ],
        ]
    )


# ======================================================================
# Оркестратор: связка бот ↔ браузер ↔ планировщик
# ======================================================================


class AutobattleApp:
    """Склеивает Telegram, BrowserService и APScheduler."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.bot = Bot(
            token=config.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self.browser = BrowserService(config)
        self.scheduler = AsyncIOScheduler(timezone="Europe/Moscow")
        self.state = AutobattleState()
        self._battle_lock = asyncio.Lock()
        self._notify_chat_id: Optional[int] = None

        # Middleware на все сообщения и колбэки
        self.dp.message.middleware(AdminOnlyMiddleware(config.telegram_admin_id))
        self.dp.callback_query.middleware(AdminOnlyMiddleware(config.telegram_admin_id))

        self._register_handlers()

    # ------------------------------------------------------------------
    # Регистрация хэндлеров
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        @self.dp.message(CommandStart())
        async def cmd_start(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(
                "⚔️ <b>Remanga Autobattle</b>\n\n"
                "Управление автобоями на remanga.org.\n"
                "Перед первым запуском выполните setup:\n"
                "<code>python browser_service.py</code>\n\n"
                "Выберите действие на клавиатуре:",
                reply_markup=main_reply_keyboard(),
            )
            await message.answer(
                "Быстрые кнопки:",
                reply_markup=main_inline_keyboard(),
            )

        @self.dp.message(Command("help"))
        async def cmd_help(message: Message) -> None:
            await message.answer(
                "<b>Команды:</b>\n"
                "/start — меню\n"
                "/battle — один бой\n"
                "/auto — запустить автобой\n"
                "/stop — остановить автобой\n"
                "/status — статус\n\n"
                "Также доступны кнопки под полем ввода.",
                reply_markup=main_reply_keyboard(),
            )

        # --- Reply-кнопки / текстовые команды ---
        @self.dp.message(F.text.in_({"▶️ Запустить автобой", "Запустить автобой"}))
        @self.dp.message(Command("auto"))
        async def start_auto_msg(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            text = await self.start_autobattle()
            await message.answer(text, reply_markup=main_reply_keyboard())

        @self.dp.message(F.text.in_({"⏹ Остановить автобой", "Остановить автобой"}))
        @self.dp.message(Command("stop"))
        async def stop_auto_msg(message: Message) -> None:
            text = await self.stop_autobattle()
            await message.answer(text, reply_markup=main_reply_keyboard())

        @self.dp.message(F.text.in_({"⚔️ Сделать 1 бой", "Сделать 1 бой"}))
        @self.dp.message(Command("battle"))
        async def one_battle_msg(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer("⏳ Запускаю один бой...")
            await self.run_single_battle(notify=True)

        @self.dp.message(F.text.in_({"📊 Статус", "Статус"}))
        @self.dp.message(Command("status"))
        async def status_msg(message: Message) -> None:
            await message.answer(
                self.state.status_text(self.config.auto_battle_interval_sec),
                reply_markup=main_reply_keyboard(),
            )

        # --- Inline-колбэки ---
        @self.dp.callback_query(F.data == "start_auto")
        async def start_auto_cb(callback: CallbackQuery) -> None:
            if callback.message:
                self._notify_chat_id = callback.message.chat.id
            text = await self.start_autobattle()
            await callback.answer("Автобой")
            if callback.message:
                await callback.message.answer(text)

        @self.dp.callback_query(F.data == "stop_auto")
        async def stop_auto_cb(callback: CallbackQuery) -> None:
            text = await self.stop_autobattle()
            await callback.answer("Стоп")
            if callback.message:
                await callback.message.answer(text)

        @self.dp.callback_query(F.data == "one_battle")
        async def one_battle_cb(callback: CallbackQuery) -> None:
            if callback.message:
                self._notify_chat_id = callback.message.chat.id
            await callback.answer("Запускаю бой...")
            if callback.message:
                await callback.message.answer("⏳ Запускаю один бой...")
            await self.run_single_battle(notify=True)

        @self.dp.callback_query(F.data == "status")
        async def status_cb(callback: CallbackQuery) -> None:
            await callback.answer()
            if callback.message:
                await callback.message.answer(
                    self.state.status_text(self.config.auto_battle_interval_sec)
                )

    # ------------------------------------------------------------------
    # Управление планировщиком
    # ------------------------------------------------------------------

    async def start_autobattle(self) -> str:
        """Запустить периодическую задачу автобоя."""
        if self.state.running:
            return "ℹ️ Автобой уже запущен."

        # Гарантируем, что браузер поднят
        if not self.browser.is_started:
            try:
                await self.browser.start(headless=True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Не удалось запустить браузер")
                return (
                    f"⚠️ Не удалось запустить браузер: <code>{exc}</code>\n"
                    "Сначала выполните setup: <code>python browser_service.py</code>"
                )

        # Удаляем старую задачу, если вдруг осталась
        if self.scheduler.get_job(self.state.job_id):
            self.scheduler.remove_job(self.state.job_id)

        self.scheduler.add_job(
            self._scheduled_battle,
            trigger=IntervalTrigger(seconds=self.config.auto_battle_interval_sec),
            id=self.state.job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        if not self.scheduler.running:
            self.scheduler.start()

        self.state.running = True
        logger.info(
            "Автобой запущен, интервал=%s сек",
            self.config.auto_battle_interval_sec,
        )

        # Сразу один бой, не дожидаясь первого тика интервала
        asyncio.create_task(self.run_single_battle(notify=True))

        return (
            f"✅ Автобой <b>запущен</b>.\n"
            f"Интервал: {self.config.auto_battle_interval_sec} сек.\n"
            "После каждого боя придёт краткий отчёт."
        )

    async def stop_autobattle(self) -> str:
        """Остановить планировщик автобоя (браузер оставляем живым)."""
        if not self.state.running and not self.scheduler.get_job(self.state.job_id):
            self.state.running = False
            return "ℹ️ Автобой уже остановлен."

        if self.scheduler.get_job(self.state.job_id):
            self.scheduler.remove_job(self.state.job_id)

        self.state.running = False
        logger.info("Автобой остановлен.")
        return "⏹ Автобой <b>остановлен</b>. Планировщик сброшен."

    async def _scheduled_battle(self) -> None:
        """Обёртка для APScheduler."""
        if not self.state.running:
            return
        await self.run_single_battle(notify=True)

    async def run_single_battle(self, notify: bool = True) -> BattleResult:
        """Выполнить один бой и (опционально) отправить отчёт в Telegram."""
        async with self._battle_lock:
            try:
                if not self.browser.is_started:
                    await self.browser.start(headless=True)
                result = await self.browser.do_battle()
            except Exception as exc:  # noqa: BLE001
                logger.exception("Критическая ошибка боя")
                result = BattleResult(
                    outcome=BattleOutcome.ERROR,
                    message=f"Критическая ошибка: {exc}",
                )

            self.state.register(result)

            if notify and self._notify_chat_id is not None:
                try:
                    await self.bot.send_message(
                        self._notify_chat_id,
                        "📋 <b>Отчёт о бое</b>\n\n"
                        + result.to_telegram()
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;"),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Не удалось отправить отчёт в Telegram: %s", exc)

            return result

    # ------------------------------------------------------------------
    # Жизненный цикл приложения
    # ------------------------------------------------------------------

    async def run(self) -> None:
        """Старт бота (long polling)."""
        logger.info("Запуск Remanga Autobattle Bot (admin_id=%s)", self.config.telegram_admin_id)

        # Пробуем заранее поднять браузер — быстрее первый бой
        try:
            await self.browser.start(headless=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Браузер не стартовал при запуске бота (%s). "
                "Будет повторная попытка при первом бое. "
                "Если сессии нет — выполните: python browser_service.py",
                exc,
            )

        if not self.scheduler.running:
            self.scheduler.start()

        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Корректное завершение: планировщик + браузер + сессия бота."""
        logger.info("Shutdown...")
        self.state.running = False
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ошибка остановки scheduler: %s", exc)

        await self.browser.stop()
        await self.bot.session.close()
        logger.info("Shutdown завершён.")


# ======================================================================
# main
# ======================================================================


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    # Приглушаем болтливые логгеры сторонних библиотек
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)

    config = load_config()
    app = AutobattleApp(config)
    await app.run()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Remanga Autobattle Telegram Bot")
    parser.add_argument(
        "--setup",
        action="store_true",
        help="Режим ручного входа (headless=False), сохраняет сессию в user_data",
    )
    args = parser.parse_args()

    if args.setup:
        # Делегируем в browser_service.run_setup
        from browser_service import main_setup

        try:
            asyncio.run(main_setup())
        except KeyboardInterrupt:
            print("\nSetup прерван")
        return

    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nВыход по Ctrl+C")


if __name__ == "__main__":
    main()
