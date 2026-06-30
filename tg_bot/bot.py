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

from cardinal import Cardinal
from automation import AutomationEngine
from config import VERSION, Settings, StarvellAccount, load_settings, save_settings
from database import Database
from plugin_manager import PluginManager
from handlers.tg.profile_panel import build_profile_brief, build_status_text
from keyboards.main import premium_main_text
from tg_bot import cbt as CBT
from tg_bot import keyboards as KB
from utils.tools import check_github_update, create_backup, export_settings_snapshot, logs_zip, system_stats
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
    reply_chat = State()
    ar_command = State()
    ar_response = State()
    ban_user = State()
    watermark = State()
    order_confirm = State()


class TelegramBot:
    """Панель управления Starvell Cardinal."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        cardinal: Cardinal,
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
        self._register_handlers()
        # Hub (плагины FPC) — регистрируем ПЕРВЫМ, чтобы перехватывал sc:plug*
        try:
            from handlers.tg.hub import create_hub_router
            self.dp.include_router(create_hub_router(self))
        except Exception as exc:
            logger.warning("Premium UI hub: %s", exc)
        self.dp.include_router(self.router)

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
        r.message.register(self.cmd_help, Command("help"))
        r.message.register(self.cmd_status, Command("status"))
        r.message.register(self.cmd_restart, Command("restart"))
        r.message.register(self.cmd_profile, Command("profile"))
        r.message.register(self.cmd_session, Command("session"))
        r.message.register(self.cmd_plugins, Command("plugins"))
        r.message.register(self.cmd_backup, Command("backup"))
        r.message.register(self.cmd_upload, Command("upload"))
        r.message.register(self.cmd_export, Command("export"))
        r.message.register(self.cmd_about, Command("about"))
        r.message.register(self.cmd_sys, Command("sys"))
        r.message.register(self.cmd_logs, Command("logs"))
        r.message.register(self.cmd_golden_key, Command("golden_key"))
        r.message.register(self.cmd_change_cookie, Command("change_cookie"))
        r.message.register(self.cmd_ban, Command("ban"))
        r.message.register(self.cmd_unban, Command("unban"))
        r.message.register(self.cmd_black_list, Command("black_list"))
        r.message.register(self.cmd_watermark, Command("watermark"))
        r.message.register(self.cmd_check_updates, Command("check_updates"))
        r.message.register(self.cmd_update, Command("update"))
        r.message.register(self.cmd_create_backup, Command("create_backup"))
        r.message.register(self.cmd_get_backup, Command("get_backup"))
        r.message.register(self.on_reply_chat, SetupStates.reply_chat)
        r.message.register(self.on_ar_command, SetupStates.ar_command)
        r.message.register(self.on_ar_response, SetupStates.ar_response)
        r.message.register(self.on_ban_user, SetupStates.ban_user)
        r.message.register(self.on_watermark, SetupStates.watermark)
        r.message.register(self.on_order_confirm, SetupStates.order_confirm)

        r.callback_query.register(self.cb_main, F.data.in_({CB["main"], CBT.MAIN}))
        r.callback_query.register(self.cb_settings_menu, F.data == CBT.SETTINGS)
        r.callback_query.register(self.cb_settings_menu2, F.data == CBT.MAIN2)
        r.callback_query.register(self.cb_category, F.data.startswith(CBT.CATEGORY))
        r.callback_query.register(self.cb_switch, F.data.startswith(CBT.SWITCH))
        r.callback_query.register(self.cb_reply_chat, F.data.startswith(CBT.REPLY_CHAT))
        r.callback_query.register(self.cb_refund, F.data.startswith(CBT.REFUND_ORDER))
        r.callback_query.register(self.cb_refund_ok, F.data.startswith(CBT.REFUND_OK))
        r.callback_query.register(self.cb_tmpl_list, F.data == CBT.TMPLT_LIST)
        r.callback_query.register(self.cb_tmpl_use, F.data.startswith(CBT.TMPLT_USE))
        r.callback_query.register(self.cb_ar_add, F.data == CBT.AR_ADD)
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
        r.callback_query.register(self.cb_setup, F.data == CB["setup"])
        r.callback_query.register(self.cb_settings, F.data == CB["settings"])
        r.callback_query.register(self.cb_gemini, F.data == CB["gemini"])
        r.callback_query.register(self.cb_check_auth, F.data == CB["check_auth"])
        r.callback_query.register(self.cb_check_gemini, F.data == CB["check_gemini"])
        r.callback_query.register(self.cb_set_session, F.data == CB["set_session"])
        r.callback_query.register(self.cb_set_gemini, F.data == CB["set_gemini"])
        r.callback_query.register(self.cb_first_setup, F.data == CB["first_setup"])
        r.callback_query.register(self.cb_toggle, F.data.startswith(CB["toggle"]))
        r.callback_query.register(self.cb_notify, F.data.startswith(CB["notify"]))
        # Плагины — только через hub (handlers/tg/plugins_panel.py), без legacy sc:plug:
        r.callback_query.register(self.cb_adel_add, F.data == CB["adel_add"])
        r.callback_query.register(self.cb_edit_welcome, F.data == CB["edit_welcome"])
        r.callback_query.register(self.cb_edit_bump, F.data == CB["edit_bump"])
        r.callback_query.register(self.cb_edit_delivery, F.data == CB["edit_delivery"])

    # ── Клавиатуры ────────────────────────────────────────────────────────

    def _flag(self, on: bool) -> str:
        return "🟢" if on else "🔴"

    def _main_kb(self) -> InlineKeyboardMarkup:
        return KB.main_menu()

    def _back_kb(self) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="◀️ Меню", callback_data=CB["main"])],
        ])

    def _setup_kb(self) -> InlineKeyboardMarkup:
        return KB.setup_kb()

    async def _main_text(self) -> str:
        return premium_main_text(VERSION)

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
        text = await build_status_text(self)
        await message.answer(text, parse_mode="HTML", reply_markup=KB.back_menu())

    async def cmd_profile(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        text, kb = await build_profile_brief(self)
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

    async def cmd_session(self, message: Message, state: FSMContext) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await state.set_state(SetupStates.session)
        await message.answer(
            "🍪 <b>Session cookie Starvell</b>\n\n"
            "1. Войдите на starvell.com\n"
            "2. F12 → Application → Cookies → <code>session</code>\n"
            "3. Отправьте значение сюда\n\n"
            "Бот сразу проверит cookie.",
            parse_mode="HTML",
        )

    async def cmd_help(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        from handlers.tg.help_panel import HELP_TEXT
        await message.answer(HELP_TEXT, parse_mode="HTML", reply_markup=KB.back_menu())

    async def cmd_backup(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        path = create_backup()
        await message.answer_document(
            open(path, "rb"),
            caption="💾 Резервная копия config + storage",
        )

    async def cmd_upload(self, message: Message, state: FSMContext) -> None:
        if not await self._has_access(message.from_user.id):
            return
        from handlers.tg.backup_panel import BackupStates
        await state.set_state(BackupStates.waiting_file)
        await message.answer(
            "📥 Отправьте файл <b>.zip</b> с бэкапом:",
            parse_mode="HTML",
        )

    async def cmd_export(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        path = export_settings_snapshot()
        await message.answer_document(
            open(path, "rb"),
            caption="📋 Текущие настройки (секреты маскированы)",
        )

    async def cmd_plugins(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        from handlers.tg.plugins_panel import build_plugins_list
        text, kb = await build_plugins_list(self.plugin_manager, self.db)
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
        """Legacy — обрабатывается profile_panel."""
        pass

    async def _status_text(self) -> str:
        return await build_status_text(self)

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
        """Legacy — обрабатывается profile_panel."""
        pass

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

    async def cb_adel(self, call: CallbackQuery) -> None:
        await call.message.edit_text(
            "ℹ️ Раздел «Склад» удалён.\nИспользуйте 🔍 Парсер лотов в главном меню.",
            reply_markup=KB.back_menu(),
        )
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
        pass

    # ── FPC-команды ───────────────────────────────────────────────────────

    async def cmd_about(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await message.answer(f"🤖 <b>Starvell Cardinal</b> v{VERSION}\nАналог FunPay Cardinal для Starvell.", parse_mode="HTML")

    async def cmd_sys(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await message.answer(system_stats(), parse_mode="HTML")

    async def cmd_logs(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        path = logs_zip()
        if path and path.exists():
            await message.answer_document(open(path, "rb"))
        else:
            await message.answer("Логи пусты")

    async def cmd_golden_key(self, message: Message, state: FSMContext) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await state.set_state(SetupStates.session)
        await message.answer(
            "🍪 <b>Session cookie Starvell</b>\n\n"
            "1. Войдите на starvell.com\n"
            "2. F12 → Application → Cookies → <code>session</code>\n"
            "3. Скопируйте значение и отправьте сюда",
            parse_mode="HTML",
        )

    async def cmd_change_cookie(self, message: Message, state: FSMContext) -> None:
        await self.cmd_golden_key(message, state)

    async def cmd_ban(self, message: Message, state: FSMContext) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await state.set_state(SetupStates.ban_user)
        await message.answer("Введите username покупателя для бана:")

    async def cmd_unban(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await message.answer("Использование: /unban username")
            return
        n = await self.db.remove_blacklist(username=parts[1])
        await message.answer("✅ Удалён" if n else "Не найден")

    async def cmd_black_list(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        bl = await self.db.list_blacklist()
        if not bl:
            await message.answer("Чёрный список пуст")
            return
        lines = ["🚫 <b>Чёрный список</b>\n"] + [f"• {b.get('username') or b.get('starvell_user_id')}" for b in bl]
        await message.answer("\n".join(lines), parse_mode="HTML")

    async def cmd_watermark(self, message: Message, state: FSMContext) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await state.set_state(SetupStates.watermark)
        s = load_settings()
        await message.answer(f"Текущий: <code>{s.watermark_text}</code>\nОтправьте новый:", parse_mode="HTML")

    async def cmd_check_updates(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        ok, info = await check_github_update()
        await message.answer(f"{'✅' if ok else '❌'} {info}")

    async def cmd_update(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        await message.answer(
            "🔄 <b>Обновление Starvell Cardinal</b>\n\n"
            "На сервере выполните:\n"
            "<code>curl -fsSL https://raw.githubusercontent.com/asto7996-maker/fpc-plugins/cursor/fpc-parity-280c/update_starvell_cardinal.sh | sudo bash</code>\n\n"
            "Или из каталога установки:\n"
            "<code>sudo bash update_starvell_cardinal.sh</code>",
            parse_mode="HTML",
        )

    async def cmd_create_backup(self, message: Message) -> None:
        if not await self._has_access(message.from_user.id):
            return
        path = create_backup()
        await message.answer_document(open(path, "rb"))

    async def cmd_get_backup(self, message: Message) -> None:
        await self.cmd_create_backup(message)

    async def cb_settings_menu(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        await call.message.edit_text("⚙️ <b>Настройки</b> (стр. 1)", parse_mode="HTML", reply_markup=KB.settings_page1())
        await call.answer()

    async def cb_settings_menu2(self, call: CallbackQuery) -> None:
        if not await self._has_access(call.from_user.id):
            return
        await call.message.edit_text("⚙️ <b>Настройки</b> (стр. 2)", parse_mode="HTML", reply_markup=KB.settings_page2())
        await call.answer()

    async def cb_category(self, call: CallbackQuery, state: FSMContext) -> None:
        if not await self._has_access(call.from_user.id):
            return
        cat = call.data.replace(CBT.CATEGORY, "")
        s = load_settings()
        if cat == "main":
            await call.message.edit_text("🌐 Глобальные переключатели", reply_markup=KB.category_main(s))
        elif cat == "tg":
            from handlers.tg.notifications import _render_notify
            await _render_notify(call, self)
            return
        elif cat == "ar":
            from handlers.tg.autoresponder import _render_ar_page
            await _render_ar_page(call, self, page=1)
            return
        elif cat == "ad":
            await call.message.edit_text(
                "ℹ️ Раздел «Склад» удалён.\n"
                "Используйте <b>🔍 Парсер лотов</b> для копирования товаров с FunPay.",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔍 Парсер", callback_data=CBT.PARSER)],
                    [InlineKeyboardButton(text="◀️", callback_data=CBT.MAIN2)],
                ]),
            )
        elif cat == "gr":
            await call.message.edit_text(
                f"👋 Приветствие\n\n{s.welcome_text[:200]}…",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="✏️ Текст", callback_data=CB["edit_welcome"])],
                    [InlineKeyboardButton(text="◀️", callback_data=CBT.MAIN2)],
                ]),
            )
        elif cat == "oc":
            await state.set_state(SetupStates.order_confirm)
            await call.message.answer(f"Текущий текст:\n{s.order_confirm_text}\n\nОтправьте новый:")
        elif cat == "rr":
            lines = [f"{k}⭐: {v[:40]}…" for k, v in s.review_replies.items()]
            await call.message.edit_text("⭐ Отзывы\n" + "\n".join(lines), reply_markup=KB.back_menu())
        elif cat == "bl":
            bl = await self.db.list_blacklist()
            text = "🚫 <b>Чёрный список</b>\n" + (
                "\n".join(f"• {b.get('username') or b.get('starvell_user_id')}" for b in bl) or "пусто"
            )
            await call.message.edit_text(text, parse_mode="HTML", reply_markup=KB.back_menu())
        await call.answer()

    async def cb_switch(self, call: CallbackQuery) -> None:
        key = call.data.replace(CBT.SWITCH, "")
        call.data = f"{CB['toggle']}{key}"
        await self.cb_toggle(call)

    async def cb_reply_chat(self, call: CallbackQuery, state: FSMContext) -> None:
        chat_id = call.data.replace(CBT.REPLY_CHAT, "")
        await state.set_state(SetupStates.reply_chat)
        await state.update_data(reply_chat_id=chat_id)
        await call.message.answer(f"💬 Ответ в чат <code>{chat_id}</code>:", parse_mode="HTML")
        await call.answer()

    async def on_reply_chat(self, message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        chat_id = data.get("reply_chat_id")
        text = (message.text or "").strip()
        if not chat_id or not text:
            return
        api = self.cardinal.get_api()
        if api:
            s = load_settings()
            text = api.apply_watermark(text, s.watermark_on, s.watermark_text)
            await api.send_message(chat_id, text)
            await message.answer("✅ Отправлено в Starvell")
        await state.clear()

    async def cb_refund(self, call: CallbackQuery) -> None:
        order_id = call.data.replace(CBT.REFUND_ORDER, "")
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да", callback_data=f"{CBT.REFUND_OK}{order_id}")],
            [InlineKeyboardButton(text="❌ Нет", callback_data=CBT.MAIN)],
        ])
        await call.message.answer(f"Возврат по заказу #{order_id}?", reply_markup=kb)
        await call.answer()

    async def cb_refund_ok(self, call: CallbackQuery) -> None:
        order_id = call.data.replace(CBT.REFUND_OK, "")
        api = self.cardinal.get_api()
        if api:
            try:
                await api.refund_order(order_id)
                await call.message.answer(f"✅ Возврат #{order_id} выполнен")
            except Exception as exc:
                await call.message.answer(f"❌ {exc}")
        await call.answer()

    async def cb_tmpl_list(self, call: CallbackQuery) -> None:
        tpls = await self.db.list_templates()
        buttons = [[InlineKeyboardButton(text=t.get("title") or t["content"][:30], callback_data=f"{CBT.TMPLT_USE}{t['id']}")] for t in tpls]
        buttons.append([InlineKeyboardButton(text="◀️", callback_data=CBT.MAIN)])
        await call.message.edit_text("📝 Шаблоны", reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons or [[InlineKeyboardButton(text="◀️", callback_data=CBT.MAIN)]]))
        await call.answer()

    async def cb_tmpl_use(self, call: CallbackQuery, state: FSMContext) -> None:
        tpl_id = int(call.data.replace(CBT.TMPLT_USE, ""))
        tpl = await self.db.get_template(tpl_id)
        if tpl:
            await call.message.answer(tpl["content"])
        await call.answer()

    async def cb_ar_add(self, call: CallbackQuery, state: FSMContext) -> None:
        await state.set_state(SetupStates.ar_command)
        await call.message.answer("Введите команду (ключевое слово):")
        await call.answer()

    async def on_ar_command(self, message: Message, state: FSMContext) -> None:
        cmd = (message.text or "").strip().lower()
        await state.update_data(ar_cmd=cmd)
        await state.set_state(SetupStates.ar_response)
        await message.answer("Ответ на команду:")

    async def on_ar_response(self, message: Message, state: FSMContext) -> None:
        data = await state.get_data()
        await self.db.add_ar_command(data.get("ar_cmd", ""), message.text or "")
        await state.clear()
        await message.answer("✅ Команда добавлена")

    async def on_ban_user(self, message: Message, state: FSMContext) -> None:
        await self.db.add_blacklist(username=(message.text or "").strip())
        await state.clear()
        await message.answer("✅ В чёрный список добавлен")

    async def on_watermark(self, message: Message, state: FSMContext) -> None:
        s = load_settings()
        s.watermark_text = (message.text or "").strip()
        s.watermark_on = True
        save_settings(s)
        await state.clear()
        await message.answer("✅ Водяной знак обновлён")

    async def on_order_confirm(self, message: Message, state: FSMContext) -> None:
        s = load_settings()
        s.order_confirm_text = (message.text or "").strip()
        save_settings(s)
        await state.clear()
        await message.answer("✅ Текст подтверждения сохранён")

    async def notify_order(self, text: str, order_id: str, chat_id: str = "") -> None:
        s = load_settings()
        for uid in {s.owner_id, *s.admin_ids}:
            if not uid:
                continue
            user = await self.db.get_user(uid)
            if not int(user.get("notify_orders", 1)):
                continue
            try:
                await self.bot.send_message(uid, text, parse_mode="HTML", reply_markup=KB.order_actions(order_id, chat_id))
            except Exception:
                pass

    async def notify_chat(self, text: str, chat_id: str) -> None:
        s = load_settings()
        for uid in {s.owner_id, *s.admin_ids}:
            if not uid:
                continue
            user = await self.db.get_user(uid)
            if not int(user.get("notify_chats", 1)):
                continue
            try:
                await self.bot.send_message(uid, text, parse_mode="HTML", reply_markup=KB.chat_actions(chat_id))
            except Exception:
                pass

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
        commands = [
            {"command": "start", "description": "Главное меню"},
            {"command": "menu", "description": "Главное меню"},
            {"command": "profile", "description": "Профиль и баланс"},
            {"command": "status", "description": "Статистика"},
            {"command": "session", "description": "Привязать Starvell"},
            {"command": "plugins", "description": "Плагины"},
            {"command": "backup", "description": "Создать бэкап"},
            {"command": "upload", "description": "Загрузить бэкап"},
            {"command": "export", "description": "Текущие данные"},
            {"command": "parser", "description": "Парсер FunPay"},
            {"command": "restart", "description": "Перезапуск бота"},
            {"command": "help", "description": "Справка"},
        ]
        try:
            from aiogram.types import BotCommand
            await self.bot.set_my_commands([BotCommand(**c) for c in commands])
        except Exception as exc:
            logger.warning("set_my_commands: %s", exc)
        await self.dp.start_polling(self.bot)

    async def stop(self) -> None:
        await self.bot.session.close()
