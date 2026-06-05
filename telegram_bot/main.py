"""
Main entry point for the Telegram Content & Marketing Bot.

Admin bot built with python-telegram-bot v20+ (async).
User-level operations (content machine, parser, inviter) use Telethon.

Architecture:
  ┌──────────────────┐
  │  Admin Bot       │  ← python-telegram-bot (Bot API)
  │  (this module)   │
  └────────┬─────────┘
           │ controls
  ┌────────▼─────────────────────────────────────────────┐
  │  ContentMachine  │  UserParser  │  Inviter            │
  └──────────────────┴──────────────┴─────────────────────┘
           │                │               │
           └────────────────┴───────────────┘
                         Telethon client(s)
"""

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from telethon import TelegramClient
from telethon.sessions import StringSession

from config import AppConfig, config
from database import Database
from content_machine import ContentMachine
from parser import UserParser
from inviter import AccountManager, Inviter
from utils import (
    format_uptime,
    load_proxies_from_file,
    normalise_channel_ref,
    setup_logging,
    truncate,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conversation states
# ---------------------------------------------------------------------------

(
    STATE_IDLE,
    STATE_AWAIT_DONOR_ADD,
    STATE_AWAIT_DONOR_DEL,
    STATE_AWAIT_SOURCE_ADD,
    STATE_AWAIT_SOURCE_DEL,
    STATE_AWAIT_TARGET_CHANNEL,
    STATE_AWAIT_INVITE_TARGET,
    STATE_AWAIT_AD_TEXT,
    STATE_AWAIT_DM_MESSAGE,
    STATE_AWAIT_PROXY_LINES,
    STATE_AWAIT_INVITE_DELAY,
) = range(11)


# ---------------------------------------------------------------------------
# Admin guard decorator
# ---------------------------------------------------------------------------

def admin_only(handler):
    """Decorator that restricts a handler to admin users."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id if update.effective_user else None
        if user_id not in config.telegram.admin_ids:
            if update.message:
                await update.message.reply_text("Access denied.")
            elif update.callback_query:
                await update.callback_query.answer("Access denied.", show_alert=True)
            return ConversationHandler.END
        return await handler(update, context)
    wrapper.__name__ = handler.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Keyboard builders
# ---------------------------------------------------------------------------

def kb_main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 Статистика", callback_data="stats"),
            InlineKeyboardButton("⚙️ Настройки", callback_data="settings"),
        ],
        [
            InlineKeyboardButton("🔄 Контент-машина", callback_data="cm_menu"),
            InlineKeyboardButton("🔍 Парсер", callback_data="parser_menu"),
        ],
        [
            InlineKeyboardButton("📨 Инвайтер/DM", callback_data="inv_menu"),
            InlineKeyboardButton("👥 Аккаунты", callback_data="accounts_menu"),
        ],
        [
            InlineKeyboardButton("📝 Лог событий", callback_data="events"),
        ],
    ])


def kb_back(target: str = "main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=target)]])


def kb_cm_menu(running: bool) -> InlineKeyboardMarkup:
    toggle_label = "⏹ Остановить" if running else "▶️ Запустить"
    toggle_data = "cm_stop" if running else "cm_start"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data=toggle_data)],
        [InlineKeyboardButton("➕ Добавить донора", callback_data="cm_add_donor")],
        [InlineKeyboardButton("➖ Удалить донора", callback_data="cm_del_donor")],
        [InlineKeyboardButton("📋 Список доноров", callback_data="cm_donors_list")],
        [InlineKeyboardButton("🎯 Установить целевой канал", callback_data="cm_set_target")],
        [InlineKeyboardButton("📣 Установить рекламный текст", callback_data="cm_set_ad")],
        [InlineKeyboardButton("🔃 Проверить сейчас", callback_data="cm_force_check")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main")],
    ])


def kb_parser_menu(running: bool) -> InlineKeyboardMarkup:
    toggle_label = "⏹ Остановить" if running else "▶️ Запустить"
    toggle_data = "parser_stop" if running else "parser_start"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(toggle_label, callback_data=toggle_data)],
        [InlineKeyboardButton("➕ Добавить группу", callback_data="parser_add_group")],
        [InlineKeyboardButton("➖ Удалить группу", callback_data="parser_del_group")],
        [InlineKeyboardButton("📋 Список групп", callback_data="parser_groups_list")],
        [InlineKeyboardButton("🔃 Запустить парсинг", callback_data="parser_run_once")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main")],
    ])


def kb_inv_menu(running: bool) -> InlineKeyboardMarkup:
    toggle_label = "⏹ Остановить" if running else "▶️ Запустить"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(toggle_label + " (invite)",
                                  callback_data="inv_stop" if running else "inv_start_invite"),
            InlineKeyboardButton(toggle_label + " (DM)",
                                  callback_data="inv_stop" if running else "inv_start_dm"),
        ],
        [InlineKeyboardButton("🎯 Целевой канал/группа", callback_data="inv_set_target")],
        [InlineKeyboardButton("✉️ Текст DM", callback_data="inv_set_dm")],
        [InlineKeyboardButton("⏱ Задержка", callback_data="inv_set_delay")],
        [InlineKeyboardButton("▶️ Разослать сейчас (invite, 10)", callback_data="inv_run_invite")],
        [InlineKeyboardButton("▶️ Разослать сейчас (DM, 10)", callback_data="inv_run_dm")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main")],
    ])


def kb_accounts_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 Перезагрузить сессии", callback_data="acc_reload")],
        [InlineKeyboardButton("📋 Список аккаунтов", callback_data="acc_list")],
        [InlineKeyboardButton("🌐 Загрузить прокси", callback_data="acc_add_proxies")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main")],
    ])


def kb_settings_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📣 Рекламный текст", callback_data="cm_set_ad")],
        [InlineKeyboardButton("🔗 Кнопка (URL)", callback_data="settings_ad_button")],
        [InlineKeyboardButton("✉️ Текст DM", callback_data="inv_set_dm")],
        [InlineKeyboardButton("◀️ Назад", callback_data="main")],
    ])


# ---------------------------------------------------------------------------
# Bot Application
# ---------------------------------------------------------------------------

class BotApp:
    """
    Orchestrates the admin Telegram bot and all background services.
    """

    def __init__(self, cfg: AppConfig):
        self.cfg = cfg
        self.db = Database(cfg.database.path)
        self.start_time = datetime.now()

        # Telethon client for content machine and parser (main account)
        self._telethon_client: Optional[TelegramClient] = None

        # Service instances (created after DB init)
        self.content_machine: Optional[ContentMachine] = None
        self.parser: Optional[UserParser] = None
        self.account_manager: Optional[AccountManager] = None
        self.inviter: Optional[Inviter] = None

        # python-telegram-bot application
        self.app: Optional[Application] = None

        # Conversation state per user
        self._conv_state: dict = {}
        self._conv_data: dict = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialise(self) -> None:
        """Set up DB, Telethon client, and all services."""
        self.cfg.ensure_dirs()
        await self.db.init()

        # Import proxies from file into DB
        proxy_path = self.cfg.accounts.proxy_file
        if Path(proxy_path).exists():
            proxies = load_proxies_from_file(proxy_path)
            for p in proxies:
                await self.db.add_proxy(
                    proxy_type=p["type"],
                    host=p["host"],
                    port=p["port"],
                    username=p.get("username"),
                    password=p.get("password"),
                )
            logger.info("Loaded %d proxies from %s", len(proxies), proxy_path)

        # Build the main Telethon client (used for content machine + parser)
        await self._init_telethon()

        # Build services
        self.content_machine = ContentMachine(self._telethon_client, self.db, self.cfg)
        self.parser = UserParser(self._telethon_client, self.db, self.cfg)
        self.account_manager = AccountManager(self.db, self.cfg)
        await self.account_manager.load()
        self.inviter = Inviter(self.account_manager, self.db, self.cfg)

        logger.info("BotApp initialised")

    async def _init_telethon(self) -> None:
        """Create and connect the main Telethon user client."""
        sessions_dir = Path(self.cfg.accounts.sessions_dir)
        sessions_dir.mkdir(parents=True, exist_ok=True)
        session_path = str(sessions_dir / "main_client")

        self._telethon_client = TelegramClient(
            session=session_path,
            api_id=self.cfg.telegram.api_id,
            api_hash=self.cfg.telegram.api_hash,
            connection_retries=5,
            retry_delay=3,
        )
        await self._telethon_client.connect()
        if await self._telethon_client.is_user_authorized():
            me = await self._telethon_client.get_me()
            logger.info("Main Telethon client connected as @%s", getattr(me, "username", "?"))
        else:
            logger.warning(
                "Main Telethon client is NOT authorised. "
                "Run the interactive login helper: python login_helper.py"
            )

    async def run(self) -> None:
        """Build and run the admin bot."""
        self.app = (
            Application.builder()
            .token(self.cfg.telegram.bot_token)
            .build()
        )

        self._register_handlers()

        await self.app.bot.set_my_commands([
            BotCommand("start", "Главное меню"),
            BotCommand("status", "Статус сервисов"),
            BotCommand("stats", "Статистика"),
            BotCommand("help", "Помощь"),
            BotCommand("stop_all", "Остановить все сервисы"),
        ])

        # Schedule midnight daily reset
        self.app.job_queue.run_daily(
            self._midnight_reset,
            time=datetime.strptime("00:01", "%H:%M").time(),
        )

        logger.info("Starting admin bot...")
        async with self.app:
            await self.app.start()
            await self.app.updater.start_polling(
                drop_pending_updates=True,
                allowed_updates=Update.ALL_TYPES,
            )
            # Notify admins that the bot started
            for admin_id in self.cfg.telegram.admin_ids:
                try:
                    await self.app.bot.send_message(
                        chat_id=admin_id,
                        text=(
                            "Bot started successfully!\n"
                            "Use /start to open the control panel."
                        ),
                    )
                except TelegramError:
                    pass

            # Keep running until stopped
            await self._wait_for_stop()

            await self.app.updater.stop()
            await self.app.stop()

        await self._shutdown()

    async def _wait_for_stop(self) -> None:
        """Block until a SIGINT/SIGTERM or manual stop is issued."""
        stop_event = asyncio.Event()

        def _sig_handler(*_):
            logger.info("Shutdown signal received")
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _sig_handler)
            except NotImplementedError:
                # Windows
                signal.signal(sig, _sig_handler)

        await stop_event.wait()

    async def _shutdown(self) -> None:
        """Gracefully stop all services."""
        logger.info("Shutting down...")
        if self.content_machine:
            await self.content_machine.stop()
        if self.parser:
            await self.parser.stop()
        if self.inviter:
            await self.inviter.stop()
        if self.account_manager:
            await self.account_manager.disconnect_all()
        if self._telethon_client:
            await self._telethon_client.disconnect()
        await self.db.close()
        logger.info("Shutdown complete")

    # ------------------------------------------------------------------
    # Handler registration
    # ------------------------------------------------------------------

    def _register_handlers(self) -> None:
        app = self.app

        # Commands
        app.add_handler(CommandHandler("start", self.cmd_start))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("stats", self.cmd_stats))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("stop_all", self.cmd_stop_all))

        # Callback queries (inline keyboard)
        app.add_handler(CallbackQueryHandler(self.cb_router))

        # Text message handler (for conversation states)
        app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.msg_text_handler)
        )

        # Error handler
        app.add_error_handler(self.error_handler)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    @admin_only
    async def cmd_start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        self._conv_state[user.id] = STATE_IDLE
        text = (
            f"Добро пожаловать, {user.first_name}!\n\n"
            f"Uptime: {format_uptime(self.start_time)}\n"
            f"Выберите раздел:"
        )
        await update.message.reply_text(text, reply_markup=kb_main_menu())

    @admin_only
    async def cmd_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = await self._build_status_text()
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        stats = await self.db.get_stats()
        text = self._format_stats(stats)
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = (
            "<b>Команды:</b>\n"
            "/start — Главное меню\n"
            "/status — Статус всех сервисов\n"
            "/stats — Статистика (парсинг, рассылка, репосты)\n"
            "/stop_all — Экстренная остановка всех сервисов\n"
            "/help — Эта справка\n\n"
            "<b>Модули:</b>\n"
            "• <b>Контент-машина</b> — мониторинг каналов-доноров и автопостинг\n"
            "• <b>Парсер</b> — сбор активных пользователей из групп\n"
            "• <b>Инвайтер/DM</b> — приглашения в канал или рассылка в ЛС\n"
            "• <b>Аккаунты</b> — управление пулом Telethon-сессий\n"
        )
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

    @admin_only
    async def cmd_stop_all(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await self.content_machine.stop()
        await self.parser.stop()
        await self.inviter.stop()
        await update.message.reply_text(
            "Все сервисы остановлены.",
            reply_markup=kb_main_menu(),
        )

    # ------------------------------------------------------------------
    # Callback router
    # ------------------------------------------------------------------

    @admin_only
    async def cb_router(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        data = query.data

        # Main menu
        if data == "main":
            self._conv_state[update.effective_user.id] = STATE_IDLE
            await query.edit_message_text(
                "Главное меню:", reply_markup=kb_main_menu()
            )

        elif data == "stats":
            await self._cb_stats(query)

        elif data == "settings":
            await query.edit_message_text(
                "⚙️ Настройки:", reply_markup=kb_settings_menu()
            )

        elif data == "events":
            await self._cb_events(query)

        # --- Content Machine callbacks ---
        elif data == "cm_menu":
            status = self.content_machine.get_status()
            text = self._format_cm_status(status)
            await query.edit_message_text(
                text, reply_markup=kb_cm_menu(status["running"]),
                parse_mode=ParseMode.HTML,
            )

        elif data == "cm_start":
            await self.content_machine.start()
            await query.edit_message_text(
                "Контент-машина запущена.",
                reply_markup=kb_cm_menu(True),
            )

        elif data == "cm_stop":
            await self.content_machine.stop()
            await query.edit_message_text(
                "Контент-машина остановлена.",
                reply_markup=kb_cm_menu(False),
            )

        elif data == "cm_add_donor":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_DONOR_ADD
            await query.edit_message_text(
                "Введите username или ссылку на канал-донор\n"
                "(например: @channel или https://t.me/channel):",
                reply_markup=kb_back("cm_menu"),
            )

        elif data == "cm_del_donor":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_DONOR_DEL
            await query.edit_message_text(
                "Введите username канала-донора для удаления:",
                reply_markup=kb_back("cm_menu"),
            )

        elif data == "cm_donors_list":
            await self._cb_cm_donors_list(query)

        elif data == "cm_set_target":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_TARGET_CHANNEL
            await query.edit_message_text(
                "Введите username или ссылку на целевой канал\n"
                "(куда публиковать репосты):",
                reply_markup=kb_back("cm_menu"),
            )

        elif data == "cm_set_ad":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_AD_TEXT
            current = self.cfg.content_machine.ad_text or "(не задан)"
            await query.edit_message_text(
                f"Текущий рекламный текст:\n<code>{current}</code>\n\n"
                "Введите новый рекламный текст (или /skip чтобы оставить прежний):",
                reply_markup=kb_back("cm_menu"),
                parse_mode=ParseMode.HTML,
            )

        elif data == "cm_force_check":
            await query.edit_message_text("Запускаю принудительную проверку доноров...")
            count = await self.content_machine.force_check()
            status = self.content_machine.get_status()
            await query.edit_message_text(
                f"Принудительная проверка завершена. Опубликовано постов: {count}",
                reply_markup=kb_cm_menu(status["running"]),
            )

        # --- Parser callbacks ---
        elif data == "parser_menu":
            status = self.parser.get_status()
            text = self._format_parser_status(status)
            await query.edit_message_text(
                text, reply_markup=kb_parser_menu(status["running"]),
                parse_mode=ParseMode.HTML,
            )

        elif data == "parser_start":
            await self.parser.start()
            await query.edit_message_text(
                "Парсер запущен.",
                reply_markup=kb_parser_menu(True),
            )

        elif data == "parser_stop":
            await self.parser.stop()
            await query.edit_message_text(
                "Парсер остановлен.",
                reply_markup=kb_parser_menu(False),
            )

        elif data == "parser_add_group":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_SOURCE_ADD
            await query.edit_message_text(
                "Введите username или ссылку на группу/чат\n"
                "откуда собирать пользователей:",
                reply_markup=kb_back("parser_menu"),
            )

        elif data == "parser_del_group":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_SOURCE_DEL
            await query.edit_message_text(
                "Введите username группы для удаления:",
                reply_markup=kb_back("parser_menu"),
            )

        elif data == "parser_groups_list":
            await self._cb_parser_groups_list(query)

        elif data == "parser_run_once":
            await query.edit_message_text("Запускаю парсинг...")
            count = await self.parser.run_once()
            status = self.parser.get_status()
            await query.edit_message_text(
                f"Парсинг завершён. Новых пользователей: {count}",
                reply_markup=kb_parser_menu(status["running"]),
            )

        # --- Inviter callbacks ---
        elif data == "inv_menu":
            status = self.inviter.get_status()
            text = self._format_inv_status(status)
            await query.edit_message_text(
                text, reply_markup=kb_inv_menu(status["running"]),
                parse_mode=ParseMode.HTML,
            )

        elif data == "inv_start_invite":
            await self.inviter.start(mode="invite")
            await query.edit_message_text(
                "Инвайтер запущен (режим: invite).",
                reply_markup=kb_inv_menu(True),
            )

        elif data == "inv_start_dm":
            await self.inviter.start(mode="dm")
            await query.edit_message_text(
                "Инвайтер запущен (режим: DM).",
                reply_markup=kb_inv_menu(True),
            )

        elif data == "inv_stop":
            await self.inviter.stop()
            await query.edit_message_text(
                "Инвайтер остановлен.",
                reply_markup=kb_inv_menu(False),
            )

        elif data == "inv_set_target":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_INVITE_TARGET
            await query.edit_message_text(
                "Введите username канала/группы для инвайта:",
                reply_markup=kb_back("inv_menu"),
            )

        elif data == "inv_set_dm":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_DM_MESSAGE
            current = self.cfg.inviter.dm_message or "(не задан)"
            await query.edit_message_text(
                f"Текущий текст DM:\n<code>{truncate(current, 300)}</code>\n\n"
                "Введите новый текст (поддерживается {first_name}, {link}):",
                reply_markup=kb_back("inv_menu"),
                parse_mode=ParseMode.HTML,
            )

        elif data == "inv_set_delay":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_INVITE_DELAY
            await query.edit_message_text(
                f"Текущая задержка: {self.cfg.inviter.invite_delay}с\n"
                "Введите новую задержку в секундах (целое число):",
                reply_markup=kb_back("inv_menu"),
            )

        elif data == "inv_run_invite":
            await query.edit_message_text("Рассылаю приглашения (10 пользователей)...")
            count = await self.inviter.run_once("invite", limit=10)
            await query.edit_message_text(
                f"Разослано приглашений: {count}",
                reply_markup=kb_inv_menu(self.inviter.is_running()),
            )

        elif data == "inv_run_dm":
            await query.edit_message_text("Рассылаю DM (10 пользователей)...")
            count = await self.inviter.run_once("dm", limit=10)
            await query.edit_message_text(
                f"Отправлено DM: {count}",
                reply_markup=kb_inv_menu(self.inviter.is_running()),
            )

        # --- Accounts callbacks ---
        elif data == "accounts_menu":
            await query.edit_message_text(
                self._format_accounts_status(),
                reply_markup=kb_accounts_menu(),
                parse_mode=ParseMode.HTML,
            )

        elif data == "acc_reload":
            await self.account_manager.disconnect_all()
            self.account_manager = AccountManager(self.db, self.cfg)
            count = await self.account_manager.load()
            self.inviter.account_manager = self.account_manager
            await query.edit_message_text(
                f"Сессии перезагружены. Подключено: {count}",
                reply_markup=kb_accounts_menu(),
            )

        elif data == "acc_list":
            await self._cb_accounts_list(query)

        elif data == "acc_add_proxies":
            self._conv_state[update.effective_user.id] = STATE_AWAIT_PROXY_LINES
            await query.edit_message_text(
                "Отправьте список прокси в формате:\n"
                "<code>socks5://user:pass@host:port</code>\n"
                "По одному на строку:",
                reply_markup=kb_back("accounts_menu"),
                parse_mode=ParseMode.HTML,
            )

    # ------------------------------------------------------------------
    # Text message handler (conversation inputs)
    # ------------------------------------------------------------------

    @admin_only
    async def msg_text_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        state = self._conv_state.get(user_id, STATE_IDLE)
        text = update.message.text.strip()

        if state == STATE_IDLE:
            return

        # -------- Content Machine states --------
        if state == STATE_AWAIT_DONOR_ADD:
            username = normalise_channel_ref(text)
            await self.content_machine.add_donor(username)
            self._conv_state[user_id] = STATE_IDLE
            await update.message.reply_text(
                f"Донор добавлен: @{username}",
                reply_markup=kb_cm_menu(self.content_machine.is_running()),
            )

        elif state == STATE_AWAIT_DONOR_DEL:
            username = normalise_channel_ref(text)
            await self.content_machine.remove_donor(username)
            self._conv_state[user_id] = STATE_IDLE
            await update.message.reply_text(
                f"Донор удалён: @{username}",
                reply_markup=kb_cm_menu(self.content_machine.is_running()),
            )

        elif state == STATE_AWAIT_TARGET_CHANNEL:
            username = normalise_channel_ref(text)
            self.cfg.content_machine.target_channel = username
            await self.db.set_setting("cm_target_channel", username)
            self._conv_state[user_id] = STATE_IDLE
            await update.message.reply_text(
                f"Целевой канал установлен: @{username}",
                reply_markup=kb_cm_menu(self.content_machine.is_running()),
            )

        elif state == STATE_AWAIT_AD_TEXT:
            if text != "/skip":
                self.cfg.content_machine.ad_text = text
                await self.db.set_setting("cm_ad_text", text)
            self._conv_state[user_id] = STATE_IDLE
            await update.message.reply_text(
                "Рекламный текст сохранён.",
                reply_markup=kb_cm_menu(self.content_machine.is_running()),
            )

        # -------- Parser states --------
        elif state == STATE_AWAIT_SOURCE_ADD:
            username = normalise_channel_ref(text)
            await self.parser.add_source_group(username)
            self._conv_state[user_id] = STATE_IDLE
            await update.message.reply_text(
                f"Группа добавлена: @{username}",
                reply_markup=kb_parser_menu(self.parser.is_running()),
            )

        elif state == STATE_AWAIT_SOURCE_DEL:
            username = normalise_channel_ref(text)
            await self.parser.remove_source_group(username)
            self._conv_state[user_id] = STATE_IDLE
            await update.message.reply_text(
                f"Группа удалена: @{username}",
                reply_markup=kb_parser_menu(self.parser.is_running()),
            )

        # -------- Inviter states --------
        elif state == STATE_AWAIT_INVITE_TARGET:
            username = normalise_channel_ref(text)
            self.cfg.inviter.invite_target = username
            await self.db.set_setting("inv_target", username)
            self._conv_state[user_id] = STATE_IDLE
            await update.message.reply_text(
                f"Целевой канал для инвайта: @{username}",
                reply_markup=kb_inv_menu(self.inviter.is_running()),
            )

        elif state == STATE_AWAIT_DM_MESSAGE:
            if text != "/skip":
                self.cfg.inviter.dm_message = text
                await self.db.set_setting("inv_dm_message", text)
            self._conv_state[user_id] = STATE_IDLE
            await update.message.reply_text(
                "Текст DM сохранён.",
                reply_markup=kb_inv_menu(self.inviter.is_running()),
            )

        elif state == STATE_AWAIT_INVITE_DELAY:
            try:
                delay = float(text)
                self.cfg.inviter.invite_delay = delay
                await self.db.set_setting("inv_delay", delay)
                await update.message.reply_text(
                    f"Задержка установлена: {delay}с",
                    reply_markup=kb_inv_menu(self.inviter.is_running()),
                )
            except ValueError:
                await update.message.reply_text("Введите число (например: 45)")
                return
            self._conv_state[user_id] = STATE_IDLE

        # -------- Accounts: proxy input --------
        elif state == STATE_AWAIT_PROXY_LINES:
            lines = text.splitlines()
            added = 0
            for line in lines:
                from utils import parse_proxy_line
                proxy = parse_proxy_line(line)
                if proxy:
                    await self.db.add_proxy(
                        proxy_type=proxy["type"],
                        host=proxy["host"],
                        port=proxy["port"],
                        username=proxy.get("username"),
                        password=proxy.get("password"),
                    )
                    added += 1
            self._conv_state[user_id] = STATE_IDLE
            await update.message.reply_text(
                f"Добавлено прокси: {added} из {len(lines)}",
                reply_markup=kb_accounts_menu(),
            )

    # ------------------------------------------------------------------
    # Callback implementations
    # ------------------------------------------------------------------

    async def _cb_stats(self, query) -> None:
        stats = await self.db.get_stats()
        text = self._format_stats(stats)
        await query.edit_message_text(
            text, reply_markup=kb_back("main"), parse_mode=ParseMode.HTML
        )

    async def _cb_events(self, query) -> None:
        events = await self.db.get_recent_events(limit=20)
        if not events:
            text = "Лог пуст."
        else:
            lines = []
            for ev in events:
                ts = ev["logged_at"][:16]
                lines.append(f"[{ts}] <b>{ev['level']}</b> {ev['module']}: {truncate(ev['message'], 80)}")
            text = "\n".join(lines)
        await query.edit_message_text(
            f"<b>Последние события:</b>\n\n{text}",
            reply_markup=kb_back("main"),
            parse_mode=ParseMode.HTML,
        )

    async def _cb_cm_donors_list(self, query) -> None:
        donors = await self.content_machine.get_donor_list()
        if not donors:
            text = "Нет активных доноров."
        else:
            lines = []
            for d in donors:
                status = "✅" if d["is_accessible"] else "❌"
                lines.append(
                    f"{status} @{d['username']} | посты сегодня: {d['posts_today']} | "
                    f"ошибок: {d['consecutive_errors']}"
                )
            text = "<b>Каналы-доноры:</b>\n\n" + "\n".join(lines)
        await query.edit_message_text(
            text, reply_markup=kb_back("cm_menu"), parse_mode=ParseMode.HTML
        )

    async def _cb_parser_groups_list(self, query) -> None:
        groups = await self.parser.get_source_groups()
        if not groups:
            text = "Нет активных групп."
        else:
            lines = [
                f"• @{g['username']} | {g.get('title') or '?'} | "
                f"последний парсинг: {(g.get('last_parsed') or '—')[:16]}"
                for g in groups
            ]
            text = "<b>Группы-источники:</b>\n\n" + "\n".join(lines)
        await query.edit_message_text(
            text, reply_markup=kb_back("parser_menu"), parse_mode=ParseMode.HTML
        )

    async def _cb_accounts_list(self, query) -> None:
        accounts = self.account_manager._sessions
        if not accounts:
            text = "Нет подключённых аккаунтов."
        else:
            lines = []
            for acc in accounts:
                flag = "🟢" if acc.is_connected and not acc.is_banned else "🔴"
                lines.append(
                    f"{flag} {acc.session_name} | invite: {acc.invites_today} | "
                    f"dm: {acc.dms_today} | err: {acc.errors_streak}"
                )
            text = "<b>Аккаунты:</b>\n\n" + "\n".join(lines)
        await query.edit_message_text(
            text, reply_markup=kb_back("accounts_menu"), parse_mode=ParseMode.HTML
        )

    # ------------------------------------------------------------------
    # Scheduled jobs
    # ------------------------------------------------------------------

    async def _midnight_reset(self, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Reset daily counters at midnight."""
        await self.account_manager.reset_daily_counters()
        if self.content_machine:
            self.content_machine._posts_today = 0
        logger.info("Midnight reset done")

    # ------------------------------------------------------------------
    # Text formatters
    # ------------------------------------------------------------------

    async def _build_status_text(self) -> str:
        cm_status = self.content_machine.get_status()
        parser_status = self.parser.get_status()
        inv_status = self.inviter.get_status()

        cm_icon = "🟢" if cm_status["running"] else "🔴"
        parser_icon = "🟢" if parser_status["running"] else "🔴"
        inv_icon = "🟢" if inv_status["running"] else "🔴"

        return (
            f"<b>Статус сервисов</b> | Uptime: {format_uptime(self.start_time)}\n\n"
            f"{cm_icon} Контент-машина: {'работает' if cm_status['running'] else 'остановлена'}\n"
            f"   Доноров: {cm_status['donors']} | постов сегодня: {cm_status['posts_today']}/{cm_status['max_posts_per_day']}\n\n"
            f"{parser_icon} Парсер: {'работает' if parser_status['running'] else 'остановлен'}\n"
            f"   Собрано за сессию: {parser_status['session_parsed']}\n\n"
            f"{inv_icon} Инвайтер: {'работает' if inv_status['running'] else 'остановлен'} "
            f"(режим: {inv_status['mode']})\n"
            f"   Действий за сессию: {inv_status['actions_session']}\n"
            f"   Аккаунтов активных: {inv_status['active_accounts']} | "
            f"заблокированных: {inv_status['banned_accounts']}\n"
        )

    def _format_stats(self, stats: dict) -> str:
        return (
            "<b>Статистика</b>\n\n"
            f"<b>Парсер:</b>\n"
            f"  Собрано пользователей: {stats['parsed_total']}\n\n"
            f"<b>Инвайтер:</b>\n"
            f"  Приглашений всего: {stats['invites_total']}\n"
            f"  Приглашений сегодня: {stats['invites_today']}\n"
            f"  DM всего: {stats['dms_total']}\n"
            f"  DM сегодня: {stats['dms_today']}\n\n"
            f"<b>Контент-машина:</b>\n"
            f"  Репостов всего: {stats['reposts_total']}\n"
            f"  Репостов сегодня: {stats['reposts_today']}\n\n"
            f"<b>Аккаунты:</b>\n"
            f"  Активных: {stats['accounts_active']}\n"
            f"  Забанено: {stats['accounts_banned']}\n"
        )

    def _format_cm_status(self, status: dict) -> str:
        icon = "🟢 Работает" if status["running"] else "🔴 Остановлена"
        return (
            f"<b>Контент-машина</b> — {icon}\n\n"
            f"Целевой канал: <code>{status['target_channel'] or '—'}</code>\n"
            f"Доноров: {status['donors']}\n"
            f"Постов сегодня: {status['posts_today']}/{status['max_posts_per_day']}\n"
        )

    def _format_parser_status(self, status: dict) -> str:
        icon = "🟢 Работает" if status["running"] else "🔴 Остановлен"
        return (
            f"<b>Парсер</b> — {icon}\n\n"
            f"Активность за последние: {status['activity_hours']}ч\n"
            f"Интервал запуска: {status['run_interval']}с\n"
            f"Собрано за сессию: {status['session_parsed']}\n"
        )

    def _format_inv_status(self, status: dict) -> str:
        icon = "🟢 Работает" if status["running"] else "🔴 Остановлен"
        return (
            f"<b>Инвайтер/DM</b> — {icon}\n"
            f"Режим: {status['mode']}\n\n"
            f"Действий за сессию: {status['actions_session']}\n"
            f"Активных аккаунтов: {status['active_accounts']}\n"
            f"Забанено: {status['banned_accounts']}\n"
        )

    def _format_accounts_status(self) -> str:
        active = self.account_manager.active_count()
        banned = self.account_manager.banned_count()
        total = len(self.account_manager._sessions)
        return (
            f"<b>Аккаунты</b>\n\n"
            f"Всего: {total}\n"
            f"Активных: {active}\n"
            f"Забанено: {banned}\n\n"
            f"Папка сессий: <code>{self.cfg.accounts.sessions_dir}</code>\n"
        )

    # ------------------------------------------------------------------
    # Error handler
    # ------------------------------------------------------------------

    async def error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        logger.error("Unhandled exception in bot handler: %s", context.error, exc_info=True)


# ---------------------------------------------------------------------------
# Login helper (interactive account setup)
# ---------------------------------------------------------------------------

async def run_login_helper(cfg: AppConfig) -> None:
    """
    Interactive Telethon login for the main_client session.
    Run this once manually to authorise the account:
        python main.py --login
    """
    print("\n=== Telethon Login Helper ===")
    sessions_dir = Path(cfg.accounts.sessions_dir)
    sessions_dir.mkdir(parents=True, exist_ok=True)
    session_path = str(sessions_dir / "main_client")

    client = TelegramClient(session_path, cfg.telegram.api_id, cfg.telegram.api_hash)
    await client.connect()
    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Already authorised as {me.first_name} (@{me.username})")
    else:
        phone = input("Enter phone number (+7...): ").strip()
        await client.send_code_request(phone)
        code = input("Enter the code you received: ").strip()
        try:
            await client.sign_in(phone, code)
        except Exception:
            password = input("2FA password: ").strip()
            await client.sign_in(password=password)
        me = await client.get_me()
        print(f"Authorised as {me.first_name} (@{me.username})")
    await client.disconnect()
    print("Done. You can now start the bot with: python main.py")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def amain() -> None:
    cfg = config

    setup_logging(
        level=cfg.logging.level,
        log_file=cfg.logging.file,
        max_bytes=cfg.logging.max_bytes,
        backup_count=cfg.logging.backup_count,
    )

    if "--login" in sys.argv:
        await run_login_helper(cfg)
        return

    if not cfg.validate():
        logger.critical("Invalid configuration. Fix the errors above and restart.")
        sys.exit(1)

    bot = BotApp(cfg)
    await bot.initialise()

    # Load persisted settings from DB (override .env for runtime values)
    saved = await bot.db.get_all_settings()
    if "cm_target_channel" in saved:
        cfg.content_machine.target_channel = saved["cm_target_channel"]
    if "cm_ad_text" in saved:
        cfg.content_machine.ad_text = saved["cm_ad_text"]
    if "inv_target" in saved:
        cfg.inviter.invite_target = saved["inv_target"]
    if "inv_dm_message" in saved:
        cfg.inviter.dm_message = saved["inv_dm_message"]
    if "inv_delay" in saved:
        try:
            cfg.inviter.invite_delay = float(saved["inv_delay"])
        except (TypeError, ValueError):
            pass

    await bot.run()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")


if __name__ == "__main__":
    main()
