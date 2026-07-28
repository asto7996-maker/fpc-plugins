"""
main.py — точка входа Channel Reposter.

Запускает параллельно:
  1) Pyrogram Client (юзербот) — чтение/копирование постов;
  2) aiogram Bot — админ-панель управления;
  3) фоновый планировщик циклов публикации по интервалу из SQLite.
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
from pyrogram import Client

import admin_bot
import config
from database import Database
from poster import ChannelPoster

# ---------------------------------------------------------------------------
# Логирование
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")

# Глобальная блокировка, чтобы не запускать два цикла одновременно
_cycle_lock = asyncio.Lock()


async def run_cycle_safe(poster: ChannelPoster) -> int:
    """Выполнить цикл публикации с защитой от параллельного запуска."""
    if _cycle_lock.locked():
        logger.warning("Цикл уже выполняется — пропуск")
        return 0
    async with _cycle_lock:
        return await poster.run_cycle()


async def scheduler_loop(poster: ChannelPoster, db: Database) -> None:
    """
    Фоновый планировщик.

    Каждые N часов (из настроек) запускает цикл, если автопостинг включён.
    Интервал можно менять на лету через админ-панель — он читается перед сном.
    """
    logger.info("Планировщик запущен")
    # Небольшая пауза после старта, чтобы сессия успела подняться
    await asyncio.sleep(5)

    while True:
        settings = db.get_settings()
        interval_hours = max(settings.interval_hours, 0.05)  # минимум ~3 минуты
        interval_sec = interval_hours * 3600

        if settings.is_running:
            try:
                count = await run_cycle_safe(poster)
                logger.info("Планировщик: опубликовано %s", count)
            except Exception:
                logger.exception("Ошибка в цикле планировщика")
        else:
            logger.debug("Планировщик: автопостинг на паузе")

        # Перечитываем интервал на случай изменения настроек
        settings = db.get_settings()
        interval_sec = max(settings.interval_hours, 0.05) * 3600
        logger.info("Следующий цикл через %.1f ч. (%.0f сек.)", settings.interval_hours, interval_sec)
        await asyncio.sleep(interval_sec)


async def main() -> None:
    # --- База ---
    db = Database(config.DATABASE_PATH)
    db.ensure_defaults(
        caption=(
            "<b>Новый пост</b>\n"
            "<i>Описание можно изменить через админ-панель</i>\n"
            '<a href="https://t.me/">Ссылка</a>'
        ),
        interval_hours=config.DEFAULT_INTERVAL_HOURS,
        posts_per_cycle=config.DEFAULT_POSTS_PER_CYCLE,
        source_channel=config.SOURCE_CHANNEL,
        target_channel=config.TARGET_CHANNEL,
    )
    logger.info("SQLite: %s", config.DATABASE_PATH)

    # --- Pyrogram юзербот ---
    # workdir — каталог проекта, чтобы .session лежал рядом
    workdir = str(Path(__file__).resolve().parent)
    userbot = Client(
        name=config.SESSION_NAME,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        workdir=workdir,
    )

    poster = ChannelPoster(client=userbot, db=db)

    # --- aiogram админ-бот ---
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    admin_bot.set_dependencies(
        db=db,
        poster=poster,
        trigger_cycle=lambda: run_cycle_safe(poster),
    )
    admin_bot.setup_dispatcher(dp)

    # --- Запуск ---
    await userbot.start()
    me = await userbot.get_me()
    logger.info(
        "Юзербот авторизован как %s (id=%s)",
        me.username or me.first_name,
        me.id,
    )

    # Проверка доступа к каналам (мягкая — только предупреждение)
    for label, chat in (
        ("источник", db.get_settings().source_channel or config.SOURCE_CHANNEL),
        ("назначение", db.get_settings().target_channel or config.TARGET_CHANNEL),
    ):
        try:
            info = await userbot.get_chat(chat)
            logger.info("Канал-%s: %s (id=%s)", label, info.title, info.id)
        except Exception as e:
            logger.warning(
                "Не удалось получить канал-%s (%s): %s. "
                "Убедитесь, что аккаунт состоит в канале / является админом назначения.",
                label,
                chat,
                e,
            )

    scheduler_task = asyncio.create_task(scheduler_loop(poster, db), name="scheduler")

    logger.info("Админ-бот запущен. Напишите боту /start")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler_task.cancel()
        try:
            await scheduler_task
        except asyncio.CancelledError:
            pass
        await userbot.stop()
        await bot.session.close()
        logger.info("Остановка завершена")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Остановлено пользователем (Ctrl+C)")
