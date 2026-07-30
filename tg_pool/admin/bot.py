"""
Assemble aiogram 3 dispatcher: commands, middleware, routers.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, BotCommandScopeDefault

from tg_pool.admin.middleware import AccessMiddleware
from tg_pool.admin.routers.access import build_access_router
from tg_pool.admin.routers.accounts import build_accounts_router
from tg_pool.admin.routers.add_account import build_add_account_router
from tg_pool.admin.routers.drafts import build_drafts_router
from tg_pool.admin.routers.menu import build_menu_router
from tg_pool.admin.routers.proxies import build_proxies_router
from tg_pool.admin.routers.selftest import build_selftest_router
from tg_pool.admin.routers.start import build_start_router
from tg_pool.config import Settings
from tg_pool.taskqueue.broker import RedisTaskBroker

if TYPE_CHECKING:
    from tg_pool.services.draft_engine import DraftEngine
    from tg_pool.services.listener_manager import ListenerManager

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand(command="start", description="Запуск бота / Перезапуск"),
    BotCommand(command="menu", description="Главное меню управления"),
    BotCommand(command="profile", description="Профиль и статус доступа"),
    BotCommand(command="admin", description="Панель суперадминистратора"),
    BotCommand(command="selftest", description="Self-test / health-check (creator)"),
    BotCommand(command="help", description="Справка и инструкция"),
]


async def setup_bot_commands(bot: Bot) -> None:
    """Register the official Telegram command menu."""
    await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeDefault())
    logger.info("Bot commands registered: %s", [c.command for c in BOT_COMMANDS])


def build_dispatcher(
    settings: Settings,
    broker: RedisTaskBroker,
    bot: Bot | None = None,
    *,
    draft_engine: Optional["DraftEngine"] = None,
    listeners: Optional["ListenerManager"] = None,
) -> Dispatcher:
    """
    Wire routers + access middleware.

    Note: `bot` is required for AccessMiddleware creator notifications / downloads.
    If not passed, a temporary Bot instance is created from settings token.
    """
    dp = Dispatcher(storage=MemoryStorage())

    if bot is None:
        if not settings.admin_bot_token:
            raise RuntimeError("ADMIN_BOT_TOKEN is required to build dispatcher")
        bot = Bot(token=settings.admin_bot_token)

    # Middleware on both message & callback pipelines
    access_mw = AccessMiddleware(settings, bot)
    dp.message.middleware(access_mw)
    dp.callback_query.middleware(access_mw)

    # Order matters: global nav / callbacks before FSM catch-alls (TData archive).
    dp.include_router(build_start_router(settings))
    dp.include_router(build_selftest_router(settings, listeners=listeners))
    dp.include_router(build_menu_router(settings))
    dp.include_router(build_proxies_router(settings))
    dp.include_router(build_accounts_router(settings, broker, listeners=listeners))
    dp.include_router(build_access_router(settings))
    if draft_engine is not None:
        dp.include_router(build_drafts_router(settings, draft_engine, listeners))
    # FSM-heavy flows last so they cannot swallow reply-keyboard navigation
    dp.include_router(build_add_account_router(settings))

    return dp
