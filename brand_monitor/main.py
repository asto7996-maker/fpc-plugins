"""Entry point: start userbot pool + admin bot together."""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot

from brand_monitor.admin.bot import build_admin_dispatcher, make_admin_notifier
from brand_monitor.config import get_settings
from brand_monitor.core.userbot_manager import UserbotManager
from brand_monitor.database.repository import Database


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    # Quiet noisy libraries
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)


async def amain() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    logger = logging.getLogger("brand_monitor")

    db = Database(settings.database_path)
    await db.connect()
    await db.seed_defaults()

    bot: Bot | None = None
    manager = UserbotManager(db=db, settings=settings)

    if settings.admin_bot_token:
        bot = Bot(token=settings.admin_bot_token)
        manager.admin_notifier = await make_admin_notifier(bot, settings)
    else:
        logger.warning("ADMIN_BOT_TOKEN is empty — admin panel disabled")

    await manager.start()

    try:
        if bot is not None:
            dp = build_admin_dispatcher(db, manager, settings)
            logger.info("Admin bot polling started")
            await dp.start_polling(bot)
        else:
            # Keep process alive with only the userbot pool
            logger.info("Running userbot pool only (Ctrl+C to stop)")
            while True:
                await asyncio.sleep(3600)
    finally:
        await manager.stop()
        if bot is not None:
            await bot.session.close()
        await db.close()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
