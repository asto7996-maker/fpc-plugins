"""
Application bootstrap — сборка всех слоёв.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from automation import AutomationEngine
from config import load_settings
from core.bot_core import BotCore, EventBus
from core.i18n import setup_i18n
from core.plugins.manager import PluginEngine
from core.plugins.scheduler import TaskScheduler
from database import Database
from tg_bot import TelegramBot

if TYPE_CHECKING:
    pass

logger = logging.getLogger("starvell.app")


class Application:
    """Главный контейнер приложения (Clean Architecture entry)."""

    def __init__(self) -> None:
        self.settings = load_settings()
        self.db = Database()
        self.events = EventBus()
        self.core = BotCore(self.settings, self.db, self.events)
        self.scheduler = TaskScheduler()
        self.core.scheduler = self.scheduler
        self.plugin_engine: PluginEngine | None = None
        self.automation: AutomationEngine | None = None
        self.tg_bot: TelegramBot | None = None
        self._hot_reload_task: asyncio.Task | None = None

    async def setup(self) -> None:
        setup_i18n(self.settings.language if hasattr(self.settings, "language") else "ru")
        await self.db.init()
        await self.db.sync_feature_flags(self.settings)

        self.plugin_engine = PluginEngine(self.core)
        self.core.plugin_manager = self.plugin_engine
        self.plugin_engine.load_all()

        self.scheduler.start()
        self.automation = AutomationEngine(self.db, self.core)

        async def notify(text: str, notify_type: str = "notify_orders", **extra) -> None:
            if not self.tg_bot:
                return
            if extra.get("order_id"):
                await self.tg_bot.notify_order(text, str(extra["order_id"]), str(extra.get("chat_id") or ""))
            elif extra.get("chat_id") and notify_type == "notify_chats":
                await self.tg_bot.notify_chat(text, str(extra["chat_id"]))
            else:
                await self.tg_bot.broadcast(text, notify_type)

        self.automation.notify_cb = notify
        self.core.set_notify_callback(notify)

        self.tg_bot = TelegramBot(
            self.settings,
            self.db,
            self.core,
            self.plugin_engine,
            self.automation,
        )

        # FPC Telegram adapter
        from core.fpc.telegram import TelegramAdapter
        self.core.telegram = TelegramAdapter()
        self.core.telegram.attach(
            self.tg_bot.bot,
            self.tg_bot.dp,
            self.settings.owner_id,
            self.settings.admin_ids,
        )

        # Роутеры плагинов BasePlugin + FPC compat
        if self.tg_bot and self.plugin_engine:
            for router in self.plugin_engine.get_tg_routers():
                self.tg_bot.dp.include_router(router)
            self.tg_bot.dp.include_router(self.core.telegram.router)

    async def start(self) -> None:
        assert self.automation and self.tg_bot and self.plugin_engine
        await self.automation.start()
        await self.plugin_engine.startup_starvell_plugins()
        self._hot_reload_task = asyncio.create_task(self._watch_plugins())
        try:
            await self.tg_bot.start_polling()
        finally:
            await self.shutdown()

    async def _watch_plugins(self) -> None:
        """Фоновая hot-reload проверка изменений плагинов."""
        while True:
            try:
                await asyncio.sleep(5.0)
                if self.plugin_engine:
                    changed = await self.plugin_engine.reload_changed()
                    if changed:
                        logger.info("Hot-reload: %s", changed)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("plugin watch: %s", exc)

    async def shutdown(self) -> None:
        if self._hot_reload_task:
            self._hot_reload_task.cancel()
            try:
                await self._hot_reload_task
            except asyncio.CancelledError:
                pass
        if self.automation:
            await self.automation.stop()
        if self.plugin_engine:
            await self.plugin_engine.shutdown_starvell_plugins()
            self.plugin_engine.unload_all()
        if self.tg_bot:
            await self.tg_bot.stop()
        self.scheduler.stop()
        logger.info("Application shutdown complete")
