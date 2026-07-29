"""
main.py — Channel Reposter только на Bot API (без api_id / api_hash).

1. Добавьте @бота админом в канал-источник и канал-назначение
2. /start → укажите каналы и стартовую ссылку
3. Старт автопостинга
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import admin_bot
import config
from database import Database
from poster_botapi import ChannelPosterBotAPI

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")

_cycle_lock = asyncio.Lock()
POSTER: ChannelPosterBotAPI | None = None
DB: Database | None = None


async def run_cycle_safe() -> int:
    if POSTER is None:
        return 0
    if _cycle_lock.locked():
        logger.warning("Цикл уже идёт")
        return 0
    async with _cycle_lock:
        return await POSTER.run_cycle()


async def scheduler_loop() -> None:
    logger.info("Планировщик запущен")
    await asyncio.sleep(5)
    while True:
        assert DB is not None
        settings = DB.get_settings()
        if settings.is_running:
            try:
                n = await run_cycle_safe()
                logger.info("Планировщик: %s пост(ов)", n)
            except Exception:
                logger.exception("Ошибка цикла")
        settings = DB.get_settings()
        wait = max(settings.interval_hours, 0.05) * 3600
        logger.info("Следующий цикл через %.1f ч.", settings.interval_hours)
        await asyncio.sleep(wait)


async def main() -> None:
    global POSTER, DB

    DB = Database(config.DATABASE_PATH)
    DB.ensure_defaults(
        caption=(
            "<b>Описание</b>\n"
            "<i>Пришлите текст с форматированием — HTML соберётся сам</i>"
        ),
        interval_hours=config.DEFAULT_INTERVAL_HOURS,
        posts_per_cycle=config.DEFAULT_POSTS_PER_CYCLE,
        source_channel=config.SOURCE_CHANNEL or "",
        target_channel=config.TARGET_CHANNEL or "",
    )

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    me = await bot.get_me()
    logger.info("Бот @%s (id=%s) — режим Bot API", me.username, me.id)

    POSTER = ChannelPosterBotAPI(bot=bot, db=DB)
    admin_bot.set_dependencies(
        db=DB,
        poster=POSTER,
        trigger_cycle=run_cycle_safe,
        bot_username=me.username or "",
    )

    dp = Dispatcher(storage=MemoryStorage())
    admin_bot.setup_dispatcher(dp)

    sched = asyncio.create_task(scheduler_loop(), name="scheduler")
    logger.info("Онлайн: https://t.me/%s → /start", me.username)
    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        sched.cancel()
        try:
            await sched
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stop")
