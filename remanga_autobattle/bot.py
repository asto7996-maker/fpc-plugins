"""
bot.py — Telegram-бот: Remanga.org (автобои) + MangaBuff.ru (авточтение / награды).

Двухуровневое меню:
  Главное → Remanga / MangaBuff
  Remanga → автобой, 1 бой, статус, уведомления, рейтинг...
  MangaBuff → авточтение, награды, задержки, статус
"""

from __future__ import annotations

import asyncio
import logging
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup, TelegramObject

from config import Config, load_config
from scheduler import (
    JOB_MANGABUFF_READ,
    JOB_REMANGA_AUTOBATTLE,
    AppScheduler,
)
from services.mangabuff_service import MangaBuffService
from services.remanga_service import BattleOutcome, BattleResult, BrowserService
from settings_store import (
    load_settings,
    toggle_notify,
    update_notify,
    update_settings,
)
from stats_store import (
    get_cached_rating,
    load_stats,
    save_stats,
    update_stats_from_result,
)

logger = logging.getLogger(__name__)


# ======================================================================
# FSM
# ======================================================================


class SettingsStates(StatesGroup):
    waiting_battle_url = State()
    waiting_interval = State()
    waiting_timeout = State()
    waiting_summary_every = State()
    waiting_mangabuff_url = State()


# ======================================================================
# Remanga session stats
# ======================================================================


@dataclass
class AutobattleState:
    running: bool = False
    total_battles: int = 0
    wins: int = 0
    losses: int = 0
    draws: int = 0
    skipped: int = 0
    errors: int = 0
    last_result: Optional[BattleResult] = field(default=None)

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
            f"<b>Remanga — статус</b>",
            f"Автобой: {flag}",
            f"⏱ Интервал: {interval_sec} сек",
            f"⚔️ Боёв: {self.total_battles}",
            f"🏆 {self.wins} · 💀 {self.losses} · 🤝 {self.draws}",
            f"⏸ Пропуски: {self.skipped} · ⚠️ Ошибки: {self.errors}",
        ]
        if self.last_result:
            last = (
                self.last_result.to_telegram()
                .replace("&", "&amp;")
                .replace("<", "&lt;")
                .replace(">", "&gt;")
            )
            lines += ["", "<b>Последний бой:</b>", f"<code>{last}</code>"]
        return "\n".join(lines)


# ======================================================================
# Middleware
# ======================================================================


class AdminOnlyMiddleware(BaseMiddleware):
    def __init__(self, app: "App") -> None:
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
        if admin_id <= 0:
            self.app.bind_admin(user.id)
            admin_id = user.id
            logger.info("Админ закреплён: %s", user.id)
        if user.id != admin_id:
            if isinstance(event, Message):
                await event.answer("⛔ Доступ запрещён.")
            return None
        return await handler(event, data)


# ======================================================================
# Клавиатуры (двухуровневое меню)
# ======================================================================


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚔️ Remanga.org"), KeyboardButton(text="📚 MangaBuff.ru")],
            [KeyboardButton(text="ℹ️ Помощь")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def remanga_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="▶️ Запустить автобой"),
                KeyboardButton(text="⏹ Остановить автобой"),
            ],
            [
                KeyboardButton(text="⚔️ Сделать 1 бой"),
                KeyboardButton(text="📊 Статус Remanga"),
            ],
            [
                KeyboardButton(text="📈 Статистика"),
                KeyboardButton(text="🏅 Рейтинг"),
            ],
            [
                KeyboardButton(text="🔔 Уведомления"),
                KeyboardButton(text="⚙️ Настройки Remanga"),
            ],
            [KeyboardButton(text="🏠 Главное меню")],
        ],
        resize_keyboard=True,
    )


def mangabuff_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="▶️ Запустить фарм"),
                KeyboardButton(text="⏹ Остановить фарм"),
            ],
            [
                KeyboardButton(text="🎁 Собрать награды"),
                KeyboardButton(text="🗺 Изучить макеты"),
            ],
            [
                KeyboardButton(text="📊 Статус MangaBuff"),
                KeyboardButton(text="⏱ Настройки задержки"),
            ],
            [
                KeyboardButton(text="🔗 URL чтения"),
                KeyboardButton(text="🔐 Логин MangaBuff"),
            ],
            [KeyboardButton(text="🏠 Главное меню")],
        ],
        resize_keyboard=True,
    )


def delay_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚡ Быстро 5–8с"), KeyboardButton(text="📖 Норма 5–15с")],
            [KeyboardButton(text="🐢 Медленно 10–20с"), KeyboardButton(text="🧘 Очень медленно 15–30с")],
            [KeyboardButton(text="◀️ В меню MangaBuff")],
        ],
        resize_keyboard=True,
    )


def settings_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌐 URL боёв"), KeyboardButton(text="⏱ Интервал")],
            [KeyboardButton(text="⌛ Таймаут"), KeyboardButton(text="📋 Показать настройки")],
            [KeyboardButton(text="◀️ В меню Remanga")],
        ],
        resize_keyboard=True,
    )


def notify_keyboard() -> ReplyKeyboardMarkup:
    ns = load_settings().notify_settings()

    def lab(title: str, on: bool) -> str:
        return f"{'✅' if on else '❌'} {title}"

    summary = (
        f"📋 Сводка: {ns.notify_summary_every}"
        if ns.notify_summary_every > 0
        else "📋 Сводка: выкл"
    )
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=lab("Победы", ns.notify_wins)), KeyboardButton(text=lab("Поражения", ns.notify_losses))],
            [KeyboardButton(text=lab("Ничьи", ns.notify_draws)), KeyboardButton(text=lab("Пропуски", ns.notify_skipped))],
            [KeyboardButton(text=lab("Ошибки", ns.notify_errors)), KeyboardButton(text=lab("Старт/стоп", ns.notify_autobattle_start_stop))],
            [KeyboardButton(text=lab("Тихий режим", ns.quiet_mode)), KeyboardButton(text=summary)],
            [KeyboardButton(text="🔔 Все вкл"), KeyboardButton(text="🔕 Все выкл")],
            [KeyboardButton(text="◀️ В меню Remanga")],
        ],
        resize_keyboard=True,
    )


def rating_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔄 Обновить рейтинг")],
            [KeyboardButton(text="◀️ В меню Remanga")],
        ],
        resize_keyboard=True,
    )


def stats_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="♻️ Сбросить статистику")],
            [KeyboardButton(text="◀️ В меню Remanga")],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True,
    )


# ======================================================================
# App
# ======================================================================


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.bot = Bot(
            token=config.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self.scheduler = AppScheduler()

        # Remanga
        self.remanga = BrowserService(config)
        self.remanga_state = AutobattleState()
        self._battle_lock = asyncio.Lock()

        # MangaBuff — отдельный профиль
        self.mangabuff = MangaBuffService(
            config,
            user_data_dir=config.mangabuff_user_data_dir,
            start_url=config.mangabuff_start_url,
            delay_min_sec=config.mangabuff_delay_min_sec,
            delay_max_sec=config.mangabuff_delay_max_sec,
            email=config.mangabuff_email,
            password=config.mangabuff_password,
        )
        self._mb_task: Optional[asyncio.Task] = None

        self._notify_chat_id: Optional[int] = None
        self.dp.message.middleware(AdminOnlyMiddleware(self))
        self._register_handlers()

    def bind_admin(self, user_id: int) -> None:
        self.config.telegram_admin_id = user_id
        update_settings(telegram_admin_id=user_id)

    def _remanga_settings_text(self) -> str:
        return (
            "<b>Настройки Remanga</b>\n\n"
            f"🌐 URL: <code>{self.config.battle_url}</code>\n"
            f"⏱ Интервал: <b>{self.config.auto_battle_interval_sec}</b> сек\n"
            f"⌛ Таймаут: <b>{self.config.selector_timeout_ms // 1000}</b> сек"
        )

    # ------------------------------------------------------------------
    # Handlers
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        # ===== Главное меню =====
        @self.dp.message(CommandStart())
        async def cmd_start(message: Message, state: FSMContext) -> None:
            await state.clear()
            self._notify_chat_id = message.chat.id
            await message.answer(
                "🤖 <b>Мультибот автоматизации</b>\n\n"
                "Выберите модуль:",
                reply_markup=main_menu_keyboard(),
            )

        @self.dp.message(F.text.in_({"🏠 Главное меню", "Главное меню"}))
        @self.dp.message(Command("menu"))
        async def go_main(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer("Главное меню — выберите модуль:", reply_markup=main_menu_keyboard())

        @self.dp.message(F.text.in_({"ℹ️ Помощь", "Помощь"}))
        @self.dp.message(Command("help"))
        async def cmd_help(message: Message) -> None:
            await message.answer(
                "<b>Модули</b>\n"
                "⚔️ <b>Remanga.org</b> — автобои murim-cards\n"
                "📚 <b>MangaBuff.ru</b> — авточтение и сбор наград\n\n"
                "Сессии браузера раздельные:\n"
                f"• Remanga: <code>{self.config.user_data_dir.name}</code>\n"
                f"• MangaBuff: <code>{self.config.mangabuff_user_data_dir.name}</code>\n\n"
                "Setup MangaBuff на сервере:\n"
                "<code>cd /root/remanga_autobattle && source .venv/bin/activate && "
                "python -m services.mangabuff_service</code>",
                reply_markup=main_menu_keyboard(),
            )

        @self.dp.message(F.text.in_({"⚔️ Remanga.org", "Remanga.org", "Remanga"}))
        async def open_remanga(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(
                "⚔️ <b>Модуль Remanga.org</b>\nАвтобои карточных дуэлей.",
                reply_markup=remanga_menu_keyboard(),
            )

        @self.dp.message(F.text.in_({"📚 MangaBuff.ru", "MangaBuff.ru", "MangaBuff"}))
        async def open_mangabuff(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            s = load_settings()
            delay = f"{self.config.mangabuff_delay_min_sec:.0f}–{self.config.mangabuff_delay_max_sec:.0f}с"
            tip = ""
            if not s.mangabuff_setup_done:
                tip = (
                    "\n\n⚠️ Сначала сохраните сессию на сервере:\n"
                    "<code>systemctl stop remanga-autobattle && "
                    "cd /root/remanga_autobattle && source .venv/bin/activate && "
                    "python -m services.mangabuff_service && "
                    "systemctl start remanga-autobattle</code>"
                )
            await message.answer(
                f"📚 <b>Модуль MangaBuff.ru</b>\n"
                f"Авточтение и фарм наград.\n"
                f"⏱ Задержка: {delay}\n"
                f"🔗 URL: <code>{self.config.mangabuff_start_url}</code>"
                f"{tip}",
                reply_markup=mangabuff_menu_keyboard(),
            )

        @self.dp.message(F.text == "◀️ В меню Remanga")
        async def back_remanga(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer("Меню Remanga:", reply_markup=remanga_menu_keyboard())

        @self.dp.message(F.text.in_({"◀️ В меню MangaBuff", "В меню MangaBuff"}))
        async def back_mb(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer("Меню MangaBuff:", reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(F.text.in_({"❌ Отмена", "Отмена"}))
        async def cancel_any(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer("Отменено.", reply_markup=main_menu_keyboard())

        # ===== Remanga =====
        @self.dp.message(StateFilter(None), F.text.in_({"▶️ Запустить автобой", "Запустить автобой"}))
        @self.dp.message(StateFilter(None), Command("auto"))
        async def remanga_start(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            text = await self.start_remanga_autobattle()
            await message.answer(text, reply_markup=remanga_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"⏹ Остановить автобой", "Остановить автобой"}))
        @self.dp.message(StateFilter(None), Command("stop"))
        async def remanga_stop(message: Message) -> None:
            text = await self.stop_remanga_autobattle()
            await message.answer(text, reply_markup=remanga_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"⚔️ Сделать 1 бой", "Сделать 1 бой"}))
        @self.dp.message(StateFilter(None), Command("battle"))
        async def remanga_one(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer("⏳ Один бой Remanga...", reply_markup=remanga_menu_keyboard())
            await self.run_remanga_battle(notify=True)

        @self.dp.message(StateFilter(None), F.text.in_({"📊 Статус Remanga", "Статус Remanga"}))
        async def remanga_status(message: Message) -> None:
            await message.answer(
                self.remanga_state.status_text(self.config.auto_battle_interval_sec),
                reply_markup=remanga_menu_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text.in_({"📈 Статистика", "Статистика"}))
        @self.dp.message(StateFilter(None), Command("stats"))
        async def remanga_stats(message: Message) -> None:
            session = (
                f"<b>Сессия:</b> "
                f"{'🟢' if self.remanga_state.running else '🔴'} · "
                f"W{self.remanga_state.wins}/L{self.remanga_state.losses}"
            )
            await message.answer(load_stats().to_telegram(session_extra=session), reply_markup=stats_keyboard())

        @self.dp.message(StateFilter(None), F.text == "♻️ Сбросить статистику")
        async def stats_reset(message: Message) -> None:
            from stats_store import BattleStats

            save_stats(BattleStats())
            await message.answer("Статистика обнулена.", reply_markup=stats_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"🏅 Рейтинг", "Рейтинг"}))
        @self.dp.message(StateFilter(None), Command("rating"))
        async def rating_msg(message: Message) -> None:
            cached = get_cached_rating()
            text = cached.to_telegram()
            if not cached.rank and cached.glory is None:
                text += "\n\nНажмите «🔄 Обновить рейтинг»."
            await message.answer(text, reply_markup=rating_keyboard())

        @self.dp.message(StateFilter(None), F.text == "🔄 Обновить рейтинг")
        async def rating_refresh(message: Message) -> None:
            await message.answer("⏳ Читаю рейтинг...")
            try:
                info = await self.remanga.fetch_rating()
                stats = load_stats()
                stats.rating = asdict(info)
                save_stats(stats)
                await message.answer(info.to_telegram(), reply_markup=rating_keyboard())
            except Exception as exc:  # noqa: BLE001
                await message.answer(f"⚠️ {exc}", reply_markup=rating_keyboard())

        # --- Remanga notify ---
        @self.dp.message(StateFilter(None), F.text.in_({"🔔 Уведомления", "Уведомления"}))
        async def notify_menu(message: Message) -> None:
            await message.answer(
                load_settings().notify_settings().to_telegram() + "\n\nПереключите кнопкой:",
                reply_markup=notify_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text == "🔔 Все вкл")
        async def notify_all_on(message: Message) -> None:
            update_notify(
                notify_wins=True, notify_losses=True, notify_draws=True,
                notify_skipped=True, notify_errors=True,
                notify_autobattle_start_stop=True, quiet_mode=False,
            )
            await message.answer(load_settings().notify_settings().to_telegram(), reply_markup=notify_keyboard())

        @self.dp.message(StateFilter(None), F.text == "🔕 Все выкл")
        async def notify_all_off(message: Message) -> None:
            update_notify(
                notify_wins=False, notify_losses=False, notify_draws=False,
                notify_skipped=False, notify_errors=False,
                notify_autobattle_start_stop=False, quiet_mode=True,
                notify_summary_every=0,
            )
            await message.answer(load_settings().notify_settings().to_telegram(), reply_markup=notify_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.regexp(r"^(✅|❌)\s*(Победы|Поражения|Ничьи|Пропуски|Ошибки|Старт/стоп|Тихий режим)$"),
        )
        async def notify_toggle(message: Message) -> None:
            label = (message.text or "").split(maxsplit=1)[-1].strip()
            key_map = {
                "Победы": "notify_wins", "Поражения": "notify_losses",
                "Ничьи": "notify_draws", "Пропуски": "notify_skipped",
                "Ошибки": "notify_errors", "Старт/стоп": "notify_autobattle_start_stop",
                "Тихий режим": "quiet_mode",
            }
            key = key_map.get(label)
            if not key:
                return
            ns = toggle_notify(key)
            await message.answer(f"Переключено: <b>{label}</b>\n\n{ns.to_telegram()}", reply_markup=notify_keyboard())

        @self.dp.message(StateFilter(None), F.text.regexp(r"^📋 Сводка:"))
        async def notify_summary_ask(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_summary_every)
            await message.answer("Число боёв для сводки (0 = выкл):", reply_markup=cancel_keyboard())

        @self.dp.message(SettingsStates.waiting_summary_every)
        async def notify_summary_set(message: Message, state: FSMContext) -> None:
            try:
                n = int((message.text or "").strip())
            except ValueError:
                await message.answer("Введите целое число")
                return
            if n < 0:
                await message.answer(">= 0")
                return
            ns = update_notify(notify_summary_every=n)
            await state.clear()
            await message.answer(ns.to_telegram(), reply_markup=notify_keyboard())

        # --- Remanga settings ---
        @self.dp.message(StateFilter(None), F.text.in_({"⚙️ Настройки Remanga", "Настройки Remanga"}))
        async def remanga_settings(message: Message) -> None:
            await message.answer(self._remanga_settings_text() + "\n\nЧто изменить?", reply_markup=settings_keyboard())

        @self.dp.message(StateFilter(None), F.text == "📋 Показать настройки")
        async def show_settings(message: Message) -> None:
            await message.answer(self._remanga_settings_text(), reply_markup=settings_keyboard())

        @self.dp.message(StateFilter(None), F.text == "🌐 URL боёв")
        async def ask_url(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_battle_url)
            await message.answer(f"URL боёв:\nСейчас: <code>{self.config.battle_url}</code>", reply_markup=cancel_keyboard())

        @self.dp.message(SettingsStates.waiting_battle_url)
        async def set_url(message: Message, state: FSMContext) -> None:
            text = (message.text or "").strip()
            if not (text.startswith("http://") or text.startswith("https://")):
                await message.answer("Нужен полный URL")
                return
            self.config.battle_url = text
            update_settings(battle_url=text, setup_completed=True)
            await state.clear()
            await message.answer(f"✅ URL: <code>{text}</code>", reply_markup=settings_keyboard())

        @self.dp.message(StateFilter(None), F.text == "⏱ Интервал")
        async def ask_interval(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_interval)
            await message.answer("Интервал автобоя в секундах:", reply_markup=cancel_keyboard())

        @self.dp.message(SettingsStates.waiting_interval)
        async def set_interval(message: Message, state: FSMContext) -> None:
            try:
                interval = int((message.text or "").strip())
            except ValueError:
                await message.answer("Целое число")
                return
            if interval < 5:
                await message.answer("Минимум 5")
                return
            self.config.auto_battle_interval_sec = interval
            update_settings(auto_battle_interval_sec=interval)
            if self.remanga_state.running:
                self.scheduler.set_interval_job(
                    JOB_REMANGA_AUTOBATTLE, self._scheduled_remanga_battle, interval
                )
            await state.clear()
            await message.answer(f"✅ Интервал: {interval} сек", reply_markup=settings_keyboard())

        @self.dp.message(StateFilter(None), F.text == "⌛ Таймаут")
        async def ask_timeout(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_timeout)
            await message.answer("Таймаут ожидания в секундах:", reply_markup=cancel_keyboard())

        @self.dp.message(SettingsStates.waiting_timeout)
        async def set_timeout(message: Message, state: FSMContext) -> None:
            try:
                sec = int((message.text or "").strip())
            except ValueError:
                await message.answer("Целое число")
                return
            if sec < 5:
                await message.answer("Минимум 5")
                return
            self.config.selector_timeout_ms = sec * 1000
            update_settings(selector_timeout_ms=sec * 1000)
            await state.clear()
            await message.answer(f"✅ Таймаут: {sec} сек", reply_markup=settings_keyboard())

        # ===== MangaBuff =====
        @self.dp.message(
            StateFilter(None),
            F.text.in_({"▶️ Запустить фарм", "▶️ Запустить авточтение", "Запустить фарм"}),
        )
        async def mb_start(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            text = await self.start_mangabuff_read()
            await message.answer(text, reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"⏹ Остановить фарм", "⏹ Остановить авточтение", "Остановить фарм"}),
        )
        async def mb_stop(message: Message) -> None:
            text = await self.stop_mangabuff_read()
            await message.answer(text, reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text == "🎁 Собрать награды")
        async def mb_claim(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(
                "⏳ Собираю награды / карты / дейлики...",
                reply_markup=mangabuff_menu_keyboard(),
            )
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                result = await self.mangabuff.claim_rewards()
                await message.answer(result.to_telegram(), reply_markup=mangabuff_menu_keyboard())
            except Exception as exc:  # noqa: BLE001
                logger.exception("mangabuff claim")
                await message.answer(
                    f"⚠️ Ошибка сбора: <code>{exc}</code>",
                    reply_markup=mangabuff_menu_keyboard(),
                )

        @self.dp.message(StateFilter(None), F.text == "🗺 Изучить макеты")
        async def mb_layouts(message: Message) -> None:
            await message.answer("⏳ Обхожу разделы MangaBuff...", reply_markup=mangabuff_menu_keyboard())
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                n = await self.mangabuff.explore_layouts()
                await message.answer(
                    f"✅ Пройдено разделов: <b>{n}</b>",
                    reply_markup=mangabuff_menu_keyboard(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("mangabuff layouts")
                await message.answer(f"⚠️ {exc}", reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text == "🔐 Логин MangaBuff")
        async def mb_login(message: Message) -> None:
            await message.answer("⏳ Логин MangaBuff...", reply_markup=mangabuff_menu_keyboard())
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                ok = await self.mangabuff.ensure_login()
                if ok:
                    update_settings(mangabuff_setup_done=True)
                await message.answer(
                    "✅ Сессия MangaBuff активна" if ok else "⚠️ Логин не удался — проверьте .env",
                    reply_markup=mangabuff_menu_keyboard(),
                )
            except Exception as exc:  # noqa: BLE001
                await message.answer(f"⚠️ {exc}", reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text == "📊 Статус MangaBuff")
        async def mb_status(message: Message) -> None:
            await message.answer(
                self.mangabuff.stats.to_telegram(
                    (self.config.mangabuff_delay_min_sec, self.config.mangabuff_delay_max_sec)
                ),
                reply_markup=mangabuff_menu_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text == "⏱ Настройки задержки")
        async def mb_delay_menu(message: Message) -> None:
            await message.answer(
                f"Текущая задержка чтения: "
                f"<b>{self.config.mangabuff_delay_min_sec:.0f}–"
                f"{self.config.mangabuff_delay_max_sec:.0f}</b> сек\n"
                "Выберите пресет:",
                reply_markup=delay_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text == "⚡ Быстро 5–8с")
        async def delay_fast(message: Message) -> None:
            await self._set_mb_delay(message, 5, 8)

        @self.dp.message(StateFilter(None), F.text == "📖 Норма 5–15с")
        async def delay_norm(message: Message) -> None:
            await self._set_mb_delay(message, 5, 15)

        @self.dp.message(StateFilter(None), F.text == "🐢 Медленно 10–20с")
        async def delay_slow(message: Message) -> None:
            await self._set_mb_delay(message, 10, 20)

        @self.dp.message(StateFilter(None), F.text == "🧘 Очень медленно 15–30с")
        async def delay_vslow(message: Message) -> None:
            await self._set_mb_delay(message, 15, 30)

        @self.dp.message(StateFilter(None), F.text == "🔗 URL чтения")
        async def mb_url_ask(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_mangabuff_url)
            await message.answer(
                "Отправьте URL главы/тайтла MangaBuff для авточтения:\n"
                f"Сейчас: <code>{self.config.mangabuff_start_url}</code>",
                reply_markup=cancel_keyboard(),
            )

        @self.dp.message(SettingsStates.waiting_mangabuff_url)
        async def mb_url_set(message: Message, state: FSMContext) -> None:
            text = (message.text or "").strip()
            if "mangabuff.ru" not in text and not text.startswith("http"):
                await message.answer("Нужен URL вида https://mangabuff.ru/...")
                return
            self.config.mangabuff_start_url = text
            self.mangabuff.start_url = text
            update_settings(mangabuff_start_url=text)
            await state.clear()
            await message.answer(f"✅ URL чтения: <code>{text}</code>", reply_markup=mangabuff_menu_keyboard())

    async def _set_mb_delay(self, message: Message, dmin: float, dmax: float) -> None:
        self.config.mangabuff_delay_min_sec = dmin
        self.config.mangabuff_delay_max_sec = dmax
        self.mangabuff.set_delay(dmin, dmax)
        update_settings(mangabuff_delay_min_sec=dmin, mangabuff_delay_max_sec=dmax)
        await message.answer(
            f"✅ Задержка чтения: <b>{dmin:.0f}–{dmax:.0f}</b> сек",
            reply_markup=mangabuff_menu_keyboard(),
        )

    # ------------------------------------------------------------------
    # Remanga jobs
    # ------------------------------------------------------------------

    async def start_remanga_autobattle(self) -> str:
        if self.remanga_state.running:
            return "ℹ️ Автобой Remanga уже запущен."
        if not self.remanga.is_started:
            try:
                await self.remanga.start(headless=True)
            except Exception as exc:  # noqa: BLE001
                return f"⚠️ Браузер Remanga: <code>{exc}</code>"
        self.scheduler.set_interval_job(
            JOB_REMANGA_AUTOBATTLE,
            self._scheduled_remanga_battle,
            self.config.auto_battle_interval_sec,
        )
        self.scheduler.start()
        self.remanga_state.running = True
        asyncio.create_task(self.run_remanga_battle(notify=True))
        return (
            f"✅ Автобой Remanga <b>запущен</b>.\n"
            f"Интервал: {self.config.auto_battle_interval_sec} сек."
        )

    async def stop_remanga_autobattle(self) -> str:
        if not self.remanga_state.running:
            return "ℹ️ Автобой Remanga уже остановлен."
        self.scheduler.remove_job(JOB_REMANGA_AUTOBATTLE)
        self.remanga_state.running = False
        return "⏹ Автобой Remanga <b>остановлен</b>."

    async def _scheduled_remanga_battle(self) -> None:
        if self.remanga_state.running:
            await self.run_remanga_battle(notify=True)

    def _should_notify(self, outcome: BattleOutcome) -> bool:
        return load_settings().notify_settings().allows_outcome(outcome.value)

    async def _maybe_summary(self) -> None:
        ns = load_settings().notify_settings()
        every = ns.notify_summary_every
        if every <= 0 or self._notify_chat_id is None:
            return
        stats = load_stats()
        if stats.total_battles > 0 and stats.total_battles % every == 0:
            try:
                await self.bot.send_message(
                    self._notify_chat_id,
                    f"📋 <b>Сводка</b>\n\n{stats.to_telegram()}",
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("summary: %s", exc)

    async def run_remanga_battle(self, notify: bool = True) -> BattleResult:
        async with self._battle_lock:
            try:
                if not self.remanga.is_started:
                    await self.remanga.start(headless=True)
                result = await self.remanga.do_battle()
            except Exception as exc:  # noqa: BLE001
                result = BattleResult(outcome=BattleOutcome.ERROR, message=str(exc))

            self.remanga_state.register(result)
            summary = f"{result.outcome.value}: {result.message}"
            if result.rating_change:
                summary += f" ({result.rating_change})"
            rating_info = None
            if result.raw_text:
                try:
                    parsed = BrowserService._parse_rating_from_text(result.raw_text)
                    if parsed.rank or parsed.glory is not None or parsed.username:
                        parsed.updated_at = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
                        rating_info = parsed
                except Exception:  # noqa: BLE001
                    pass
            update_stats_from_result(
                outcome=result.outcome.value,
                rating_change=result.rating_change,
                summary=summary,
                rating=rating_info,
            )
            if notify and self._notify_chat_id and self._should_notify(result.outcome):
                try:
                    await self.bot.send_message(
                        self._notify_chat_id,
                        "📋 <b>Отчёт Remanga</b>\n\n"
                        + result.to_telegram()
                        .replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;"),
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("notify: %s", exc)
            await self._maybe_summary()
            return result

    # ------------------------------------------------------------------
    # MangaBuff jobs
    # ------------------------------------------------------------------

    async def start_mangabuff_read(self) -> str:
        if self.mangabuff.stats.running or (self._mb_task and not self._mb_task.done()):
            return "ℹ️ Фарм MangaBuff уже запущен."

        async def _runner() -> None:
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                # Полный фарм каталога; если задан URL конкретной главы — читать с него
                url = (self.config.mangabuff_start_url or "").strip()
                await self.mangabuff.read_loop(
                    start_url=url if url and url.rstrip("/") != "https://mangabuff.ru" else None,
                    max_chapters=0,
                    on_progress=self._mb_progress,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("mangabuff farm")
                if self._notify_chat_id:
                    try:
                        await self.bot.send_message(
                            self._notify_chat_id,
                            f"⚠️ MangaBuff фарм упал: <code>{exc}</code>",
                        )
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                self.mangabuff.stats.running = False
                self.scheduler.remove_job(JOB_MANGABUFF_READ)

        self.scheduler.start()
        self._mb_task = asyncio.create_task(_runner(), name=JOB_MANGABUFF_READ)
        update_settings(mangabuff_setup_done=True)
        return (
            "✅ Фарм MangaBuff <b>запущен</b>.\n"
            "• популярные тайтлы из каталога\n"
            "• чтение глав + сбор наград/карт\n"
            "• обход макетов и ивентов\n"
            "• ночной перерыв 01:00–05:00 МСК (4ч)\n"
            f"⏱ Задержка: {self.config.mangabuff_delay_min_sec:.0f}–"
            f"{self.config.mangabuff_delay_max_sec:.0f} сек\n"
            "Остановка — «Остановить фарм»."
        )

    async def stop_mangabuff_read(self) -> str:
        self.mangabuff.request_stop()
        self.scheduler.remove_job(JOB_MANGABUFF_READ)
        if self._mb_task and not self._mb_task.done():
            self._mb_task.cancel()
            try:
                await self._mb_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._mb_task = None
        self.mangabuff.stats.running = False
        return "⏹ Фарм MangaBuff <b>остановлен</b>."

    async def _mb_progress(self, stats) -> None:
        # Тихие промежуточные апдейты — только в лог; статус по кнопке
        logger.info(
            "MangaBuff progress: chapters=%s pages=%s rewards=%s",
            stats.chapters_read,
            stats.pages_scrolled,
            stats.rewards_claimed,
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info("Старт бота (admin=%s)", self.config.telegram_admin_id or "pending")
        # Soft-start браузеров — падение одного не валит polling
        try:
            await self.remanga.start(headless=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Remanga browser: %s", exc)
        try:
            if self.config.mangabuff_email:
                await self.mangabuff.start(headless=True)
                ok = await self.mangabuff.ensure_login()
                logger.info("MangaBuff login on boot: %s", ok)
                # автозапуск фарма после успешного логина
                if ok:
                    asyncio.create_task(self.start_mangabuff_read())
                    logger.info("MangaBuff farm auto-started")
        except Exception as exc:  # noqa: BLE001
            logger.warning("MangaBuff browser: %s", exc)
        self.scheduler.start()
        try:
            await self.dp.start_polling(self.bot, handle_signals=True)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        logger.info("Shutdown...")
        self.remanga_state.running = False
        self.mangabuff.request_stop()
        self.scheduler.shutdown()
        await self.remanga.stop()
        await self.mangabuff.stop()
        await self.bot.session.close()


async def amain() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    app = App(load_config())
    await app.run()


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Remanga + MangaBuff bot")
    parser.add_argument("--setup", action="store_true", help="Setup Remanga (headed)")
    parser.add_argument("--setup-mangabuff", action="store_true", help="Setup MangaBuff (headed)")
    args = parser.parse_args()

    if args.setup:
        from services.remanga_service import main_setup

        asyncio.run(main_setup())
        return
    if args.setup_mangabuff:
        from services.mangabuff_service import main_setup as mb_setup

        asyncio.run(mb_setup())
        return
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        print("\nВыход")


if __name__ == "__main__":
    main()
