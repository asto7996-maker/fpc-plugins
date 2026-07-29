#!/usr/bin/env python3
import asyncio
import logging
from pathlib import Path

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

import config
from database import Database
from poster_hybrid import HybridPoster
from userbot_auth import UserbotAuth

logging.basicConfig(level=logging.INFO)


async def main() -> None:
    db = Database(config.DATABASE_PATH)
    print("progress", db.get_progress_id(), "src", db.get_settings().source_channel, "dst", db.get_settings().target_channel)
    auth = UserbotAuth(db=db, workdir=Path(__file__).resolve().parent)
    assert await auth.try_start_existing()
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    poster = HybridPoster(auth.client, bot, db)
    db.set_running(True)
    old = db.get_settings().posts_per_cycle
    db.set_posts_per_cycle(1)
    try:
        n = await poster.run_cycle()
        print("PUBLISHED", n, "progress", db.get_progress_id())
    finally:
        db.set_posts_per_cycle(old)
        db.set_running(False)
        await auth.stop()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
