"""
main.py — точка входа.

Архитектура:
  • главный поток / asyncio — aiogram админ-панель (всегда отзывчивая);
  • отдельный поток — Pyrogram юзербот + планировщик + rewrite.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

import admin_bot
import config
from bridge import BRIDGE
from database import Database
from poster import ChannelPoster
from userbot_auth import UserbotAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


async def _worker_bootstrap(db: Database, workdir: Path) -> None:
    """Инициализация юзербота внутри worker-потока."""
    auth = UserbotAuth(db=db, workdir=workdir)
    BRIDGE.auth = auth
    BRIDGE.db = db

    started = await auth.try_start_existing()
    if started and auth.client is not None:
        BRIDGE.poster = ChannelPoster(client=auth.client, db=db)
        logger.info("Worker: USERBOT готов")
    else:
        BRIDGE.poster = None
        logger.info("Worker: юзербот не авторизован — /login в боте")


async def _worker_scheduler() -> None:
    """Планировщик циклов публикации — только в worker-loop."""
    logger.info("Worker scheduler started")
    await asyncio.sleep(5)
    while True:
        db = BRIDGE.db
        poster = BRIDGE.poster
        if db is None:
            await asyncio.sleep(5)
            continue
        settings = db.get_settings()
        if settings.is_running and poster is not None:
            try:
                count = await poster.run_cycle()
                logger.info("Scheduler: опубликовано %s", count)
            except Exception:
                logger.exception("Ошибка цикла планировщика")
        elif settings.is_running and poster is None:
            logger.warning("Автопостинг включён, но юзербот не готов")

        settings = db.get_settings()
        interval_sec = max(settings.interval_hours, 0.05) * 3600
        logger.info(
            "Следующий цикл через %.1f ч. (%.0f сек.)",
            settings.interval_hours,
            interval_sec,
        )
        await asyncio.sleep(interval_sec)


async def _on_userbot_ready() -> None:
    """После /login — создать poster в worker-потоке."""
    auth = BRIDGE.auth
    db = BRIDGE.db
    if auth is None or db is None or not auth.is_ready or auth.client is None:
        return
    BRIDGE.poster = ChannelPoster(client=auth.client, db=db)
    logger.info("Worker: poster пересоздан после login")


async def main() -> None:
    db = Database(config.DATABASE_PATH)
    db.ensure_defaults(
        caption="<b>Описание</b>\n<i>Пришлите текст с форматированием через бота — HTML соберётся сам</i>",
        interval_hours=config.DEFAULT_INTERVAL_HOURS,
        posts_per_cycle=config.DEFAULT_POSTS_PER_CYCLE,
        source_channel=config.SOURCE_CHANNEL or "",
        target_channel=config.TARGET_CHANNEL or "",
    )
    logger.info("SQLite: %s", config.DATABASE_PATH)

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    me = await bot.get_me()
    logger.info("Админ-бот: @%s (id=%s)", me.username, me.id)

    # --- worker thread ---
    workdir = Path(__file__).resolve().parent
    BRIDGE.start()
    BRIDGE.admin_loop = asyncio.get_running_loop()

    async def _notify(chat_id: int, text: str, **kwargs):
        await bot.send_message(chat_id, text, **kwargs)

    BRIDGE.notify_fn = _notify

    await BRIDGE.call(_worker_bootstrap(db, workdir))
    BRIDGE.submit(_worker_scheduler())

    # --- admin panel (этот loop больше не трогает Pyrogram напрямую) ---
    admin_bot.set_dependencies(
        db=db,
        poster=None,  # операции через BRIDGE
        trigger_cycle=None,
        auth=None,
        on_userbot_ready=None,
        bridge=BRIDGE,
        on_worker_userbot_ready=_on_userbot_ready,
    )

    dp = Dispatcher(storage=MemoryStorage())
    admin_bot.setup_dispatcher(dp)
    logger.info("Онлайн: https://t.me/%s  →  /start", me.username)

    try:
        await dp.start_polling(bot, drop_pending_updates=True)
    finally:
        BRIDGE.stop()
        await bot.session.close()
        logger.info("Остановка завершена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем (Ctrl+C)")
