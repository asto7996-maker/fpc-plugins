"""
main.py — админ-бот + гибридное копирование.

Юзербот читает чужой канал (подписка аккаунта).
Бот публикует в ваш канал (где он админ).

Админ-loop и worker-loop полностью разделены:
  • polling никогда не ждёт publish/stage;
  • планировщик будит сам себя каждые 20 сек (не sleep(часы)).
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

    publish_bot = Bot(
        token=config.BOT_TOKEN,
        session=AiohttpSession(timeout=120.0),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    BRIDGE.publish_bot = publish_bot
    BRIDGE.poster = HybridPoster(client=auth.client, bot=publish_bot, db=db)
    me = await auth.client.get_me()
    logger.info("Hybrid ready: reader=%s publisher=@bot", me.username or me.first_name)
    return "hybrid"


async def _worker_scheduler() -> None:
    """Тик каждые 20с: если автопост включён и подошёл интервал — цикл."""
    logger.info("Scheduler started (tick=20s)")
    await asyncio.sleep(3)
    next_due = 0.0
    idle_rounds = 0
    while True:
        try:
            db = BRIDGE.db
            poster = BRIDGE.poster
            now = time.monotonic()
            if db is None:
                await asyncio.sleep(5)
                continue

            settings = db.get_settings()
            if settings.is_running and poster is not None and now >= next_due:
                if getattr(poster, "_busy", False):
                    logger.info("Scheduler: cycle busy, wait")
                else:
                    try:
                        published = await poster.run_cycle()
                        logger.info("Scheduler published %s", published)
                    except Exception:
                        logger.exception("scheduler")
                        published = 0
                    if published == 0:
                        idle_rounds += 1
                        delay = min(90.0, 15.0 * idle_rounds)
                    else:
                        idle_rounds = 0
                        delay = max(settings.interval_hours, 0.05) * 3600
                    next_due = time.monotonic() + delay
                    logger.info("Next cycle in %.0fs (%.2fh)", delay, delay / 3600.0)
        except Exception:
            logger.exception("scheduler tick")
        await asyncio.sleep(20)


async def _admin_heartbeat(bot: Bot) -> None:
    """Пишет в лог, что admin-loop жив; помогает ловить залипший polling."""
    while True:
        try:
            me = await asyncio.wait_for(bot.get_me(), timeout=15)
            wh = await asyncio.wait_for(bot.get_webhook_info(), timeout=15)
            logger.info(
                "heartbeat @%s pending=%s webhook=%s",
                me.username,
                wh.pending_update_count,
                "yes" if wh.url else "no",
            )
            if wh.url:
                logger.warning("Webhook set — deleting so polling works")
                await bot.delete_webhook(drop_pending_updates=False)
        except Exception:
            logger.exception("heartbeat")
        await asyncio.sleep(30)


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

    # Admin bot: короткий timeout, чтобы кнопки не висели на сети
    bot = Bot(
        token=config.BOT_TOKEN,
        session=AiohttpSession(timeout=30.0),
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # На всякий случай сбрасываем webhook
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        logger.exception("delete_webhook")

    me = await bot.get_me()
    logger.info("Admin bot @%s", me.username)

    fallback = ChannelPosterBotAPI(bot=bot, db=db)

    workdir = Path(__file__).resolve().parent
    BRIDGE.start()
    BRIDGE.admin_loop = asyncio.get_running_loop()

    async def _notify(chat_id: int, text: str, **kwargs):
        try:
            await asyncio.wait_for(
                bot.send_message(chat_id, text, **kwargs), timeout=20
            )
        except Exception:
            logger.exception("notify send")

    BRIDGE.notify_fn = _notify

    mode = await BRIDGE.call(_worker_bootstrap(db, workdir), timeout=60)
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

    hb = asyncio.create_task(_admin_heartbeat(bot), name="admin-heartbeat")

    try:
        await dp.start_polling(
            bot,
            drop_pending_updates=True,
            polling_timeout=20,
            handle_as_tasks=True,
            allowed_updates=["message", "callback_query"],
        )
    finally:
        hb.cancel()
        try:
            await hb
        except Exception:
            pass

        async def _close_pub():
            pub = getattr(BRIDGE, "publish_bot", None)
            if pub is not None:
                await pub.session.close()

        try:
            await BRIDGE.call(_close_pub(), timeout=15)
        except Exception:
            pass
        BRIDGE.stop()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Stop")
