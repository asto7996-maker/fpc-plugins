#!/usr/bin/env python3
"""
Starvell Cardinal — точка входа.
Telegram-бот для автоматизации продаж на маркетплейсе Starvell.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from ai_service import AIService
from automation import AutomationEngine
from config import LOGS_DIR, VERSION, create_default_settings_file, load_settings
from database import Database
from plugin_manager import CardinalCore, EventManager, PluginManager
from tg_bot import TelegramBot


def setup_logging(debug: bool = False) -> None:
    """Настраивает логирование в консоль и файл."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    level = logging.DEBUG if debug else logging.INFO
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOGS_DIR / "cardinal.log",
        maxBytes=5_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)


async def main() -> None:
    create_default_settings_file()
    settings = load_settings()
    setup_logging(settings.debug)

    logger = logging.getLogger("starvell.main")
    logger.info("Starvell Cardinal v%s запускается…", VERSION)

    if not settings.bot_token:
        logger.error(
            "BOT_TOKEN не задан! Укажите в config/settings.json или переменной окружения BOT_TOKEN"
        )
        sys.exit(1)

    db = Database()
    await db.init()
    await db.sync_feature_flags(settings)

    event_manager = EventManager()
    cardinal = CardinalCore(settings, db, event_manager)
    plugin_manager = PluginManager(cardinal)
    plugin_manager.load_all()

    tg_bot: TelegramBot | None = None
    automation = AutomationEngine(db, cardinal)

    async def notify(text: str, notify_type: str = "notify_orders") -> None:
        if tg_bot:
            await tg_bot.broadcast(text, notify_type)

    automation.notify_cb = notify

    tg_bot = TelegramBot(settings, db, cardinal, plugin_manager, automation)

    await automation.start()

    try:
        await tg_bot.start_polling()
    finally:
        await automation.stop()
        plugin_manager.unload_all()
        await tg_bot.stop()
        logger.info("Бот остановлен")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
