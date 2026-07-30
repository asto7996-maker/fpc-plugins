"""
Application entrypoint.

Starts:
* PostgreSQL schema init
* Redis task worker + APScheduler
* Gemini Draft Engine listeners (Telethon)
* aiogram admin bot polling (+ command menu, invite access)
"""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from aiogram import Bot
from redis.asyncio import from_url as redis_from_url

from tg_pool.admin.bot import build_dispatcher, setup_bot_commands
from tg_pool.config import get_settings
from tg_pool.db.session import create_all, dispose_engine, init_engine, session_scope
from tg_pool.queue.broker import RedisTaskBroker
from tg_pool.queue.scheduler import PoolScheduler
from tg_pool.services.access_service import AccessService
from tg_pool.services.alerts import AlertService
from tg_pool.services.listener_manager import ListenerManager
from tg_pool.services.task_router import TaskRouter


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        stream=sys.stdout,
    )
    logging.getLogger("telethon").setLevel(logging.WARNING)
    logging.getLogger("aiogram").setLevel(logging.INFO)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def amain() -> None:
    settings = get_settings()
    setup_logging(settings.log_level)
    logger = logging.getLogger("tg_pool")

    init_engine(settings)
    await create_all()
    logger.info("Database schema ready")

    redis = redis_from_url(settings.redis_url, decode_responses=False)
    broker = RedisTaskBroker(redis, settings=settings)
    alerts = AlertService(settings=settings)
    TaskRouter(broker, alerts, settings=settings)

    listeners = ListenerManager(settings, redis, alert_service=alerts)

    scheduler = PoolScheduler(broker)
    scheduler.start()
    await broker.start_worker()

    bot: Bot | None = None
    polling_task: asyncio.Task | None = None
    stop_event = asyncio.Event()

    if settings.admin_bot_token:
        bot = Bot(token=settings.admin_bot_token)
        alerts.bind_bot(bot)
        listeners.bind_bot(bot)
        async with session_scope() as session:
            await AccessService(session, creator_id=settings.creator_id).ensure_user(
                settings.creator_id,
                username=None,
                full_name="Creator",
            )
        await setup_bot_commands(bot)
        dp = build_dispatcher(
            settings,
            broker,
            bot=bot,
            draft_engine=listeners.engine,
            listeners=listeners,
        )
        polling_task = asyncio.create_task(dp.start_polling(bot), name="admin-polling")
        logger.info("Admin bot polling started (creator_id=%s)", settings.creator_id)
    else:
        logger.warning("ADMIN_BOT_TOKEN empty — admin UI disabled, worker-only mode")

    try:
        await listeners.start()
    except Exception as exc:  # noqa: BLE001
        logger.error("ListenerManager start failed: %s", exc)

    loop = asyncio.get_running_loop()

    def _shutdown(sig: str) -> None:
        logger.info("Signal %s — shutting down", sig)
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown, sig.name)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _shutdown(sig.name))

    try:
        if polling_task is not None:
            stop_wait = asyncio.create_task(stop_event.wait())
            done, _ = await asyncio.wait(
                {polling_task, stop_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if polling_task in done and stop_wait not in done:
                await polling_task
        else:
            await stop_event.wait()
    finally:
        logger.info("Graceful shutdown…")
        if polling_task is not None and not polling_task.done():
            polling_task.cancel()
            try:
                await polling_task
            except asyncio.CancelledError:
                pass
        await listeners.stop()
        scheduler.shutdown()
        await broker.stop_worker()
        await redis.aclose()
        if bot is not None:
            await bot.session.close()
        await dispose_engine()
        logger.info("Shutdown complete")


def main() -> None:
    try:
        import uvloop

        uvloop.install()
    except ImportError:
        pass
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
