"""
main.py — точка входа Channel Reposter.

Режимы:
  • Bot API (по умолчанию, если нет API_ID/API_HASH) — достаточно BOT_TOKEN;
    бот должен быть админом обоих каналов.
  • Userbot (Pyrogram) — если заданы API_ID + API_HASH; при первом запуске
    нужна интерактивная авторизация по телефону.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Protocol

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


class PosterLike(Protocol):
    async def run_cycle(self) -> int: ...
    async def apply_start_link(self, link: str) -> Any: ...


async def run_cycle_safe(poster: PosterLike) -> int:
    if _cycle_lock.locked():
        logger.warning("Цикл уже выполняется — пропуск")
        return 0
    async with _cycle_lock:
        return await poster.run_cycle()


async def scheduler_loop(poster: PosterLike, db: Database) -> None:
    logger.info("Планировщик запущен")
    await asyncio.sleep(5)

    while True:
        settings = db.get_settings()
        if settings.is_running:
            try:
                count = await run_cycle_safe(poster)
                logger.info("Планировщик: опубликовано %s", count)
            except Exception:
                logger.exception("Ошибка в цикле планировщика")
        else:
            logger.debug("Планировщик: автопостинг на паузе")

        settings = db.get_settings()
        interval_sec = max(settings.interval_hours, 0.05) * 3600
        logger.info(
            "Следующий цикл через %.1f ч. (%.0f сек.)",
            settings.interval_hours,
            interval_sec,
        )
        await asyncio.sleep(interval_sec)


async def main() -> None:
    db = Database(config.DATABASE_PATH)
    db.ensure_defaults(
        caption=(
            "<b>Новый пост</b>\n"
            "<i>Описание можно изменить через админ-панель</i>\n"
            '<a href="https://t.me/">Ссылка</a>'
        ),
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
    logger.info("Бот: @%s (id=%s)", me.username, me.id)

    userbot = None
    poster: PosterLike

    if config.userbot_enabled():
        from pyrogram import Client
        from poster import ChannelPoster

        workdir = str(Path(__file__).resolve().parent)
        userbot = Client(
            name=config.SESSION_NAME,
            api_id=config.API_ID,
            api_hash=config.API_HASH,
            workdir=workdir,
        )
        poster = ChannelPoster(client=userbot, db=db)
        await userbot.start()
        ube = await userbot.get_me()
        logger.info(
            "Режим USERBOT: %s (id=%s)",
            ube.username or ube.first_name,
            ube.id,
        )
        for label, chat in (
            ("источник", db.get_settings().source_channel or config.SOURCE_CHANNEL),
            ("назначение", db.get_settings().target_channel or config.TARGET_CHANNEL),
        ):
            if not chat:
                continue
            try:
                info = await userbot.get_chat(chat)
                logger.info("Канал-%s: %s (id=%s)", label, info.title, info.id)
            except Exception as e:
                logger.warning("Канал-%s (%s) недоступен: %s", label, chat, e)
    else:
        poster = ChannelPosterBotAPI(bot=bot, db=db)
        logger.info(
            "Режим BOT API (без юзербота). "
            "Добавьте бота админом в оба канала. "
            "Для альбомов «как есть» задайте API_ID/API_HASH и USERBOT_MODE=1."
        )

    dp = Dispatcher(storage=MemoryStorage())
    admin_bot.set_dependencies(
        db=db,
        poster=poster,
        trigger_cycle=lambda: run_cycle_safe(poster),
    )
    admin_bot.setup_dispatcher(dp)

    scheduler_task = asyncio.create_task(scheduler_loop(poster, db), name="scheduler")
    logger.info("Админ-панель онлайн. Напишите боту /start — https://t.me/%s", me.username)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        if userbot is not None:
            await userbot.stop()
        await bot.session.close()
        logger.info("Остановка завершена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем (Ctrl+C)")
