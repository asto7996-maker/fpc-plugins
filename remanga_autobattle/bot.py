"""
bot.py — точка входа Telegram-бота управления автобоями Remanga.

Настройки (URL боёв, интервал, таймаут, admin) вводятся прямо в Telegram.
Для старта нужен только BOT_TOKEN в .env (install.sh спросит его сам).
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
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from browser_service import BattleOutcome, BattleResult, BrowserService
from config import Config, load_config
from settings_store import load_settings, save_settings, update_settings

logger = logging.getLogger(__name__)


# ======================================================================
# FSM настроек (ввод в Telegram)
# ======================================================================


class SettingsStates(StatesGroup):
    """Мастер настройки / изменение параметров в чате."""

    waiting_battle_url = State()
    waiting_interval = State()
    waiting_timeout = State()


# ======================================================================
# Состояние автобоя
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
            last = (
                self.last_result.to_telegram()
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            lines.append(f"<code>{last}</code>")
        return "\n".join(lines)


# ======================================================================
# Middleware: только админ (первый /start закрепляет админа)
# ======================================================================


class AdminOnlyMiddleware(BaseMiddleware):
    """
    Если admin ещё не задан (0) — первый написавший становится админом.
    Дальше пускаем только его.
    """

    def __init__(self, app: "AutobattleApp") -> None:
        self.app = app

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return None

        admin_id = self.app.config.telegram_admin_id

        # Первый пользователь закрепляется как админ
        if admin_id <= 0:
            self.app.bind_admin(user.id)
            admin_id = user.id
            logger.info("Админ закреплён по первому сообщению: %s", user.id)

        if user.id != admin_id:
            if isinstance(event, Message):
                await event.answer("⛔ Доступ запрещён. Этот бот приватный.")
            logger.warning("Отклонён запрос от user_id=%s", user.id)
            return None

        return await handler(event, data)


# ======================================================================
# Клавиатуры
# ======================================================================


def main_reply_keyboard() -> ReplyKeyboardMarkup:
    """Кнопки управления прямо в боте."""
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
            [
                KeyboardButton(text="⚙️ Настройки"),
            ],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def settings_keyboard() -> ReplyKeyboardMarkup:
    """Подменю настроек."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌐 URL боёв"), KeyboardButton(text="⏱ Интервал")],
            [KeyboardButton(text="⌛ Таймаут"), KeyboardButton(text="📋 Показать настройки")],
            [KeyboardButton(text="◀️ Назад")],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


# ======================================================================
# Оркестратор
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
        self._setup_done_hint = (config.user_data_dir / "Default").exists()

        self.dp.message.middleware(AdminOnlyMiddleware(self))
        self._register_handlers()

    def bind_admin(self, user_id: int) -> None:
        """Сохранить админа в память + settings.json."""
        self.config.telegram_admin_id = user_id
        update_settings(telegram_admin_id=user_id)

    def _settings_text(self) -> str:
        return (
            "<b>Текущие настройки</b>\n\n"
            f"👤 Admin ID: <code>{self.config.telegram_admin_id}</code>\n"
            f"🌐 URL боёв: <code>{self.config.battle_url}</code>\n"
            f"⏱ Интервал: <b>{self.config.auto_battle_interval_sec}</b> сек\n"
            f"⌛ Таймаут: <b>{self.config.selector_timeout_ms // 1000}</b> сек\n"
            f"📁 Профиль: <code>{self.config.user_data_dir}</code>"
        )

    def _register_handlers(self) -> None:
        # --- /start ---
        @self.dp.message(CommandStart())
        async def cmd_start(message: Message, state: FSMContext) -> None:
            await state.clear()
            self._notify_chat_id = message.chat.id
            text = (
                "⚔️ <b>Remanga Autobattle</b>\n\n"
                "Управление автобоями на remanga.org.\n"
                f"Интервал: <b>{self.config.auto_battle_interval_sec} сек</b> "
                "(бесконечно до остановки).\n\n"
                f"{self._settings_text()}\n\n"
                "Параметры — кнопка <b>⚙️ Настройки</b>.\n"
                "Перед боями один раз сохраните сессию Remanga:\n"
                "<code>cd ~/remanga_autobattle && source .venv/bin/activate && python bot.py --setup</code>"
            )
            await message.answer(text, reply_markup=main_reply_keyboard())

            # Первый запуск — мастер настроек прямо в Telegram
            if not load_settings().setup_completed:
                await state.update_data(wizard=True)
                await message.answer(
                    "🔧 Первый запуск — настроим бота в чате.\n"
                    "Отправьте URL страницы боёв или «Пропустить»:\n"
                    f"<code>{self.config.battle_url}</code>",
                    reply_markup=ReplyKeyboardMarkup(
                        keyboard=[
                            [KeyboardButton(text="Пропустить")],
                            [KeyboardButton(text="❌ Отмена")],
                        ],
                        resize_keyboard=True,
                    ),
                )
                await state.set_state(SettingsStates.waiting_battle_url)

        @self.dp.message(Command("help"))
        async def cmd_help(message: Message) -> None:
            await message.answer(
                "<b>Кнопки:</b>\n"
                "▶️ Запустить автобой\n"
                "⏹ Остановить автобой\n"
                "⚔️ Сделать 1 бой\n"
                "📊 Статус\n"
                "⚙️ Настройки — URL, интервал, таймаут\n",
                reply_markup=main_reply_keyboard(),
            )

        # --- Основные кнопки ---
        @self.dp.message(StateFilter(None), F.text.in_({"▶️ Запустить автобой", "Запустить автобой"}))
        @self.dp.message(StateFilter(None), Command("auto"))
        async def start_auto_msg(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            text = await self.start_autobattle()
            await message.answer(text, reply_markup=main_reply_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"⏹ Остановить автобой", "Остановить автобой"}))
        @self.dp.message(StateFilter(None), Command("stop"))
        async def stop_auto_msg(message: Message) -> None:
            text = await self.stop_autobattle()
            await message.answer(text, reply_markup=main_reply_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"⚔️ Сделать 1 бой", "Сделать 1 бой"}))
        @self.dp.message(StateFilter(None), Command("battle"))
        async def one_battle_msg(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer("⏳ Запускаю один бой...", reply_markup=main_reply_keyboard())
            await self.run_single_battle(notify=True)

        @self.dp.message(StateFilter(None), F.text.in_({"📊 Статус", "Статус"}))
        @self.dp.message(StateFilter(None), Command("status"))
        async def status_msg(message: Message) -> None:
            await message.answer(
                self.state.status_text(self.config.auto_battle_interval_sec),
                reply_markup=main_reply_keyboard(),
            )

        # --- Меню настроек ---
        @self.dp.message(StateFilter(None), F.text.in_({"⚙️ Настройки", "Настройки"}))
        @self.dp.message(StateFilter(None), Command("settings"))
        async def open_settings(message: Message) -> None:
            await message.answer(
                self._settings_text() + "\n\nЧто изменить?",
                reply_markup=settings_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text == "◀️ Назад")
        async def settings_back(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer("Главное меню:", reply_markup=main_reply_keyboard())

        @self.dp.message(StateFilter(None), F.text == "📋 Показать настройки")
        async def show_settings(message: Message) -> None:
            await message.answer(self._settings_text(), reply_markup=settings_keyboard())

        @self.dp.message(StateFilter(None), F.text == "🌐 URL боёв")
        async def ask_url(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_battle_url)
            await state.update_data(wizard=False)
            await message.answer(
                "Отправьте URL страницы боёв Remanga:\n"
                f"Сейчас: <code>{self.config.battle_url}</code>",
                reply_markup=cancel_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text == "⏱ Интервал")
        async def ask_interval(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_interval)
            await state.update_data(wizard=False)
            await message.answer(
                "Отправьте интервал автобоя в <b>секундах</b> (например 30):\n"
                f"Сейчас: <b>{self.config.auto_battle_interval_sec}</b>",
                reply_markup=cancel_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text == "⌛ Таймаут")
        async def ask_timeout(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_timeout)
            await state.update_data(wizard=False)
            await message.answer(
                "Отправьте таймаут ожидания кнопки в <b>секундах</b> (например 30):\n"
                f"Сейчас: <b>{self.config.selector_timeout_ms // 1000}</b>",
                reply_markup=cancel_keyboard(),
            )

        @self.dp.message(F.text.in_({"❌ Отмена", "Отмена"}))
        async def cancel_input(message: Message, state: FSMContext) -> None:
            data = await state.get_data()
            if data.get("wizard"):
                update_settings(setup_completed=True)
            await state.clear()
            await message.answer("Отменено. Настройки можно изменить позже.", reply_markup=main_reply_keyboard())

        # --- Ввод значений ---
        @self.dp.message(SettingsStates.waiting_battle_url)
        async def set_battle_url(message: Message, state: FSMContext) -> None:
            text = (message.text or "").strip()
            if text.lower() in {"пропустить", "skip", "-"}:
                url = self.config.battle_url
            else:
                if not (text.startswith("http://") or text.startswith("https://")):
                    await message.answer(
                        "Нужен полный URL, например:\n<code>https://remanga.org/cards</code>"
                    )
                    return
                url = text

            self.config.battle_url = url
            update_settings(battle_url=url)
            data = await state.get_data()
            if not data.get("wizard"):
                await state.clear()
                await message.answer(
                    f"✅ URL сохранён: <code>{url}</code>",
                    reply_markup=settings_keyboard(),
                )
                return

            await state.set_state(SettingsStates.waiting_interval)
            await message.answer(
                f"✅ URL сохранён: <code>{url}</code>\n\n"
                "Интервал автобоя в секундах (например <b>30</b>) или «Пропустить»:",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [KeyboardButton(text="30"), KeyboardButton(text="60")],
                        [KeyboardButton(text="Пропустить"), KeyboardButton(text="❌ Отмена")],
                    ],
                    resize_keyboard=True,
                ),
            )

        @self.dp.message(SettingsStates.waiting_interval)
        async def set_interval(message: Message, state: FSMContext) -> None:
            text = (message.text or "").strip().lower()
            if text in {"пропустить", "skip", "-"}:
                interval = self.config.auto_battle_interval_sec
            else:
                try:
                    interval = int(text)
                except ValueError:
                    await message.answer("Введите целое число секунд, например 30")
                    return
                if interval < 5:
                    await message.answer("Минимум 5 секунд.")
                    return

            self.config.auto_battle_interval_sec = interval
            update_settings(auto_battle_interval_sec=interval)
            if self.state.running:
                await self._reschedule()

            data = await state.get_data()
            if not data.get("wizard"):
                await state.clear()
                await message.answer(
                    f"✅ Интервал: <b>{interval}</b> сек",
                    reply_markup=settings_keyboard(),
                )
                return

            await state.set_state(SettingsStates.waiting_timeout)
            await message.answer(
                f"✅ Интервал: <b>{interval}</b> сек\n\n"
                "Таймаут ожидания кнопки в секундах (например <b>30</b>) или «Пропустить»:",
                reply_markup=ReplyKeyboardMarkup(
                    keyboard=[
                        [KeyboardButton(text="30"), KeyboardButton(text="60")],
                        [KeyboardButton(text="Пропустить"), KeyboardButton(text="❌ Отмена")],
                    ],
                    resize_keyboard=True,
                ),
            )

        @self.dp.message(SettingsStates.waiting_timeout)
        async def set_timeout(message: Message, state: FSMContext) -> None:
            text = (message.text or "").strip().lower()
            if text in {"пропустить", "skip", "-"}:
                timeout_sec = self.config.selector_timeout_ms // 1000
            else:
                try:
                    timeout_sec = int(text)
                except ValueError:
                    await message.answer("Введите целое число секунд, например 30")
                    return
                if timeout_sec < 5:
                    await message.answer("Минимум 5 секунд.")
                    return

            self.config.selector_timeout_ms = timeout_sec * 1000
            data = await state.get_data()
            update_settings(
                selector_timeout_ms=self.config.selector_timeout_ms,
                setup_completed=True,
            )
            await state.clear()
            kb = main_reply_keyboard() if data.get("wizard") else settings_keyboard()
            await message.answer(
                f"✅ Таймаут: <b>{timeout_sec}</b> сек\n\n"
                f"{self._settings_text()}\n\n"
                "Готово!",
                reply_markup=kb,
            )

    async def _reschedule(self) -> None:
        """Перезапустить job автобоя с актуальным интервалом."""
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

    # ------------------------------------------------------------------
    # Планировщик
    # ------------------------------------------------------------------

    async def start_autobattle(self) -> str:
        if self.state.running:
            return "ℹ️ Автобой уже запущен."

        if not self.browser.is_started:
            try:
                await self.browser.start(headless=True)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Не удалось запустить браузер")
                return (
                    f"⚠️ Не удалось запустить браузер: <code>{exc}</code>\n"
                    "Сначала setup:\n"
                    "<code>cd ~/remanga_autobattle && source .venv/bin/activate && python bot.py --setup</code>"
                )

        await self._reschedule()
        if not self.scheduler.running:
            self.scheduler.start()

        self.state.running = True
        logger.info("Автобой запущен, интервал=%s", self.config.auto_battle_interval_sec)
        asyncio.create_task(self.run_single_battle(notify=True))

        return (
            f"✅ Автобой <b>запущен</b> (бесконечно).\n"
            f"⏱ Интервал: {self.config.auto_battle_interval_sec} сек.\n"
            "Остановка — «Остановить автобой».\n"
            "Отчёт — текст с кнопки на сайте."
        )

    async def stop_autobattle(self) -> str:
        if not self.state.running and not self.scheduler.get_job(self.state.job_id):
            self.state.running = False
            return "ℹ️ Автобой уже остановлен."

        if self.scheduler.get_job(self.state.job_id):
            self.scheduler.remove_job(self.state.job_id)
        self.state.running = False
        return "⏹ Автобой <b>остановлен</b>."

    async def _scheduled_battle(self) -> None:
        if not self.state.running:
            return
        await self.run_single_battle(notify=True)

    async def run_single_battle(self, notify: bool = True) -> BattleResult:
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

    async def run(self) -> None:
        logger.info(
            "Запуск бота (admin_id=%s). Настройки — в Telegram.",
            self.config.telegram_admin_id or "будет закреплён при первом /start",
        )
        try:
            await self.browser.start(headless=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Браузер не стартовал сразу: %s", exc)

        if not self.scheduler.running:
            self.scheduler.start()

        try:
            await self.dp.start_polling(self.bot)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("Shutdown...")
        self.state.running = False
        try:
            if self.scheduler.running:
                self.scheduler.shutdown(wait=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Ошибка остановки scheduler: %s", exc)
        await self.browser.stop()
        await self.bot.session.close()


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
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
