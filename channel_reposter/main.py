"""
main.py — точка входа Channel Reposter.

Юзербот (Pyrogram) копирует посты: в источнике достаточно подписки,
в назначении аккаунт должен быть администратором.

Вход в аккаунт: команда /login в админ-боте
(API_ID → API_HASH → телефон → код → пароль 2FA).
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
from poster import ChannelPoster
from poster_botapi import ChannelPosterBotAPI
from userbot_auth import UserbotAuth

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


class AppState:
    """Общее состояние процесса: auth + текущий poster."""

    def __init__(self) -> None:
        self.db: Database | None = None
        self.bot: Bot | None = None
        self.auth: UserbotAuth | None = None
        self.poster: PosterLike | None = None

    def bind_poster(self) -> None:
        assert self.db and self.bot and self.auth
        if self.auth.is_ready and self.auth.client is not None:
            self.poster = ChannelPoster(client=self.auth.client, db=self.db)
            logger.info("Активный движок: USERBOT (Pyrogram)")
        else:
            self.poster = ChannelPosterBotAPI(bot=self.bot, db=self.db)
            logger.info("Активный движок: Bot API (fallback до /login)")

        admin_bot.set_dependencies(
            db=self.db,
            poster=self.poster,
            trigger_cycle=lambda: run_cycle_safe(self.poster),
            auth=self.auth,
            on_userbot_ready=self.on_userbot_ready,
        )

    async def on_userbot_ready(self) -> None:
        """Колбэк после успешного /login — переключаем poster на юзербот."""
        self.bind_poster()
        logger.info("Юзербот готов, poster переключён")


STATE = AppState()


async def run_cycle_safe(poster: PosterLike | None) -> int:
    if poster is None:
        return 0
    if _cycle_lock.locked():
        logger.warning("Цикл уже выполняется — пропуск")
        return 0
    async with _cycle_lock:
        return await poster.run_cycle()


async def scheduler_loop() -> None:
    logger.info("Планировщик запущен")
    await asyncio.sleep(5)
    while True:
        assert STATE.db is not None
        settings = STATE.db.get_settings()
        if settings.is_running:
            try:
                count = await run_cycle_safe(STATE.poster)
                logger.info("Планировщик: опубликовано %s", count)
            except Exception:
                logger.exception("Ошибка в цикле планировщика")
        settings = STATE.db.get_settings()
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

    workdir = Path(__file__).resolve().parent
    auth = UserbotAuth(db=db, workdir=workdir)

    STATE.db = db
    STATE.bot = bot
    STATE.auth = auth

    started = await auth.try_start_existing()
    if started:
        logger.info("Юзербот поднят из существующей сессии")
    else:
        logger.info("Юзербот не авторизован — используйте /login в боте")

    STATE.bind_poster()

    dp = Dispatcher(storage=MemoryStorage())
    admin_bot.setup_dispatcher(dp)

    scheduler_task = asyncio.create_task(scheduler_loop(), name="scheduler")
    logger.info("Онлайн: https://t.me/%s  →  /start  или  /login", me.username)

    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        await auth.stop()
        await bot.session.close()
        logger.info("Остановка завершена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем (Ctrl+C)")
