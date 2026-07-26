"""
bot.py — Telegram-бот: Remanga.org (автобои) + MangaBuff.ru (авточтение / награды).

Двухуровневое меню:
  Главное → Remanga / MangaBuff
  Remanga → автобой, 1 бой, статус, уведомления, рейтинг...
  MangaBuff → авточтение, награды, задержки, статус
"""

from __future__ import annotations

import asyncio
import json
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
from aiogram.types import (
    BotCommand,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    TelegramObject,
)

from config import Config, load_config
from scheduler import (
    JOB_MANGABUFF_READ,
    JOB_REMANGA_AUTOBATTLE,
    AppScheduler,
)
from services.mangabuff_service import CardDropInfo, MangaBuffService
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
from ui_theme import (
    BRAND,
    BRAND_LINE,
    DEFAULT_SPEED_KEY,
    SPEED_PRESETS,
    cards_events_home,
    help_text,
    mangabuff_home,
    pulse_text,
    remanga_home,
    speed_menu_text,
    welcome_text,
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
    waiting_custom_speed = State()
    waiting_milestone_every = State()


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

    @classmethod
    def from_disk(cls) -> "AutobattleState":
        """Восстановить счётчики из stats.json (чтобы не обнулялись после рестарта)."""
        try:
            s = load_stats()
        except Exception:  # noqa: BLE001
            return cls()
        return cls(
            running=False,
            total_battles=int(s.total_battles or 0),
            wins=int(s.wins or 0),
            losses=int(s.losses or 0),
            draws=int(s.draws or 0),
            skipped=int(s.skipped or 0),
            errors=int(s.errors or 0),
        )

    def sync_from_disk(self) -> None:
        seeded = AutobattleState.from_disk()
        self.total_battles = seeded.total_battles
        self.wins = seeded.wins
        self.losses = seeded.losses
        self.draws = seeded.draws
        self.skipped = seeded.skipped
        self.errors = seeded.errors

    def clear_counters(self) -> None:
        self.total_battles = 0
        self.wins = 0
        self.losses = 0
        self.draws = 0
        self.skipped = 0
        self.errors = 0
        self.last_result = None

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
            [KeyboardButton(text="⚔️ Remanga"), KeyboardButton(text="📚 MangaBuff")],
            [KeyboardButton(text="🃏 Карты · Эвенты"), KeyboardButton(text="✦ Пульс")],
            [KeyboardButton(text="🎚 Темп"), KeyboardButton(text="ℹ️ О боте")],
        ],
        resize_keyboard=True,
        is_persistent=True,
    )


def remanga_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="▶️ Автобой"),
                KeyboardButton(text="⏹ Стоп бой"),
            ],
            [
                KeyboardButton(text="⚔️ Один бой"),
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
            [KeyboardButton(text="⌂ Домой")],
        ],
        resize_keyboard=True,
    )


def mangabuff_menu_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Фарм"), KeyboardButton(text="⏹ Стоп")],
            [KeyboardButton(text="📊 Статус"), KeyboardButton(text="⏭ Тайтл")],
            [KeyboardButton(text="🎚 Темп"), KeyboardButton(text="🔔 Оповещения")],
            [KeyboardButton(text="🃏 Карты · Эвенты"), KeyboardButton(text="⌂ Домой")],
        ],
        resize_keyboard=True,
    )


def cards_events_keyboard() -> ReplyKeyboardMarkup:
    s = load_settings()
    notify = "🔔 Карты: вкл" if s.mangabuff_notify_cards else "🔕 Карты: выкл"
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="▶️ Автофарм карт"), KeyboardButton(text="⏹ Стоп карт")],
            [KeyboardButton(text="🎁 Собрать сейчас"), KeyboardButton(text="📊 Статус карт")],
            [KeyboardButton(text=notify), KeyboardButton(text="📡 Вехи")],
            [KeyboardButton(text="◀️ MangaBuff"), KeyboardButton(text="⌂ Домой")],
        ],
        resize_keyboard=True,
    )


def mb_notify_keyboard() -> ReplyKeyboardMarkup:
    s = load_settings()
    return ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(
                    text=("✅" if s.mangabuff_notify_cards else "❌") + " Дроп карт"
                ),
                KeyboardButton(
                    text=("✅" if s.mangabuff_notify_milestones else "❌") + " Вехи глав"
                ),
            ],
            [KeyboardButton(text="📡 Интервал вех")],
            [KeyboardButton(text="◀️ MangaBuff")],
        ],
        resize_keyboard=True,
    )


def delay_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="⚡ Турбо"), KeyboardButton(text="🏃 Быстрый")],
            [KeyboardButton(text="✨ Живой"), KeyboardButton(text="📖 Норма")],
            [KeyboardButton(text="🐢 Неспешно"), KeyboardButton(text="🕯 Медленно")],
            [KeyboardButton(text="🎛 Своя скорость")],
            [KeyboardButton(text="⌂ Домой")],
        ],
        resize_keyboard=True,
    )


def settings_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🌐 URL боёв"), KeyboardButton(text="⏱ Интервал")],
            [KeyboardButton(text="⌛ Таймаут"), KeyboardButton(text="📋 Показать настройки")],
            [KeyboardButton(text="◀️ Remanga")],
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
            [KeyboardButton(text="◀️ Remanga")],
        ],
        resize_keyboard=True,
    )


def rating_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔄 Обновить рейтинг")],
            [KeyboardButton(text="◀️ Remanga")],
        ],
        resize_keyboard=True,
    )


def stats_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="♻️ Сбросить статистику")],
            [KeyboardButton(text="◀️ Remanga")],
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
        self.remanga_state = AutobattleState.from_disk()
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
            # синхронизировать числа пресета (после обновления кода турбо стал быстрее)
            update_settings(
                mangabuff_delay_min_sec=preset.delay_min,
                mangabuff_delay_max_sec=preset.delay_max,
                mangabuff_speed_preset=preset.key,
            )
        else:
            self.mangabuff.set_delay(
                s.mangabuff_delay_min_sec or 0.20,
                s.mangabuff_delay_max_sec or 0.45,
            )

    def _speed_label(self) -> str:
        s = load_settings()
        preset = SPEED_PRESETS.get(s.mangabuff_speed_preset or "")
        # фактический темп, если уже есть замер
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
        """Живой темп: сначала замер по последним главам, иначе среднее за сессию."""
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
            remanga_on=self.remanga_state.running,
            mb_on=self.mangabuff.stats.running,
            speed_label=self._speed_label(),
            chapters=self._session_chapters(),
            battles=self.remanga_state.total_battles,
            chapters_total=self.mangabuff.stats.chapters_read,
        )

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
            await message.answer(self._dashboard_text(), reply_markup=main_menu_keyboard())

        @self.dp.message(F.text.in_({"⌂ Домой", "🏠 Главное меню", "Главное меню"}))
        @self.dp.message(Command("menu"))
        async def go_main(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer(self._dashboard_text(), reply_markup=main_menu_keyboard())

        @self.dp.message(F.text.in_({"ℹ️ О боте", "ℹ️ Помощь", "Помощь"}))
        @self.dp.message(Command("help"))
        async def cmd_help(message: Message) -> None:
            await message.answer(help_text(), reply_markup=main_menu_keyboard())

        @self.dp.message(F.text.in_({"✦ Пульс", "Пульс"}))
        @self.dp.message(Command("pulse"))
        async def cmd_pulse(message: Message) -> None:
            await message.answer(
                pulse_text(
                    remanga_on=self.remanga_state.running,
                    mb_on=self.mangabuff.stats.running,
                    chapters=self._session_chapters(),
                    cph=self._chapters_per_hour(),
                    battles=self.remanga_state.total_battles,
                    speed=self._speed_label(),
                    last_mb=self.mangabuff.stats.last_action,
                    chapters_total=self.mangabuff.stats.chapters_read,
                    sec_per_chapter=self._sec_per_chapter(),
                    events_on=bool(self.mangabuff._events_running),
                    cards_session=self.mangabuff.session_cards,
                ),
                reply_markup=main_menu_keyboard(),
            )

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"🃏 Карты · Эвенты", "Карты · Эвенты", "Карты", "Эвенты"}),
        )
        @self.dp.message(Command("cards"))
        async def open_cards_events(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(
                self._cards_status_card(),
                reply_markup=cards_events_keyboard(),
            )

        @self.dp.message(F.text.in_({"⚔️ Remanga", "⚔️ Remanga.org", "Remanga.org", "Remanga"}))
        async def open_remanga(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(
                remanga_home(
                    self.remanga_state.running,
                    self.config.auto_battle_interval_sec,
                    self.remanga_state.wins,
                    self.remanga_state.losses,
                    self.remanga_state.draws,
                    self.remanga_state.total_battles,
                ),
                reply_markup=remanga_menu_keyboard(),
            )

        @self.dp.message(F.text.in_({"📚 MangaBuff", "📚 MangaBuff.ru", "MangaBuff.ru", "MangaBuff"}))
        async def open_mangabuff(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer(self._mb_status_card(), reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(F.text.in_({"◀️ Remanga", "◀️ В меню Remanga"}))
        async def back_remanga(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer(
                remanga_home(
                    self.remanga_state.running,
                    self.config.auto_battle_interval_sec,
                    self.remanga_state.wins,
                    self.remanga_state.losses,
                    self.remanga_state.draws,
                    self.remanga_state.total_battles,
                ),
                reply_markup=remanga_menu_keyboard(),
            )

        @self.dp.message(F.text.in_({"◀️ MangaBuff", "◀️ В меню MangaBuff", "В меню MangaBuff"}))
        async def back_mb(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer(self._mb_status_card(), reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(F.text.in_({"❌ Отмена", "Отмена"}))
        async def cancel_any(message: Message, state: FSMContext) -> None:
            await state.clear()
            await message.answer("Отменено.", reply_markup=main_menu_keyboard())

        # ===== Remanga =====
        @self.dp.message(StateFilter(None), F.text.in_({"▶️ Автобой", "▶️ Запустить автобой", "Запустить автобой"}))
        @self.dp.message(StateFilter(None), Command("auto"))
        async def remanga_start(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            text = await self.start_remanga_autobattle()
            await message.answer(text, reply_markup=remanga_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"⏹ Стоп бой", "⏹ Остановить автобой", "Остановить автобой"}))
        @self.dp.message(StateFilter(None), Command("stop"))
        async def remanga_stop(message: Message) -> None:
            text = await self.stop_remanga_autobattle()
            await message.answer(text, reply_markup=remanga_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"⚔️ Один бой", "⚔️ Сделать 1 бой", "Сделать 1 бой"}))
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
            self.remanga_state.clear_counters()
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
            F.text.in_({"▶️ Фарм", "▶️ Запустить фарм", "▶️ Запустить авточтение", "Запустить фарм"}),
        )
        async def mb_start(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            text = await self.start_mangabuff_read()
            await message.answer(text, reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.in_(
                {
                    "⏹ Стоп",
                    "⏹ Стоп фарм",
                    "⏹ Остановить фарм",
                    "⏹ Остановить авточтение",
                    "Остановить фарм",
                }
            ),
        )
        async def mb_stop(message: Message) -> None:
            text = await self.stop_mangabuff_read()
            await message.answer(text, reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"⏭ Тайтл", "⏭ След. тайтл", "След. тайтл"}),
        )
        async def mb_skip(message: Message) -> None:
            if not self.mangabuff.stats.running:
                await message.answer("Фарм не запущен.", reply_markup=mangabuff_menu_keyboard())
                return
            self.mangabuff.request_skip_title()
            await message.answer(
                "⏭ Переключаю на следующий тайтл…",
                reply_markup=mangabuff_menu_keyboard(),
            )

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"📊 Статус", "📊 Статус MangaBuff"}),
        )
        async def mb_status(message: Message) -> None:
            await message.answer(self._mb_status_card(), reply_markup=mangabuff_menu_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"🔔 Оповещения", "Оповещения"}))
        async def mb_notify_menu(message: Message) -> None:
            s = load_settings()
            await message.answer(
                "<b>🔔 Оповещения MangaBuff</b>\n"
                f"Карты: <b>{'вкл' if s.mangabuff_notify_cards else 'выкл'}</b>\n"
                f"Вехи: <b>{'вкл' if s.mangabuff_notify_milestones else 'выкл'}</b>"
                f" · каждые <b>{s.mangabuff_milestone_every}</b> гл",
                reply_markup=mb_notify_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text.regexp(r"^[✅❌] Дроп карт$"))
        async def mb_toggle_card_notify(message: Message) -> None:
            s = load_settings()
            on = not s.mangabuff_notify_cards
            update_settings(mangabuff_notify_cards=on)
            await message.answer(
                f"🃏 Дроп карт: <b>{'вкл' if on else 'выкл'}</b>",
                reply_markup=mb_notify_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text.regexp(r"^[✅❌] Вехи глав$"))
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
            F.text.in_({"🎚 Темп", "🎚 Темп чтения", "⏱ Настройки задержки"}),
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

        # ===== Карты · Эвенты =====
        @self.dp.message(StateFilter(None), F.text.in_({"▶️ Автофарм карт", "Автофарм карт"}))
        async def cards_start(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            text = await self.start_events_farm()
            await message.answer(text, reply_markup=cards_events_keyboard())

        @self.dp.message(StateFilter(None), F.text.in_({"⏹ Стоп карт", "Стоп карт"}))
        async def cards_stop(message: Message) -> None:
            text = await self.stop_events_farm()
            await message.answer(text, reply_markup=cards_events_keyboard())

        @self.dp.message(
            StateFilter(None),
            F.text.in_({"🎁 Собрать сейчас", "🎁 Награды", "🎁 Собрать награды"}),
        )
        async def cards_claim_now(message: Message) -> None:
            self._notify_chat_id = message.chat.id
            await message.answer("⏳ Обхожу эвенты и карты…", reply_markup=cards_events_keyboard())
            try:
                if not self.mangabuff.is_started:
                    await self.mangabuff.start(headless=True)
                result = await self.mangabuff.claim_rewards()
                await message.answer(result.to_telegram(), reply_markup=cards_events_keyboard())
            except Exception as exc:  # noqa: BLE001
                logger.exception("cards claim")
                await message.answer(
                    f"⚠️ Ошибка сбора: <code>{exc}</code>",
                    reply_markup=cards_events_keyboard(),
                )

        @self.dp.message(StateFilter(None), F.text.in_({"📊 Статус карт", "Статус карт"}))
        async def cards_status(message: Message) -> None:
            await message.answer(self._cards_status_card(), reply_markup=cards_events_keyboard())

        @self.dp.message(StateFilter(None), F.text.regexp(r"^🔔 Карты: (вкл|выкл)$|^🔕 Карты: (вкл|выкл)$"))
        async def cards_toggle_notify(message: Message) -> None:
            s = load_settings()
            on = not s.mangabuff_notify_cards
            update_settings(mangabuff_notify_cards=on)
            await message.answer(
                f"🃏 Уведомления о картах: <b>{'вкл' if on else 'выкл'}</b>",
                reply_markup=cards_events_keyboard(),
            )

        @self.dp.message(StateFilter(None), F.text == "⚡ Турбо")
        async def delay_turbo(message: Message) -> None:
            await self._apply_speed_preset(message, "turbo")

        @self.dp.message(StateFilter(None), F.text == "🏃 Быстрый")
        async def delay_fast(message: Message) -> None:
            await self._apply_speed_preset(message, "fast")

        @self.dp.message(StateFilter(None), F.text == "✨ Живой")
        async def delay_lively(message: Message) -> None:
            await self._apply_speed_preset(message, "lively")

        @self.dp.message(StateFilter(None), F.text == "📖 Норма")
        async def delay_norm(message: Message) -> None:
            await self._apply_speed_preset(message, "normal")

        @self.dp.message(StateFilter(None), F.text == "🐢 Неспешно")
        async def delay_slow(message: Message) -> None:
            await self._apply_speed_preset(message, "slow")

        @self.dp.message(StateFilter(None), F.text == "🕯 Медленно")
        async def delay_crawl(message: Message) -> None:
            await self._apply_speed_preset(message, "crawl")

        @self.dp.message(StateFilter(None), F.text == "🎛 Своя скорость")
        async def delay_custom_ask(message: Message, state: FSMContext) -> None:
            await state.set_state(SettingsStates.waiting_custom_speed)
            await message.answer(
                "Введите две паузы в секундах через пробел или дефис.\n"
                "Пример: <code>2.5 5</code> или <code>3-6</code>\n"
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
                await message.answer("Диапазон: от 0.8 до 60 сек, min ≤ max")
                return
            await state.clear()
            await self._set_mb_delay(message, dmin, dmax, preset_key="custom")

        @self.dp.message(StateFilter(None), F.text.in_({"📡 Вехи", "📡 Интервал вех"}))
        async def mb_milestones(message: Message, state: FSMContext) -> None:
            s = load_settings()
            flag = "вкл" if s.mangabuff_notify_milestones else "выкл"
            await state.set_state(SettingsStates.waiting_milestone_every)
            await message.answer(
                f"<b>📡 Вехи чтения</b>\n"
                f"Сейчас: <b>{flag}</b>, каждые <b>{s.mangabuff_milestone_every}</b> глав\n\n"
                f"Введите число глав между отчётами\n"
                f"<code>0</code> — выключить уведомления",
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
                "📡 Вехи выключены" if n == 0 else f"📡 Буду писать каждые <b>{n}</b> глав",
                reply_markup=cards_events_keyboard(),
            )

    def _sync_mb_stats_from_disk(self) -> None:
        try:
            disk = self.mangabuff._load_stats()
            live = self.mangabuff.stats
            if disk.chapters_read > live.chapters_read:
                live.chapters_read = disk.chapters_read
                live.pages_scrolled = max(live.pages_scrolled, disk.pages_scrolled)
                live.rewards_claimed = max(live.rewards_claimed, disk.rewards_claimed)
                live.cards_claimed = max(live.cards_claimed, disk.cards_claimed)
                live.comments_posted = max(live.comments_posted, disk.comments_posted)
                live.titles_visited = max(live.titles_visited, disk.titles_visited)
                live.layouts_visited = max(live.layouts_visited, disk.layouts_visited)
                live.scrolls_claimed = max(live.scrolls_claimed, disk.scrolls_claimed)
                live.chests_opened = max(live.chests_opened, disk.chests_opened)
                live.packs_opened = max(live.packs_opened, disk.packs_opened)
                live.events_actions = max(live.events_actions, disk.events_actions)
                if disk.last_url:
                    live.last_url = disk.last_url
                if disk.last_action:
                    live.last_action = disk.last_action
                if disk.last_at:
                    live.last_at = disk.last_at
                if disk.last_card_drop:
                    live.last_card_drop = disk.last_card_drop
            # running берём из live-процесса
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
        )

    async def _on_card_drop(self, info: CardDropInfo) -> None:
        """Telegram: новая карта (+N) и имена, если есть."""
        if not info.cards:
            return
        if not load_settings().mangabuff_notify_cards:
            return
        chat = self._notify_chat_id or self.config.telegram_admin_id
        if not chat:
            return
        names = ""
        if info.names:
            shown = ", ".join(info.names[:4])
            names = f"\n{shown}"
        src = ""
        if info.source and "notifications" in info.source:
            src = "\n<i>из уведомлений MangaBuff</i>"
        elif info.source:
            src = f"\n<i>{info.source}</i>"
        scrolls = f" · 📜 +{info.scrolls}" if info.scrolls else ""
        try:
            await self.bot.send_message(
                chat,
                f"<b>🃏 +{info.cards} карт{scrolls}</b>"
                f"{names}{src}\n"
                f"Всего: <b>{self.mangabuff.stats.cards_claimed}</b>"
                f" · сессия <b>{self.mangabuff.session_cards}</b>",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("card drop notify: %s", exc)

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
            f"Пауза шага скролла: <code>{dmin:.2f}–{dmax:.2f}</code> сек\n"
            f"Шагов на главу: <code>{steps_min}–{steps_max}</code>\n"
            f"<i>Применяется сразу. «С/глава» в описании — полное время главы с переходом.</i>",
            reply_markup=mangabuff_menu_keyboard(),
        )

    # ------------------------------------------------------------------
    # Remanga jobs
    # ------------------------------------------------------------------

    async def start_remanga_autobattle(self, *, resume: bool = False) -> str:
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
        update_settings(remanga_autobattle_enabled=True)
        asyncio.create_task(self.run_remanga_battle(notify=not resume))
        if resume:
            return (
                f"♻️ Автобой Remanga <b>возобновлён</b> после рестарта.\n"
                f"Интервал: {self.config.auto_battle_interval_sec} сек."
            )
        return (
            f"✅ Автобой Remanga <b>запущен</b>.\n"
            f"Интервал: {self.config.auto_battle_interval_sec} сек."
        )

    async def stop_remanga_autobattle(self) -> str:
        if not self.remanga_state.running:
            update_settings(remanga_autobattle_enabled=False)
            return "ℹ️ Автобой Remanga уже остановлен."
        self.scheduler.remove_job(JOB_REMANGA_AUTOBATTLE)
        self.remanga_state.running = False
        update_settings(remanga_autobattle_enabled=False)
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
                            f"⚠️ Автофарм карт упал: <code>{exc}</code>",
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
            "────────────────────\n"
            "• /battle · сундук · дейлики\n"
            "• /cards/pack · бесплатные паки\n"
            "• уведомления · notifications\n"
            "• быстрый цикл каждые ~35–75 сек\n"
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
        self._mb_session_started_at = datetime.now()
        self._mb_session_titles_base = int(self.mangabuff.stats.titles_visited or 0)
        self.mangabuff.mark_farm_session_start()
        update_settings(mangabuff_setup_done=True, mangabuff_farm_enabled=True)
        # карты/эвенты — параллельно с чтением (основной источник дропов)
        try:
            if not (
                self.mangabuff._events_running
                or (self._events_task and not self._events_task.done())
            ):
                await self.start_events_farm()
        except Exception as exc:  # noqa: BLE001
            logger.warning("auto-start events farm: %s", exc)
        return (
            f"<b>📚 Фарм запущен</b>\n"
            f"────────────────────\n"
            f"• топ тайтлы · чтение до 90%\n"
            f"• карты · сундуки · эвенты\n"
            f"• ночь 01:00–05:00 МСК\n"
            f"🎚 Темп: <b>{self._speed_label()}</b>\n"
            f"Карты — «Карты · Эвенты» · стоп чтения — «Стоп»."
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
            f"────────────────────\n"
            f"📖 За сессию: <b>{self._session_chapters()}</b>\n"
            f"📚 Всего: <b>{st.chapters_read}</b>\n"
            f"📈 Темп: <b>{self._chapters_per_hour():.0f}</b> гл/час{pace}"
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
                f"────────────────────\n"
                f"📚 Всего: {stats.chapters_read}\n"
                f"📈 {self._chapters_per_hour():.0f} гл/час{pace}\n"
                f"🏷 Тайтлов за сессию: {self._session_titles()}\n"
                f"🎁 Наград: {stats.rewards_claimed} · 🃏 {stats.cards_claimed}\n"
                f"🎚 {self._speed_label()}\n"
                f"🔗 <code>{(stats.last_url or '—')[:100]}</code>",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("milestone notify: %s", exc)

    async def _setup_bot_profile(self) -> None:
        """Название, описание и команды бота в Telegram."""
        try:
            await self.bot.set_my_name(name=BRAND)
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_my_name: %s", exc)
        try:
            await self.bot.set_my_short_description(
                short_description=f"{BRAND_LINE} · автобои и авточтение"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_my_short_description: %s", exc)
        try:
            await self.bot.set_my_description(
                description=(
                    f"{BRAND} — личный автопилот для Remanga и MangaBuff.\n\n"
                    "⚔️ Remanga: автобои murim-cards, статус, рейтинг, тонкие уведомления.\n"
                    "📚 MangaBuff: фарм популярных тайтлов, награды, карты, макеты, "
                    "редкие комментарии, ночной режим.\n\n"
                    "Темп чтения настраивается пресетами. Сессии браузера раздельные. "
                    "Интерфейс — чистый, статус — всегда под рукой."
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_my_description: %s", exc)
        try:
            await self.bot.set_my_commands(
                [
                    BotCommand(command="start", description="Домой · панель Orion"),
                    BotCommand(command="pulse", description="Пульс модулей"),
                    BotCommand(command="menu", description="Главное меню"),
                    BotCommand(command="cards", description="Карты · эвенты"),
                    BotCommand(command="auto", description="Remanga: автобой"),
                    BotCommand(command="battle", description="Remanga: один бой"),
                    BotCommand(command="help", description="О боте"),
                ]
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("set_my_commands: %s", exc)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info("Старт бота (admin=%s)", self.config.telegram_admin_id or "pending")
        await self._setup_bot_profile()
        # Soft-start браузеров — падение одного не валит polling
        try:
            await self.remanga.start(headless=True)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Remanga browser: %s", exc)

        # Если в settings ещё старые медленные дефолты 5–15 — поднять до «Живой»
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
            logger.info("MangaBuff speed defaulted to Живой 0.20–0.45с")
            s = load_settings()

        # Восстановить автобой Remanga, если был включён до рестарта/обновления
        if s.remanga_autobattle_enabled:
            try:
                self.remanga_state.sync_from_disk()
                msg = await self.start_remanga_autobattle(resume=True)
                logger.info("Remanga autobattle resumed: %s", msg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Remanga autobattle resume failed: %s", exc)

        # MangaBuff: логин всегда; фарм — если включён в settings (с миграцией)
        try:
            if self.config.mangabuff_email:
                await self.mangabuff.start(headless=True)
                ok = await self.mangabuff.ensure_login()
                logger.info("MangaBuff login on boot: %s", ok)
                if ok and self._should_resume_mangabuff_farm():
                    # start_mangabuff_read сам поднимет автофарм карт
                    asyncio.create_task(self.start_mangabuff_read())
                    logger.info("MangaBuff farm resumed")
                elif ok and load_settings().mangabuff_events_farm_enabled:
                    asyncio.create_task(self.start_events_farm(resume=True))
                    logger.info("MangaBuff events farm resumed")
        except Exception as exc:  # noqa: BLE001
            logger.warning("MangaBuff browser: %s", exc)
        self.scheduler.start()
        try:
            await self.dp.start_polling(self.bot, handle_signals=True)
        finally:
            await self.shutdown()

    def _should_resume_mangabuff_farm(self) -> bool:
        """
        True если фарм был включён до рестарта.
        Миграция: если ключа ещё нет в settings.json, но фарм уже
        настраивали / есть прогресс — считаем что нужно возобновить.
        """
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

        # Старые установки без флага
        if s.mangabuff_setup_done or self.mangabuff.stats.chapters_read > 0:
            update_settings(mangabuff_farm_enabled=True)
            return True
        return False

    async def shutdown(self) -> None:
        logger.info("Shutdown...")
        # Флаги remanga_autobattle_enabled / mangabuff_farm_enabled не трогаем —
        # после рестарта/обновления возобновим по settings.json
        self.remanga_state.running = False
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
