"""
Telegram-интерфейс Starvell Cardinal (стиль FunPay Cardinal).
Вся настройка — в боте, с мгновенной проверкой данных.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from automation import AutomationEngine
from config import VERSION, Settings, StarvellAccount, load_settings, save_settings
from database import Database
from plugin_manager import CardinalCore, PluginManager
from validators import test_gemini_key, test_starvell_session

logger = logging.getLogger("starvell.tg")

# Callback data (как CBT в FunPay Cardinal)
CB = {
    "main": "sc:main",
    "back": "sc:back",
    "profile": "sc:profile",
    "status": "sc:status",
    "plugins": "sc:plugins",
    "settings": "sc:settings",
    "adel": "sc:adel",
    "gemini": "sc:gemini",
    "notify": "sc:notify:",
    "toggle": "sc:toggle:",
    "plug": "sc:plug:",
    "setup": "sc:setup",
    "check_auth": "sc:check_auth",
    "check_gemini": "sc:check_gemini",
    "set_session": "sc:set_session",
    "set_gemini": "sc:set_gemini",
    "adel_add": "sc:adel_add",
    "edit_welcome": "sc:edit_welcome",
    "edit_bump": "sc:edit_bump",
    "edit_delivery": "sc:edit_delivery",
    "first_setup": "sc:first_setup",
}


class SetupStates(StatesGroup):
    session = State()
    gemini = State()
    welcome = State()
    bump_interval = State()
    delivery_tpl = State()
    adel_product = State()
    adel_items = State()
    gemini_prompt = State()


class TelegramBot:
    """Панель управления Starvell Cardinal."""

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

    # ── Доступ (без пароля, как Cardinal у владельца) ─────────────────────

    async def _claim_owner(self, user_id: int) -> None:
        s = load_settings()
        if s.owner_id == 0:
            s.owner_id = user_id
            if user_id not in s.admin_ids:
                s.admin_ids.append(user_id)
            save_settings(s)
        await self.db.set_authorized(user_id, True)

    async def _has_access(self, user_id: int) -> bool:
        s = load_settings()
        if s.owner_id == 0:
            return True
        if user_id == s.owner_id or user_id in s.admin_ids:
            return True
        return False

    async def _deny(self, message_or_call: Message | CallbackQuery) -> None:
        text = "⛔ Бот уже привязан к другому владельцу."
        if isinstance(message_or_call, CallbackQuery):
            await message_or_call.answer(text, show_alert=True)
        else:
            await message_or_call.answer(text)

    def _register_handlers(self) -> None:
        r = self.router
        r.message.register(self.cmd_start, CommandStart())
        r.message.register(self.cmd_menu, Command("menu"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_restart, Command("restart"))
        r.message.register(self.cmd_profile, Command("profile"))
        r.message.register(self.cmd_plugins, Command("plugins"))

        r.message.register(self.on_session, SetupStates.session)
        r.message.register(self.on_gemini, SetupStates.gemini)
        r.message.register(self.on_welcome, SetupStates.welcome)
        r.message.register(self.on_bump, SetupStates.bump_interval)
        r.message.register(self.on_delivery, SetupStates.delivery_tpl)
        r.message.register(self.on_adel_product, SetupStates.adel_product)
        r.message.register(self.on_adel_items, SetupStates.adel_items)
        r.message.register(self.on_gemini_prompt, SetupStates.gemini_prompt)

        r.callback_query.register(self.cb_main, F.data == CB["main"])
        r.callback_query.register(self.cb_back, F.data == CB["back"])
        r.callback_query.register(self.cb_profile, F.data == CB["profile"])
        r.callback_query.register(self.cb_status, F.data == CB["status"])
        r.callback_query.register(self.cb_plugins, F.data == CB["plugins"])
        r.callback_query.register(self.cb_settings, F.data == CB["settings"])
        r.callback_query.register(self.cb_adel, F.data == CB["adel"])
        r.callback_query.register(self.cb_gemini, F.data == CB["gemini"])
        r.callback_query.register(self.cb_setup, F.data == CB["setup"])
        r.callback_query.register(self.cb_check_auth, F.data == CB["check_auth"])
        r.callback_query.register(self.cb_check_gemini, F.data == CB["check_gemini"])
        r.callback_query.register(self.cb_set_session, F.data == CB["set_session"])
        r.callback_query.register(self.cb_set_gemini, F.data == CB["set_gemini"])
        r.callback_query.register(self.cb_first_setup, F.data == CB["first_setup"])
        r.callback_query.register(self.cb_toggle, F.data.startswith(CB["toggle"]))
        r.callback_query.register(self.cb_notify, F.data.startswith(CB["notify"]))
        r.callback_query.register(self.cb_plug_toggle, F.data.startswith(CB["plug"]))
        r.callback_query.register(self.cb_adel_add, F.data == CB["adel_add"])
        r.callback_query.register(self.cb_edit_welcome, F.data == CB["edit_welcome"])
        r.callback_query.register(self.cb_edit_bump, F.data == CB["edit_bump"])
        r.callback_query.register(self.cb_edit_delivery, F.data == CB["edit_delivery"])

    # ── Клавиатуры ────────────────────────────────────────────────────────

    def _flag(self, on: bool) -> str:
        return "🟢" if on else "🔴"

    def _main_kb(self) -> InlineKeyboardMarkup:
        s = load_settings()
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data=CB["status"])],
            [
                InlineKeyboardButton(text=f"{self._flag(s.auto_delivery_enabled)} Выдача", callback_data=f"{CB['toggle']}auto_delivery"),
                InlineKeyboardButton(text=f"{self._flag(s.auto_bump_enabled)} Бамп", callback_data=f"{CB['toggle']}auto_bump"),
            ],
            [
                InlineKeyboardButton(text=f"{self._flag(s.auto_welcome_enabled)} Приветствие", callback_data=f"{CB['toggle']}auto_welcome"),
                InlineKeyboardButton(text=f"{self._flag(s.auto_review_enabled)} Отзывы", callback_data=f"{CB['toggle']}auto_review"),
            ],
            [InlineKeyboardButton(text=f"{self._flag(s.ai_replies_enabled)} Gemini в чатах", callback_data=f"{CB['toggle']}ai_replies")],
            [
                InlineKeyboardButton(text="👤 Профиль Starvell", callback_data=CB["profile"]),
                InlineKeyboardButton(text="🔔 Уведомления", callback_data=f"{CB['notify']}menu"),
            ],
            [
                InlineKeyboardButton(text="📦 Автовыдача", callback_data=CB["adel"]),
                InlineKeyboardButton(text="🤖 Gemini", callback_data=CB["gemini"]),
            ],
            [
                InlineKeyboardButton(text="⚙️ Настройки", callback_data=CB["settings"]),
                InlineKeyboardButton(text="🔌 Плагины", callback_data=CB["plugins"]),
            ],
            [InlineKeyboardButton(text="🛠 Первичная настройка", callback_data=CB["setup"])],
        ])

    def _back_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])],
        ])

    def _setup_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="1️⃣ Привязать Starvell (session)", callback_data=CB["set_session"])],
            [InlineKeyboardButton(text="2️⃣ Настроить Gemini", callback_data=CB["set_gemini"])],
            [InlineKeyboardButton(text="3️⃣ Добавить товары на склад", callback_data=CB["adel_add"])],
            [InlineKeyboardButton(text="✅ Проверить Starvell", callback_data=CB["check_auth"])],
            [InlineKeyboardButton(text="✅ Проверить Gemini", callback_data=CB["check_gemini"])],
            [InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])],
        ])

    async def _main_text(self) -> str:
        s = load_settings()
        starvell = "✅" if s.is_starvell_configured() else "❌"
        gemini = "✅" if s.is_gemini_configured() else "❌"
        user = s.starvell_username or "—"
        return (
            f"🤖 <b>Starvell Cardinal</b> v{VERSION}\n\n"
            f"Starvell: {starvell} <code>{user}</code>\n"
            f"Gemini: {gemini}\n\n"
            f"<i>Управление как в FunPay Cardinal — всё через это меню.</i>"
        )

    # ── Команды ───────────────────────────────────────────────────────────

    async def cmd_start(self, message: Message, state: FSMContext) -> None:
        uid = message.from_user.id
        if not await self._has_access(uid):
            await self._deny(message)
            return
        await self._claim_owner(uid)
        await state.clear()
        s = load_settings()

        if not s.is_starvell_configured():
            await message.answer(
                "👋 <b>Добро пожаловать в Starvell Cardinal!</b>\n\n"
                "Для начала привяжите аккаунт Starvell.\n"
                "Нажмите кнопку ниже и отправьте cookie <b>session</b>.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🍪 Привязать Starvell", callback_data=CB["first_setup"])],
                ]),
            )
            return

        await message.answer(await self._main_text(), parse_mode="HTML", reply_markup=self._main_kb())

    async def cmd_menu(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await message.answer(await self._main_text(), parse_mode="HTML", reply_markup=self._main_kb())

    async def cmd_status(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await message.answer(await self._status_text(), parse_mode="HTML", reply_markup=self._back_kb())

    async def cmd_profile(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await message.answer(await self._profile_text(), parse_mode="HTML", reply_markup=self._profile_kb())

    async def cmd_plugins(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        text, kb = self._plugins_view()
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

    async def cmd_restart(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await message.answer("♻️ Перезапуск бота…")
        os._exit(0)

    # ── Callbacks ─────────────────────────────────────────────────────────

    async def cb_main(self, call: CallbackQuery, state: FSMContext) -> None:
        if not await self._has_access(call.from_user.id):
            await self._deny(call)
            return
        await state.clear()
        await call.message.edit_text(await self._main_text(), parse_mode="HTML", reply_markup=self._main_kb())
        await call.answer()

    async def cb_back(self, call: CallbackQuery, state: FSMContext) -> None:
        await self.cb_main(call, state)

    async def cb_first_setup(self, call: CallbackQuery, state: FSMContext) -> None:
        await self.cb_set_session(call, state)

    async def cb_setup(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        await call.message.edit_text(
            "🛠 <b>Первичная настройка</b>\n\nНастройте всё по порядку. "
            "После ввода каждого параметра бот сразу проверит его.",
            parse_mode="HTML",
            reply_markup=self._setup_kb(),
        )
        await call.answer()

    async def cb_toggle(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        key = call.data.replace(CB["toggle"], "")
        new_val = await self.db.toggle_feature_flag(key)
        s = load_settings()
        mapping = {
            "auto_delivery": "auto_delivery_enabled",
            "auto_bump": "auto_bump_enabled",
            "auto_welcome": "auto_welcome_enabled",
            "auto_review": "auto_review_enabled",
            "ai_replies": "ai_replies_enabled",
        }
        if key in mapping:
            setattr(s, mapping[key], new_val)
            save_settings(s)
            if key == "auto_bump":
                await self.automation.reload()
        labels = {
            "auto_delivery": "Автовыдача",
            "auto_bump": "Автобамп",
            "auto_welcome": "Приветствие",
            "auto_review": "Авто-отзывы",
            "ai_replies": "Gemini в чатах",
        }
        await call.answer(f"{labels.get(key, key)}: {'вкл' if new_val else 'выкл'}")
        await call.message.edit_reply_markup(reply_markup=self._main_kb())

    async def cb_status(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        await call.message.edit_text(await self._status_text(), parse_mode="HTML", reply_markup=self._back_kb())
        await call.answer()

    async def _status_text(self) -> str:
        status = await self.automation.get_status()
        lines = ["📊 <b>Статистика</b>\n"]
        for acc in status.get("accounts", []):
            if acc.get("error"):
                lines.append(f"❌ {acc.get('name')}: {acc['error']}")
                continue
            if acc.get("authorized"):
                lines += [
                    f"👤 <b>{acc.get('username', '?')}</b>",
                    f"💰 Баланс: <b>{acc.get('balance', '?')} ₽</b>",
                    f"📦 Активных заказов: {acc.get('active_orders', 0)}",
                    f"📋 В ленте: {acc.get('total_orders', 0)}",
                    "",
                ]
            else:
                lines.append("❌ Starvell не авторизован — обновите session в профиле\n")
        products = await self.db.list_autodelivery_products()
        if products:
            lines.append("<b>Склад:</b>")
            for name, cnt in products:
                lines.append(f"  • {name}: {cnt} шт.")
        return "\n".join(lines)

    def _profile_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🍪 Изменить session", callback_data=CB["set_session"])],
            [InlineKeyboardButton(text="✅ Проверить авторизацию", callback_data=CB["check_auth"])],
            [InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])],
        ])

    async def _profile_text(self) -> str:
        s = load_settings()
        session_ok = "✅" if s.is_starvell_configured() else "❌"
        return (
            f"👤 <b>Профиль Starvell</b>\n\n"
            f"Аккаунт: {session_ok} <b>{s.starvell_username or 'не привязан'}</b>\n"
            f"Session: {'задан' if s.session_cookie else 'не задан'}\n\n"
            f"<i>Cookie session — аналог golden_key в FunPay Cardinal.</i>"
        )

    async def cb_profile(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        await call.message.edit_text(await self._profile_text(), parse_mode="HTML", reply_markup=self._profile_kb())
        await call.answer()

    async def cb_check_auth(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        s = load_settings()
        if not s.session_cookie:
            await call.answer("Session не задан!", show_alert=True)
            return
        await call.answer("Проверяю…")
        ok, msg, info = await test_starvell_session(s.session_cookie)
        if ok:
            user = (info.get("user") or {})
            s.starvell_username = str(user.get("username") or "")
            s.sid_cookie = str(info.get("sid") or s.sid_cookie)
            s.my_games_cookie = str(info.get("my_games") or s.my_games_cookie)
            save_settings(s)
        await call.message.answer(f"{'✅' if ok else '❌'} {msg}")

    async def cb_check_gemini(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        s = load_settings()
        if not s.gemini_api_key:
            await call.answer("Gemini ключ не задан!", show_alert=True)
            return
        await call.answer("Проверяю Gemini…")
        ok, msg = await test_gemini_key(s.gemini_api_key, s.ai_system_prompt)
        await call.message.answer(f"{'✅' if ok else '❌'} {msg}")

    async def cb_set_session(self, call: CallbackQuery, state: FSMContext) -> None:
        if not await self._has_access(call.from_user.id):
            return
        await state.set_state(SetupStates.session)
        await call.message.answer(
            "🍪 <b>Session cookie Starvell</b>\n\n"
            "1. Войдите на starvell.com\n"
            "2. F12 → Application → Cookies → <code>session</code>\n"
            "3. Скопируйте значение и отправьте сюда\n\n"
            "Бот сразу проверит cookie.",
            parse_mode="HTML",
        )
        await call.answer()

    async def cb_set_gemini(self, call: CallbackQuery, state: FSMContext) -> None:
        if not await self._has_access(call.from_user.id):
            return
        await state.set_state(SetupStates.gemini)
        await call.message.answer(
            "🤖 <b>Gemini API ключ</b>\n\n"
            "Получить: https://aistudio.google.com/apikey\n"
            "Отправьте ключ — бот сразу проверит его.",
            parse_mode="HTML",
        )
        await call.answer()

    async def cb_settings(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        s = load_settings()
        text = (
            f"⚙️ <b>Настройки</b>\n\n"
            f"Чаты: {s.chat_poll_interval}с | Заказы: {s.orders_poll_interval}с\n"
            f"Бамп: {int(s.bump_interval)}с | API delay: {s.api_delay_seconds}с\n"
            f"Приветствие: кулдаун {s.welcome_cooldown_minutes} мин"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Текст приветствия", callback_data=CB["edit_welcome"])],
            [InlineKeyboardButton(text="⏱ Интервал бампа (сек)", callback_data=CB["edit_bump"])],
            [InlineKeyboardButton(text="📄 Шаблон выдачи", callback_data=CB["edit_delivery"])],
            [InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])],
        ])
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    async def cb_notify(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        field = call.data.replace(CB["notify"], "")
        uid = call.from_user.id
        if field == "menu":
            user = await self.db.get_user(uid)
            ic = lambda v: "🟢" if v else "🔴"
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=f"{ic(user['notify_orders'])} Заказы", callback_data=f"{CB['notify']}notify_orders")],
                [InlineKeyboardButton(text=f"{ic(user['notify_chats'])} Чаты", callback_data=f"{CB['notify']}notify_chats")],
                [InlineKeyboardButton(text=f"{ic(user['notify_bump'])} Бамп", callback_data=f"{CB['notify']}notify_bump")],
                [InlineKeyboardButton(text=f"{ic(user['notify_auth'])} Авторизация", callback_data=f"{CB['notify']}notify_auth")],
                [InlineKeyboardButton(text=f"{ic(user['notify_delivery'])} Выдача", callback_data=f"{CB['notify']}notify_delivery")],
                [InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])],
            ])
            await call.message.edit_text("🔔 <b>Уведомления</b>", parse_mode="HTML", reply_markup=kb)
            await call.answer()
            return
        new_val = await self.db.toggle_notify(uid, field)
        await call.answer("Вкл" if new_val else "Выкл")
        user = await self.db.get_user(uid)
        ic = lambda v: "🟢" if v else "🔴"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=f"{ic(user['notify_orders'])} Заказы", callback_data=f"{CB['notify']}notify_orders")],
            [InlineKeyboardButton(text=f"{ic(user['notify_chats'])} Чаты", callback_data=f"{CB['notify']}notify_chats")],
            [InlineKeyboardButton(text=f"{ic(user['notify_bump'])} Бамп", callback_data=f"{CB['notify']}notify_bump")],
            [InlineKeyboardButton(text=f"{ic(user['notify_auth'])} Авторизация", callback_data=f"{CB['notify']}notify_auth")],
            [InlineKeyboardButton(text=f"{ic(user['notify_delivery'])} Выдача", callback_data=f"{CB['notify']}notify_delivery")],
            [InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])],
        ])
        await call.message.edit_reply_markup(reply_markup=kb)

    def _plugins_view(self) -> tuple[str, InlineKeyboardMarkup]:
        metas = self.plugin_manager.plugins.values() if self.plugin_manager.plugins else self.plugin_manager.load_all()
        lines = ["🔌 <b>Плагины</b>\n"]
        buttons: list[list[InlineKeyboardButton]] = []
        for meta in metas:
            st = "🟢" if meta.enabled else "🔴"
            err = f"\n  ⚠️ {meta.load_error[:50]}" if meta.load_error else ""
            lines.append(f"{st} <b>{meta.name}</b> v{meta.version}{err}")
            buttons.append([InlineKeyboardButton(
                text=f"{'Выкл' if meta.enabled else 'Вкл'} {meta.name}",
                callback_data=f"{CB['plug']}{meta.uuid}",
            )])
        if not list(metas):
            lines.append("<i>Нет плагинов в plugins/</i>")
        buttons.append([InlineKeyboardButton(text="🔄 Обновить", callback_data=CB["plugins"])])
        buttons.append([InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])])
        return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)

    async def cb_plugins(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        self.plugin_manager.load_all()
        text, kb = self._plugins_view()
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    async def cb_plug_toggle(self, call: CallbackQuery) -> None:
        uuid = call.data.replace(CB["plug"], "")
        enabled = self.plugin_manager.toggle(uuid)
        await call.answer("Включён" if enabled else "Выключен")
        await self.cb_plugins(call)

    async def cb_adel(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        products = await self.db.list_autodelivery_products()
        lines = ["📦 <b>Автовыдача — склад</b>\n"]
        for name, cnt in products:
            lines.append(f"• <code>{name}</code> — {cnt} шт.")
        if not products:
            lines.append("<i>Пусто. Добавьте товары.</i>")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="➕ Добавить", callback_data=CB["adel_add"])],
            [InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])],
        ])
        await call.message.edit_text("\n".join(lines), parse_mode="HTML", reply_markup=kb)
        await call.answer()

    async def cb_adel_add(self, call: CallbackQuery, state: FSMContext) -> None:
        if not await self._has_access(call.from_user.id):
            return
        await state.set_state(SetupStates.adel_product)
        await call.message.answer("Введите название товара <b>точно как на Starvell</b>:", parse_mode="HTML")
        await call.answer()

    async def cb_gemini(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        s = load_settings()
        key_ok = "✅" if s.is_gemini_configured() else "❌"
        text = (
            f"🤖 <b>Gemini — ИИ-консультант</b>\n\n"
            f"Ключ: {key_ok}\n"
            f"Модель: {s.gemini_model}\n"
            f"В чатах: {self._flag(s.ai_replies_enabled)}\n\n"
            f"<i>{s.ai_system_prompt[:150]}…</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔑 API ключ", callback_data=CB["set_gemini"])],
            [InlineKeyboardButton(text="✅ Проверить", callback_data=CB["check_gemini"])],
            [InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])],
        ])
        await call.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        await call.answer()

    async def cb_edit_welcome(self, call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SetupStates.welcome)
        await call.message.answer("Введите текст приветствия:")
        await call.answer()

    async def cb_edit_bump(self, call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SetupStates.bump_interval)
        s = load_settings()
        await call.message.answer(f"Текущий интервал бампа: {int(s.bump_interval)} сек.\nВведите новый (мин. 300):")
        await call.answer()

    async def cb_edit_delivery(self, call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SetupStates.delivery_tpl)
        await call.message.answer(
            "Шаблон выдачи. Переменные: {product}, {content}\n"
            "Отправьте новый текст:"
        )
        await call.answer()

    # ── Ввод с проверкой ────────────────────────────────────────────────────

    async def on_session(self, message: Message, state: FSMContext) -> None:
        cookie = (message.text or "").strip()
        if not cookie or cookie.startswith("/"):
            return
        wait = await message.answer("⏳ Проверяю session…")
        ok, msg, info = await test_starvell_session(cookie)
        if not ok:
            await wait.edit_text(f"❌ {msg}\n\nПопробуйте ещё раз.")
            return

        user = info.get("user") or {}
        s = load_settings()
        s.session_cookie = cookie
        s.sid_cookie = str(info.get("sid") or "")
        s.my_games_cookie = str(info.get("my_games") or "")
        s.starvell_username = str(user.get("username") or "")
        s.accounts = [StarvellAccount(name="default", session_cookie=cookie, sid_cookie=s.sid_cookie, my_games_cookie=s.my_games_cookie)]
        save_settings(s)
        await self.automation.reload()
        await state.clear()
        await wait.edit_text(
            f"✅ Starvell привязан!\n<b>{msg}</b>\n\nАвтоматизация запущена.",
            parse_mode="HTML",
            reply_markup=self._main_kb(),
        )

    async def on_gemini(self, message: Message, state: FSMContext) -> None:
        key = (message.text or "").strip()
        if not key:
            return
        wait = await message.answer("⏳ Проверяю Gemini…")
        ok, msg = await test_gemini_key(key)
        if not ok:
            await wait.edit_text(f"❌ {msg}")
            return
        s = load_settings()
        s.gemini_api_key = key
        save_settings(s)
        await state.clear()
        await wait.edit_text(f"✅ Gemini настроен!\n{msg}", reply_markup=self._back_kb())

    async def on_welcome(self, message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        s = load_settings()
        s.welcome_text = text
        save_settings(s)
        await state.clear()
        await message.answer("✅ Текст приветствия сохранён.", reply_markup=self._back_kb())

    async def on_bump(self, message: Message, state: FSMContext) -> None:
        try:
            val = int((message.text or "").strip())
            val = max(300, val)
        except ValueError:
            await message.answer("❌ Введите число секунд")
            return
        s = load_settings()
        s.bump_interval = float(val)
        save_settings(s)
        await self.automation.reload()
        await state.clear()
        await message.answer(f"✅ Интервал бампа: {val} сек.", reply_markup=self._back_kb())

    async def on_delivery(self, message: Message, state: FSMContext) -> None:
        text = (message.text or "").strip()
        if not text:
            return
        s = load_settings()
        s.delivery_template = text
        save_settings(s)
        await state.clear()
        await message.answer("✅ Шаблон выдачи сохранён.", reply_markup=self._back_kb())

    async def on_adel_product(self, message: Message, state: FSMContext) -> None:
        product = (message.text or "").strip()
        if not product:
            return
        await state.update_data(adel_product=product)
        await state.set_state(SetupStates.adel_items)
        await message.answer(f"Товар: <b>{product}</b>\nКоды (каждый с новой строки):", parse_mode="HTML")

    async def on_adel_items(self, message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        product = data.get("adel_product", "")
        items = [x.strip() for x in (message.text or "").splitlines() if x.strip()]
        if not product or not items:
            await message.answer("❌ Пустой список")
            return
        added = await self.db.add_autodelivery_items(product, items)
        await state.clear()
        await message.answer(f"✅ +{added} шт. для «{product}»", reply_markup=self._back_kb())

    async def on_gemini_prompt(self, message: Message, state: FSMContext) -> None:
        pass  # reserved

    # ── Уведомления ───────────────────────────────────────────────────────

    async def broadcast(self, text: str, notify_type: str = "notify_orders") -> None:
        s = load_settings()
        recipients = set()
        if s.owner_id:
            recipients.add(s.owner_id)
        recipients.update(s.admin_ids)
        for uid in recipients:
            user = await self.db.get_user(uid)
            if not int(user.get(notify_type, 1)):
                continue
            try:
                await self.bot.send_message(uid, text, parse_mode="HTML")
            except Exception as exc:
                logger.warning("notify %s: %s", uid, exc)

    async def start_polling(self) -> None:
        await self.dp.start_polling(self.bot)

    async def stop(self) -> None:
        await self.bot.session.close()
