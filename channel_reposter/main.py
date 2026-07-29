"""
main.py — Channel Reposter на чистом USERBOT.

Admin-бот (aiogram) — только панель.
Публикация — Pyrogram-аккаунт (api_id / api_hash).
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
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
from poster import ChannelPoster
from userbot_auth import UserbotAuth

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger("main")


def _norm_channel(val: str) -> str:
    from links import normalize_channel

    return normalize_channel(val)


async def _worker_bootstrap(db: Database, workdir: Path) -> str:
    auth = UserbotAuth(db=db, workdir=workdir)
    BRIDGE.auth = auth
    BRIDGE.db = db

    ok = await auth.try_start_existing()
    if not ok or auth.client is None:
        BRIDGE.poster = None
        logger.warning("Юзербот не готов — нужен /login (api_id + api_hash)")
        return "need_login"

    BRIDGE.poster = ChannelPoster(client=auth.client, db=db)
    me = await auth.client.get_me()
    logger.info(
        "USERBOT ready: %s (@%s) id=%s",
        me.first_name,
        me.username or "—",
        me.id,
    )
    return "userbot"


async def _worker_scheduler() -> None:
    logger.info("Scheduler tick=20s")
    await asyncio.sleep(3)
    next_due = 0.0
    idle = 0
    while True:
        try:
            db = BRIDGE.db
            poster = BRIDGE.poster
            if db is None:
                await asyncio.sleep(5)
                continue
            now = time.monotonic()
            s = db.get_settings()
            if s.is_running and poster is not None and now >= next_due:
                if getattr(poster, "_busy", False):
                    logger.info("Scheduler: busy")
                else:
                    try:
                        n = await poster.run_cycle()
                        logger.info("Scheduler published %s", n)
                    except Exception:
                        logger.exception("scheduler")
                        n = 0
                    if n == 0:
                        idle += 1
                        delay = min(90.0, 15.0 * idle)
                    else:
                        idle = 0
                        delay = max(s.interval_hours, 0.05) * 3600
                    next_due = time.monotonic() + delay
                    logger.info("Next cycle in %.0fs", delay)
        except Exception:
            logger.exception("scheduler tick")
        await asyncio.sleep(20)


async def _heartbeat(bot: Bot) -> None:
    while True:
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=15)
            wh = await asyncio.wait_for(bot.get_webhook_info(), timeout=15)
            logger.info(
                "heartbeat @%s pending=%s mode=userbot",
                me.username,
                wh.pending_update_count,
            )
            if wh.url:
                await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            logger.exception("heartbeat")
        await asyncio.sleep(30)


async def main() -> None:
    if not config.BOT_TOKEN:
        raise RuntimeError("Задайте BOT_TOKEN в .env (только для админ-панели)")

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

    bot = Bot(
        token=config.BOT_TOKEN,
        session=AiohttpSession(timeout=30.0),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("delete_webhook")

    me = await bot.get_me()
    logger.info("Admin panel @%s", me.username)

    workdir = Path(__file__).resolve().parent
    BRIDGE.start()
    BRIDGE.admin_loop = asyncio.get_running_loop()

    async def _notify(chat_id: int, text: str, **kwargs):
        try:
            await asyncio.wait_for(bot.send_message(chat_id, text, **kwargs), timeout=20)
        except Exception:
            logger.exception("notify")

    BRIDGE.notify_fn = _notify

    mode = await BRIDGE.call(_worker_bootstrap(db, workdir), timeout=90)
    if mode == "userbot":
        BRIDGE.submit(_worker_scheduler())

    admin_bot.set_dependencies(
        db=db,
        bot_username=me.username or "",
        bridge=BRIDGE,
        bot=bot,
    )

    dp = Dispatcher(storage=MemoryStorage())
    admin_bot.setup_dispatcher(dp)
    logger.info("Online https://t.me/%s · engine=%s", me.username, mode)

    hb = asyncio.create_task(_heartbeat(bot), name="heartbeat")

    # Свежее меню админу после рестарта — старые кнопки часто «мертвые»
    async def _announce_restart() -> None:
        await asyncio.sleep(2)
        raw = db.get("staging_chat_id") or ""
        if not raw.isdigit():
            return
        try:
            await bot.send_message(
                int(raw),
                "♻️ Бот перезапущен.\nНажмите /start — откроется новое меню "
                "(старые кнопки могут не работать).",
                reply_markup=admin_bot.menu_kb(db.get_settings().is_running),
                parse_mode="HTML",
            )
        except Exception:
            logger.exception("restart announce")

    announce = asyncio.create_task(_announce_restart(), name="announce")
    try:
        await dp.start_polling(
            bot,
            drop_pending_updates=True,
            polling_timeout=20,
            handle_as_tasks=True,
            allowed_updates=["message", "callback_query"],
        )
    finally:
        announce.cancel()
        hb.cancel()
        for task in (hb, announce):
            try:
                await task
            except BaseException:
                pass
        try:
            BRIDGE.stop()
        except Exception:
            logger.exception("bridge stop")
        try:
            await bot.session.close()
        except Exception:
            logger.exception("bot session close")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stop")
