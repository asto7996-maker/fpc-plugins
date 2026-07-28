"""
bot.py — Telegram-бот MangaBuff Autopilot (чтение · карты · эвенты).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, Optional

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from config import Config, load_config
from scheduler import JOB_MANGABUFF_MARKET, JOB_MANGABUFF_READ, AppScheduler
from services.mangabuff_service import CardDropInfo, MangaBuffService
from settings_store import load_settings, update_settings
from ui_theme import (
    hr,
    BRAND,
    BRAND_LINE,
    DEFAULT_SPEED_KEY,
    SPEED_PRESETS,
    cards_events_home,
    help_text,
    mangabuff_home,
    pulse_text,
    speed_menu_text,
    welcome_text,
)

logger = logging.getLogger(__name__)


class SettingsStates(StatesGroup):
    waiting_custom_speed = State()
    waiting_milestone_every = State()


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


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▸ Фарм"), KeyboardButton(text="■ Стоп")],
            [KeyboardButton(text="Статус"), KeyboardButton(text="Тайтл ›")],
            [KeyboardButton(text="Карты · Бои"), KeyboardButton(text="Пульс")],
            [KeyboardButton(text="Темп"), KeyboardButton(text="Оповещения")],
            [KeyboardButton(text="О боте")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def cards_events_keyboard() -> ReplyKeyboardMarkup:
    s = load_settings()
    notify = "Алерты · вкл" if s.mangabuff_notify_cards else "Алерты · выкл"
    auto_m = "Лоты · вкл" if s.mangabuff_auto_market else "Лоты · выкл"
    auto_b = "Бои · вкл" if getattr(s, "mangabuff_auto_battle", True) else "Бои · выкл"
    auto_t = "Обмены · вкл" if getattr(s, "mangabuff_auto_trade", True) else "Обмены · выкл"
    auto_q = "Викторина · вкл" if getattr(s, "mangabuff_auto_quiz", True) else "Викторина · выкл"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▸ Автофарм карт"), KeyboardButton(text="■ Стоп карт")],
            [KeyboardButton(text="Собрать"), KeyboardButton(text="Статус карт")],
            [KeyboardButton(text="Бой сейчас"), KeyboardButton(text="Обмены сейчас")],
            [KeyboardButton(text="Викторина сейчас"), KeyboardButton(text=auto_q)],
            [KeyboardButton(text="На площадку"), KeyboardButton(text=auto_m)],
            [KeyboardButton(text=auto_b), KeyboardButton(text=auto_t)],
            [KeyboardButton(text=notify), KeyboardButton(text="Вехи")],
            [KeyboardButton(text="⌂ Домой")],
        ],
        resize_keyboard=True,
    )


def mb_notify_keyboard() -> ReplyKeyboardMarkup:
    s = load_settings()
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=("●" if s.mangabuff_notify_cards else "○") + " Дроп карт"
                ),
                KeyboardButton(
                    text=("●" if s.mangabuff_notify_milestones else "○") + " Вехи глав"
                ),
            ],
            [KeyboardButton(text="Интервал вех")],
            [KeyboardButton(text="⌂ Домой")],
        ],
        resize_keyboard=True,
    )


def delay_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="Турбо"), KeyboardButton(text="Быстрый")],
            [KeyboardButton(text="Живой"), KeyboardButton(text="Норма")],
            [KeyboardButton(text="Неспешно"), KeyboardButton(text="Медленно")],
            [KeyboardButton(text="Своя скорость")],
            [KeyboardButton(text="⌂ Домой")],
        ],
        resize_keyboard=True,
    )


def cancel_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="Отмена")]],
        resize_keyboard=True,
    )


class App:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.bot = Bot(
            token=config.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dp = Dispatcher()
        self.scheduler = AppScheduler()

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
        self._events_task: Optional[asyncio.Task] = None
        self._mb_session_started_at: Optional[datetime] = None
        self._mb_session_titles_base: int = 0
        self._apply_speed_from_settings()
        self.mangabuff.on_card_drop = self._on_card_drop

        self._notify_chat_id: Optional[int] = None
        self.dp.message.middleware(AdminOnlyMiddleware(self))
        self._register_handlers()

    def _apply_speed_from_settings(self) -> None:
        s = load_settings()
        key = s.mangabuff_speed_preset or DEFAULT_SPEED_KEY
        preset = SPEED_PRESETS.get(key)
        if preset:
            self.config.mangabuff_delay_min_sec = preset.delay_min
            self.config.mangabuff_delay_max_sec = preset.delay_max
            self.mangabuff.set_delay(
                preset.delay_min,
                preset.delay_max,
                preset.steps_min,
                preset.steps_max,
            )

    def _speed_label(self) -> str:
        s = load_settings()
        preset = SPEED_PRESETS.get(s.mangabuff_speed_preset or "")
        spc = self.mangabuff.measured_sec_per_chapter()
        fact = f" · факт ~{spc:.1f} с/гл" if spc > 0 else ""
        if preset:
            return (
                f"{preset.title} · шаг {preset.delay_min:.2f}–{preset.delay_max:.2f}с"
                f"{fact}"
            )
        return (
            f"Своя · шаг {self.config.mangabuff_delay_min_sec:.2f}–"
            f"{self.config.mangabuff_delay_max_sec:.2f}с{fact}"
        )

    def _session_chapters(self) -> int:
        return int(self.mangabuff.session_chapters)

    def _session_titles(self) -> int:
        total = int(self.mangabuff.stats.titles_visited or 0)
        return max(0, total - int(self._mb_session_titles_base or 0))

    def _chapters_per_hour(self) -> float:
        measured = self.mangabuff.measured_cph()
        if measured > 0:
            return measured
        session = self._session_chapters()
        if not self._mb_session_started_at or session <= 0:
            return 0.0
        elapsed = (datetime.now() - self._mb_session_started_at).total_seconds() / 3600.0
        if elapsed <= 0.01:
            return 0.0
        return session / elapsed

    def _sec_per_chapter(self) -> float:
        spc = self.mangabuff.measured_sec_per_chapter()
        if spc > 0:
            return spc
        cph = self._chapters_per_hour()
        return (3600.0 / cph) if cph > 0 else 0.0

    def _dashboard_text(self) -> str:
        return welcome_text(
            mb_on=self.mangabuff.stats.running,
            speed_label=self._speed_label(),
            chapters=self._session_chapters(),
            chapters_total=self.mangabuff.stats.chapters_read,
            events_on=bool(self.mangabuff._events_running),
            cards_session=self.mangabuff.session_cards,
        )

    def bind_admin(self, user_id: int) -> None:
        self.config.telegram_admin_id = user_id
        update_settings(telegram_admin_id=user_id)

    def _register_handlers(self) -> None:
        @self.dp.message(CommandStart())
        async def cmd_start(message: Message, state: FSMContext) -> None:
            await state.clear()
            self._notify_chat_id = message.chat.id
            await message.answer(self._dashboard_text(), reply_markup=main_menu_keyboard())

        @self.dp.message(F.text.in_({"⌂ Домой", "🏠 Главное меню", "Главное меню"}))
        @self.dp.message(Command("menu"))
        async def go_main(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer(self._dashboard_text(), reply_markup=main_menu_keyboard())

        @self.dp.message(F.text.in_({"ℹ️ О боте", "ℹ️ Помощь", "Помощь", "О боте"}))
        @self.dp.message(Command("help"))
        async def cmd_help(message: Message) -> None:
            await message.answer(help_text(), reply_markup=main_menu_keyboard())

        @self.dp.message(F.text.in_({"✦ Пульс", "Пульс"}))
        @self.dp.message(Command("pulse"))
        async def cmd_pulse(message: Message) -> None:
            await message.answer(
                pulse_text(
                    mb_on=self.mangabuff.stats.running,
                    chapters=self._session_chapters(),
                    cph=self._chapters_per_hour(),
                    speed=self._speed_label(),
                    last_mb=self.mangabuff.stats.last_action,
                    chapters_total=self.mangabuff.stats.chapters_read,
                    sec_per_chapter=self._sec_per_chapter(),
                    events_on=bool(self.mangabuff._events_running),
                    cards_session=self.mangabuff.session_cards,
                ),
                reply_markup=main_menu_keyboard(),
            )

        @self.dp.message(F.text.in_({"❌ Отмена", "Отмена"}))
        async def cancel_any(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer("Отменено.", reply_markup=main_menu_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"▶️ Фарм", "▶️ Запустить фарм", "Запустить фарм", "▸ Фарм", "Фарм"}),
        )
        async def mb_start(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            text = await self.start_mangabuff_read()
            await message.answer(text, reply_markup=main_menu_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"⏹ Стоп", "⏹ Стоп фарм", "Остановить фарм", "■ Стоп", "Стоп"}),
        )
        async def mb_stop(message: Message) -> None:
            text = await self.stop_mangabuff_read()
            await message.answer(text, reply_markup=main_menu_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"⏭ Тайтл", "⏭ След. тайтл", "След. тайтл", "Тайтл ›", "Тайтл"}),
        )
        async def mb_skip(message: Message) -> None:
            if not self.mangabuff.stats.running:
                await message.answer("Фарм не запущен.", reply_markup=main_menu_keyboard())
                return
            self.mangabuff.request_skip_title()
            await message.answer("⏭ Следующий тайтл…", reply_markup=main_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"📊 Статус", "📊 Статус MangaBuff", "Статус"}))
        async def mb_status(message: Message) -> None:
            await message.answer(self._mb_status_card(), reply_markup=main_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"🔔 Оповещения", "Оповещения"}))
        async def mb_notify_menu(message: Message) -> None:
            s = load_settings()
            await message.answer(
                "<b>🔔 Оповещения</b>\n"
                f"Карты: <b>{'вкл' if s.mangabuff_notify_cards else 'выкл'}</b>\n"
                f"Вехи: <b>{'вкл' if s.mangabuff_notify_milestones else 'выкл'}</b>"
                f" · каждые <b>{s.mangabuff_milestone_every}</b> гл",
                reply_markup=mb_notify_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text.regexp(r"^[✅❌●○] Дроп карт$"))
        async def mb_toggle_card_notify(message: Message) -> None:
            s = load_settings()
            on = not s.mangabuff_notify_cards
            update_settings(mangabuff_notify_cards=on)
            await message.answer(
                f"🃏 Дроп карт: <b>{'вкл' if on else 'выкл'}</b>",
                reply_markup=mb_notify_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text.regexp(r"^[✅❌●○] Вехи глав$"))
        async def mb_toggle_milestones(message: Message) -> None:
            s = load_settings()
            on = not s.mangabuff_notify_milestones
            update_settings(mangabuff_notify_milestones=on)
            await message.answer(
                f"📡 Вехи: <b>{'вкл' if on else 'выкл'}</b>",
                reply_markup=mb_notify_keyboard(),
            )

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"🎚 Темп", "🎚 Темп чтения", "Темп"}),
        )
        async def mb_delay_menu(message: Message) -> None:
            s = load_settings()
            await message.answer(
                speed_menu_text(
                    self.config.mangabuff_delay_min_sec,
                    self.config.mangabuff_delay_max_sec,
                    s.mangabuff_speed_preset or "",
                ),
                reply_markup=delay_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text.in_({"⚡ Турбо", "Турбо"}))
        async def delay_turbo(message: Message) -> None:
            await self._apply_speed_preset(message, "turbo")

        @self.dp.message(StateFilter(None), F.text.in_({"🏃 Быстрый", "Быстрый"}))
        async def delay_fast(message: Message) -> None:
            await self._apply_speed_preset(message, "fast")

        @self.dp.message(StateFilter(None), F.text.in_({"✨ Живой", "Живой"}))
        async def delay_lively(message: Message) -> None:
            await self._apply_speed_preset(message, "lively")

        @self.dp.message(StateFilter(None), F.text.in_({"📖 Норма", "Норма"}))
        async def delay_norm(message: Message) -> None:
            await self._apply_speed_preset(message, "normal")

        @self.dp.message(StateFilter(None), F.text.in_({"🐢 Неспешно", "Неспешно"}))
        async def delay_slow(message: Message) -> None:
            await self._apply_speed_preset(message, "slow")

        @self.dp.message(StateFilter(None), F.text.in_({"🕯 Медленно", "Медленно"}))
        async def delay_crawl(message: Message) -> None:
            await self._apply_speed_preset(message, "crawl")

        @self.dp.message(StateFilter(None), F.text.in_({"🎛 Своя скорость", "Своя скорость"}))
        async def delay_custom_ask(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_custom_speed)
            await message.answer(
                "Две паузы в секундах: <code>2.5 5</code>\n"
                f"Сейчас: <code>{self.config.mangabuff_delay_min_sec:.1f}–"
                f"{self.config.mangabuff_delay_max_sec:.1f}</code>",
                reply_markup=cancel_keyboard(),
            )

        @self.dp.message(SettingsStates.waiting_custom_speed)
        async def delay_custom_set(message: Message, state: FSMContext) -> None:
            raw = (message.text or "").strip().replace(",", ".")
            parts = raw.replace("–", "-").replace("—", "-").replace(" ", "-").split("-")
            parts = [p for p in parts if p]
            try:
                if len(parts) == 1:
                    dmin = dmax = float(parts[0])
                else:
                    dmin, dmax = float(parts[0]), float(parts[1])
            except ValueError:
                await message.answer("Формат: <code>2.8 5.5</code>")
                return
            if dmin < 0.8 or dmax < dmin or dmax > 60:
                await message.answer("Диапазон: 0.8–60 сек, min ≤ max")
                return
            await state.clear()
            await self._set_mb_delay(message, dmin, dmax, preset_key="custom")

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"🃏 Карты · Эвенты", "Карты · Эвенты", "Карты · Бои", "Карты", "Эвенты"}),
        )
        @self.dp.message(Command("cards"))
        async def open_cards_events(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(
                self._cards_status_card(),
                reply_markup=cards_events_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text.in_({"▶️ Автофарм карт", "Автофарм карт", "▸ Автофарм карт"}))
        async def cards_start(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            text = await self.start_events_farm()
            await message.answer(text, reply_markup=cards_events_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"⏹ Стоп карт", "Стоп карт", "■ Стоп карт"}))
        async def cards_stop(message: Message) -> None:
            text = await self.stop_events_farm()
            await message.answer(text, reply_markup=cards_events_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"🎁 Собрать сейчас", "🎁 Награды", "Собрать"}),
        )
        async def cards_claim_now(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer("⏳ Обхожу эвенты…", reply_markup=cards_events_keyboard())
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                result = await self.mangabuff.claim_rewards()
                await message.answer(result.to_telegram(), reply_markup=cards_events_keyboard())
            except Exception as exc:  # noqa: BLE001
                logger.exception("cards claim")
                await message.answer(
                    f"⚠️ <code>{exc}</code>",
                    reply_markup=cards_events_keyboard(),
                )

        @self.dp.message(StateFilter(None), F.text.in_({"📊 Статус карт", "Статус карт"}))
        async def cards_status(message: Message) -> None:
            await message.answer(self._cards_status_card(), reply_markup=cards_events_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.regexp(
                r"^(🔔 Карты: |🔕 Карты: |Алерты · )(вкл|выкл)$"
            ),
        )
        async def cards_toggle_notify(message: Message) -> None:
            s = load_settings()
            on = not s.mangabuff_notify_cards
            update_settings(mangabuff_notify_cards=on)
            await message.answer(
                f"Алерты карт · <b>{'вкл' if on else 'выкл'}</b>",
                reply_markup=cards_events_keyboard(),
            )

        @self.dp.message(
            StateFilter(None),
            F.text.regexp(r"^(Бои · )(вкл|выкл)$"),
        )
        async def cards_toggle_battle(message: Message) -> None:
            s = load_settings()
            on = not bool(getattr(s, "mangabuff_auto_battle", True))
            update_settings(mangabuff_auto_battle=on)
            await message.answer(
                f"Авто-бои · <b>{'вкл' if on else 'выкл'}</b>",
                reply_markup=cards_events_keyboard(),
            )

        @self.dp.message(
            StateFilter(None),
            F.text.regexp(r"^(Обмены · )(вкл|выкл)$"),
        )
        async def cards_toggle_trade(message: Message) -> None:
            s = load_settings()
            on = not bool(getattr(s, "mangabuff_auto_trade", True))
            update_settings(mangabuff_auto_trade=on)
            await message.answer(
                f"Авто-обмены · <b>{'вкл' if on else 'выкл'}</b>",
                reply_markup=cards_events_keyboard(),
            )

        @self.dp.message(
            StateFilter(None),
            F.text.regexp(r"^(Викторина · )(вкл|выкл)$"),
        )
        async def cards_toggle_quiz(message: Message) -> None:
            s = load_settings()
            on = not bool(getattr(s, "mangabuff_auto_quiz", True))
            update_settings(mangabuff_auto_quiz=on)
            await message.answer(
                f"Авто-викторина · <b>{'вкл' if on else 'выкл'}</b>\n"
                "<i>правильные ответы из API + кэш + комменты + веб · цель — 1 место</i>",
                reply_markup=cards_events_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text.in_({"Викторина сейчас"}))
        async def cards_quiz_now(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(
                "Фарм викторины…\n"
                "<i>отвечаю правильно, коплю серию к рекорду топа</i>",
                reply_markup=cards_events_keyboard(),
            )
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                stats = await self.mangabuff.run_quiz_farm(max_answers=120)
                await message.answer(
                    f"Викторина · верно <b>{stats.get('correct', 0)}</b> / "
                    f"{stats.get('answered', 0)}\n"
                    f"Серия сейчас · <b>{self.mangabuff.stats.quiz_last_streak}</b>\n"
                    f"Лучшая серия · <b>{self.mangabuff.stats.quiz_best_streak}</b>\n"
                    f"Всего верных · <b>{self.mangabuff.stats.quiz_correct}</b>\n"
                    f"Наград ×10 · <b>{stats.get('milestones', 0)}</b>",
                    reply_markup=cards_events_keyboard(),
                )
            except Exception as exc:  # noqa: BLE001
                await message.answer(
                    f"⚠️ <code>{exc}</code>",
                    reply_markup=cards_events_keyboard(),
                )

        @self.dp.message(StateFilter(None), F.text.in_({"Бой сейчас"}))
        async def cards_battle_now(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer("Ищу бои…", reply_markup=cards_events_keyboard())
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                async with self.mangabuff._lock:
                    await self.mangabuff._ensure_login_unlocked()
                    n = await self.mangabuff._run_card_battles(
                        self.mangabuff._page, max_fights=3
                    )
                    aw = await self.mangabuff._run_card_awakening(
                        self.mangabuff._page, max_cards=2
                    )
                await message.answer(
                    f"Бои · побед <b>{n}</b> · пробуждение <b>{aw}</b>\n"
                    f"Всего побед · <b>{self.mangabuff.stats.battles_won}</b>",
                    reply_markup=cards_events_keyboard(),
                )
            except Exception as exc:  # noqa: BLE001
                await message.answer(
                    f"⚠️ <code>{exc}</code>",
                    reply_markup=cards_events_keyboard(),
                )

        @self.dp.message(StateFilter(None), F.text.in_({"Обмены сейчас"}))
        async def cards_trade_now(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(
                "Фарм обменов…\n"
                "<i>только выгодные: R→R+1 · 2S→1X · без отдачи X\n"
                "входящие невыгодные — автоотклонение · 1 обмен/человек</i>",
                reply_markup=cards_events_keyboard(),
            )
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                sent = await self.mangabuff.run_card_trades(offers=50)
                up = await self.mangabuff.run_card_upgrades(max_ops=2)
                await message.answer(
                    f"Обмены · отправлено <b>{sent}</b>\n"
                    f"Улучшение · <b>{up}</b>\n"
                    f"Всего обменов · <b>{self.mangabuff.stats.trades_sent}</b>\n"
                    f"Уже кидали · <b>{len(getattr(self.mangabuff, '_trade_receivers', []) or [])}</b>",
                    reply_markup=cards_events_keyboard(),
                )
            except Exception as exc:  # noqa: BLE001
                await message.answer(
                    f"⚠️ <code>{exc}</code>",
                    reply_markup=cards_events_keyboard(),
                )

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"📤 На площадку", "На площадку"}),
        )
        async def cards_list_market(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(
                "⏳ Выставляю <b>топ-10</b> самых дорогих карт…\n"
                "<i>лимит лотов 10 · 1× выше · X→2×X · сутки↔2× та же</i>",
                reply_markup=cards_events_keyboard(),
            )
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                result = await self.mangabuff.list_cards_on_market(
                    maintain=True
                )
                await message.answer(
                    result.to_telegram(),
                    reply_markup=cards_events_keyboard(),
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("market list")
                await message.answer(
                    f"⚠️ <code>{exc}</code>",
                    reply_markup=cards_events_keyboard(),
                )

        @self.dp.message(
            StateFilter(None),
            F.text.regexp(r"^(📤 Авто-лоты: |Лоты · )(вкл|выкл)$"),
        )
        async def cards_toggle_auto_market(message: Message) -> None:
            s = load_settings()
            on = not s.mangabuff_auto_market
            update_settings(mangabuff_auto_market=on)
            await message.answer(
                f"📤 Авто-лоты (топ-10): <b>{'вкл' if on else 'выкл'}</b>\n"
                f"<i>только самые дорогие · 1× выше · X→2×X · сутки↔2× та же</i>",
                reply_markup=cards_events_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text.in_({"📡 Вехи", "📡 Интервал вех", "Вехи", "Интервал вех"}))
        async def mb_milestones(message: Message, state: FSMContext) -> None:
            s = load_settings()
            flag = "вкл" if s.mangabuff_notify_milestones else "выкл"
            await state.set_state(SettingsStates.waiting_milestone_every)
            await message.answer(
                f"<b>📡 Вехи чтения</b>\n"
                f"Сейчас: <b>{flag}</b>, каждые <b>{s.mangabuff_milestone_every}</b> глав\n\n"
                f"Число глав между отчётами · <code>0</code> — выкл",
                reply_markup=cancel_keyboard(),
            )

        @self.dp.message(SettingsStates.waiting_milestone_every)
        async def mb_milestones_set(message: Message, state: FSMContext) -> None:
            try:
                n = int((message.text or "").strip())
            except ValueError:
                await message.answer("Целое число, например 10")
                return
            if n < 0:
                await message.answer("≥ 0")
                return
            update_settings(
                mangabuff_milestone_every=n,
                mangabuff_notify_milestones=n > 0,
            )
            await state.clear()
            await message.answer(
                "📡 Вехи выключены" if n == 0 else f"📡 Каждые <b>{n}</b> глав",
                reply_markup=cards_events_keyboard(),
            )

    def _sync_mb_stats_from_disk(self) -> None:
        try:
            disk = self.mangabuff._load_stats()
            live = self.mangabuff.stats
            if disk.chapters_read > live.chapters_read:
                live.chapters_read = disk.chapters_read
                live.chapters_pending = max(
                    int(live.chapters_pending or 0),
                    int(disk.chapters_pending or 0),
                )
                live.pages_scrolled = max(live.pages_scrolled, disk.pages_scrolled)
                live.rewards_claimed = max(live.rewards_claimed, disk.rewards_claimed)
                live.cards_claimed = max(live.cards_claimed, disk.cards_claimed)
                live.comments_posted = max(live.comments_posted, disk.comments_posted)
                live.titles_visited = max(live.titles_visited, disk.titles_visited)
                live.scrolls_claimed = max(live.scrolls_claimed, disk.scrolls_claimed)
                live.chests_opened = max(live.chests_opened, disk.chests_opened)
                live.packs_opened = max(live.packs_opened, disk.packs_opened)
                live.events_actions = max(live.events_actions, disk.events_actions)
                live.battles_won = max(
                    int(getattr(live, "battles_won", 0) or 0),
                    int(getattr(disk, "battles_won", 0) or 0),
                )
                live.battles_total = max(
                    int(getattr(live, "battles_total", 0) or 0),
                    int(getattr(disk, "battles_total", 0) or 0),
                )
                live.trades_sent = max(
                    int(getattr(live, "trades_sent", 0) or 0),
                    int(getattr(disk, "trades_sent", 0) or 0),
                )
                if disk.last_url:
                    live.last_url = disk.last_url
                if disk.last_action:
                    live.last_action = disk.last_action
                if disk.last_at:
                    live.last_at = disk.last_at
                if disk.last_card_drop:
                    live.last_card_drop = disk.last_card_drop
        except Exception:  # noqa: BLE001
            pass

    def _cards_status_card(self) -> str:
        self._sync_mb_stats_from_disk()
        st = self.mangabuff.stats
        s = load_settings()
        return cards_events_home(
            events_on=bool(self.mangabuff._events_running)
            or bool(self._events_task and not self._events_task.done()),
            read_on=bool(st.running),
            cards_total=int(st.cards_claimed or 0),
            cards_session=self.mangabuff.session_cards,
            scrolls=int(st.scrolls_claimed or 0),
            chests=int(st.chests_opened or 0),
            packs=int(st.packs_opened or 0),
            events=int(st.events_actions or 0),
            rewards=int(st.rewards_claimed or 0),
            notify_cards=bool(s.mangabuff_notify_cards),
            last_drop=st.last_card_drop or "",
            last_action=st.last_action or "",
            auto_market=bool(s.mangabuff_auto_market),
            auto_battle=bool(getattr(s, "mangabuff_auto_battle", True)),
            auto_trade=bool(getattr(s, "mangabuff_auto_trade", True)),
            auto_quiz=bool(getattr(s, "mangabuff_auto_quiz", True)),
            battles_won=int(getattr(st, "battles_won", 0) or 0),
            battles_total=int(getattr(st, "battles_total", 0) or 0),
            trades_sent=int(getattr(st, "trades_sent", 0) or 0),
            quiz_correct=int(getattr(st, "quiz_correct", 0) or 0),
            quiz_best=int(getattr(st, "quiz_best_streak", 0) or 0),
        )

    async def _on_card_drop(self, info: CardDropInfo) -> None:
        if not info.cards:
            return
        settings = load_settings()
        chat = self._notify_chat_id or self.config.telegram_admin_id
        if settings.mangabuff_notify_cards and chat:
            cards_line = info.cards_line(4)
            ranks_only = ""
            if info.ranks and not cards_line:
                ranks_only = "\nРедкость: " + ", ".join(
                    f"<b>{r}</b>" for r in info.ranks[:4]
                )
            detail = f"\n{cards_line}" if cards_line else ranks_only
            src = ""
            if info.source and "notifications" in info.source:
                src = "\n<i>из уведомлений MangaBuff</i>"
            elif info.source:
                src = f"\n<i>{info.source}</i>"
            scrolls = f" · 📜 +{info.scrolls}" if info.scrolls else ""
            rarity_hint = ""
            if info.ranks:
                rarity_hint = f" · редкость <b>{'/'.join(info.ranks[:3])}</b>"
            try:
                await self.bot.send_message(
                    chat,
                    f"<b>Новая карта</b> · +{info.cards}{scrolls}{rarity_hint}"
                    f"{detail}{src}\n"
                    f"Всего · <b>{self.mangabuff.stats.cards_claimed}</b>"
                    f" · сессия <b>{self.mangabuff.session_cards}</b>",
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("card drop notify: %s", exc)

        if settings.mangabuff_auto_market:
            # отложенно — не держим lock фарма и даём карте стать tradable
            asyncio.create_task(self._auto_list_dropped_cards(info))

    async def _auto_list_dropped_cards(self, info: CardDropInfo) -> None:
        """Авто-выставление дропа на площадку."""
        await asyncio.sleep(10.0)
        if self.mangabuff.stats.running:
            logger.info("auto market deferred — chapter farm active")
            return
        chat = self._notify_chat_id or self.config.telegram_admin_id
        try:
            if not self.mangabuff.is_started:
                await self.mangabuff.start(headless=True)
            result = await self.mangabuff.list_cards_on_market(
                user_card_ids=info.user_card_ids or None,
                maintain=True,
                lock_timeout=25.0,
            )
            if not chat or (not result.listed and not result.errors):
                return
            await self.bot.send_message(chat, result.to_telegram())
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto market list: %s", exc)
            if chat:
                try:
                    await self.bot.send_message(
                        chat, f"⚠️ Авто-лот: <code>{exc}</code>"
                    )
                except Exception:  # noqa: BLE001
                    pass

    async def _market_maintain_job(self) -> None:
        """Раз в час: доложить новые карты + суточная смена цены."""
        if not load_settings().mangabuff_auto_market:
            return
        try:
            if not self.mangabuff.is_started:
                await self.mangabuff.start(headless=True)
            result = await self.mangabuff.maintain_market_lots()
            if result.listed or result.repriced or result.errors:
                chat = self._notify_chat_id or self.config.telegram_admin_id
                if chat:
                    await self.bot.send_message(chat, result.to_telegram())
        except Exception as exc:  # noqa: BLE001
            logger.warning("market maintain job: %s", exc)

    def _mb_status_card(self) -> str:
        self._sync_mb_stats_from_disk()
        s = load_settings()
        preset = SPEED_PRESETS.get(s.mangabuff_speed_preset or "")
        title = preset.title if preset else "Своя"
        st = self.mangabuff.stats
        return mangabuff_home(
            running=st.running,
            dmin=self.config.mangabuff_delay_min_sec,
            dmax=self.config.mangabuff_delay_max_sec,
            chapters=self._session_chapters(),
            pages=st.pages_scrolled,
            titles=st.titles_visited,
            rewards=st.rewards_claimed,
            cards=st.cards_claimed,
            comments=st.comments_posted,
            cph=self._chapters_per_hour(),
            last_action=st.last_action,
            last_url=st.last_url,
            night=st.night_break_until,
            preset_title=title,
            chapters_total=st.chapters_read,
            sec_per_chapter=self._sec_per_chapter(),
            session_titles=self._session_titles(),
            chapters_pending=int(st.chapters_pending or 0),
            battles_won=int(getattr(st, "battles_won", 0) or 0),
            trades_sent=int(getattr(st, "trades_sent", 0) or 0),
        )

    async def _apply_speed_preset(self, message: Message, key: str) -> None:
        preset = SPEED_PRESETS[key]
        await self._set_mb_delay(
            message,
            preset.delay_min,
            preset.delay_max,
            preset_key=key,
            steps_min=preset.steps_min,
            steps_max=preset.steps_max,
        )

    async def _set_mb_delay(
        self,
        message: Message,
        dmin: float,
        dmax: float,
        preset_key: str = "custom",
        steps_min: int = 8,
        steps_max: int = 12,
    ) -> None:
        self.config.mangabuff_delay_min_sec = dmin
        self.config.mangabuff_delay_max_sec = dmax
        self.mangabuff.set_delay(dmin, dmax, steps_min, steps_max)
        update_settings(
            mangabuff_delay_min_sec=dmin,
            mangabuff_delay_max_sec=dmax,
            mangabuff_speed_preset=preset_key,
        )
        label = SPEED_PRESETS[preset_key].title if preset_key in SPEED_PRESETS else "Своя"
        preset = SPEED_PRESETS.get(preset_key)
        blurb = f"\n<i>{preset.blurb}</i>" if preset else ""
        await message.answer(
            f"🎚 Темп: <b>{label}</b>{blurb}\n"
            f"Пауза шага: <code>{dmin:.2f}–{dmax:.2f}</code> сек\n"
            f"Шагов на главу: <code>{steps_min}–{steps_max}</code>",
            reply_markup=main_menu_keyboard(),
        )

    async def start_events_farm(self, *, resume: bool = False) -> str:
        if self.mangabuff._events_running or (
            self._events_task and not self._events_task.done()
        ):
            return "ℹ️ Автофарм карт уже запущен."

        async def _runner() -> None:
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                await self.mangabuff.events_loop(interval_sec=75.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("events farm")
                if self._notify_chat_id:
                    try:
                        await self.bot.send_message(
                            self._notify_chat_id,
                            f"⚠️ Автофарм карт: <code>{exc}</code>",
                        )
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                self.mangabuff._events_running = False

        self._events_task = asyncio.create_task(_runner(), name="mangabuff_events")
        update_settings(mangabuff_events_farm_enabled=True)
        if resume:
            return "♻️ Автофарм карт <b>возобновлён</b>."
        return (
            "<b>🃏 Автофарм карт запущен</b>\n"
            "Сундуки · паки · уведомления · цикл ~35–75 сек.\n"
            "Стоп — «Стоп карт»."
        )

    async def stop_events_farm(self) -> str:
        self.mangabuff.request_stop_events()
        if self._events_task and not self._events_task.done():
            self._events_task.cancel()
            try:
                await self._events_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._events_task = None
        self.mangabuff._events_running = False
        update_settings(mangabuff_events_farm_enabled=False)
        return "⏹ Автофарм карт <b>остановлен</b>."

    async def start_mangabuff_read(self) -> str:
        if self.mangabuff.stats.running or (self._mb_task and not self._mb_task.done()):
            return "ℹ️ Фарм уже запущен."

        async def _runner() -> None:
            self.mangabuff.stats.running = True
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
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
                            f"⚠️ Фарм упал: <code>{exc}</code>",
                        )
                    except Exception:  # noqa: BLE001
                        pass
            finally:
                self.mangabuff.stats.running = False
                self.scheduler.remove_job(JOB_MANGABUFF_READ)

        self.scheduler.start()
        self.mangabuff.stats.running = True
        self._mb_task = asyncio.create_task(_runner(), name=JOB_MANGABUFF_READ)
        self._mb_session_started_at = datetime.now()
        self._mb_session_titles_base = int(self.mangabuff.stats.titles_visited or 0)
        self.mangabuff.mark_farm_session_start()
        update_settings(mangabuff_setup_done=True, mangabuff_farm_enabled=True)
        return (
            f"<b>📚 Фарм запущен</b>\n"
            f"{'─' * 20}\n"
            f"• топ тайтлы · чтение до 90%\n"
            f"• главы через addHistory\n"
            f"• эвенты между тайтлами\n"
            f"• ночь 01:00–05:00 МСК\n"
            f"🎚 <b>{self._speed_label()}</b>\n"
            f"Стоп — «Стоп»."
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
        update_settings(mangabuff_farm_enabled=False)
        try:
            self.mangabuff._persist_stats()
        except Exception:  # noqa: BLE001
            pass
        st = self.mangabuff.stats
        spc = self._sec_per_chapter()
        pace = f" · ~{spc:.1f} с/гл" if spc > 0 else ""
        return (
            f"<b>⏹ Фарм остановлен</b>\n"
            f"📖 Сессия: <b>{self._session_chapters()}</b>\n"
            f"📚 Всего: <b>{st.chapters_read}</b>\n"
            f"📈 <b>{self._chapters_per_hour():.0f}</b> гл/час{pace}"
        )

    async def _mb_progress(self, stats) -> None:
        logger.info(
            "MangaBuff progress: chapters=%s pages=%s rewards=%s",
            stats.chapters_read,
            stats.pages_scrolled,
            stats.rewards_claimed,
        )
        s = load_settings()
        every = int(s.mangabuff_milestone_every or 0)
        session_n = self._session_chapters()
        if (
            not s.mangabuff_notify_milestones
            or every <= 0
            or self._notify_chat_id is None
            or session_n <= 0
            or session_n % every != 0
        ):
            return
        try:
            spc = self._sec_per_chapter()
            pace = f" · ~{spc:.1f} с/гл" if spc > 0 else ""
            await self.bot.send_message(
                self._notify_chat_id,
                f"<b>📡 Веха · сессия {session_n} гл</b>\n"
                f"📚 Всего: {stats.chapters_read}\n"
                f"📈 {self._chapters_per_hour():.0f} гл/час{pace}\n"
                f"🃏 {stats.cards_claimed} · 🎁 {stats.rewards_claimed}\n"
                f"🎚 {self._speed_label()}",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("milestone notify: %s", exc)

    async def _setup_bot_profile(self) -> None:
        try:
            await self.bot.set_my_name(name=BRAND)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_my_name: %s", exc)
        try:
            await self.bot.set_my_short_description(
                short_description=f"{BRAND_LINE}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_my_short_description: %s", exc)
        try:
            await self.bot.set_my_description(
                description=(
                    f"{BRAND} — авточтение MangaBuff, карты и эвенты.\n"
                    "Главы засчитываются через addHistory. "
                    "Уведомления о дропе карт в Telegram."
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_my_description: %s", exc)
        try:
            await self.bot.set_my_commands(
                [
                    BotCommand(command="start", description="Домой"),
                    BotCommand(command="pulse", description="Пульс"),
                    BotCommand(command="menu", description="Меню"),
                    BotCommand(command="cards", description="Карты · эвенты"),
                    BotCommand(command="help", description="О боте"),
                ]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_my_commands: %s", exc)

    async def run(self) -> None:
        logger.info("Старт MangaBuff Autopilot (admin=%s)", self.config.telegram_admin_id or "pending")
        await self._setup_bot_profile()

        s = load_settings()
        if (
            abs(s.mangabuff_delay_min_sec - 5.0) < 0.01
            and abs(s.mangabuff_delay_max_sec - 15.0) < 0.01
        ) or not s.mangabuff_speed_preset:
            preset = SPEED_PRESETS[DEFAULT_SPEED_KEY]
            update_settings(
                mangabuff_delay_min_sec=preset.delay_min,
                mangabuff_delay_max_sec=preset.delay_max,
                mangabuff_speed_preset=preset.key,
            )
            self._apply_speed_from_settings()
            s = load_settings()

        try:
            if self.config.mangabuff_email:
                await self.mangabuff.start(headless=True)
                ok = await self.mangabuff.ensure_login()
                logger.info("MangaBuff login on boot: %s", ok)
                if ok and self._should_resume_mangabuff_farm():
                    # Сначала фарм — events/market не должны перехватить браузер
                    await self.start_mangabuff_read()
                    logger.info("MangaBuff farm resumed")
                if ok and load_settings().mangabuff_events_farm_enabled:
                    # параллельно с чтением (idle while farm active), не elif
                    if not (self._events_task and not self._events_task.done()):
                        asyncio.create_task(self.start_events_farm(resume=True))
                        logger.info("MangaBuff events farm resumed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("MangaBuff browser: %s", exc)

        self.scheduler.start()
        try:
            if not self.scheduler.raw.get_job(JOB_MANGABUFF_MARKET):
                self.scheduler.raw.add_job(
                    self._market_maintain_job,
                    trigger="interval",
                    hours=1,
                    id=JOB_MANGABUFF_MARKET,
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                )
                logger.info("MangaBuff market maintain job scheduled (1h)")
        except Exception as exc:  # noqa: BLE001
            logger.warning("market job schedule: %s", exc)
        try:
            await self.dp.start_polling(self.bot, handle_signals=True)
        finally:
            await self.shutdown()

    def _should_resume_mangabuff_farm(self) -> bool:
        from settings_store import SETTINGS_PATH

        raw: dict = {}
        try:
            if SETTINGS_PATH.exists():
                raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raw = {}
        except Exception:  # noqa: BLE001
            raw = {}

        s = load_settings()
        if "mangabuff_farm_enabled" in raw:
            return bool(s.mangabuff_farm_enabled)
        if s.mangabuff_setup_done or self.mangabuff.stats.chapters_read > 0:
            update_settings(mangabuff_farm_enabled=True)
            return True
        return False

    async def shutdown(self) -> None:
        logger.info("Shutdown...")
        self.mangabuff.request_stop()
        self.mangabuff.request_stop_events()
        if self._events_task and not self._events_task.done():
            self._events_task.cancel()
            try:
                await self._events_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        try:
            self.mangabuff._persist_stats()
        except Exception:  # noqa: BLE001
            pass
        self.scheduler.shutdown()
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

    parser = argparse.ArgumentParser(description="MangaBuff Autopilot")
    parser.add_argument("--setup-mangabuff", action="store_true", help="Setup MangaBuff (headed)")
    args = parser.parse_args()

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
