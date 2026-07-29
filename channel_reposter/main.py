"""
main.py — админ-бот + гибридное копирование.

Юзербот читает чужой канал (подписка аккаунта).
Бот публикует в ваш канал (где он админ).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import admin_bot
import config
from bridge import BRIDGE
from database import Database
from poster_botapi import ChannelPosterBotAPI
from poster_hybrid import HybridPoster
from userbot_auth import UserbotAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


def _norm_channel(val: str) -> str:
    v = (val or "").strip()
    if v and not v.startswith("@") and not v.lstrip("-").isdigit():
        return "@" + v
    return v


async def _worker_bootstrap(db: Database, workdir: Path) -> str:
    auth = UserbotAuth(db=db, workdir=workdir)
    BRIDGE.auth = auth
    BRIDGE.db = db

    ok = await auth.try_start_existing()
    if not ok or auth.client is None:
        BRIDGE.poster = None
        logger.warning("Нет сессии юзербота")
        return "none"

    # Отдельный Bot-клиент в worker-потоке для публикации (длинный timeout на большие видео)
    publish_bot = Bot(
        token=config.BOT_TOKEN,
        session=AiohttpSession(timeout=600.0),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    BRIDGE.publish_bot = publish_bot
    BRIDGE.poster = HybridPoster(client=auth.client, bot=publish_bot, db=db)
    me = await auth.client.get_me()
    logger.info("Hybrid ready: reader=%s publisher=@bot", me.username or me.first_name)
    return "hybrid"


async def _worker_scheduler() -> None:
    """Циклы по интервалу + быстрый повтор, если автопост включён и цикл дал 0."""
    logger.info("Scheduler started")
    await asyncio.sleep(5)
    idle_rounds = 0
    while True:
        db = BRIDGE.db
        poster = BRIDGE.poster
        if db is None:
            await asyncio.sleep(5)
            continue
        published = 0
        if db.get_settings().is_running and poster is not None:
            try:
                published = await poster.run_cycle()
                logger.info("Scheduler published %s", published)
            except Exception:
                logger.exception("scheduler")
        if db.get_settings().is_running and published == 0:
            idle_rounds += 1
            # Пока идут ошибки/дыры — не ждать 9 часов, пробовать чаще
            wait = min(120.0, 15.0 * idle_rounds)
            logger.info("No posts this round — retry in %.0fs", wait)
        else:
            idle_rounds = 0
            wait = max(db.get_settings().interval_hours, 0.05) * 3600
            logger.info("Next cycle in %.1f h", db.get_settings().interval_hours)
        await asyncio.sleep(wait)


async def main() -> None:
    db = Database(config.DATABASE_PATH)
    db.ensure_defaults(
        caption="<b>Описание</b>",
        interval_hours=config.DEFAULT_INTERVAL_HOURS,
        posts_per_cycle=config.DEFAULT_POSTS_PER_CYCLE,
        source_channel=config.SOURCE_CHANNEL or "",
        target_channel=config.TARGET_CHANNEL or "",
    )
    s = db.get_settings()
    if s.source_channel:
        db.set_source_channel(_norm_channel(s.source_channel))
    if s.target_channel:
        db.set_target_channel(_norm_channel(s.target_channel))

    # Откат progress после пустого прогона с WRITE_FORBIDDEN
    # (чтобы не ускакал далеко без публикаций)
    if db.get_progress_id() > 6000 and db.history_count() > 0:
        # мягкий откат к месту, где точно был контент при тесте
        pass

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    me = await bot.get_me()
    logger.info("Admin bot @%s", me.username)

    fallback = ChannelPosterBotAPI(bot=bot, db=db)

    workdir = Path(__file__).resolve().parent
    BRIDGE.start()
    BRIDGE.admin_loop = asyncio.get_running_loop()

    async def _notify(chat_id: int, text: str, **kwargs):
        await bot.send_message(chat_id, text, **kwargs)

    BRIDGE.notify_fn = _notify

    mode = await BRIDGE.call(_worker_bootstrap(db, workdir))
    if mode == "hybrid":
        BRIDGE.submit(_worker_scheduler())

    admin_bot.set_dependencies(
        db=db,
        poster=fallback,
        bot_username=me.username or "",
        bridge=BRIDGE,
        bot=bot,
    )

    dp = Dispatcher(storage=MemoryStorage())
    admin_bot.setup_dispatcher(dp)
    logger.info("Online https://t.me/%s mode=%s", me.username, mode)

    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        async def _close_pub():
            pub = getattr(BRIDGE, "publish_bot", None)
            if pub is not None:
                await pub.session.close()

        try:
            await BRIDGE.call(_close_pub())
        except Exception:
            pass
        BRIDGE.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stop")
