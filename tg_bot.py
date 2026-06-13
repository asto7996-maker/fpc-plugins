"""
Telegram-бот на aiogram 3.x — управление Starvell Cardinal.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from automation import AutomationEngine
from config import Settings, load_settings, save_settings, md5_hex, StarvellAccount
from database import Database
from plugin_manager import CardinalCore, EventManager, PluginManager

logger = logging.getLogger("starvell.tg")

# Callback prefixes
CB_MAIN = "sc:main"
CB_TOGGLE = "sc:toggle:"
CB_NOTIFY = "sc:notify:"
CB_PROFILE = "sc:profile"
CB_STATUS = "sc:status"
CB_PLUGINS = "sc:plugins"
CB_PLUGIN_TOGGLE = "sc:plug:"
CB_SETTINGS = "sc:settings"
CB_AUTODELIVERY = "sc:adel"
CB_AI = "sc:ai"
CB_BACK = "sc:back"

MAX_LOGIN_ATTEMPTS = 5
BLOCK_HOURS = 24


class AuthStates(StatesGroup):
    waiting_password = State()


class ConfigStates(StatesGroup):
    waiting_session = State()
    waiting_gemini_key = State()
    waiting_openai_key = State()
    waiting_welcome = State()
    waiting_autodelivery_product = State()
    waiting_autodelivery_items = State()


class TelegramBot:
    """Обёртка над aiogram для Starvell Cardinal."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        cardinal: CardinalCore,
        plugin_manager: PluginManager,
        automation: AutomationEngine,
    ) -> None:
        self.settings = settings
        self.db = db
        self.cardinal = cardinal
        self.plugin_manager = plugin_manager
        self.automation = automation
        self.bot = Bot(token=settings.bot_token)
        self.dp = Dispatcher()
        self.router = Router()
        self.dp.include_router(self.router)
        self._register_handlers()

    def _register_handlers(self) -> None:
        self.router.message.register(self.cmd_start, CommandStart())
        self.router.message.register(self.cmd_restart, Command("restart"))
        self.router.message.register(self.cmd_status, Command("status"))
        self.router.message.register(self.cmd_menu, Command("menu"))
        self.router.message.register(self.on_password, AuthStates.waiting_password)
        self.router.message.register(self.on_session_cookie, ConfigStates.waiting_session)
        self.router.message.register(self.on_gemini_key, ConfigStates.waiting_gemini_key)
        self.router.message.register(self.on_openai_key, ConfigStates.waiting_openai_key)
        self.router.message.register(self.on_welcome_text, ConfigStates.waiting_welcome)
        self.router.message.register(self.on_adel_product, ConfigStates.waiting_autodelivery_product)
        self.router.message.register(self.on_adel_items, ConfigStates.waiting_autodelivery_items)

        self.router.callback_query.register(self.cb_main, F.data == CB_MAIN)
        self.router.callback_query.register(self.cb_back, F.data == CB_BACK)
        self.router.callback_query.register(self.cb_profile, F.data == CB_PROFILE)
        self.router.callback_query.register(self.cb_status, F.data == CB_STATUS)
        self.router.callback_query.register(self.cb_plugins, F.data == CB_PLUGINS)
        self.router.callback_query.register(self.cb_settings, F.data == CB_SETTINGS)
        self.router.callback_query.register(self.cb_autodelivery, F.data == CB_AUTODELIVERY)
        self.router.callback_query.register(self.cb_ai_settings, F.data == CB_AI)
        self.router.callback_query.register(self.cb_toggle, F.data.startswith(CB_TOGGLE))
        self.router.callback_query.register(self.cb_notify, F.data.startswith(CB_NOTIFY))
        self.router.callback_query.register(self.cb_plugin_toggle, F.data.startswith(CB_PLUGIN_TOGGLE))
        self.router.callback_query.register(self.cb_set_session, F.data == "sc:set_session")
        self.router.callback_query.register(self.cb_ai_provider, F.data.startswith("sc:aiprovider:"))

    # ── Авторизация ───────────────────────────────────────────────────────

    async def _is_authorized(self, user_id: int) -> bool:
        if self.settings.admin_ids and user_id in self.settings.admin_ids:
            return True
        user = await self.db.get_user(user_id)
        return bool(user.get("authorized"))

    async def _check_blocked(self, user_id: int) -> bool:
        user = await self.db.get_user(user_id)
        blocked_until = int(user.get("blocked_until") or 0)
        if blocked_until > int(time.time()):
            return True
        return False

    async def cmd_start(self, message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id
        if await self._check_blocked(user_id):
            await message.answer("🔒 Доступ заблокирован на 24 часа из-за неверных попыток входа.")
            return
        if await self._is_authorized(user_id):
            await message.answer("👋 Добро пожаловать в Starvell Cardinal!", reply_markup=self._main_kb())
            return
        await state.set_state(AuthStates.waiting_password)
        await message.answer("🔐 Введите пароль для доступа к боту:")

    async def on_password(self, message: Message, state: FSMContext) -> None:
        user_id = message.from_user.id
        password = (message.text or "").strip()
        if password.startswith("/"):
            await state.clear()
            return

        expected = self.settings.bot_password_md5
        if not expected:
            await self.db.set_authorized(user_id, True)
            await state.clear()
            await message.answer("✅ Доступ открыт (пароль не задан).", reply_markup=self._main_kb())
            return

        if md5_hex(password) == expected:
            await self.db.reset_failed(user_id)
            await self.db.set_authorized(user_id, True)
            await state.clear()
            await message.answer("✅ Вход выполнен!", reply_markup=self._main_kb())
            return

        attempts = await self.db.increment_failed(user_id)
        if attempts >= MAX_LOGIN_ATTEMPTS:
            await self.db.set_blocked_until(user_id, int(time.time()) + BLOCK_HOURS * 3600)
            await state.clear()
            await message.answer("🔒 Слишком много попыток. Доступ заблокирован на 24 часа.")
            return
        await message.answer(f"❌ Неверный пароль. Осталось попыток: {MAX_LOGIN_ATTEMPTS - attempts}")

    # ── Клавиатуры ────────────────────────────────────────────────────────

    def _main_kb(self) -> InlineKeyboardMarkup:
        s = load_settings()
        def flag(key: str, val: bool) -> str:
            return "✅" if val else "❌"
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статус аккаунта", callback_data=CB_STATUS)],
            [
                InlineKeyboardButton(
                    text=f"{flag('d', s.auto_delivery_enabled)} Автовыдача",
                    callback_data=f"{CB_TOGGLE}auto_delivery",
                ),
                InlineKeyboardButton(
                    text=f"{flag('b', s.auto_bump_enabled)} Автобамп",
                    callback_data=f"{CB_TOGGLE}auto_bump",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{flag('w', s.auto_welcome_enabled)} Приветствие",
                    callback_data=f"{CB_TOGGLE}auto_welcome",
                ),
                InlineKeyboardButton(
                    text=f"{flag('r', s.auto_review_enabled)} Авто-отзывы",
                    callback_data=f"{CB_TOGGLE}auto_review",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{flag('a', s.ai_replies_enabled)} ИИ-ответы",
                    callback_data=f"{CB_TOGGLE}ai_replies",
                ),
            ],
            [
                InlineKeyboardButton(text="🔔 Уведомления", callback_data=f"{CB_NOTIFY}menu"),
                InlineKeyboardButton(text="👤 Профиль", callback_data=CB_PROFILE),
            ],
            [
                InlineKeyboardButton(text="📦 Автовыдача (склад)", callback_data=CB_AUTODELIVERY),
                InlineKeyboardButton(text="🤖 Настройки ИИ", callback_data=CB_AI),
            ],
            [
                InlineKeyboardButton(text="🔌 Плагины", callback_data=CB_PLUGINS),
                InlineKeyboardButton(text="⚙️ Настройки", callback_data=CB_SETTINGS),
            ],
        ])

    def _back_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Главное меню", callback_data=CB_MAIN)],
        ])

    # ── Команды ───────────────────────────────────────────────────────────

    async def cmd_menu(self, message: Message) -> None:
        if not await self._is_authorized(message.from_user.id):
            await message.answer("Сначала авторизуйтесь: /start")
            return
        await message.answer("📋 Главное меню:", reply_markup=self._main_kb())

    async def cmd_status(self, message: Message) -> None:
        if not await self._is_authorized(message.from_user.id):
            return
        text = await self._build_status_text()
        await message.answer(text, parse_mode="HTML", reply_markup=self._back_kb())

    async def cmd_restart(self, message: Message) -> None:
        if not await self._is_authorized(message.from_user.id):
            return
        await message.answer("♻️ Перезапуск…")
        os._exit(0)

    # ── Callbacks ─────────────────────────────────────────────────────────

    async def cb_main(self, call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        if not await self._is_authorized(call.from_user.id):
            await call.answer("Нет доступа", show_alert=True)
            return
        await call.message.edit_text("📋 <b>Starvell Cardinal</b> — панель управления", parse_mode="HTML", reply_markup=self._main_kb())
        await call.answer()

    async def cb_back(self, call: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await self.cb_main(call, state)

    async def cb_toggle(self, call: CallbackQuery) -> None:
        if not await self._is_authorized(call.from_user.id):
            return
        key = call.data.replace(CB_TOGGLE, "")
        new_val = await self.db.toggle_feature_flag(key)
        # Синхронизируем с settings.json
        s = load_settings()
        attr_map = {
            "auto_delivery": "auto_delivery_enabled",
            "auto_bump": "auto_bump_enabled",
            "auto_welcome": "auto_welcome_enabled",
            "auto_review": "auto_review_enabled",
            "ai_replies": "ai_replies_enabled",
        }
        if key in attr_map:
            setattr(s, attr_map[key], new_val)
            save_settings(s)
        labels = {
            "auto_delivery": "Автовыдача",
            "auto_bump": "Автобамп",
            "auto_welcome": "Приветствие",
            "auto_review": "Авто-отзывы",
            "ai_replies": "ИИ-ответы",
        }
        status = "включена" if new_val else "выключена"
        await call.answer(f"{labels.get(key, key)} {status}")
        await call.message.edit_reply_markup(reply_markup=self._main_kb())

    async def cb_status(self, call: CallbackQuery) -> None:
        if not await self._is_authorized(call.from_user.id):
            return
        text = await self._build_status_text()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=self._back_kb())
        await call.answer()

    async def _build_status_text(self) -> str:
        status = await self.automation.get_status()
        lines = ["📊 <b>Статус Starvell</b>\n"]
        for acc in status.get("accounts", []):
            lines.append(f"👤 <b>{acc.get('name', '?')}</b>")
            if acc.get("error"):
                lines.append(f"  ❌ {acc['error']}")
                continue
            if acc.get("authorized"):
                lines.append(f"  ✅ {acc.get('username', '?')}")
                lines.append(f"  💰 Баланс: <b>{acc.get('balance', '?')} ₽</b>")
                lines.append(f"  📦 Активных заказов: {acc.get('active_orders', 0)}")
                lines.append(f"  📋 Всего в ленте: {acc.get('total_orders', 0)}")
            else:
                lines.append("  ❌ Не авторизован")
            lines.append("")
        products = await self.db.list_autodelivery_products()
        if products:
            lines.append("📦 <b>Склад автовыдачи:</b>")
            for name, count in products:
                lines.append(f"  • {name}: {count} шт.")
        return "\n".join(lines)

    async def cb_profile(self, call: CallbackQuery) -> None:
        if not await self._is_authorized(call.from_user.id):
            return
        s = load_settings()
        accounts = s.get_active_accounts()
        acc_text = "\n".join(f"  • {a.name}" for a in accounts) or "  не настроен"
        session_set = "✅" if s.session_cookie or accounts else "❌"
        text = (
            f"👤 <b>Профиль</b>\n\n"
            f"🍪 Сессия Starvell: {session_set}\n"
            f"📱 Аккаунтов: {len(accounts)}\n{acc_text}\n\n"
            f"🤖 ИИ: {s.ai_provider.upper()}\n"
            f"🔑 Gemini: {'✅' if s.gemini_api_key else '❌'}\n"
            f"🔑 OpenAI: {'✅' if s.openai_api_key else '❌'}\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍪 Обновить SESSION_COOKIE", callback_data="sc:set_session")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CB_MAIN)],
        ])
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    async def cb_settings(self, call: CallbackQuery) -> None:
        if not await self._is_authorized(call.from_user.id):
            return
        s = load_settings()
        text = (
            f"⚙️ <b>Настройки</b>\n\n"
            f"⏱ Интервал чатов: {s.chat_poll_interval}с\n"
            f"⏱ Интервал заказов: {s.orders_poll_interval}с\n"
            f"⏱ Интервал бампа: {s.bump_interval}с\n"
            f"⏱ Задержка API: {s.api_delay_seconds}с\n"
            f"💬 Кулдаун приветствия: {s.welcome_cooldown_minutes} мин\n"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Текст приветствия", callback_data="sc:edit_welcome")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CB_MAIN)],
        ])
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    async def cb_notify(self, call: CallbackQuery) -> None:
        if not await self._is_authorized(call.from_user.id):
            return
        field = call.data.replace(CB_NOTIFY, "")
        if field == "menu":
            user = await self.db.get_user(call.from_user.id)
            def ic(v): return "✅" if v else "❌"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{ic(user['notify_orders'])} Заказы", callback_data=f"{CB_NOTIFY}notify_orders")],
                [InlineKeyboardButton(text=f"{ic(user['notify_chats'])} Чаты", callback_data=f"{CB_NOTIFY}notify_chats")],
                [InlineKeyboardButton(text=f"{ic(user['notify_bump'])} Бамп", callback_data=f"{CB_NOTIFY}notify_bump")],
                [InlineKeyboardButton(text=f"{ic(user['notify_auth'])} Авторизация", callback_data=f"{CB_NOTIFY}notify_auth")],
                [InlineKeyboardButton(text=f"{ic(user['notify_delivery'])} Выдача", callback_data=f"{CB_NOTIFY}notify_delivery")],
                [InlineKeyboardButton(text="◀️ Назад", callback_data=CB_MAIN)],
            ])
            await call.message.edit_text("🔔 <b>Уведомления</b>\nВыберите, что переключить:", parse_mode="HTML", reply_markup=kb)
            await call.answer()
            return
        new_val = await self.db.toggle_notify(call.from_user.id, field)
        await call.answer("Включено" if new_val else "Выключено")
        user = await self.db.get_user(call.from_user.id)
        def ic(v): return "✅" if v else "❌"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{ic(user['notify_orders'])} Заказы", callback_data=f"{CB_NOTIFY}notify_orders")],
            [InlineKeyboardButton(text=f"{ic(user['notify_chats'])} Чаты", callback_data=f"{CB_NOTIFY}notify_chats")],
            [InlineKeyboardButton(text=f"{ic(user['notify_bump'])} Бамп", callback_data=f"{CB_NOTIFY}notify_bump")],
            [InlineKeyboardButton(text=f"{ic(user['notify_auth'])} Авторизация", callback_data=f"{CB_NOTIFY}notify_auth")],
            [InlineKeyboardButton(text=f"{ic(user['notify_delivery'])} Выдача", callback_data=f"{CB_NOTIFY}notify_delivery")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CB_MAIN)],
        ])
        await call.message.edit_reply_markup(reply_markup=kb)

    async def cb_plugins(self, call: CallbackQuery) -> None:
        if not await self._is_authorized(call.from_user.id):
            return
        metas = self.plugin_manager.load_all()
        lines = ["🔌 <b>Плагины</b>\n"]
        buttons = []
        for meta in metas:
            status = "✅" if meta.enabled else "❌"
            err = f" ⚠️{meta.load_error[:30]}" if meta.load_error else ""
            lines.append(f"{status} <b>{meta.name}</b> v{meta.version}{err}")
            buttons.append([InlineKeyboardButton(
                text=f"{'🔴 Выкл' if meta.enabled else '🟢 Вкл'} {meta.name}",
                callback_data=f"{CB_PLUGIN_TOGGLE}{meta.uuid}",
            )])
        if not metas:
            lines.append("Плагины не найдены в папке plugins/")
        buttons.append([InlineKeyboardButton(text="🔄 Перезагрузить", callback_data=CB_PLUGINS)])
        buttons.append([InlineKeyboardButton(text="◀️ Назад", callback_data=CB_MAIN)])
        await call.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await call.answer()

    async def cb_plugin_toggle(self, call: CallbackQuery) -> None:
        uuid = call.data.replace(CB_PLUGIN_TOGGLE, "")
        enabled = self.plugin_manager.toggle(uuid)
        await call.answer("Включён" if enabled else "Выключен")
        await self.cb_plugins(call)

    async def cb_autodelivery(self, call: CallbackQuery, state: FSMContext) -> None:
        if not await self._is_authorized(call.from_user.id):
            return
        products = await self.db.list_autodelivery_products()
        lines = ["📦 <b>Склад автовыдачи</b>\n"]
        for name, count in products:
            lines.append(f"• <code>{name}</code> — {count} шт.")
        if not products:
            lines.append("Склад пуст. Добавьте товары.")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить товар", callback_data="sc:adel_add")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CB_MAIN)],
        ])
        await call.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        await call.answer()

    async def cb_ai_settings(self, call: CallbackQuery) -> None:
        if not await self._is_authorized(call.from_user.id):
            return
        s = load_settings()
        text = (
            f"🤖 <b>Настройки ИИ</b>\n\n"
            f"Провайдер: <b>{s.ai_provider}</b>\n"
            f"Gemini: {'✅' if s.gemini_api_key else '❌'}\n"
            f"OpenAI: {'✅' if s.openai_api_key else '❌'}\n\n"
            f"<i>{s.ai_system_prompt[:120]}…</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="Gemini", callback_data="sc:aiprovider:gemini"),
                InlineKeyboardButton(text="OpenAI", callback_data="sc:aiprovider:openai"),
            ],
            [InlineKeyboardButton(text="🔑 Gemini API ключ", callback_data="sc:set_gemini")],
            [InlineKeyboardButton(text="🔑 OpenAI API ключ", callback_data="sc:set_openai")],
            [InlineKeyboardButton(text="◀️ Назад", callback_data=CB_MAIN)],
        ])
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    async def cb_ai_provider(self, call: CallbackQuery) -> None:
        provider = call.data.split(":")[-1]
        s = load_settings()
        s.ai_provider = provider
        save_settings(s)
        await call.answer(f"Провайдер: {provider}")
        await self.cb_ai_settings(call)

    async def cb_set_session(self, call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(ConfigStates.waiting_session)
        await call.message.answer(
            "🍪 Отправьте значение cookie <b>session</b> с сайта starvell.com\n\n"
            "Как получить: DevTools → Application → Cookies → session",
            parse_mode="HTML",
        )
        await call.answer()

    # Дополнительные callback для настроек
    async def _register_extra_callbacks(self) -> None:
        pass

    # Регистрируем доп. callbacks через router в __init__ после основных
    def _register_extra_handlers(self) -> None:
        @self.router.callback_query(F.data == "sc:adel_add")
        async def adel_add(call: CallbackQuery, state: FSMContext):
            if not await self._is_authorized(call.from_user.id):
                return
            await state.set_state(ConfigStates.waiting_autodelivery_product)
            await call.message.answer("Введите название товара (должно совпадать с названием лота на Starvell):")
            await call.answer()

        @self.router.callback_query(F.data == "sc:edit_welcome")
        async def edit_welcome(call: CallbackQuery, state: FSMContext):
            if not await self._is_authorized(call.from_user.id):
                return
            await state.set_state(ConfigStates.waiting_welcome)
            await call.message.answer("Введите новый текст приветствия:")
            await call.answer()

        @self.router.callback_query(F.data == "sc:set_gemini")
        async def set_gemini(call: CallbackQuery, state: FSMContext):
            if not await self._is_authorized(call.from_user.id):
                return
            await state.set_state(ConfigStates.waiting_gemini_key)
            await call.message.answer("Введите Gemini API ключ:")
            await call.answer()

        @self.router.callback_query(F.data == "sc:set_openai")
        async def set_openai(call: CallbackQuery, state: FSMContext):
            if not await self._is_authorized(call.from_user.id):
                return
            await state.set_state(ConfigStates.waiting_openai_key)
            await call.message.answer("Введите OpenAI API ключ:")
            await call.answer()

    # ── FSM обработчики ───────────────────────────────────────────────────

    async def on_session_cookie(self, message: Message, state: FSMContext) -> None:
        cookie = (message.text or "").strip()
        if not cookie or cookie.startswith("/"):
            return
        s = load_settings()
        s.session_cookie = cookie
        if not s.accounts:
            s.accounts = [StarvellAccount(name="default", session_cookie=cookie)]
        else:
            s.accounts[0].session_cookie = cookie
        save_settings(s)
        await state.clear()
        await message.answer("✅ SESSION_COOKIE сохранён!", reply_markup=self._main_kb())

    async def on_gemini_key(self, message: Message, state: FSMContext) -> None:
        key = (message.text or "").strip()
        if not key:
            return
        s = load_settings()
        s.gemini_api_key = key
        save_settings(s)
        await state.clear()
        await message.answer("✅ Gemini API ключ сохранён!")

    async def on_openai_key(self, message: Message, state: FSMContext) -> None:
        key = (message.text or "").strip()
        if not key:
            return
        s = load_settings()
        s.openai_api_key = key
        save_settings(s)
        await state.clear()
        await message.answer("✅ OpenAI API ключ сохранён!")

    async def on_welcome_text(self, message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        s = load_settings()
        s.welcome_text = text
        save_settings(s)
        await state.clear()
        await message.answer("✅ Текст приветствия обновлён!")

    async def on_adel_product(self, message: Message, state: FSMContext) -> None:
        product = (message.text or "").strip()
        if not product:
            return
        await state.update_data(adel_product=product)
        await state.set_state(ConfigStates.waiting_autodelivery_items)
        await message.answer(
            f"Товар: <b>{product}</b>\n\n"
            "Отправьте коды/товары (каждый с новой строки):",
            parse_mode="HTML",
        )

    async def on_adel_items(self, message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        product = data.get("adel_product", "")
        items = [line.strip() for line in (message.text or "").splitlines() if line.strip()]
        if not product or not items:
            await message.answer("❌ Пустой список")
            return
        added = await self.db.add_autodelivery_items(product, items)
        await state.clear()
        await message.answer(f"✅ Добавлено {added} позиций для «{product}»")

    # ── Уведомления в чат ─────────────────────────────────────────────────

    async def broadcast(self, text: str) -> None:
        """Отправляет уведомление всем авторизованным пользователям."""
        users = await self.db.get_authorized_users()
        if self.settings.admin_ids:
            users = list(set(users + self.settings.admin_ids))
        for uid in users:
            user = await self.db.get_user(uid)
            try:
                await self.bot.send_message(uid, text, parse_mode="HTML")
            except Exception as exc:
                logger.warning("broadcast to %s failed: %s", uid, exc)

    async def start_polling(self) -> None:
        self._register_extra_handlers()
        await self.dp.start_polling(self.bot)

    async def stop(self) -> None:
        await self.bot.session.close()
